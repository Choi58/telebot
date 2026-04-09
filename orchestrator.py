from __future__ import annotations

import json
import re
from dataclasses import dataclass
from settings import get_settings
from pathlib import Path
from typing import Any, Callable

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from tools import tool_parse_pdf


@dataclass
class OrchestratorConfig:
    model: str = "google/gemma-3-4b"
    api_base: str = "http://host.docker.internal:1234/v1"
    api_key: str = "lm-studio"
    max_pages_default: int = 20
    max_pages_summary: int = 5
    max_chars_per_section: int = 2200
    max_iterations: int = 5
    initial_sections: int = 2
    section_step: int = 2
    pdf_dir: str = "./Papers"
    parse_pages_initial: int = 6
    parse_pages_step: int = 6


class PaperLLMOrchestrator:
    """LLM-driven orchestration with iterative context expansion (max 5 loops)."""

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
        self.llm = ChatOpenAI(
            model=self.config.model,
            base_url=self.config.api_base,
            api_key=self.config.api_key,
            temperature=0.1,
            max_tokens=900,
        )

        self.intent_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are an intent classifier for a paper assistant. Return JSON only.\n"
                        "Do 2-step classification:\n"
                        "Step 1) Choose top-level category from: chitchat, file_ops, paper_qa.\n"
                        "Step 2) Choose final intent.\n"
                        "- If category=chitchat -> intent must be chitchat\n"
                        "- If category=file_ops -> intent must be list_files\n"
                        "- If category=paper_qa -> intent must be one of: summary, method, result, limitation, background, qa\n"
                        "Important disambiguation:\n"
                        "- If user asks about content of a specific PDF (title, summary, method, result, limitation, explanation), "
                        "it is paper_qa, not file_ops.\n"
                        "- file_ops/list_files is only for availability/search/listing of files.\n"
                        "Also provide preferred_section_keywords as a short array.\n"
                        "If uncertain, prefer intent=qa.\n"
                        "Output schema:\n"
                        '{{"category":"paper_qa","intent":"qa","preferred_section_keywords":["..."],"confidence":0.0}}\n'
                        "Examples:\n"
                        'Q: "안녕, 잘 지내?"\n'
                        'A: {{"category":"chitchat","intent":"chitchat","preferred_section_keywords":[],"confidence":0.98}}\n'
                        'Q: "무슨 pdf 파일 읽을 수 있어?"\n'
                        'A: {{"category":"file_ops","intent":"list_files","preferred_section_keywords":[],"confidence":0.97}}\n'
                        'Q: "2504.10789v1.pdf 읽고 제목 알려줘"\n'
                        'A: {{"category":"paper_qa","intent":"qa","preferred_section_keywords":["title","abstract"],"confidence":0.96}}\n'
                        'Q: "파일 목록 보여줘"\n'
                        'A: {{"category":"file_ops","intent":"list_files","preferred_section_keywords":[],"confidence":0.98}}\n'
                        'Q: "이 논문 방법론 자세히 설명해줘"\n'
                        'A: {{"category":"paper_qa","intent":"method","preferred_section_keywords":["method","approach"],"confidence":0.93}}\n'
                        'Q: "이 논문의 핵심 결과가 뭐야?"\n'
                        'A: {{"category":"paper_qa","intent":"result","preferred_section_keywords":["result","experiment"],"confidence":0.92}}\n'
                        'Q: "이 논문 요약해줘"\n'
                        'A: {{"category":"paper_qa","intent":"summary","preferred_section_keywords":["abstract","conclusion"],"confidence":0.95}}\n'
                        'Q: "이 분야 배경을 알려줘"\n'
                        'A: {{"category":"paper_qa","intent":"background","preferred_section_keywords":["introduction","background"],"confidence":0.88}}\n'
                        'Q: "이 논문의 한계는?"\n'
                        'A: {{"category":"paper_qa","intent":"limitation","preferred_section_keywords":["limitation","discussion"],"confidence":0.91}}\n'
                        'Q: "이 내용이 맞는지 설명해줘"\n'
                        'A: {{"category":"paper_qa","intent":"qa","preferred_section_keywords":[],"confidence":0.65}}',
                    ),
                    (
                        "human",
                        "Question: {question}\n"
                        "Return only one JSON object following the schema.",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.answer_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You answer questions about academic PDFs using only context. "
                        "If insufficient, state limitations briefly.",
                    ),
                    (
                        "human",
                        "[Question]\n{question}\n\n[Context]\n{context}\n\n"
                        "Answer in Korean and reference section names briefly.",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

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
                        "JSON format: "
                        '{{"sufficient":true,"need_more":false,"reason":"..."}}',
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.direct_answer_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are a helpful assistant. Answer in Korean. "
                        "If the question appears to require a specific paper, clearly say that document context is needed.",
                    ),
                    ("human", "Question: {question}"),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.next_step_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are a one-step planner for a paper QA agent. Return JSON only.\n"
                        "Allowed actions: parse_pdf, expand_context, answer, finish.\n"
                        "Rules:\n"
                        "1) If no parsed PDF context exists and paper evidence is needed, choose parse_pdf.\n"
                        "2) If answer is missing, choose answer after enough preparation.\n"
                        "3) If checker says need_more and parsed context exists, usually choose expand_context first.\n"
                        "4) Choose finish only when answer is sufficient.\n"
                        "Output format: "
                        '{{"action":"answer","reason":"...","preferred_section_keywords":["method"]}}',
                    ),
                    (
                        "human",
                        "Question: {question}\n"
                        "Current intent hint: {intent}\n"
                        "Has parsed PDF: {has_parsed}\n"
                        "Current top_k sections: {top_k}\n"
                        "Last checker need_more: {last_need_more}\n"
                        "Last checker reason: {last_reason}\n"
                        "Current answer empty: {answer_empty}",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

    @staticmethod
    def _require_ok(result: dict[str, Any], source: str) -> Any:
        if not result.get("ok"):
            raise RuntimeError(f"{source} failed: {result.get('error', 'unknown error')}")
        return result["data"]

    def _pick_pdf_path(self, query: str, explicit_pdf_path: str | None) -> str:
        pdf_root = Path(self.config.pdf_dir)
        if explicit_pdf_path:
            return explicit_pdf_path
        m = re.search(r"([\w\-.]+\.pdf)", query, flags=re.IGNORECASE)
        if m:
            return str(pdf_root / m.group(1))
        pdfs = sorted(pdf_root.glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError(f"No PDF found in {pdf_root}")
        return str(pdfs[0])

    @staticmethod
    def _flatten_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def dfs(node: dict[str, Any]) -> None:
            out.append(node)
            for c in node.get("children", []):
                dfs(c)

        for s in sections:
            dfs(s)
        return out

    @staticmethod
    def _parse_json_obj(raw: str) -> dict[str, Any]:
        txt = raw.strip()
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
    def _score_section(section: dict[str, Any], query: str, preferred: list[str]) -> float:
        q_tokens = {t for t in re.findall(r"[A-Za-z0-9가-힣]+", query.lower()) if len(t) >= 2}
        name = str(section.get("name", "")).lower()
        para = " ".join(section.get("paragraphs", [])[:2]).lower()
        text = f"{name} {para}"

        hit_q = sum(1 for t in q_tokens if t in text)
        hit_pref = sum(1 for t in preferred if t and t.lower() in name)
        return hit_q * 1.0 + hit_pref * 1.8

    def _list_pdf_files(self) -> list[str]:
        pdf_root = Path(self.config.pdf_dir)
        if not pdf_root.exists():
            return []
        return sorted([p.name for p in pdf_root.glob("*.pdf")])

    def _search_pdf_files(self, query: str) -> list[str]:
        files = self._list_pdf_files()
        if not files:
            return []
        q_tokens = [t for t in re.findall(r"[A-Za-z0-9가-힣]+", query.lower()) if len(t) >= 2]
        if not q_tokens:
            return files

        scored: list[tuple[int, str]] = []
        for f in files:
            name = f.lower()
            score = sum(1 for t in q_tokens if t in name)
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored] or files

    @staticmethod
    def _is_pdf_content_request(query: str) -> bool:
        q = query.lower()
        has_pdf = re.search(r"[\w\-.]+\.pdf", q) is not None
        if not has_pdf:
            return False
        content_keywords = [
            "제목",
            "요약",
            "내용",
            "설명",
            "핵심",
            "방법",
            "결과",
            "한계",
            "read",
            "title",
            "summary",
            "method",
            "result",
            "limitation",
        ]
        return any(k in q for k in content_keywords)

    def _build_context(
        self,
        parsed: dict[str, Any],
        query: str,
        preferred_section_keywords: list[str],
        top_k: int,
    ) -> tuple[str, list[str]]:
        title = parsed.get("title", "")
        abstract = parsed.get("abstract", "")
        sections = self._flatten_sections(parsed.get("sections", []))

        ranked = sorted(
            sections,
            key=lambda s: self._score_section(s, query, preferred_section_keywords),
            reverse=True,
        )
        picked = ranked[: max(1, min(top_k, len(ranked)))]

        lines = [f"Title: {title}"]
        if abstract:
            lines.append(f"Abstract: {abstract[:1200]}")

        used_names: list[str] = []
        for s in picked:
            name = str(s.get("name", ""))
            used_names.append(name)
            body = "\n".join(s.get("paragraphs", []))[: self.config.max_chars_per_section]
            lines.append(f"Section: {name}\n{body}")

        return "\n\n".join(lines).strip(), used_names

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
                # Never fail main flow because of event callback issues.
                pass

        user_query = intent_query or query
        emit("start", query=user_query, pdf_path=pdf_path or "(auto)")

        # 1) LLM intent planning
        intent_raw = self.intent_chain.invoke({"question": user_query}).strip()
        intent_obj = self._parse_json_obj(intent_raw)
        intent = str(intent_obj.get("intent", "qa")).lower()
        preferred = [str(x) for x in intent_obj.get("preferred_section_keywords", []) if str(x).strip()]
        if intent == "list_files" and self._is_pdf_content_request(user_query):
            intent = "qa"
            if not preferred:
                preferred = ["title", "abstract"]
            emit("intent_corrected", from_intent="list_files", to_intent=intent, reason="pdf_content_request")
        emit("intent_planned", intent=intent, preferred_section_keywords=preferred)

        if intent == "list_files":
            matched = self._search_pdf_files(user_query)
            if matched:
                lines = "\n".join(f"- {name}" for name in matched[:50])
                answer = f"현재 PDF 파일 목록(검색 반영)입니다:\n{lines}"
            else:
                answer = f"PDF 파일을 찾지 못했습니다. (dir: {self.config.pdf_dir})"
            result = {
                "route": intent,
                "pdf_path": "",
                "answer": answer,
                "context_meta": {
                    "iterations_used": 0,
                    "max_iterations": self.config.max_iterations,
                    "used_section_names": [],
                    "check_reason": "handled_by_list_files",
                    "pages_used": 0,
                },
            }
            emit("completed", route=intent, iterations_used=0, used_section_names=[])
            return result

        if intent == "chitchat":
            answer = self.direct_answer_chain.invoke({"question": query}).strip()
            result = {
                "route": intent,
                "pdf_path": "",
                "answer": answer,
                "context_meta": {
                    "iterations_used": 0,
                    "max_iterations": self.config.max_iterations,
                    "used_section_names": [],
                    "check_reason": "handled_by_chitchat",
                    "pages_used": 0,
                },
            }
            emit("completed", route=intent, iterations_used=0, used_section_names=[])
            return result

        # 2) Hybrid loop: Planner(1-step) + deterministic Executor + Checker
        parse_max_pages = self.config.max_pages_summary if intent == "summary" else self.config.max_pages_default
        parse_target_pages = max(1, min(self.config.parse_pages_initial, parse_max_pages))

        target_pdf: str | None = pdf_path
        parsed: dict[str, Any] | None = None
        pages_used = 0
        answer = ""
        last_reason = ""
        last_need_more = False
        used_sections: list[str] = []
        top_k = self.config.initial_sections
        iteration = 0

        for iteration in range(1, self.config.max_iterations + 1):
            emit(
                "iteration_start",
                iteration=iteration,
                top_k=top_k,
                has_parsed=parsed is not None,
                parse_target_pages=parse_target_pages,
            )

            planner_raw = self.next_step_chain.invoke(
                {
                    "question": query,
                    "intent": intent,
                    "has_parsed": parsed is not None,
                    "top_k": top_k,
                    "last_need_more": last_need_more,
                    "last_reason": last_reason,
                    "answer_empty": answer == "",
                }
            ).strip()
            planner_obj = self._parse_json_obj(planner_raw)
            action = str(planner_obj.get("action", "")).strip().lower()
            reason = str(planner_obj.get("reason", "")).strip()
            planner_preferred = [
                str(x) for x in planner_obj.get("preferred_section_keywords", []) if str(x).strip()
            ]
            if planner_preferred:
                preferred = planner_preferred

            if action not in {"parse_pdf", "expand_context", "answer", "finish"}:
                action = "parse_pdf" if parsed is None else "answer"
                reason = "fallback_invalid_action"

            emit("step_planned", iteration=iteration, action=action, reason=reason)

            if action == "finish":
                if answer:
                    break
                action = "answer"
                emit("step_overridden", iteration=iteration, action=action, reason="finish_without_answer")

            if action == "parse_pdf":
                if pages_used >= parse_max_pages and parsed is not None:
                    action = "answer"
                    emit(
                        "step_overridden",
                        iteration=iteration,
                        action=action,
                        reason="already_at_parse_page_limit",
                    )
                else:
                    if target_pdf is None:
                        target_pdf = self._pick_pdf_path(user_query, pdf_path)
                    emit("tool_call", name="tool_parse_pdf", max_pages=parse_target_pages)
                    parsed = self._require_ok(
                        tool_parse_pdf(target_pdf, max_pages=parse_target_pages), "tool_parse_pdf"
                    )
                    pages_used = int(parsed.get("pages_used") or parse_target_pages)
                    emit(
                        "tool_result",
                        name="tool_parse_pdf",
                        pages_used=pages_used,
                        pdf_path=target_pdf,
                    )
                    parse_target_pages = min(parse_max_pages, parse_target_pages + self.config.parse_pages_step)
                    continue

            if action == "expand_context":
                top_k += self.config.section_step
                emit("context_expanded", next_top_k=top_k)
                continue

            # action == "answer"
            if parsed is not None:
                context, used_sections = self._build_context(parsed, query, preferred, top_k)
                emit("context_selected", iteration=iteration, section_names=used_sections)
                answer = self.answer_chain.invoke({"question": query, "context": context}).strip()
                check_context = context[:7000]
            else:
                used_sections = []
                emit("context_selected", iteration=iteration, section_names=used_sections)
                answer = self.direct_answer_chain.invoke({"question": query}).strip()
                check_context = "(no_pdf_context)"

            check_raw = self.check_chain.invoke(
                {"question": query, "answer": answer, "context": check_context}
            ).strip()
            check_obj = self._parse_json_obj(check_raw)

            sufficient = bool(check_obj.get("sufficient", False))
            last_need_more = bool(check_obj.get("need_more", not sufficient))
            last_reason = str(check_obj.get("reason", ""))
            emit(
                "iteration_check",
                iteration=iteration,
                sufficient=sufficient,
                need_more=last_need_more,
                reason=last_reason,
            )

            if sufficient or not last_need_more:
                break

        if not answer:
            emit("fallback_answer", reason="no_answer_generated_in_loop")
            if parsed is not None:
                context, used_sections = self._build_context(parsed, query, preferred, top_k)
                emit("context_selected", iteration=iteration, section_names=used_sections)
                answer = self.answer_chain.invoke({"question": query, "context": context}).strip()
            else:
                answer = self.direct_answer_chain.invoke({"question": query}).strip()

        result = {
            "route": intent,
            "pdf_path": target_pdf or "",
            "answer": answer,
            "context_meta": {
                "iterations_used": iteration,
                "max_iterations": self.config.max_iterations,
                "used_section_names": used_sections,
                "check_reason": last_reason,
                "pages_used": pages_used,
            },
        }
        emit("completed", route=intent, iterations_used=iteration, used_section_names=used_sections)
        return result


if __name__ == "__main__":
    orchestrator = PaperLLMOrchestrator(OrchestratorConfig(max_pages_default=6, max_pages_summary=12))
    q = "이 논문의 방법론을 자세히 설명해줘"
    result = orchestrator.answer(q)
    print(result["route"])
    print(result["context_meta"])
    print(result["answer"])
