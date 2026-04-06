import json
import os
import glob
import re
import requests
import telebot
import numpy as np
from typing import TypedDict
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
from langgraph.graph import StateGraph, END

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://host.docker.internal:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "google/gemma-3-4b")
PDF_DIR = os.getenv("PDF_DIR", "./Papers")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN 이 비어 있습니다.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

# 전역 인덱스
pdf_chunks: list[str] = []
pdf_embeddings: np.ndarray | None = None
pdf_file_names: list[str] = []


# ──────────────────────────────────────────────
# GraphState
# ──────────────────────────────────────────────
class GraphState(TypedDict):
    question: str
    intent: str           # "list_files" | "rag_answer"
    pdf_file_names: list[str]
    answer: str


# ──────────────────────────────────────────────
# PDF 로더 / 인덱서
# ──────────────────────────────────────────────
def load_and_chunk_pdfs(pdf_dir: str, chunk_size: int = 500, overlap: int = 50) -> tuple[list[str], list[str]]:
    """PDF 디렉토리에서 텍스트를 추출하고 청크 + 파일명 목록을 반환합니다."""
    chunks: list[str] = []
    files = glob.glob(os.path.join(pdf_dir, "**", "*.pdf"), recursive=True)

    if not files:
        print(f"[PDF] {pdf_dir} 에 PDF 파일이 없습니다.")
        return chunks, []

    names = [os.path.splitext(os.path.basename(f))[0] for f in files]

    for pdf_path in files:
        print(f"[PDF] 로딩: {pdf_path}")
        try:
            reader = PdfReader(pdf_path)
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"

            step = chunk_size - overlap
            for i in range(0, max(1, len(text) - overlap), step):
                chunk = text[i : i + chunk_size].strip()
                if chunk:
                    chunks.append(chunk)
        except Exception as e:
            print(f"[PDF] 오류 ({pdf_path}): {e}")

    print(f"[PDF] 총 {len(files)}개 파일, {len(chunks)}개 청크 생성")
    return chunks, names


def build_index(chunks: list[str]) -> np.ndarray:
    if not chunks:
        return np.empty((0,), dtype=np.float32)
    print(f"[INDEX] {len(chunks)}개 청크 임베딩 중...")
    emb = EMBED_MODEL.encode(chunks, show_progress_bar=False, convert_to_numpy=True)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    return (emb / norms).astype(np.float32)


def retrieve_context(query: str, chunks: list[str], embeddings: np.ndarray, top_k: int = 3) -> str:
    if not chunks or embeddings.shape[0] == 0:
        return ""
    q_vec = EMBED_MODEL.encode([query], convert_to_numpy=True)
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_vec = q_vec / q_norm
    scores = (embeddings @ q_vec.T).flatten()
    top_idx = np.argsort(scores)[::-1][:top_k]
    selected = [chunks[i] for i in top_idx if scores[i] > 0.1]
    return "\n\n---\n\n".join(selected)


def reload_index():
    global pdf_chunks, pdf_embeddings, pdf_file_names
    pdf_chunks, pdf_file_names = load_and_chunk_pdfs(PDF_DIR)
    pdf_embeddings = build_index(pdf_chunks)
    print(f"[INDEX] 인덱스 빌드 완료 | 파일: {pdf_file_names}")


def call_lm_studio(messages: list[dict], max_tokens: int = 512) -> str:
    url = f"{LM_STUDIO_BASE_URL}/chat/completions"
    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }
    res = requests.post(url, json=payload, timeout=180)
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


# ──────────────────────────────────────────────
# LangGraph 노드
# ──────────────────────────────────────────────
def node_classify_intent(state: GraphState) -> GraphState:
    """LM Studio로 질문 의도를 분류합니다."""
    question = state["question"]
    names = state["pdf_file_names"]
    names_str = "\n".join(f"- {n}" for n in names) if names else "(없음)"

    system_prompt = (
        "You are an intent classifier. "
        "Given a list of available PDF file names and a user question, "
        "decide whether the user wants to:\n"
        "  (A) know what PDF files are available → respond with: {\"intent\": \"list_files\"}\n"
        "  (B) ask something about the content of a specific document → respond with: {\"intent\": \"rag_answer\"}\n\n"
        "Reply ONLY with a valid JSON object. No explanation.\n\n"
        f"Available files:\n{names_str}"
    )

    print(f"[GRAPH] classify_intent: {question}")
    try:
        raw = call_lm_studio(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            max_tokens=50,
        )
        # JSON 파싱 — 로컬 LLM이 여분 텍스트를 덧붙일 수 있으므로 정규식으로 추출
        match = re.search(r'\{[^}]+\}', raw)
        intent_raw = json.loads(match.group()).get("intent", "") if match else ""
        intent = intent_raw if intent_raw in ("list_files", "rag_answer") else "rag_answer"
    except Exception as e:
        print(f"[GRAPH] classify_intent 파싱 실패, rag_answer로 폴백: {e}")
        intent = "rag_answer"

    print(f"[GRAPH] intent → {intent}")
    return {**state, "intent": intent}


