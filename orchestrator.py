from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from tools import tool_parse_pdf


@dataclass
class OrchestratorConfig:
    model: str = os.getenv("PAPER_LLM_MODEL", "gpt-4o-mini")
    api_base: str = os.getenv("PAPER_LLM_API_BASE", "http://host.docker.internal:1234/v1")
    api_key: str = os.getenv("PAPER_LLM_API_KEY", "lm-studio")
    max_pages_default: int = 8
    max_pages_summary: int = 20
    max_chars_per_section: int = 2200
    max_iterations: int = 5
    initial_sections: int = 2
    section_step: int = 2


class PaperLLMOrchestrator:
    """LLM-driven orchestration with iterative context expansion (max 5 loops)."""

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
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
                        "Classify user question intent for paper QA. Return JSON only. "
                        "Allowed intent: summary, method, result, limitation, background, qa. "
                        "Also provide preferred_section_keywords array.",
                    ),
                    (
                        "human",
                        "Question: {question}\n"
                        "JSON format: "
                        '{{"intent":"qa","preferred_section_keywords":["method","experiment"],"query_keywords":["..."]}}',
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

    @staticmethod
    def _require_ok(result: dict[str, Any], source: str) -> Any:
        if not result.get("ok"):
            raise RuntimeError(f"{source} failed: {result.get('error', 'unknown error')}")
        return result["data"]

    @staticmethod
    def _pick_pdf_path(query: str, explicit_pdf_path: str | None) -> str:
        if explicit_pdf_path:
            return explicit_pdf_path
        m = re.search(r"([\w\-.]+\.pdf)", query, flags=re.IGNORECASE)
        if m:
            return str(Path("Papers") / m.group(1))
        pdfs = sorted(Path("Papers").glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError("No PDF found in Papers")
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

    def answer(self, query: str, pdf_path: str | None = None) -> dict[str, Any]:
        target_pdf = self._pick_pdf_path(query, pdf_path)

        # 1) LLM intent planning
        intent_raw = self.intent_chain.invoke({"question": query}).strip()
        intent_obj = self._parse_json_obj(intent_raw)
        intent = str(intent_obj.get("intent", "qa")).lower()
        preferred = [str(x) for x in intent_obj.get("preferred_section_keywords", []) if str(x).strip()]

        pages = self.config.max_pages_summary if intent == "summary" else self.config.max_pages_default
        parsed = self._require_ok(tool_parse_pdf(target_pdf, max_pages=pages), "tool_parse_pdf")

        # 2) Iterative loop (max 5)
        answer = ""
        last_reason = ""
        used_sections: list[str] = []
        top_k = self.config.initial_sections
        iteration = 1

        while iteration <= self.config.max_iterations:
            context, used_sections = self._build_context(parsed, query, preferred, top_k)
            answer = self.answer_chain.invoke({"question": query, "context": context}).strip()

            check_raw = self.check_chain.invoke(
                {"question": query, "answer": answer, "context": context[:7000]}
            ).strip()
            check_obj = self._parse_json_obj(check_raw)

            sufficient = bool(check_obj.get("sufficient", False))
            need_more = bool(check_obj.get("need_more", not sufficient))
            last_reason = str(check_obj.get("reason", ""))

            if sufficient or not need_more:
                break

            top_k += self.config.section_step
            iteration += 1

        return {
            "route": intent,
            "pdf_path": target_pdf,
            "answer": answer,
            "context_meta": {
                "iterations_used": iteration,
                "max_iterations": self.config.max_iterations,
                "used_section_names": used_sections,
                "check_reason": last_reason,
                "pages_used": parsed.get("pages_used"),
            },
        }


if __name__ == "__main__":
    orchestrator = PaperLLMOrchestrator(OrchestratorConfig(max_pages_default=6, max_pages_summary=12))
    q = "이 논문의 방법론을 자세히 설명해줘"
    result = orchestrator.answer(q)
    print(result["route"])
    print(result["context_meta"])
    print(result["answer"])
