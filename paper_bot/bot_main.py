from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pypdf import PdfReader

from .paper_service import OrchestratorConfig, PaperService
from .pdf_tools import tool_list_pdfs, tool_open_pdf


TraceCallback = Callable[[str, dict[str, Any]], None]


class PaperBotService:
    """Application service that wraps orchestrator calls and per-session memory."""

    def __init__(
        self,
        orchestrator: PaperService | None = None,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self.orchestrator = orchestrator or PaperService(config=config)

        self.session_history_turns = int(os.getenv("SESSION_HISTORY_TURNS", "4"))
        self.session_reset_timezone = os.getenv("SESSION_RESET_TIMEZONE", "Asia/Seoul")
        self.max_summary_chars = int(os.getenv("SESSION_SUMMARY_MAX_CHARS", "1800"))
        self.sessions_dir = Path(os.getenv("SESSION_LOG_DIR", "sessions"))
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_state_dir = Path(os.getenv("SESSION_STATE_DIR", str(self.sessions_dir / "state")))
        self.session_state_dir.mkdir(parents=True, exist_ok=True)
        self.parse_cache_dir = Path(os.getenv("PARSE_CACHE_DIR", "cache/parsed"))
        self.parse_cache_dir.mkdir(parents=True, exist_ok=True)
        self.summary_max_steps = int(os.getenv("SUMMARY_MAX_STEPS", "5"))
        self.summary_section_batch = int(os.getenv("SUMMARY_SECTION_BATCH", "2"))
        self.summary_max_sections = int(os.getenv("SUMMARY_MAX_SECTIONS", "8"))
        self.summary_section_chars = int(os.getenv("SUMMARY_SECTION_CHARS", "2200"))
        self.trace_default_on = os.getenv("TRACE_DEFAULT_ON", "true").lower() in {"1", "true", "yes", "on"}

        try:
            self.session_tz = ZoneInfo(self.session_reset_timezone)
        except ZoneInfoNotFoundError:
            self.session_tz = ZoneInfo("UTC")

        self.sessions: dict[str, dict[str, Any]] = {}
        self.title_normalize_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You normalize academic paper titles. "
                        "Return only one final title line, no markdown.\n"
                        "Remove portal/update/url/doi noise. "
                        "If uncertain, return fallback exactly.",
                    ),
                    (
                        "human",
                        "Raw title: {raw_title}\n"
                        "PDF metadata title: {metadata_title}\n"
                        "Section names: {section_names}\n"
                        "Intro snippet: {intro_snippet}\n"
                        "Fallback: {fallback}\n"
                        "Output title only.",
                    ),
                ]
            )
            | self.orchestrator.llm
            | StrOutputParser()
        )
        self.query_refine_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You normalize user questions for academic paper QA. Return JSON only.",
                    ),
                    (
                        "human",
                        "Question: {question}\n"
                        'JSON format: {{"normalized_ko":"...","english":"...","focus_keywords":["..."]}}',
                    ),
                ]
            )
            | self.orchestrator.llm
            | StrOutputParser()
        )

    @staticmethod
    def _format_event(event: dict[str, Any]) -> str | None:
        name = event.get("event")

        if name == "start":
            return f"[START] PDF: {event.get('pdf_path', '-')}"
        if name == "intent_planned":
            intent = event.get("intent", "qa")
            pref = event.get("preferred_section_keywords", []) or []
            pref_text = ", ".join(str(x) for x in pref[:4]) if pref else "(없음)"
            return f"[PLAN] intent={intent}, section_hint={pref_text}"
        if name == "query_refined":
            return (
                f"[QUERY] normalized={event.get('normalized_ko', '')} | "
                f"en={event.get('english', '')}"
            )
        if name == "query_translated":
            return f"[QUERY] translated={event.get('translated', '')}"
        if name == "intent_corrected":
            return (
                f"[PLAN] intent corrected: {event.get('from_intent')} -> {event.get('to_intent')} "
                f"({event.get('reason', '')})"
            )
        if name == "tool_call":
            return f"[TOOL] {event.get('name')} 호출 (max_pages={event.get('max_pages')})"
        if name == "tool_result":
            return f"[TOOL] {event.get('name')} 완료 (pages_used={event.get('pages_used')})"
        if name == "active_pdf_selected":
            return f"[CTX] session active_pdf 사용: {event.get('pdf_path', '')}"
        if name == "iteration_start":
            return (
                f"[LOOP] iter={event.get('iteration')} 시작 "
                f"(top_k={event.get('top_k')}, has_parsed={event.get('has_parsed')}, "
                f"chunks={event.get('available_chunks', 0)})"
            )
        if name == "step_planned":
            return (
                f"[PLAN-STEP] iter={event.get('iteration')} "
                f"action={event.get('action')} reason={event.get('reason', '')}"
            )
        if name == "step_overridden":
            return (
                f"[PLAN-OVERRIDE] iter={event.get('iteration')} "
                f"action={event.get('action')} reason={event.get('reason', '')}"
            )
        if name == "context_selected":
            names = event.get("section_names", []) or []
            chunk_ids = event.get("chunk_ids", []) or []
            preview = ", ".join(str(n) for n in names[:3]) if names else "(없음)"
            return (
                f"[CTX] iter={event.get('iteration')} 선택 섹션: {preview} "
                f"(chunks={len(chunk_ids)})"
            )
        if name == "iteration_check":
            return (
                f"[CHECK] iter={event.get('iteration')} "
                f"sufficient={event.get('sufficient')} need_more={event.get('need_more')}"
            )
        if name == "context_expanded":
            return f"[LOOP] 컨텍스트 확장 (next_top_k={event.get('next_top_k')})"
        if name == "fallback_answer":
            return f"[FALLBACK] {event.get('reason', 'fallback answer')}"
        if name == "readback_refined":
            return (
                f"[READBACK] section-level re-read applied "
                f"(pdf={event.get('pdf_path', '')}, sections={event.get('section_ids', [])})"
            )
        if name == "completed":
            return (
                f"[DONE] route={event.get('route')}, "
                f"iterations={event.get('iterations_used')}"
            )

        return None

    @staticmethod
    def _safe_session_id(raw: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw.strip())
        return safe or "default"

    def _default_session(self, safe_session_id: str) -> dict[str, Any]:
        return {
            "summary": "",
            "recent_turns": [],
            "state": {"active_pdf": ""},
            "briefing_cache": {},
            "trace_enabled": self.trace_default_on,
            "log_file_name": self._new_log_file_name(safe_session_id),
            "session_id_safe": safe_session_id,
        }

    def _session_state_path(self, safe_session_id: str) -> Path:
        return self.session_state_dir / f"{safe_session_id}.json"

    def _load_session_state(self, safe_session_id: str) -> dict[str, Any] | None:
        path = self._session_state_path(safe_session_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return payload
        except Exception:
            return None

    def _save_session_state(self, safe_session_id: str, session: dict[str, Any]) -> None:
        path = self._session_state_path(safe_session_id)
        payload = {
            "summary": str(session.get("summary", "")).strip(),
            "recent_turns": session.get("recent_turns", []),
            "state": session.get("state", {}),
            "briefing_cache": session.get("briefing_cache", {}),
            "trace_enabled": bool(session.get("trace_enabled", self.trace_default_on)),
            "log_file_name": str(session.get("log_file_name", "")).strip() or self._new_log_file_name(safe_session_id),
            "session_id_safe": safe_session_id,
            "updated_at": datetime.now(self.session_tz).isoformat(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _normalize_session_shape(self, session: dict[str, Any], safe_session_id: str) -> dict[str, Any]:
        normalized = self._default_session(safe_session_id)

        if isinstance(session.get("summary"), str):
            normalized["summary"] = session["summary"]
        recent_turns = session.get("recent_turns", [])
        if isinstance(recent_turns, list):
            clean_turns: list[dict[str, str]] = []
            for turn in recent_turns:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role", "")).strip()
                content = str(turn.get("content", "")).strip()
                if role and content:
                    clean_turns.append({"role": role, "content": content})
            normalized["recent_turns"] = clean_turns

        state = session.get("state", {})
        if isinstance(state, dict):
            normalized_state = {"active_pdf": str(state.get("active_pdf", "")).strip()}
            normalized["state"] = normalized_state

        briefing_cache = session.get("briefing_cache", {})
        if isinstance(briefing_cache, dict):
            clean_cache: dict[str, dict[str, Any]] = {}
            for k, v in briefing_cache.items():
                key = str(k).strip()
                if not key or not isinstance(v, dict):
                    continue
                summary_text = str(v.get("summary", "")).strip()
                if not summary_text:
                    continue
                clean_cache[key] = {
                    "summary": summary_text,
                    "used_sections": [str(x) for x in (v.get("used_sections", []) or []) if str(x).strip()],
                    "used_chunk_ids": [str(x) for x in (v.get("used_chunk_ids", []) or []) if str(x).strip()],
                    "title": str(v.get("title", "")).strip(),
                }
            normalized["briefing_cache"] = clean_cache

        normalized["trace_enabled"] = bool(session.get("trace_enabled", self.trace_default_on))

        log_file_name = str(session.get("log_file_name", "")).strip()
        if log_file_name:
            normalized["log_file_name"] = log_file_name
        normalized["session_id_safe"] = safe_session_id
        return normalized

    def _get_or_create_session(self, session_id: str) -> dict[str, Any]:
        sid = self._safe_session_id(session_id)
        if sid not in self.sessions:
            loaded = self._load_session_state(sid) or {}
            self.sessions[sid] = self._normalize_session_shape(loaded, sid)
        session = self.sessions[sid]
        state = session.setdefault("state", {})
        if not isinstance(state, dict):
            session["state"] = {"active_pdf": ""}
        else:
            state.setdefault("active_pdf", "")
        briefing_cache = session.setdefault("briefing_cache", {})
        if not isinstance(briefing_cache, dict):
            session["briefing_cache"] = {}
        session.setdefault("session_id_safe", sid)
        return self.sessions[sid]

    @staticmethod
    def _briefing_cache_key(pdf_path: str | Path) -> str:
        p = Path(str(pdf_path))
        try:
            return str(p.resolve())
        except Exception:
            return str(p)

    def _apply_briefing_cache_to_orchestrator(self, session: dict[str, Any], effective_pdf_path: str | None) -> bool:
        cache = session.get("briefing_cache", {})
        if not isinstance(cache, dict) or not cache:
            return False

        candidates: list[str] = []
        if effective_pdf_path:
            candidates.append(self._briefing_cache_key(effective_pdf_path))
            candidates.append(str(effective_pdf_path))
        state = session.get("state", {})
        if isinstance(state, dict):
            active_pdf = str(state.get("active_pdf", "")).strip()
            if active_pdf:
                candidates.append(self._briefing_cache_key(active_pdf))
                candidates.append(active_pdf)

        seen: set[str] = set()
        for key in candidates:
            if not key or key in seen:
                continue
            seen.add(key)
            payload = cache.get(key)
            if not isinstance(payload, dict):
                continue
            summary_text = str(payload.get("summary", "")).strip()
            if not summary_text:
                continue
            self.orchestrator.apply_briefing_summary_cache(
                summary_text=summary_text,
                used_sections=[str(x) for x in (payload.get("used_sections", []) or []) if str(x).strip()],
                used_chunk_ids=[str(x) for x in (payload.get("used_chunk_ids", []) or []) if str(x).strip()],
            )
            return True
        return False

    def _new_log_file_name(self, safe_session_id: str) -> str:
        stamp = datetime.now(self.session_tz).strftime("%Y%m%d_%H%M%S")
        return f"{safe_session_id}_{stamp}.md"

    def _list_pdf_paths(self) -> list[Path]:
        listed = tool_list_pdfs(self.orchestrator.config.pdf_dir)
        if not listed.get("ok"):
            return []
        data = listed.get("data", {}) or {}
        files = [str(x) for x in (data.get("files", []) or [])]
        root = Path(self.orchestrator.config.pdf_dir)
        return [root / f for f in files]

    def _cache_key_for_pdf(self, pdf_path: Path) -> str:
        stat = pdf_path.stat()
        raw = f"{pdf_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path_for_pdf(self, pdf_path: Path) -> Path:
        return self.parse_cache_dir / f"{self._cache_key_for_pdf(pdf_path)}.json"

    def _load_cached_parsed(self, pdf_path: Path) -> dict[str, Any] | None:
        cache_path = self._cache_path_for_pdf(pdf_path)
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            parsed = payload.get("parsed")
            if not isinstance(parsed, dict):
                return None
            return parsed
        except Exception:
            return None

    def _save_cached_parsed(self, pdf_path: Path, parsed: dict[str, Any]) -> None:
        cache_path = self._cache_path_for_pdf(pdf_path)
        payload = {
            "source_file": str(pdf_path.resolve()),
            "cached_at": datetime.now(self.session_tz).isoformat(),
            "parsed": parsed,
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _build_or_load_parsed(self, pdf_path: Path) -> tuple[dict[str, Any], bool]:
        cached = self._load_cached_parsed(pdf_path)
        if cached is not None:
            return cached, True

        parsed_raw = tool_open_pdf(str(pdf_path), max_pages=self.orchestrator.config.max_pages_default)
        if not parsed_raw.get("ok"):
            raise RuntimeError(str(parsed_raw.get("error", "unknown parse error")))
        parsed = parsed_raw.get("data", {}) or {}
        if not isinstance(parsed, dict):
            raise RuntimeError("invalid parsed payload")
        self._save_cached_parsed(pdf_path, parsed)
        return parsed, False

    @staticmethod
    def _clean_paper_title(raw_title: str, fallback: str) -> str:
        t = (raw_title or "").strip()
        if not t:
            return fallback
        # Drop private-use/invalid glyph noise from OCR/extract pipeline.
        t = re.sub(r"[\uE000-\uF8FF]", "", t)
        t = re.sub(r"https?://\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\bdoi:\s*10\.\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\b10\.\d{4,9}/\S+", " ", t)
        t = re.sub(r"latest updates?", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip(" -_|")
        if not t or len(t) < 4:
            return fallback
        # If the remaining title still looks like URL/portal noise, fallback to file stem.
        noisy_keywords = ["dl.acm.org", "doi", "http", "www."]
        if any(k in t.lower() for k in noisy_keywords) and len(t.split()) <= 4:
            return fallback
        return t

    @staticmethod
    def _is_suspicious_title(title: str) -> bool:
        t = (title or "").strip().lower()
        if not t or len(t) < 4:
            return True
        suspicious_keywords = ["latest updates", "doi", "http", "www.", "dl.acm.org"]
        return any(k in t for k in suspicious_keywords)

    @staticmethod
    def _extract_intro_snippet(parsed: dict[str, Any], max_chars: int = 420) -> str:
        sections = parsed.get("sections", []) or []
        for sec in sections[:4]:
            paragraphs = sec.get("paragraphs", []) or []
            if paragraphs:
                txt = str(paragraphs[0]).strip().replace("\n", " ")
                if txt:
                    return txt[:max_chars]
        return ""

    @staticmethod
    def _extract_section_names(parsed: dict[str, Any], max_n: int = 8) -> str:
        names: list[str] = []
        sections = parsed.get("sections", []) or []
        for sec in sections[:max_n]:
            name = str(sec.get("name", "")).strip()
            if name:
                names.append(name)
        return ", ".join(names)

    def _extract_pdf_metadata_title(self, pdf_path: Path) -> str:
        try:
            reader = PdfReader(str(pdf_path))
            md = reader.metadata or {}
            raw = str(md.get("/Title", "")).strip()
            return self._clean_paper_title(raw, "")
        except Exception:
            return ""

    def _normalize_title_with_llm(
        self,
        *,
        raw_title: str,
        metadata_title: str,
        parsed: dict[str, Any],
        fallback: str,
    ) -> str:
        try:
            out = self.title_normalize_chain.invoke(
                {
                    "raw_title": raw_title or "",
                    "metadata_title": metadata_title or "",
                    "section_names": self._extract_section_names(parsed) or "(none)",
                    "intro_snippet": self._extract_intro_snippet(parsed) or "(none)",
                    "fallback": fallback,
                }
            ).strip()
            out = re.sub(r"^['\"`]+|['\"`]+$", "", out).strip()
            return self._clean_paper_title(out, fallback)
        except Exception:
            return fallback

    def _resolve_paper_title(self, pdf_path: Path, parsed: dict[str, Any]) -> str:
        fallback = pdf_path.stem
        raw_title = str(parsed.get("title", "")).strip()
        metadata_title = self._extract_pdf_metadata_title(pdf_path)

        cleaned_meta = self._clean_paper_title(metadata_title, "")
        cleaned_raw = self._clean_paper_title(raw_title, "")

        if cleaned_meta and not self._is_suspicious_title(cleaned_meta):
            return cleaned_meta
        if cleaned_raw and not self._is_suspicious_title(cleaned_raw):
            return cleaned_raw

        llm_title = self._normalize_title_with_llm(
            raw_title=raw_title,
            metadata_title=metadata_title,
            parsed=parsed,
            fallback=fallback,
        )
        if llm_title and not self._is_suspicious_title(llm_title):
            return llm_title

        return cleaned_meta or cleaned_raw or fallback

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

    def _summarize_with_model(self, question: str, context: str) -> str:
        return self.orchestrator.answer_chain.invoke({"question": question, "context": context}).strip()

    @staticmethod
    def _token_overlap_score(query: str, text: str) -> float:
        q_tokens = {t for t in re.findall(r"[A-Za-z0-9가-힣]+", query.lower()) if len(t) >= 2}
        if not q_tokens:
            return 0.0
        lower = text.lower()
        return float(sum(1 for t in q_tokens if t in lower))

    def _rank_sections_for_query(self, parsed: dict[str, Any], query: str) -> list[dict[str, Any]]:
        sections = self._flatten_sections(parsed.get("sections", []))
        sections = [s for s in sections if str(s.get("name", "")).strip()]
        ranked = sorted(
            sections,
            key=lambda s: self._token_overlap_score(
                query, f"{s.get('name', '')}\n" + "\n".join(s.get("paragraphs", [])[:2])
            ),
            reverse=True,
        )
        return ranked

    def _iterative_section_summaries(
        self,
        *,
        parsed: dict[str, Any],
        title: str,
        question: str,
    ) -> tuple[str, list[tuple[str, str]], dict[str, Any]]:
        abstract = str(parsed.get("abstract", "")).strip()
        ranked = self._rank_sections_for_query(parsed, question)
        ranked = ranked[: max(1, self.summary_max_sections)]

        selected_count = 0
        section_summaries: list[tuple[str, str]] = []
        paper_core = ""
        check_reason = ""
        sufficient = False
        iteration = 0

        for iteration in range(1, self.summary_max_steps + 1):
            if selected_count < len(ranked):
                add_n = min(self.summary_section_batch, len(ranked) - selected_count)
                new_sections = ranked[selected_count : selected_count + add_n]
                selected_count += add_n
            else:
                new_sections = []

            for sec in new_sections:
                sec_name = str(sec.get("name", "")).strip()
                sec_body = "\n".join(sec.get("paragraphs", []))[: self.summary_section_chars]
                if not sec_name or not sec_body.strip():
                    continue
                context = (
                    f"Title: {title}\n"
                    f"Abstract: {abstract[:1000]}\n"
                    f"Section: {sec_name}\n"
                    f"{sec_body}"
                )
                q = (
                    "이 섹션의 핵심을 한국어로 2~3문장 요약해줘. "
                    "링크/업데이트 문구/노이즈는 제외하고, 내용적 핵심만 남겨."
                )
                sec_summary = self._summarize_with_model(q, context)
                section_summaries.append((sec_name, sec_summary))

            core_context_parts = [f"Title: {title}"]
            if abstract:
                core_context_parts.append(f"Abstract: {abstract[:1200]}")
            for sec_name, sec_summary in section_summaries:
                core_context_parts.append(f"Section Summary - {sec_name}: {sec_summary}")
            core_context = "\n\n".join(core_context_parts)

            core_q = (
                "논문의 핵심 기여, 방법, 주요 결과를 한국어로 4~6줄로 요약해줘. "
                "불필요한 메타 문구 없이 핵심만."
            )
            paper_core = self._summarize_with_model(core_q, core_context)

            check_raw = self.orchestrator.check_chain.invoke(
                {"question": question, "answer": paper_core, "context": core_context[:7000]}
            ).strip()
            check_obj = self.orchestrator._parse_json_obj(check_raw)
            sufficient = bool(check_obj.get("sufficient", False))
            need_more = bool(check_obj.get("need_more", not sufficient))
            check_reason = str(check_obj.get("reason", ""))

            if sufficient or not need_more:
                break
            if selected_count >= len(ranked):
                break

        meta = {
            "iterations_used": iteration,
            "sections_used": len(section_summaries),
            "sufficient": sufficient,
            "check_reason": check_reason,
        }
        return paper_core, section_summaries, meta

    def _refine_query(self, query: str) -> dict[str, Any]:
        try:
            raw = self.query_refine_chain.invoke({"question": query}).strip()
            obj = self.orchestrator._parse_json_obj(raw)
            normalized_ko = str(obj.get("normalized_ko", "")).strip() or query
            english = str(obj.get("english", "")).strip()
            focus_keywords = [str(x) for x in obj.get("focus_keywords", []) if str(x).strip()]
            return {
                "normalized_ko": normalized_ko,
                "english": english,
                "focus_keywords": focus_keywords,
            }
        except Exception:
            return {"normalized_ko": query, "english": "", "focus_keywords": []}

    def _extract_pdf_path_from_query(self, query: str) -> str | None:
        m = re.search(r"([\w\-.]+\.pdf)", query, flags=re.IGNORECASE)
        if not m:
            return None
        candidate = m.group(1)
        p = Path(candidate)
        if p.exists():
            return str(p)
        p2 = Path(self.orchestrator.config.pdf_dir) / candidate
        if p2.exists():
            return str(p2)
        return None

    @staticmethod
    def _is_low_confidence_answer(answer: str) -> bool:
        t = (answer or "").strip().lower()
        if not t:
            return True
        weak_patterns = [
            "정보가 부족",
            "알 수 없습니다",
            "전문",
            "full text",
            "추론",
            "일반적인",
        ]
        return any(p in t for p in weak_patterns)

    def _find_section_ids_for_query(
        self,
        parsed: dict[str, Any],
        query_ko: str,
        query_en: str,
        top_n: int = 2,
    ) -> list[str]:
        merged_query = f"{query_ko}\n{query_en}".strip()
        sections = self._flatten_sections(parsed.get("sections", []))
        scored: list[tuple[float, str]] = []
        for sec in sections:
            sec_id = str(sec.get("id", "")).strip()
            if not sec_id:
                continue
            text = f"{sec.get('name', '')}\n" + "\n".join(sec.get("paragraphs", [])[:2])
            score = self._token_overlap_score(merged_query, text)
            if score > 0:
                scored.append((score, sec_id))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = [sid for _, sid in scored[:top_n]]
        if out:
            return out
        # fallback to first sections
        fallback: list[str] = []
        for sec in sections[:top_n]:
            sid = str(sec.get("id", "")).strip()
            if sid:
                fallback.append(sid)
        return fallback

    def _refresh_answer_with_section_readback(
        self,
        *,
        query_ko: str,
        query_en: str,
        target_pdf: str,
    ) -> tuple[str | None, list[str]]:
        pdf_path = Path(target_pdf)
        if not pdf_path.exists():
            return None, []

        parsed, _ = self._build_or_load_parsed(pdf_path)
        section_ids = self._find_section_ids_for_query(parsed, query_ko, query_en, top_n=2)
        if not section_ids:
            return None, []

        flat = self._flatten_sections(parsed.get("sections", []))
        by_id: dict[str, dict[str, Any]] = {}
        for sec in flat:
            sid = str(sec.get("id", "")).strip()
            if sid:
                by_id[sid] = sec

        blocks: list[str] = []
        for sid in section_ids:
            sec = by_id.get(sid, {})
            sec_name = str(sec.get("name", sid)).strip()
            sec_text = "\n".join(sec.get("paragraphs", []))[:3000]
            if sec_text.strip():
                blocks.append(f"Section: {sec_name}\n{sec_text}")

        if not blocks:
            return None, []

        context = "\n\n".join(blocks)
        q = (
            f"{query_ko}\n"
            + (f"English version: {query_en}\n" if query_en else "")
            + "위 컨텍스트에 근거해서만 답해줘. 일반론 추측은 피하고, 근거 섹션 중심으로 답해줘."
        )
        improved = self.orchestrator.answer_chain.invoke({"question": q, "context": context}).strip()
        return improved or None, section_ids

    def set_trace_enabled(self, session_id: str, enabled: bool) -> bool:
        sid = self._safe_session_id(session_id)
        session = self._get_or_create_session(session_id)
        session["trace_enabled"] = bool(enabled)
        self._save_session_state(sid, session)
        return bool(session["trace_enabled"])

    def get_trace_enabled(self, session_id: str) -> bool:
        session = self._get_or_create_session(session_id)
        return bool(session.get("trace_enabled", self.trace_default_on))

    def reset_session(self, session_id: str) -> None:
        session = self._get_or_create_session(session_id)
        sid = self._safe_session_id(session_id)
        session["summary"] = ""
        session["recent_turns"] = []
        session["state"] = {"active_pdf": ""}
        session["log_file_name"] = self._new_log_file_name(sid)
        self._save_session_state(sid, session)

    def _resolve_effective_pdf_path(
        self,
        *,
        session: dict[str, Any],
        user_query: str,
        explicit_pdf_path: str | None,
    ) -> str | None:
        # 1) Explicit API argument always wins.
        if explicit_pdf_path:
            return explicit_pdf_path

        # 2) If current message mentions a pdf filename, use it.
        mentioned = self._extract_pdf_path_from_query(user_query)
        if mentioned:
            return mentioned

        # 3) Deictic reference handling: "이거/그거/저거/위에서/아까/that/it..." -> pull previous paper context.
        deictic = self._has_deictic_reference(user_query)

        # 3-a) Fallback to session active pdf for context continuity.
        state = session.get("state", {}) if isinstance(session.get("state", {}), dict) else {}
        active_pdf = str(state.get("active_pdf", "")).strip()
        if active_pdf and Path(active_pdf).exists():
            return active_pdf

        # 3-b) If active_pdf is missing but user used deictic wording, recover from recent turns.
        if deictic:
            recovered = self._recover_pdf_path_from_session_history(session)
            if recovered:
                return recovered
        return None

    @staticmethod
    def _has_deictic_reference(query: str) -> bool:
        q = (query or "").strip().lower()
        if not q:
            return False
        markers = [
            "이거",
            "그거",
            "저거",
            "이 논문",
            "그 논문",
            "저 논문",
            "아까",
            "방금",
            "위에서",
            "앞에서",
            "해당 논문",
            "that",
            "it",
            "this paper",
            "that paper",
            "previous paper",
        ]
        return any(m in q for m in markers)

    def _recover_pdf_path_from_session_history(self, session: dict[str, Any]) -> str | None:
        # 1) recent_turns에서 마지막으로 언급된 *.pdf를 역순 탐색
        recent_turns: list[dict[str, str]] = session.get("recent_turns", []) or []
        for turn in reversed(recent_turns):
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            found = self._extract_pdf_path_from_query(content)
            if found and Path(found).exists():
                return found

        # 2) session summary에서도 한 번 더 탐색
        summary = str(session.get("summary", "")).strip()
        if summary:
            found = self._extract_pdf_path_from_query(summary)
            if found and Path(found).exists():
                return found
        return None

    def _update_active_pdf_from_result(self, session: dict[str, Any], result: dict[str, Any]) -> None:
        state = session.setdefault("state", {})
        if not isinstance(state, dict):
            session["state"] = {}
            state = session["state"]
        route = str(result.get("route", "")).strip().lower()
        chosen = str(result.get("pdf_path", "")).strip()
        # Keep continuity only for paper routes.
        paper_routes = {"qa", "summary", "method", "result", "limitation", "background"}
        if route in paper_routes and chosen and Path(chosen).exists():
            state["active_pdf"] = chosen

    def _build_contextual_query(self, user_text: str, session: dict[str, Any]) -> str:
        summary = str(session.get("summary", "")).strip()
        recent_turns: list[dict[str, str]] = session.get("recent_turns", [])

        blocks: list[str] = []
        if summary:
            blocks.append(f"[Session Summary]\n{summary}")

        if recent_turns:
            lines: list[str] = []
            for turn in recent_turns[-(self.session_history_turns * 2):]:
                role = turn.get("role", "user")
                role_name = "User" if role == "user" else "Assistant"
                content = turn.get("content", "").strip()
                if content:
                    lines.append(f"{role_name}: {content}")
            if lines:
                blocks.append("[Recent Conversation]\n" + "\n".join(lines))

        if not blocks:
            return user_text

        return "\n\n".join(blocks + [f"[Current User Question]\n{user_text}"])

    def _update_memory(self, session: dict[str, Any], user_text: str, answer: str) -> None:
        recent_turns: list[dict[str, str]] = session.get("recent_turns", [])
        recent_turns.append({"role": "user", "content": user_text})
        recent_turns.append({"role": "assistant", "content": answer})

        max_recent = max(2, self.session_history_turns * 2)
        if len(recent_turns) > max_recent:
            overflow = recent_turns[: len(recent_turns) - max_recent]
            recent_turns = recent_turns[-max_recent:]

            overflow_lines: list[str] = []
            for turn in overflow:
                role = "User" if turn.get("role") == "user" else "Assistant"
                txt = str(turn.get("content", "")).strip().replace("\n", " ")
                if txt:
                    overflow_lines.append(f"{role}: {txt[:200]}")

            if overflow_lines:
                new_fragment = " | ".join(overflow_lines)
                prev_summary = str(session.get("summary", "")).strip()
                merged = f"{prev_summary} {new_fragment}".strip() if prev_summary else new_fragment
                if len(merged) > self.max_summary_chars:
                    merged = merged[-self.max_summary_chars:]
                session["summary"] = merged

        session["recent_turns"] = recent_turns

    def _append_session_log(
        self,
        session_id: str,
        session: dict[str, Any],
        user_text: str,
        answer: str,
        route: str,
        context_meta: dict[str, Any],
    ) -> None:
        sid = self._safe_session_id(session_id)
        log_file_name = str(session.get("log_file_name") or self._new_log_file_name(sid))
        path = self.sessions_dir / log_file_name
        ts = datetime.now(self.session_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

        lines = [
            f"## {ts}",
            "### User",
            user_text.strip() or "(empty)",
            "",
            "### Assistant",
            answer.strip() or "(empty)",
            "",
            "### Meta",
            f"- route: {route}",
            f"- pages_used: {context_meta.get('pages_used', 0)}",
            f"- iterations_used: {context_meta.get('iterations_used', 0)}",
            "",
        ]
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def answer_with_trace(
        self,
        query: str,
        session_id: str,
        pdf_path: str | None = None,
        on_trace: TraceCallback | None = None,
        trace_on: bool | None = None,
    ) -> dict[str, Any]:
        trace_lines: list[str] = []
        raw_events: list[dict[str, Any]] = []

        session = self._get_or_create_session(session_id)
        session["session_id_safe"] = self._safe_session_id(session_id)
        trace_enabled = self.get_trace_enabled(session_id) if trace_on is None else bool(trace_on)

        def _emit_local(event: dict[str, Any]) -> None:
            raw_events.append(event)
            line = self._format_event(event)
            if line is None:
                return
            trace_lines.append(line)
            if trace_enabled and on_trace is not None:
                on_trace(line, event)

        refined = self._refine_query(query)
        normalized_ko = str(refined.get("normalized_ko", query)).strip() or query
        english_query = str(refined.get("english", "")).strip()
        _emit_local(
            {
                "event": "query_refined",
                "normalized_ko": normalized_ko,
                "english": english_query,
            }
        )

        contextual_query = self._build_contextual_query(normalized_ko, session)
        if english_query:
            contextual_query = f"{contextual_query}\n\n[English Query]\n{english_query}"

        effective_pdf_path = self._resolve_effective_pdf_path(
            session=session,
            user_query=query,
            explicit_pdf_path=pdf_path,
        )
        used_briefing_cache = self._apply_briefing_cache_to_orchestrator(session, effective_pdf_path)
        if effective_pdf_path and trace_enabled:
            _emit_local(
                {
                    "event": "active_pdf_selected",
                    "pdf_path": effective_pdf_path,
                }
            )
        if used_briefing_cache:
            _emit_local({"event": "context_selected", "iteration": 0, "section_names": ["briefing_cache"], "chunk_ids": []})

        retrieval_query = normalized_ko
        if english_query:
            retrieval_query = f"{normalized_ko}\n{english_query}"

        result = self.orchestrator.answer(
            query=contextual_query,
            pdf_path=effective_pdf_path,
            intent_query=retrieval_query,
            on_event=_emit_local,
        )
        self._update_active_pdf_from_result(session, result)
        answer = result.get("answer", "")
        route = result.get("route", "qa")
        context_meta = result.get("context_meta", {})

        if route in {"qa", "summary", "method", "result", "limitation", "background"} and self._is_low_confidence_answer(
            answer
        ):
            target_pdf = str(result.get("pdf_path", "")).strip() or (effective_pdf_path or "")
            if not target_pdf:
                extracted = self._extract_pdf_path_from_query(query)
                target_pdf = extracted or ""
            improved, section_ids = self._refresh_answer_with_section_readback(
                query_ko=normalized_ko,
                query_en=english_query,
                target_pdf=target_pdf,
            )
            if improved:
                answer = improved
                _emit_local(
                    {
                        "event": "readback_refined",
                        "pdf_path": target_pdf,
                        "section_ids": section_ids,
                    }
                )

        self._update_memory(session, query, answer)
        self._append_session_log(session_id, session, query, answer, route, context_meta)
        self._save_session_state(self._safe_session_id(session_id), session)

        return {
            "answer": answer,
            "route": route,
            "context_meta": context_meta,
            "trace": trace_lines,
            "raw_events": raw_events,
            "trace_enabled": trace_enabled,
        }

    def generate_daily_briefing(self, session_id: str, max_papers: int | None = None) -> dict[str, Any]:
        """Create 07:30 daily summaries using SummaryPipeline and persist per-paper briefing cache."""
        session = self._get_or_create_session(session_id)
        session["session_id_safe"] = self._safe_session_id(session_id)

        pdf_paths = self._list_pdf_paths()
        if max_papers is not None:
            max_n = max(1, int(max_papers))
            pdf_paths = pdf_paths[:max_n]
        if not pdf_paths:
            msg = f"[Daily 07:30 Briefing]\nPDF 파일이 없습니다. (dir: {self.orchestrator.config.pdf_dir})"
            self._update_memory(session, "[SYSTEM] daily briefing", msg)
            self._save_session_state(self._safe_session_id(session_id), session)
            return {"ok": True, "message": msg, "count": 0}

        timestamp = datetime.now(self.session_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        lines = [f"[Daily 07:30 Briefing] {timestamp}", f"총 {len(pdf_paths)}개 논문 브리핑"]
        total_pages_used = 0
        cache_hits = 0
        briefing_cache = session.setdefault("briefing_cache", {})
        if not isinstance(briefing_cache, dict):
            briefing_cache = {}
            session["briefing_cache"] = briefing_cache

        for path in pdf_paths:
            try:
                index_doc, index_cache_hit = self.orchestrator._build_or_load_index(str(path))
                if index_cache_hit:
                    cache_hits += 1
                title = str(index_doc.get("title", "")).strip() or path.stem
                pages_used = int(index_doc.get("pages_used") or 0)
                total_pages_used += pages_used

                question = (
                    f"{path.name} 논문을 아침 브리핑용으로 요약해줘. "
                    "핵심 기여, 방법, 결과 중심으로 간결하게."
                )
                summary_text, used_sections, used_chunk_ids = self.orchestrator.summary_pipeline.build_hierarchical_summary(
                    question=question,
                    paper_index=index_doc,
                )
                lines.append(f"\n## {title} ({path.name})")
                lines.append(summary_text or "(요약 없음)")
                lines.append(
                    f"- meta: cache={'hit' if index_cache_hit else 'miss'}, "
                    f"sections={len(used_sections)}"
                )

                key = self._briefing_cache_key(path)
                briefing_cache[key] = {
                    "summary": str(summary_text or "").strip(),
                    "used_sections": used_sections,
                    "used_chunk_ids": used_chunk_ids,
                    "title": title,
                }
                session_state = session.setdefault("state", {})
                if isinstance(session_state, dict):
                    session_state["active_pdf"] = key
            except Exception as e:
                lines.append(f"\n## {path.name}\n요약 실패: {type(e).__name__}: {e}")

        briefing = "\n".join(lines).strip()

        # Store briefing in the same chat session context for follow-up QA.
        self._update_memory(session, "[SYSTEM] daily briefing", briefing)
        self._save_session_state(self._safe_session_id(session_id), session)
        return {
            "ok": True,
            "message": briefing,
            "count": len(pdf_paths),
            "pages_used": total_pages_used,
            "cache_hits": cache_hits,
        }