def node_list_files(state: GraphState) -> GraphState:
    """인덱싱된 PDF 파일 목록을 answer에 저장합니다 (LLM 호출 없음)."""
    names = state["pdf_file_names"]
    if names:
        lines = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(names))
        answer = f"현재 로드된 PDF 논문 목록입니다:\n{lines}"
    else:
        answer = f"현재 로드된 PDF 파일이 없습니다. ({PDF_DIR})"
    print("[GRAPH] list_files 응답 생성")
    return {**state, "answer": answer}


def node_rag_answer(state: GraphState) -> GraphState:
    """RAG 검색 후 LM Studio로 답변을 생성합니다."""
    question = state["question"]
    context = retrieve_context(question, pdf_chunks, pdf_embeddings) if pdf_embeddings is not None else ""

    if context:
        print(f"[GRAPH] rag_answer: 컨텍스트 {len(context)}자 전달")
        system_content = (
            "You are a helpful assistant. "
            "Answer the user's question based on the following document context.\n\n"
            f"[Context]\n{context}"
        )
    else:
        print("[GRAPH] rag_answer: 컨텍스트 없음")
        system_content = "You are a helpful assistant."

    print(f"[LM] request: {question}")
    answer = call_lm_studio(
        [
            {"role": "system", "content": system_content},
            {"role": "user", "content": question},
        ]
    )
    print(f"[LM] answer: {answer[:100]}")
    return {**state, "answer": answer}


def route(state: GraphState) -> str:
    return state["intent"]


# ──────────────────────────────────────────────
# LangGraph 빌드
# ──────────────────────────────────────────────
_builder = StateGraph(GraphState)
_builder.add_node("classify_intent", node_classify_intent)
_builder.add_node("list_files", node_list_files)
_builder.add_node("rag_answer", node_rag_answer)

_builder.set_entry_point("classify_intent")
_builder.add_conditional_edges("classify_intent", route, {
    "list_files": "list_files",
    "rag_answer": "rag_answer",
})
_builder.add_edge("list_files", END)
_builder.add_edge("rag_answer", END)

app_graph = _builder.compile()


# ──────────────────────────────────────────────
# Telegram 핸들러
# ──────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def start_handler(message):
    print("[TG] /start from", message.chat.id)
    bot.reply_to(
        message,
        f"안녕하세요. PDF RAG 텔레그램 봇입니다.\n"
        f"현재 {len(pdf_file_names)}개 파일 / {len(pdf_chunks)}개 청크 로드됨.\n"
        f"PDF를 새로고침하려면 /reload 를 입력하세요.",
    )


@bot.message_handler(commands=["reload"])
def reload_handler(message):
    print("[TG] /reload from", message.chat.id)
    bot.reply_to(message, f"PDF 디렉토리({PDF_DIR})를 다시 로드합니다...")
    try:
        reload_index()
        bot.reply_to(message, f"완료! {len(pdf_file_names)}개 파일, {len(pdf_chunks)}개 청크 로드됨.")
    except Exception as e:
        bot.reply_to(message, f"오류 발생: {e}")


@bot.message_handler(func=lambda message: message.text is not None)
def text_handler(message):
    print("[TG] message from", message.chat.id, ":", message.text)

    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "텍스트를 보내주세요.")
        return

    bot.send_chat_action(message.chat.id, "typing")

    result = app_graph.invoke({
        "question": user_text,
        "intent": "",
        "pdf_file_names": pdf_file_names,
        "answer": "",
    })

    bot.reply_to(message, result["answer"])


# ──────────────────────────────────────────────
# 부트스트랩
# ──────────────────────────────────────────────
print("[BOOT] bot starting...")
print("[BOOT] model =", LM_STUDIO_MODEL)
print("[BOOT] base_url =", LM_STUDIO_BASE_URL)
print("[BOOT] pdf_dir =", PDF_DIR)

me = requests.get(
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",
    timeout=30,
)
print("[BOOT] getMe =", me.status_code, me.text)

reload_index()

if __name__ == "__main__":
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
