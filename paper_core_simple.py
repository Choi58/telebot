from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

MAX_PAGES = 20  # None 이면 전체 페이지
OUTPUT_DIR = Path("samples")


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\t+", " ", text)
    return text


def clean_paragraph(text: str) -> str:
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)  # knowl-\nedge -> knowledge
    text = re.sub(r"\n+", " ", text)  # paragraph 내부 개행은 공백으로
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()


def process_pdf(pdf_path: Path) -> str:
    print(f"[PICK] {pdf_path.name}")

    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    limit = total_pages if MAX_PAGES is None else min(MAX_PAGES, total_pages)
    print(f"[READ] total_pages={total_pages}, using={limit}")

    page_texts = [normalize_text(reader.pages[i].extract_text() or "") for i in range(limit)]
    full_text = "\n\n".join(page_texts)
    lines = [ln.rstrip() for ln in full_text.splitlines()]

    numbered_header_re = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\.)?\s+(.+?)\s*$")
    major_header_re = re.compile(
        r"^\s*(ABSTRACT|INTRODUCTION|METHODS?|EXPERIMENTS?|RESULTS?|CONCLUSION|CONCLUSIONS|REFERENCES|KEYWORDS|CCS CONCEPTS)\s*$",
        flags=re.IGNORECASE,
    )

    headers_rule: list[dict] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue

        m = numbered_header_re.match(line)
        if m:
            sec_id = m.group(1)
            title = m.group(2).strip()
            inline_body = ""
            glued = re.match(r"^(.+?\.)(\S.*)$", title)
            if glued:
                title = glued.group(1).strip()
                inline_body = glued.group(2).strip()
            headers_rule.append(
                {
                    "line": i,
                    "type": "section",
                    "id": sec_id,
                    "title": title,
                    "inline_body": inline_body,
                    "raw": line,
                }
            )
            continue

        m2 = major_header_re.match(line)
        if m2:
            key = m2.group(1).upper()
            kind = "meta" if key in ("ABSTRACT", "KEYWORDS", "CCS CONCEPTS") else "section"
            headers_rule.append(
                {"line": i, "type": kind, "id": None, "title": key, "inline_body": "", "raw": line}
            )

    headers_merged = list(headers_rule)
    print(f"[HEADERS] rule={len(headers_rule)}, merged={len(headers_merged)}")

    first_page_lines = [ln.strip() for ln in page_texts[0].splitlines() if ln.strip()] if page_texts else []

    title = ""
    for ln in first_page_lines[:20]:
        if len(ln) < 8 or "@" in ln:
            continue
        if major_header_re.match(ln) or numbered_header_re.match(ln):
            continue
        title = ln
        break

    author_hint_re = re.compile(
        r"(@|University|Institute|Department|Laboratory|School|College|Contributed equally)",
        flags=re.IGNORECASE,
    )
    author_name_re = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}(?:[∗*†])?$")

    authors: list[str] = []
    for ln in first_page_lines[:120]:
        if major_header_re.match(ln):
            break
        if author_hint_re.search(ln) or author_name_re.match(ln):
            authors.append(ln)

    abstract_parts: list[str] = []
    meta: list[str] = []
    sections: list[dict] = []

    offsets: list[int] = []
    cur = 0
    joined = "\n".join(lines)
    for i, ln in enumerate(lines):
        offsets.append(cur)
        cur += len(ln)
        if i != len(lines) - 1:
            cur += 1

    def slice_text(line_start: int, line_end_exclusive: int) -> str:
        if not lines or line_start < 0 or line_start >= len(lines) or line_end_exclusive <= line_start:
            return ""
        start = offsets[line_start]
        end = offsets[line_end_exclusive] if line_end_exclusive < len(lines) else len(joined)
        return joined[start:end].strip()

    headers_sorted = sorted(headers_merged, key=lambda x: x["line"])

    for idx, h in enumerate(headers_sorted):
        next_line = headers_sorted[idx + 1]["line"] if idx + 1 < len(headers_sorted) else len(lines)
        body = slice_text(h["line"] + 1, next_line)
        inline_body = str(h.get("inline_body", "")).strip()
        if inline_body:
            body = f"{inline_body}\n{body}".strip() if body else inline_body

        if h["type"] == "meta":
            if h["title"].upper() == "ABSTRACT":
                if body:
                    abstract_parts.append(body)
            else:
                meta.append(h["title"])
                if body:
                    meta.append(body)
            continue

        if h["id"]:
            name = f"{h['id']} {h['title']}".strip()
            section_id = h["id"]
        else:
            name = h["title"]
            section_id = None

        section = {"id": section_id, "name": name, "paragraphs": [], "children": []}
        if body:
            raw_paragraphs = re.split(r"\n\s*\n+", body) if "\n\n" in body else [body]
            cleaned_paragraphs = [clean_paragraph(p) for p in raw_paragraphs if p.strip()]
            section["paragraphs"].extend([p for p in cleaned_paragraphs if p])
        sections.append(section)

    root_sections: list[dict] = []
    section_map: dict[str, dict] = {}
    for s in sections:
        sid = s["id"]
        if not sid:
            root_sections.append(s)
            continue
        section_map[sid] = s

    for s in sections:
        sid = s["id"]
        if not sid:
            continue
        parent_id = sid.rsplit(".", 1)[0] if "." in sid else None
        if parent_id and parent_id in section_map:
            section_map[parent_id]["children"].append(s)
        else:
            root_sections.append(s)

    authors = list(dict.fromkeys(authors))
    meta = list(dict.fromkeys(meta))

    out: list[str] = [
        "# Paper Structure Sample (Anchor-based)",
        f"- File: {pdf_path.name}",
        f"- Pages Used: {limit}",
        f"- Headers Found (Merged): {len(headers_sorted)}",
        f"- Headers Found (Rule): {len(headers_rule)}",
        f"- Headers Found (LLM): 0",
        "",
        "## Title",
        title or "(not found)",
        "",
        "## Authors",
    ]

    out += [f"- {a}" for a in authors] if authors else ["- (none)"]

    out += ["", "## Abstract"]
    out.append("\n\n".join(abstract_parts).strip() if abstract_parts else "(none)")

    out += ["", "## Meta"]
    out += [f"- {m}" for m in meta] if meta else ["- (none)"]

    out += ["", "## Sections"]

    def render_section(node: dict, depth: int = 0) -> None:
        out.append(f"{'#' * min(6, 3 + depth)} {node['name']}")
        if node["paragraphs"]:
            for p in node["paragraphs"]:
                out.append(f"- {p}")
        else:
            out.append("- (no paragraphs)")
        out.append("")
        for c in node["children"]:
            render_section(c, depth + 1)

    if root_sections:
        for s in root_sections:
            render_section(s, 0)
    else:
        out.append("- (none)")

    return "\n".join(out).strip() + "\n"


if __name__ == "__main__":
    print("[MAIN] start")

    pdfs = sorted(Path("Papers").glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError("No PDF found in Papers")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for pdf_path in pdfs:
        markdown = process_pdf(pdf_path)
        out_path = OUTPUT_DIR / f"{pdf_path.stem}.md"
        out_path.write_text(markdown, encoding="utf-8")
        print(f"[SAVE] {out_path}")

    print(f"[MAIN] done (files={len(pdfs)})")
