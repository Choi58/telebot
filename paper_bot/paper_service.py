from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from .rag_pipeline import RagPipeline
from settings import get_settings
from .summary_pipeline import SummaryPipeline
from legacy_experiments.tools import tool_list_pdfs, tool_open_pdf


@dataclass
class OrchestratorConfig:
    model: str = "google/gemma-3-4b"
    api_base: str = "http://host.docker.internal:1234/v1"
    api_key: str = "lm-studio"
    pdf_dir: str = "./Papers"
    index_dir: str = "./cache/index"
    max_pages_default: int = 20
    max_pages_summary: int = 5
    chunk_chars: int = 950
    chunk_overlap: int = 160
    max_chars_per_chunk: int = 2200
    max_context_chars: int = 9000
    retriever_top_k: int = 12
    rerank_top_k: int = 6
    sentence_window_size: int = 2
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    embedding_api_base: str = "http://host.docker.internal:1234/v1"
    embedding_api_key: str = "lm-studio"
    embedding_model: str = "nomic-embed-text-v1.5"
    embedding_dimensions: int = 768
    summary_max_sections: int = 10
    summary_section_chars: int = 3200


class PaperService:
    """Single-paper service with explicit state transition: summary -> qa."""

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        pdf_path: str | None = None,
        auto_bootstrap: bool = True,
    ) -> None:
        if config is None:
            s = get_settings()
            config = OrchestratorConfig(
                model=s.lm_studio_model,
                api_base=s.lm_studio_base_url,
                api_key="lm-studio",
                pdf_dir=s.pdf_dir,
                embedding_api_base=s.lm_studio_base_url,
                embedding_api_key="lm-studio",
                embedding_model=s.lm_studio_embedding_model,
            )
        self.config = config
        self.index_dir = Path(self.config.index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.llm = ChatOpenAI(
            model=self.config.model,
            base_url=self.config.api_base,
            api_key=self.config.api_key,
            temperature=0.1,
            max_tokens=1100,
        )

        # Kept for PaperBotService compatibility.
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

        self.qa_cited_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "당신은 논문 QA 어시스턴트다. 반드시 제공된 문맥만 사용한다. "
                        "모든 핵심 주장 뒤에 [n] 형태의 인용을 붙인다. "
                        "근거가 부족하면 '근거 부족'이라고 명확히 말한다.",
                    ),
                    (
                        "human",
                        "[질문]\n{question}\n\n[검색 문맥]\n{context}\n\n"
                        "[출력 규칙]\n"
                        "1) 한국어로 간결하게 답변\n"
                        "2) 각 문단에 최소 1개 인용 [n]\n"
                        "3) 마지막에 '근거 목록' 섹션 추가\n"
                        "4) 근거 목록 형식: [n] section=... | page=... | chunk=...",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.section_summary_chain = (
            ChatPromptTemplate.from_messages(
                [
                    ("system", "당신은 논문 섹션 요약기다. 노이즈를 제거하고 핵심 사실만 요약한다."),
                    (
                        "human",
                        "[논문 제목]\n{title}\n\n[섹션명]\n{section_name}\n\n[섹션 본문]\n{section_text}\n\n"
                        "아래 포맷으로 한국어 4줄 이내 요약:\n"
                        "- 목적\n- 방법\n- 핵심 결과\n- 한계(없으면 생략)",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.final_summary_chain = (
            ChatPromptTemplate.from_messages(
                [
                    ("system", "당신은 계층형 논문 요약기다. 섹션 요약들을 통합해 전체 핵심을 만든다."),
                    (
                        "human",
                        "[사용자 요청]\n{question}\n\n[섹션 요약들]\n{section_summaries}\n\n"
                        "한국어로 최종 요약:\n"
                        "- 논문 한줄 핵심\n"
                        "- 핵심 기여 3개\n"
                        "- 방법론 요약\n"
                        "- 실험/결과 요약\n"
                        "- 한계 및 주의점",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.check_chain = (
            ChatPromptTemplate.from_messages(
                [
                    ("system", "Evaluate if answer is sufficiently supported by context. Return JSON only."),
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

        self.rag_pipeline = RagPipeline(self.config)
        self.summary_pipeline = SummaryPipeline(self.config, self.section_summary_chain, self.final_summary_chain)

        self.active_pdf_path = ""
        self.active_paper_index: dict[str, Any] | None = None
        self.active_index_cache_hit = False
        self.active_source_kind = "pdf"
        self.mode = "summary_pending"
        self.summary_cache = ""
        self.summary_sections: list[str] = []
        self.summary_chunk_ids: list[str] = []

        if pdf_path:
            self.set_pdf(pdf_path)
        elif auto_bootstrap:
            self._bootstrap_active_paper()

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
        abstract = str(parsed.get("abstract", "")).strip()
        if abstract:
            chunks.append(
                {
                    "chunk_id": "abstract:0",
                    "section_id": "abstract",
                    "section_name": "Abstract",
                    "chunk_index": 0,
                    "page_start": None,
                    "page_end": None,
                    "text": abstract[: self.config.max_chars_per_chunk],
                }
            )

        for idx, sec in enumerate(self._flatten_sections(parsed.get("sections", []))):
            sec_id = str(sec.get("id", "")).strip() or f"sec_{idx + 1}"
            sec_name = str(sec.get("name", "")).strip() or sec_id
            body = "\n".join(sec.get("paragraphs", []) or [])
            for c_idx, chunk_text in enumerate(self._split_text_chunks(body)):
                chunks.append(
                    {
                        "chunk_id": f"{sec_id}:{c_idx}",
                        "section_id": sec_id,
                        "section_name": sec_name,
                        "chunk_index": c_idx,
                        "page_start": None,
                        "page_end": None,
                        "text": chunk_text[: self.config.max_chars_per_chunk],
                    }
                )
        return chunks

    @staticmethod
    def _is_summary_query(question: str) -> bool:
        q = question.lower()
        return any(k in q for k in ["요약", "summary", "summarize", "핵심"])

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
            "version": 2,
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
            if not same or int(payload.get("version", 0)) < 2:
                return None
            if not isinstance(payload.get("chunks", []), list):
                return None
            return payload
        except Exception:
            return None

    def _save_index_document(self, pdf_path: Path, doc: dict[str, Any]) -> None:
        self._index_path_for_pdf(pdf_path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def _build_or_load_index(self, pdf_path: str) -> tuple[dict[str, Any], bool]:
        p = Path(pdf_path)
        if not p.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        cached = self._load_index_document(p)
        if cached is not None:
            return cached, True
        built = self._build_index_document(p)
        self._save_index_document(p, built)
        return built, False

    def _resolve_single_pdf_path(self) -> str | None:
        configured = os.getenv("SINGLE_PAPER_PATH", "").strip()
        if configured:
            p = Path(configured)
            if p.exists():
                return str(p)
            p2 = Path(self.config.pdf_dir) / configured
            if p2.exists():
                return str(p2)

        listed = tool_list_pdfs(self.config.pdf_dir)
        if not listed.get("ok"):
            return None
        files = (listed.get("data", {}) or {}).get("files", []) or []
        if not files:
            return None
        return str(Path(self.config.pdf_dir) / str(files[0]))

    def _bootstrap_active_paper(self) -> None:
        path = self._resolve_single_pdf_path()
        if not path:
            self.active_pdf_path = ""
            self.active_paper_index = None
            self.active_index_cache_hit = False
            self.mode = "summary_pending"
            return
        self.set_pdf(path)

    def set_pdf(self, pdf_path: str) -> None:
        p = Path(pdf_path)
        if not p.exists():
            p2 = Path(self.config.pdf_dir) / pdf_path
            if p2.exists():
                p = p2
        if not p.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        index_doc, cache_hit = self._build_or_load_index(str(p))
        self.active_pdf_path = str(p)
        self.active_paper_index = index_doc
        self.active_index_cache_hit = cache_hit
        self.active_source_kind = "pdf"
        self.mode = "summary_pending"
        self.summary_cache = ""
        self.summary_sections = []
        self.summary_chunk_ids = []

    def set_parsed_paper_index(
        self,
        *,
        paper_index: dict[str, Any],
        source_label: str = "parsed.md",
    ) -> None:
        """Inject pre-parsed paper index (no PDF parse/index I/O)."""
        self.active_pdf_path = source_label
        self.active_paper_index = paper_index
        self.active_index_cache_hit = True
        self.active_source_kind = "parsed"
        self.mode = "summary_pending"
        self.summary_cache = ""
        self.summary_sections = []
        self.summary_chunk_ids = []

    def _ensure_active_index(self, pdf_path: str | None = None) -> tuple[dict[str, Any], str, bool]:
        if self.active_source_kind == "parsed" and self.active_paper_index is not None and not pdf_path:
            return self.active_paper_index, self.active_pdf_path, self.active_index_cache_hit

        if pdf_path:
            requested = Path(pdf_path)
            active = Path(self.active_pdf_path) if self.active_pdf_path else None
            if (not active) or (requested.resolve() != active.resolve() if requested.exists() and active.exists() else str(requested) != str(active)):
                self.set_pdf(pdf_path)

        if not self.active_pdf_path:
            self._bootstrap_active_paper()
        if not self.active_pdf_path:
            raise RuntimeError(f"No PDF found in {self.config.pdf_dir}")

        index_doc, cache_hit = self._build_or_load_index(self.active_pdf_path)
        self.active_paper_index = index_doc
        self.active_index_cache_hit = cache_hit
        return index_doc, self.active_pdf_path, cache_hit

    def _ensure_summary_ready(self, paper_index: dict[str, Any]) -> None:
        if self.mode == "qa" and self.summary_cache:
            return
        summary_text, used_sections, used_chunk_ids = self.summary_pipeline.build_hierarchical_summary(
            "논문의 핵심 기여, 방법, 결과, 한계를 요약해줘.", paper_index
        )
        self.summary_cache = summary_text
        self.summary_sections = used_sections
        self.summary_chunk_ids = used_chunk_ids
        self.mode = "qa"

    @staticmethod
    def _make_result(
        *,
        route: str,
        pdf_path: str,
        answer: str,
        used_sections: list[str],
        used_chunk_ids: list[str],
        check_reason: str,
        pages_used: int,
        index_cache_hit: bool,
    ) -> dict[str, Any]:
        return {
            "route": route,
            "pdf_path": pdf_path,
            "answer": answer,
            "context_meta": {
                "iterations_used": 1,
                "max_iterations": 1,
                "used_section_names": used_sections,
                "used_chunk_ids": used_chunk_ids,
                "check_reason": check_reason,
                "pages_used": pages_used,
                "index_cache_hit": index_cache_hit,
            },
        }

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
        ask_summary = self._is_summary_query(question)
        emit("start", query=question, pdf_path=pdf_path or self.active_pdf_path or "(auto-single)")

        try:
            paper_index, target_pdf, cache_hit = self._ensure_active_index(pdf_path=pdf_path)
        except Exception as e:
            emit("fallback_answer", reason="single_pdf_not_ready")
            result = self._make_result(
                route="qa",
                pdf_path="",
                answer=f"단일 논문 인덱스 준비 실패: {type(e).__name__}: {e}",
                used_sections=[],
                used_chunk_ids=[],
                check_reason="single_pdf_not_ready",
                pages_used=0,
                index_cache_hit=False,
            )
            emit("completed", route="qa", iterations_used=1, used_section_names=[])
            return result

        pages_used = int(paper_index.get("pages_used") or 0)
        emit(
            "tool_result",
            name="tool_open_pdf",
            pages_used=pages_used,
            pdf_path=target_pdf,
            chunk_count=len(paper_index.get("chunks", []) or []),
            index_cache_hit=cache_hit,
        )

        # Step 1: summary pre-run once, then transition to QA mode.
        if self.mode != "qa":
            self._ensure_summary_ready(paper_index)
            emit(
                "context_selected",
                iteration=1,
                section_names=self.summary_sections,
                chunk_ids=self.summary_chunk_ids,
            )

        if ask_summary:
            result = self._make_result(
                route="summary",
                pdf_path=target_pdf,
                answer=self.summary_cache or "요약을 생성하지 못했습니다.",
                used_sections=self.summary_sections,
                used_chunk_ids=self.summary_chunk_ids,
                check_reason="summary_cache",
                pages_used=pages_used,
                index_cache_hit=cache_hit,
            )
            emit("completed", route="summary", iterations_used=1, used_section_names=self.summary_sections)
            return result

        try:
            context, used_sections, used_chunk_ids, citation_lines = self.rag_pipeline.retrieve_context(question, paper_index)
        except Exception as e:
            emit("fallback_answer", reason=f"rag_pipeline_error:{type(e).__name__}")
            result = self._make_result(
                route="qa",
                pdf_path=target_pdf,
                answer=f"RAG 검색 파이프라인 오류: {type(e).__name__}: {e}",
                used_sections=[],
                used_chunk_ids=[],
                check_reason="rag_pipeline_error",
                pages_used=pages_used,
                index_cache_hit=cache_hit,
            )
            emit("completed", route="qa", iterations_used=1, used_section_names=[])
            return result

        emit("context_selected", iteration=1, section_names=used_sections, chunk_ids=used_chunk_ids)
        answer = self.qa_cited_chain.invoke({"question": question, "context": context}).strip()
        if not answer:
            emit("fallback_answer", reason="empty_answer")
            answer = "근거 부족: 답변을 생성하지 못했습니다."

        if citation_lines:
            answer = f"{answer}\n\n근거 목록\n" + "\n".join(citation_lines)

        result = self._make_result(
            route="qa",
            pdf_path=target_pdf,
            answer=answer,
            used_sections=used_sections,
            used_chunk_ids=used_chunk_ids,
            check_reason="qa_after_summary",
            pages_used=pages_used,
            index_cache_hit=cache_hit,
        )
        emit("completed", route="qa", iterations_used=1, used_section_names=used_sections)
        return result
