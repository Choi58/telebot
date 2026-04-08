from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pypdf import PdfReader


def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fallback_split_paragraphs(text: str, min_len: int = 80) -> list[str]:
    chunks = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    merged: list[str] = []
    for p in chunks:
        if not merged:
            merged.append(p)
            continue
        if len(p) < min_len:
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)
    return merged


def parse_json_array(raw: str) -> list[str] | None:
    def _coerce_list(data: object) -> list[str] | None:
        if isinstance(data, list):
            if all(isinstance(x, str) for x in data):
                return [x.strip() for x in data if x.strip()]

            out: list[str] = []
            for item in data:
                if isinstance(item, dict):
                    for key in ("paragraph", "text", "content"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            out.append(value.strip())
                            break
                elif isinstance(item, str) and item.strip():
                    out.append(item.strip())
            return out if out else None

        if isinstance(data, dict):
            for key in ("paragraphs", "items", "result"):
                value = data.get(key)
                coerced = _coerce_list(value)
                if coerced:
                    return coerced
        return None

    def _repair_common_json_issues(text: str) -> str:
        # Fix invalid backslash sequences such as "\ABSTRACT" -> "\\ABSTRACT".
        return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", text)

    def _unescape_relaxed(s: str) -> str:
        s = s.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\r", "\r")
        s = s.replace(r'\\"', '"').replace(r"\\\\", "\\")
        # Remove unknown backslash escapes that often appear in broken JSON outputs.
        s = re.sub(r"\\(?![ntr\"\\/])", "", s)
        return s.strip()

    def _extract_paragraph_fields(text: str) -> list[str] | None:
        # Recovery for malformed JSON object array such as [{"paragraph": "..."}, ...]
        pattern = r'"(?:paragraph|text|content)"\s*:\s*"((?:\\.|[^"\\])*)"'
        matches = re.findall(pattern, text, flags=re.DOTALL)
        if not matches:
            return None
        values = [_unescape_relaxed(m) for m in matches]
        values = [v for v in values if v]
        return values if values else None

    content = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if fence:
        content = fence.group(1).strip()

    try:
        data = json.loads(content)
        parsed = _coerce_list(data)
        if parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    left = content.find("[")
    right = content.rfind("]")
    if left == -1 or right == -1 or right <= left:
        return None

    candidate = content[left : right + 1]

    try:
        data = json.loads(candidate)
        parsed = _coerce_list(data)
        if parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    try:
        repaired = _repair_common_json_issues(candidate)
        data = json.loads(repaired)
        parsed = _coerce_list(data)
        if parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    extracted = _extract_paragraph_fields(candidate)
    if extracted:
        return extracted

    return None


class PDFParagraphSplitter:
    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        api_base: str = "http://host.docker.internal:1234/v1",
        api_key: str = "lm-studio",
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> None:
        print("[INIT] PDFParagraphSplitter 초기화 시작")
        llm = ChatOpenAI(
            model=model,
            base_url=api_base,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        self.split_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "Split one PDF page into logical paragraphs. "
                        "Return JSON array only. Keep original order and do not invent text.",
                    ),
                    (
                        "human",
                        "[페이지 텍스트]\n{page_text}\n\n"
                        "출력 규칙:\n"
                        "- JSON 배열만 출력\n"
                        "- 줄바꿈으로 끊긴 같은 문단은 합치기\n"
                        "- 머리말/쪽번호처럼 짧은 잡음은 인접 문단에 합치기",
                    ),
                ]
            )
            | llm
            | StrOutputParser()
        )
        print(f"[INIT][OK] split_chain 준비 완료 (model={model})")

        self.relabel_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You classify paper blocks into one of labels: "
                        "TITLE, AUTHOR, META, SECTION_HEADER, BODY. "
                        "Return JSON array only.",
                    ),
                    (
                        "human",
                        "아래는 1차 규칙 분류에서 애매한(UNCERTAIN) 블록들입니다.\n"
                        "각 항목에 대해 idx를 유지한 채 label을 재분류하세요.\n"
                        "필요하면 section_name을 넣으세요.\n\n"
                        "출력 형식(JSON 배열):\n"
                        '[{{"idx": 3, "label": "SECTION_HEADER", "section_name": "Introduction"}}]\n\n'
                        "[입력]\n{items_json}",
                    ),
                ]
            )
            | llm
            | StrOutputParser()
        )
        print("[INIT][OK] relabel_chain 준비 완료")

        self.author_meta_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You detect whether each block is AUTHOR, META, or KEEP. "
                        "Return JSON array only.",
                    ),
                    (
                        "human",
                        "다음 블록들에서 AUTHOR 또는 META로 바꿔야 할 항목만 골라주세요.\n"
                        "해당 없으면 빈 배열 [] 반환.\n\n"
                        "출력 형식(JSON 배열):\n"
                        '[{{"idx": 2, "label": "AUTHOR"}}, {{"idx": 5, "label": "META"}}]\n\n'
                        "[입력]\n{items_json}",
                    ),
                ]
            )
            | llm
            | StrOutputParser()
        )
        print("[INIT][OK] author_meta_chain 준비 완료")

    def load_page_text(self, pdf_path: str, page_number: int = 1) -> str:
        print(f"[LOAD] PDF 로드 시작: {pdf_path}")
        reader = PdfReader(pdf_path)
        print(f"[LOAD][OK] PDF 로드 완료 (total_pages={len(reader.pages)})")
        page_index = page_number - 1
        if page_index < 0 or page_index >= len(reader.pages):
            print(
                f"[LOAD][FAIL] 잘못된 페이지 번호: page_number={page_number}, total_pages={len(reader.pages)}"
            )
            raise ValueError(f"Invalid page_number={page_number}. total_pages={len(reader.pages)}")
        text = clean_text(reader.pages[page_index].extract_text() or "")
        print(f"[LOAD][OK] 페이지 텍스트 추출 완료 (page={page_number}, chars={len(text)})")
        return text

    def split_page_paragraphs(self, page_text: str) -> list[str]:
        if not page_text.strip():
            print("[SPLIT] 입력 페이지 텍스트가 비어 있어 빈 결과 반환")
            return []

        try:
            print(f"[SPLIT] LLM 문단 분할 호출 시작 (chars={len(page_text[:12000])})")
            raw = self.split_chain.invoke({"page_text": page_text[:12000]}).strip()
            print(f"[SPLIT][OK] LLM 호출 성공 (response_chars={len(raw)})")
            parsed = parse_json_array(raw)
            if parsed:
                normalized = [clean_text(p) for p in parsed if clean_text(p)]
                if normalized:
                    print(f"[SPLIT][OK] JSON 파싱 성공 (paragraphs={len(normalized)})")
                    return normalized
            print("[SPLIT][WARN] JSON 파싱 결과가 비어 fallback 사용")
        except Exception:
            print("[SPLIT][FAIL] LLM 호출/파싱 실패, fallback 사용")

        fallback = fallback_split_paragraphs(page_text)
        print(f"[SPLIT][OK] fallback 분할 완료 (paragraphs={len(fallback)})")
        return fallback

    def extract_document_blocks(self, pdf_path: str, *, max_pages: int | None = None) -> list[dict[str, Any]]:
        print(f"[BLOCK] 문서 블록 추출 시작: {pdf_path}")
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        limit = total_pages if max_pages is None else min(max_pages, total_pages)

        blocks: list[dict[str, Any]] = []
        idx = 0
        section_header_re = re.compile(
            r"^(?:\d+(?:\.\d+)*\.?$|"
            r"\d+(?:\.\d+)*\s+\S.{0,120}|"
            r"(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+[A-Z].+|"
            r"(?:INTRODUCTION|METHOD|METHODS|EXPERIMENT|EXPERIMENTS|RESULT|RESULTS|"
            r"CONCLUSION|CONCLUSIONS|REFERENCES))$",
            flags=re.IGNORECASE,
        )

        def _append_block(page_number: int, text: str, hint: str) -> None:
            nonlocal idx
            t = clean_text(text)
            if not t:
                return
            idx += 1
            blocks.append({"idx": idx, "page": page_number, "text": t, "hint": hint})

        for page_number in range(1, limit + 1):
            page_text = clean_text(reader.pages[page_number - 1].extract_text() or "")
            paragraphs = self.split_page_paragraphs(page_text)
            for p in paragraphs:
                lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
                if not lines:
                    continue
                current_body: list[str] = []

                for line in lines:
                    # Case: "2.1.1 News-Driven.News-driven architecture ..."
                    inline = re.match(
                        r"^(\d+(?:\.\d+)+(?:\s+[A-Za-z][A-Za-z0-9\- ]{0,80})?)\.\s*(.+)$",
                        line,
                    )
                    if inline:
                        if current_body:
                            _append_block(page_number, "\n".join(current_body), "BODY")
                            current_body = []
                        _append_block(page_number, inline.group(1), "SECTION_HEADER")
                        _append_block(page_number, inline.group(2), "BODY")
                        continue

                    if section_header_re.match(line):
                        if current_body:
                            _append_block(page_number, "\n".join(current_body), "BODY")
                            current_body = []
                        _append_block(page_number, line, "SECTION_HEADER")
                        continue

                    current_body.append(line)

                if current_body:
                    _append_block(page_number, "\n".join(current_body), "AUTO")

        print(f"[BLOCK][OK] 블록 추출 완료 (pages={limit}, blocks={len(blocks)})")
        return blocks

    def classify_blocks_rule(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        print(f"[RULE] 1차 규칙 분류 시작 (blocks={len(blocks)})")
        results: list[dict[str, Any]] = []

        section_header_re = re.compile(
            r"^(?:\d+(?:\.\d+)*\.?$|"  # e.g., 2 / 2.1 / 2.1.1
            r"\d+(?:\.\d+)*\s+\S.{0,120}|"  # e.g., 2.1.1 Subsection title
            r"(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+[A-Z].+|"
            r"(?:INTRODUCTION|METHOD|METHODS|EXPERIMENT|EXPERIMENTS|RESULT|RESULTS|"
            r"CONCLUSION|CONCLUSIONS|REFERENCES))$",
            flags=re.IGNORECASE,
        )
        meta_kw_re = re.compile(
            r"\b(ABSTRACT|KEYWORDS?|CCS CONCEPTS|ARXIV|DOI|RECEIVED|ACCEPTED|ACM)\b",
            flags=re.IGNORECASE,
        )
        author_kw_re = re.compile(
            r"(@|University|Institute|Department|Laboratory|School|College|Contributed equally)",
            flags=re.IGNORECASE,
        )

        for b in blocks:
            text = b["text"].strip()
            page = int(b["page"])
            hint = str(b.get("hint", "AUTO"))
            first_line = text.splitlines()[0].strip() if text.splitlines() else text
            score = {"TITLE": 0, "AUTHOR": 0, "META": 0, "SECTION_HEADER": 0, "BODY": 0}
            reasons: list[str] = []
            line_count = len([ln for ln in text.splitlines() if ln.strip()])

            if hint == "SECTION_HEADER":
                score["SECTION_HEADER"] += 7
                score["META"] -= 2
                reasons.append("hint_section")
            elif hint == "BODY":
                score["BODY"] += 3
                reasons.append("hint_body")

            # TITLE
            if page == 1 and b["idx"] <= 3:
                score["TITLE"] += 2
            if 8 <= len(first_line) <= 180 and not first_line.endswith("."):
                score["TITLE"] += 1
            if line_count <= 3:
                score["TITLE"] += 1
            if meta_kw_re.search(first_line):
                score["TITLE"] -= 2
            if "@" in text:
                score["TITLE"] -= 3

            # AUTHOR
            if author_kw_re.search(text):
                score["AUTHOR"] += 3
                reasons.append("author_keyword")
            if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", text):
                score["AUTHOR"] += 1
            if meta_kw_re.search(text):
                score["AUTHOR"] -= 4
            if section_header_re.match(first_line):
                score["AUTHOR"] -= 4

            # META
            if meta_kw_re.search(text):
                score["META"] += 4
                reasons.append("meta_keyword")
            if first_line.upper().startswith(("ABSTRACT", "KEYWORDS", "CCS CONCEPTS")):
                score["META"] += 3
            if len(first_line) <= 40 and re.fullmatch(r"\d+", first_line):
                score["META"] += 2
            if section_header_re.match(first_line):
                score["META"] -= 5

            # SECTION HEADER
            if len(text) <= 160 and section_header_re.match(first_line):
                score["SECTION_HEADER"] += 5
                reasons.append("section_header_pattern")
            if re.match(r"^\d+(?:\.\d+){1,}\.?$", first_line):
                score["SECTION_HEADER"] += 2
                reasons.append("numeric_subsection")
            if len(text) <= 80 and text.isupper():
                score["SECTION_HEADER"] += 2

            # BODY
            if len(text) >= 180:
                score["BODY"] += 3
            if re.search(r"[.!?]\s+[A-Z가-힣]", text):
                score["BODY"] += 1

            best_label = max(score, key=score.get)
            best_score = score[best_label]
            sorted_scores = sorted(score.values(), reverse=True)
            margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
            label = best_label if best_score >= 3 and margin >= 1 else "UNCERTAIN"

            results.append(
                {
                    **b,
                    "label": label,
                    "rule_label": best_label,
                    "score": best_score,
                    "reason": ",".join(reasons) if reasons else "heuristic",
                }
            )

        n_uncertain = sum(1 for x in results if x["label"] == "UNCERTAIN")
        print(f"[RULE][OK] 1차 분류 완료 (uncertain={n_uncertain})")
        return results

    def normalize_labels(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        print(f"[NORM] 라벨 정규화 시작 (items={len(items)})")
        section_header_re = re.compile(
            r"^(?:\d+(?:\.\d+)*\.?$|"
            r"\d+(?:\.\d+)*\s+\S.{0,120}|"
            r"(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+[A-Z].+|"
            r"(?:INTRODUCTION|METHOD|METHODS|EXPERIMENT|EXPERIMENTS|RESULT|RESULTS|"
            r"CONCLUSION|CONCLUSIONS|REFERENCES))$",
            flags=re.IGNORECASE,
        )
        meta_kw_re = re.compile(
            r"\b(ABSTRACT|KEYWORDS?|CCS CONCEPTS|ARXIV|DOI|RECEIVED|ACCEPTED|ACM)\b",
            flags=re.IGNORECASE,
        )

        out: list[dict[str, Any]] = []
        for item in items:
            text = item["text"].strip()
            first_line = text.splitlines()[0].strip() if text.splitlines() else text
            label = str(item["label"])

            if section_header_re.match(first_line):
                label = "SECTION_HEADER"
            elif first_line.upper().startswith(("ABSTRACT", "KEYWORDS", "CCS CONCEPTS")):
                label = "META"
            elif meta_kw_re.search(first_line) and label == "AUTHOR":
                label = "META"
            elif int(item["idx"]) == 1 and label in ("AUTHOR", "BODY", "UNCERTAIN"):
                if len(first_line) >= 8 and "@" not in first_line:
                    label = "TITLE"

            out.append({**item, "label": label})

        print("[NORM][OK] 라벨 정규화 완료")
        return out

    def refine_uncertain_with_llm(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        uncertain = [x for x in items if x["label"] == "UNCERTAIN"]
        if not uncertain:
            print("[REFINE] UNCERTAIN 없음, 2차 보정 생략")
            return items

        print(f"[REFINE] 2차 LLM 보정 시작 (targets={len(uncertain)})")
        payload = [
            {"idx": x["idx"], "page": x["page"], "text": x["text"][:1500], "rule_label": x["rule_label"]}
            for x in uncertain
        ]
        raw = self.relabel_chain.invoke({"items_json": json.dumps(payload, ensure_ascii=False)}).strip()
        parsed = self._parse_relabel_output(raw)
        if not parsed:
            print("[REFINE][WARN] LLM 보정 파싱 실패, 1차 분류 결과 유지")
            return items

        updates = {int(x["idx"]): x for x in parsed if isinstance(x.get("idx"), int)}
        allowed = {"TITLE", "AUTHOR", "META", "SECTION_HEADER", "BODY"}
        out: list[dict[str, Any]] = []
        for item in items:
            u = updates.get(int(item["idx"]))
            if u and str(u.get("label")) in allowed:
                item = {**item, "label": str(u["label"])}
                if isinstance(u.get("section_name"), str) and u["section_name"].strip():
                    item["section_name"] = u["section_name"].strip()
            out.append(item)

        print("[REFINE][OK] 2차 LLM 보정 완료")
        return out

    def refine_author_meta_with_llm(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        targets = [
            x
            for x in items
            if x["label"] not in ("AUTHOR", "META")
            and (int(x["page"]) == 1 or int(x["idx"]) <= 10)
        ]
        if not targets:
            print("[AM-REFINE] 대상 없음, 보정 생략")
            return items

        print(f"[AM-REFINE] AUTHOR/META 보정 시작 (targets={len(targets)})")
        payload = [{"idx": x["idx"], "page": x["page"], "label": x["label"], "text": x["text"][:1500]} for x in targets]
        raw = self.author_meta_chain.invoke({"items_json": json.dumps(payload, ensure_ascii=False)}).strip()
        parsed = self._parse_relabel_output(raw)
        if not parsed:
            print("[AM-REFINE][WARN] 파싱 실패, 기존 라벨 유지")
            return items

        updates = {
            int(x["idx"]): str(x["label"])
            for x in parsed
            if isinstance(x.get("idx"), int) and str(x.get("label")) in ("AUTHOR", "META")
        }
        if not updates:
            print("[AM-REFINE] 업데이트 없음")
            return items

        out: list[dict[str, Any]] = []
        for item in items:
            new_label = updates.get(int(item["idx"]))
            out.append({**item, "label": new_label} if new_label else item)

        print(f"[AM-REFINE][OK] 라벨 업데이트 완료 (updated={len(updates)})")
        return out

    def _parse_relabel_output(self, raw: str) -> list[dict[str, Any]] | None:
        content = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
        if fence:
            content = fence.group(1).strip()

        def _to_records(data: object) -> list[dict[str, Any]] | None:
            if isinstance(data, list):
                recs = [x for x in data if isinstance(x, dict)]
                return recs if recs else None
            if isinstance(data, dict):
                for key in ("items", "results", "data"):
                    value = data.get(key)
                    if isinstance(value, list):
                        recs = [x for x in value if isinstance(x, dict)]
                        return recs if recs else None
            return None

        try:
            return _to_records(json.loads(content))
        except json.JSONDecodeError:
            pass

        left = content.find("[")
        right = content.rfind("]")
        if left == -1 or right == -1 or right <= left:
            return None
        candidate = content[left : right + 1]
        try:
            return _to_records(json.loads(candidate))
        except json.JSONDecodeError:
            return None

    def build_structure(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        print(f"[STRUCT] 문서 구조화 시작 (items={len(items)})")
        title = ""
        authors: list[str] = []
        meta: list[str] = []
        sections: list[dict[str, Any]] = []
        section_index: dict[str, dict[str, Any]] = {}
        current_section: dict[str, Any] | None = None

        def _parse_numbered_header(line: str) -> tuple[str, str] | None:
            m = re.match(r"^\s*(\d+(?:\.\d+)*)(?:\.)?\s*(.*)\s*$", line)
            if not m:
                return None
            return m.group(1), m.group(2).strip()

        def _ensure_numbered_path(sec_id: str, tail_title: str) -> dict[str, Any]:
            parts = sec_id.split(".")
            parent: dict[str, Any] | None = None
            for i in range(len(parts)):
                cur_id = ".".join(parts[: i + 1])
                node = section_index.get(cur_id)
                if node is None:
                    default_name = cur_id
                    if i == len(parts) - 1 and tail_title:
                        default_name = f"{cur_id} {tail_title}".strip()
                    node = {"id": cur_id, "name": default_name, "paragraphs": [], "children": []}
                    section_index[cur_id] = node
                    if parent is None:
                        sections.append(node)
                    else:
                        parent["children"].append(node)
                elif i == len(parts) - 1 and tail_title and node["name"] == cur_id:
                    node["name"] = f"{cur_id} {tail_title}".strip()
                parent = node
            return parent if parent is not None else {"id": sec_id, "name": sec_id, "paragraphs": [], "children": []}

        for item in items:
            label = item["label"]
            text = item["text"].strip()
            if not text:
                continue

            if label == "TITLE" and not title:
                title = text.splitlines()[0].strip()
                continue

            if label == "AUTHOR":
                # Keep author-related lines only; stop at meta/section markers.
                for ln in [x.strip() for x in text.splitlines() if x.strip()]:
                    if re.match(r"^(ABSTRACT|KEYWORDS?|CCS CONCEPTS|INTRODUCTION|\d+(?:\.\d+)*\.?)\b", ln, flags=re.IGNORECASE):
                        break
                    if (
                        "@" in ln
                        or re.search(
                            r"\b(University|Institute|Department|Laboratory|School|College|Contributed equally)\b",
                            ln,
                            flags=re.IGNORECASE,
                        )
                        or re.search(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}(?:[∗*†])?$", ln)
                    ):
                        authors.append(ln)
                continue

            if label == "META":
                for ln in [x.strip() for x in text.splitlines() if x.strip()]:
                    if re.match(r"^\d+(?:\.\d+)*\.?\s+\S", ln):
                        break
                    if re.search(
                        r"\b(ABSTRACT|KEYWORDS?|CCS CONCEPTS|ARXIV|DOI|RECEIVED|ACCEPTED|ACM)\b",
                        ln,
                        flags=re.IGNORECASE,
                    ):
                        meta.append(ln)
                continue

            if label == "SECTION_HEADER":
                section_name = item.get("section_name") or text.splitlines()[0].strip()
                numbered = _parse_numbered_header(section_name)
                if numbered:
                    sec_id, tail = numbered
                    current_section = _ensure_numbered_path(sec_id, tail)
                else:
                    if section_name.strip().lower() == "introduction" and "1" in section_index:
                        current_section = section_index["1"]
                        continue
                    un_id = f"U{item['idx']}"
                    current_section = {"id": un_id, "name": section_name, "paragraphs": [], "children": []}
                    sections.append(current_section)
                continue

            if current_section is None:
                current_section = {"id": "front", "name": "Front Matter", "paragraphs": [], "children": []}
                sections.append(current_section)
            current_section["paragraphs"].append(text)

        if not title and items:
            first = items[0]["text"].splitlines()[0].strip()
            if first and "@" not in first:
                title = first

        # Fallback extraction from front-matter (page 1) when authors/meta are missing.
        if not authors or not meta:
            front_blocks = [x["text"] for x in items if int(x["page"]) == 1 and int(x["idx"]) <= 6]
            front = "\n".join(front_blocks)
            lines = [ln.strip() for ln in front.splitlines() if ln.strip()]
            in_abstract = False
            for ln in lines:
                if re.match(r"^ABSTRACT\b", ln, flags=re.IGNORECASE):
                    in_abstract = True
                    if "ABSTRACT" not in meta:
                        meta.append("ABSTRACT")
                    continue
                if re.match(r"^(KEYWORDS?|CCS CONCEPTS)\b", ln, flags=re.IGNORECASE):
                    in_abstract = False
                    meta.append(ln)
                    continue
                if re.match(r"^\d+(?:\.\d+)*\.?\s+\S", ln):
                    in_abstract = False
                if in_abstract and len(ln) > 20:
                    meta.append(ln)
                if (
                    "@" in ln
                    or re.search(
                        r"\b(University|Institute|Department|Laboratory|School|College)\b",
                        ln,
                        flags=re.IGNORECASE,
                    )
                    or re.search(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}(?:[∗*†])?$", ln)
                ):
                    authors.append(ln)

        # de-dup while keeping order
        authors = list(dict.fromkeys(authors))
        meta = list(dict.fromkeys(meta))

        result = {"title": title, "authors": authors, "meta": meta, "sections": sections}
        print(
            f"[STRUCT][OK] 구조화 완료 (title={'Y' if bool(title) else 'N'}, "
            f"authors={len(authors)}, meta={len(meta)}, sections={len(sections)})"
        )
        return result


def render_markdown(pdf_path: str, structure: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = [
        "# Paper Structure Sample",
        f"- File: {Path(pdf_path).name}",
        f"- Blocks: {len(items)}",
        "",
    ]

    lines.append("## Title")
    lines.append(structure["title"] or "(not found)")
    lines.append("")

    lines.append("## Authors")
    if structure["authors"]:
        for a in structure["authors"]:
            lines.append(f"- {a}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Meta")
    if structure["meta"]:
        for m in structure["meta"]:
            lines.append(f"- {m}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Sections")
    if not structure["sections"]:
        lines.append("- (none)")
    def _render_section(node: dict[str, Any], depth: int = 0) -> None:
        heading = "#" * min(6, 3 + depth)
        lines.append(f"{heading} {node['name']}")
        if node["paragraphs"]:
            for p in node["paragraphs"]:
                lines.append(f"- {p}")
        else:
            lines.append("- (no paragraphs)")
        lines.append("")
        for child in node.get("children", []):
            _render_section(child, depth + 1)

    for s in structure["sections"]:
        _render_section(s, 0)

    return "\n".join(lines).strip() + "\n"


def pick_one_pdf(base_dir: str = "Papers") -> Path:
    print(f"[PICK] PDF 탐색 시작: {base_dir}")
    candidates = sorted(Path(base_dir).glob("*.pdf"))
    if not candidates:
        print(f"[PICK][FAIL] PDF 파일 없음: {base_dir}")
        raise FileNotFoundError(f"No PDF found in {base_dir}")
    print(f"[PICK][OK] PDF 선택: {candidates[0].name} (총 {len(candidates)}개 중 첫 파일)")
    return candidates[0]


if __name__ == "__main__":
    print("[MAIN] 실행 시작")
    pdf_file = pick_one_pdf("Papers")
    splitter = PDFParagraphSplitter(model="google/gemma-3-4b")
    blocks = splitter.extract_document_blocks(str(pdf_file), max_pages=2)
    classified = splitter.classify_blocks_rule(blocks)
    refined = splitter.refine_uncertain_with_llm(classified)
    refined = splitter.refine_author_meta_with_llm(refined)
    refined = splitter.normalize_labels(refined)
    structure = splitter.build_structure(refined)

    output = render_markdown(str(pdf_file), structure, refined)
    Path("sample.md").write_text(output, encoding="utf-8")
    print("[SAVE][OK] sample.md 저장 완료")
    print(f"[MAIN][OK] 완료: {pdf_file.name} (blocks={len(refined)})")
