from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from settings import get_settings
from tools import tool_list_pdfs, tool_open_pdf


@dataclass
class OrchestratorConfig:
    model: str = "google/gemma-3-4b"
    api_base: str = "http://host.docker.internal:1234/v1"
    api_key: str = "lm-studio"
    pdf_dir: str = "./Papers"
    index_dir: str = "./cache/index"
    max_pages_default: int = 20
    max_pages_summary: int = 5
    initial_top_k: int = 4
    max_top_k: int = 12
    chunk_chars: int = 950
    chunk_overlap: int = 160
    max_chars_per_chunk: int = 2200
    max_context_chars: int = 7000


class PaperLLMOrchestrator:
    """Deterministic pipeline:
    question analysis -> pdf select -> chunk select -> answer generation.
    """

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        if config is None:
            s = get_settings()
            config = OrchestratorConfig(
                model=s.lm_studio_model,
                api_base=s.lm_studio_base_url,
                api_key="lm-studio",
                pdf_dir=s.pdf_dir,
            )
        self.config = config
        self.index_dir = Path(self.config.index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.llm = ChatOpenAI(
            model=self.config.model,
            base_url=self.config.api_base,
            api_key=self.config.api_key,
            temperature=0.1,
            max_tokens=900,
        )

        self.answer_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "Answer user question about a paper using provided context only. "
                        "If context is insufficient, say briefly what is missing.",
                    ),
                    ("human", "[Question]\n{question}\n\n[Context]\n{context}\n\nAnswer in Korean."),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        # Kept for PaperBotService compatibility.
        self.check_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "Evaluate if answer is sufficiently supported by context. Return JSON only.",
                    ),
                    (
                        "human",
                        "Question: {question}\n\nAnswer: {answer}\n\nContext: {context}\n\n"
                        'JSON format: {{"sufficient":true,"need_more":false,"reason":"..."}}',
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

    @staticmethod
    def _parse_json_obj(raw: str) -> dict[str, Any]:
        txt = (raw or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", txt, flags=re.DOTALL)
        if fence:
            txt = fence.group(1).strip()
        for candidate in (txt, txt[txt.find("{") : txt.rfind("}") + 1] if "{" in txt and "}" in txt else ""):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
        return {}

    @staticmethod
    def _require_ok(result: dict[str, Any], source: str) -> Any:
        if not result.get("ok"):
            raise RuntimeError(f"{source} failed: {result.get('error', 'unknown error')}")
        return result["data"]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {t for t in re.findall(r"[A-Za-z0-9가-힣]+", (text or "").lower()) if len(t) >= 2}

    @staticmethod
    def _flatten_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def dfs(node: dict[str, Any]) -> None:
            out.append(node)
            for c in node.get("children", []) or []:
                dfs(c)

        for s in sections or []:
            dfs(s)
        return out

    def _split_text_chunks(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", (text or "")).strip()
        if not cleaned:
            return []
        n = len(cleaned)
        chunk_chars = max(200, int(self.config.chunk_chars))
        overlap = max(0, min(int(self.config.chunk_overlap), chunk_chars // 2))
        chunks: list[str] = []
        start = 0
        while start < n:
            end = min(n, start + chunk_chars)
            if end < n:
                split_floor = min(n, start + int(chunk_chars * 0.6))
                split_at = cleaned.rfind(" ", split_floor, end)
                if split_at > start:
                    end = split_at
            chunk = cleaned[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= n:
                break
            start = max(start + 1, end - overlap)
        return chunks

    def _extract_chunks(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        title = str(parsed.get("title", "")).strip()
        abstract = str(parsed.get("abstract", "")).strip()
        if abstract:
            chunks.append(
                {
                    "chunk_id": "abstract:0",
                    "section_id": "abstract",
                    "section_name": "Abstract",
                    "text": abstract[: self.config.max_chars_per_chunk],
                }
            )

        for idx, sec in enumerate(self._flatten_sections(parsed.get("sections", []))):
            sec_id = str(sec.get("id", "")).strip() or f"sec_{idx+1}"
            sec_name = str(sec.get("name", "")).strip() or sec_id
            body = "\n".join(sec.get("paragraphs", []) or [])
            for c_idx, chunk_text in enumerate(self._split_text_chunks(body)):
                chunks.append(
                    {
                        "chunk_id": f"{sec_id}:{c_idx}",
                        "section_id": sec_id,
                        "section_name": sec_name,
                        "text": chunk_text,
                    }
                )

        if not chunks and (title or abstract):
            chunks.append(
                {
                    "chunk_id": "fallback:0",
                    "section_id": "fallback",
                    "section_name": "Fallback",
                    "text": f"{title}\n{abstract}".strip()[: self.config.max_chars_per_chunk],
                }
            )
        return chunks

    def _list_pdf_files(self) -> list[str]:
        listed = tool_list_pdfs(self.config.pdf_dir)
        if not listed.get("ok"):
            return []
        files = (listed.get("data", {}) or {}).get("files", []) or []
        return [str(x) for x in files]

    @staticmethod
    def _is_list_query(question: str) -> bool:
        q = question.lower()
        list_markers = [
            "목록",
            "리스트",
            "파일 목록",
            "파일들",
            "무슨 파일",
            "어떤 파일",
            "읽을 수 있어",
            "열 수 있어",
            "몇 개 파일",
            "보여줘",
            "what files",
            "list files",
            "available files",
            "what pdf",
            "which pdf",
            "show files",
        ]
        content_markers = [
            "요약",
            "summary",
            "summarize",
            "제목",
            "title",
            "설명",
            "분석",
            "의미",
            "뭐야",
            "무엇",
            "근거",
            "예시",
            "소개",
            "더 있어",
            "방법",
            "결과",
            "한계",
        ]
        has_list_marker = any(k in q for k in list_markers)
        has_content_marker = any(k in q for k in content_markers)
        return has_list_marker and not has_content_marker

    @staticmethod
    def _is_summary_query(question: str) -> bool:
        q = question.lower()
        return any(k in q for k in ["요약", "summary", "summarize", "핵심"])

    def _pick_pdf_path(self, question: str, explicit_pdf_path: str | None, files: list[str]) -> str | None:
        root = Path(self.config.pdf_dir)
        if explicit_pdf_path:
            p = Path(explicit_pdf_path)
            if p.exists():
                return str(p)
            p2 = root / explicit_pdf_path
            if p2.exists():
                return str(p2)

        m = re.search(r"([\w\-.]+\.pdf)", question, flags=re.IGNORECASE)
        if m:
            p = root / m.group(1)
            if p.exists():
                return str(p)

        if not files:
            return None
        return str(root / files[0])

    @staticmethod
    def _file_source_meta(pdf_path: Path) -> dict[str, Any]:
        st = pdf_path.stat()
        return {"path": str(pdf_path.resolve()), "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}

    def _index_path_for_pdf(self, pdf_path: Path) -> Path:
        key = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8")).hexdigest()
        return self.index_dir / f"{key}.json"

    def _build_index_document(self, pdf_path: Path) -> dict[str, Any]:
        parsed = self._require_ok(tool_open_pdf(str(pdf_path), max_pages=None), "tool_open_pdf")
        chunks = self._extract_chunks(parsed)
        return {
            "version": 1,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source": self._file_source_meta(pdf_path),
            "title": str(parsed.get("title", "")).strip(),
            "abstract": str(parsed.get("abstract", "")).strip(),
            "pages_used": int(parsed.get("pages_used") or 0),
            "chunks": chunks,
        }

    def _load_index_document(self, pdf_path: Path) -> dict[str, Any] | None:
        path = self._index_path_for_pdf(pdf_path)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            source = payload.get("source", {}) or {}
            current = self._file_source_meta(pdf_path)
            same = (
                str(source.get("path", "")) == current["path"]
                and int(source.get("size", -1)) == current["size"]
                and int(source.get("mtime_ns", -1)) == current["mtime_ns"]
            )
            if not same:
                return None
            if not isinstance(payload.get("chunks", []), list):
                return None
            return payload
        except Exception:
            return None

    def _save_index_document(self, pdf_path: Path, doc: dict[str, Any]) -> None:
        self._index_path_for_pdf(pdf_path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def _build_or_load_index(self, pdf_path_str: str) -> tuple[dict[str, Any], bool]:
        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path_str}")
        cached = self._load_index_document(pdf_path)
        if cached is not None:
            return cached, True
        built = self._build_index_document(pdf_path)
        self._save_index_document(pdf_path, built)
        return built, False

    def _score_chunk(self, question: str, chunk: dict[str, Any]) -> float:
        q_tokens = self._tokenize(question)
        sec = str(chunk.get("section_name", "")).lower()
        txt = str(chunk.get("text", "")).lower()
        blob = f"{sec} {txt}"
        hit = sum(1 for t in q_tokens if t in blob)
        # mild bias for summary questions
        if self._is_summary_query(question) and "abstract" in sec:
            hit += 1.0
        return float(hit)

    def _select_chunks(self, question: str, chunk_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not chunk_pool:
            return []
        k = self.config.max_top_k if self._is_summary_query(question) else self.config.initial_top_k
        k = max(1, min(k, len(chunk_pool)))
        ranked = sorted(chunk_pool, key=lambda c: self._score_chunk(question, c), reverse=True)
        return ranked[:k]

    def _build_context(self, paper_index: dict[str, Any], selected_chunks: list[dict[str, Any]]) -> tuple[str, list[str], list[str]]:
        title = str(paper_index.get("title", "")).strip()
        abstract = str(paper_index.get("abstract", "")).strip()
        lines = [f"Title: {title}"]
        if abstract:
            lines.append(f"Abstract: {abstract[:1200]}")

        used_sections: list[str] = []
        used_chunk_ids: list[str] = []
        for c in selected_chunks:
            sec = str(c.get("section_name", "")).strip()
            cid = str(c.get("chunk_id", "")).strip()
            txt = str(c.get("text", ""))[: self.config.max_chars_per_chunk]
            if sec and sec not in used_sections:
                used_sections.append(sec)
            if cid:
                used_chunk_ids.append(cid)
            lines.append(f"Section: {sec}\n{txt}")

        context = "\n\n".join(lines).strip()
        if len(context) > self.config.max_context_chars:
            context = context[: self.config.max_context_chars]
        return context, used_sections, used_chunk_ids

    def answer(
        self,
        query: str,
        pdf_path: str | None = None,
        intent_query: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        def emit(event: str, **payload: Any) -> None:
            if on_event is None:
                return
            try:
                on_event({"event": event, **payload})
            except Exception:
                pass

        question = (intent_query or query).strip()
        emit("start", query=question, pdf_path=pdf_path or "(auto)")

        # 1) 질문 분석
        files = self._list_pdf_files()
        if self._is_list_query(question):
            emit("intent_planned", intent="list_files", preferred_section_keywords=[])
            lines = "\n".join(f"- {f}" for f in files[:50]) if files else "(없음)"
            answer = f"현재 PDF 파일 목록입니다:\n{lines}" if files else f"PDF 파일을 찾지 못했습니다. (dir: {self.config.pdf_dir})"
            result = {
                "route": "list_files",
                "pdf_path": "",
                "answer": answer,
                "context_meta": {
                    "iterations_used": 1,
                    "max_iterations": 1,
                    "used_section_names": [],
                    "used_chunk_ids": [],
                    "check_reason": "deterministic_list_files",
                    "pages_used": 0,
                    "index_cache_hit": False,
                },
            }
            emit("completed", route="list_files", iterations_used=1, used_section_names=[])
            return result

        intent = "summary" if self._is_summary_query(question) else "qa"
        emit("intent_planned", intent=intent, preferred_section_keywords=[])

        # 2) PDF 선택
        target_pdf = self._pick_pdf_path(question, pdf_path, files=files)
        if target_pdf is None:
            answer = f"PDF 파일을 찾지 못했습니다. (dir: {self.config.pdf_dir})"
            emit("fallback_answer", reason="no_pdf_found")
            result = {
                "route": intent,
                "pdf_path": "",
                "answer": answer,
                "context_meta": {
                    "iterations_used": 1,
                    "max_iterations": 1,
                    "used_section_names": [],
                    "used_chunk_ids": [],
                    "check_reason": "no_pdf_found",
                    "pages_used": 0,
                    "index_cache_hit": False,
                },
            }
            emit("completed", route=intent, iterations_used=1, used_section_names=[])
            return result

        emit("tool_call", name="tool_open_pdf", max_pages="full(index)")
        paper_index, cache_hit = self._build_or_load_index(target_pdf)
        chunk_pool = paper_index.get("chunks", []) or []
        pages_used = int(paper_index.get("pages_used") or 0)
        emit(
            "tool_result",
            name="tool_open_pdf",
            pages_used=pages_used,
            pdf_path=target_pdf,
            chunk_count=len(chunk_pool),
            index_cache_hit=cache_hit,
        )

        # 3) 청크 선택
        selected = self._select_chunks(question, chunk_pool)
        context, used_sections, used_chunk_ids = self._build_context(paper_index, selected)
        emit("context_selected", iteration=1, section_names=used_sections, chunk_ids=used_chunk_ids)

        # 4) 답변 생성
        answer = self.answer_chain.invoke({"question": question, "context": context}).strip()
        if not answer:
            emit("fallback_answer", reason="empty_answer")
            answer = "답변을 생성하지 못했습니다."

        result = {
            "route": intent,
            "pdf_path": target_pdf,
            "answer": answer,
            "context_meta": {
                "iterations_used": 1,
                "max_iterations": 1,
                "used_section_names": used_sections,
                "used_chunk_ids": used_chunk_ids,
                "check_reason": "deterministic_pipeline",
                "pages_used": pages_used,
                "index_cache_hit": cache_hit,
            },
        }
        emit("completed", route=intent, iterations_used=1, used_section_names=used_sections)
        return result


if __name__ == "__main__":
    orch = PaperLLMOrchestrator()
    out = orch.answer("3582560.pdf 요약해줘")
    print(out["context_meta"])
    print(out["answer"])
