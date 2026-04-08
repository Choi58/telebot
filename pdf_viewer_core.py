from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pypdf import PdfReader


@dataclass
class SectionNode:
    id: str | None
    name: str
    paragraphs: list[str] = field(default_factory=list)
    children: list["SectionNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "paragraphs": self.paragraphs,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ParsedPaper:
    file_path: str
    pages_used: int
    title: str
    authors: list[str]
    abstract: str
    meta: list[str]
    sections: list[SectionNode]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "pages_used": self.pages_used,
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "meta": self.meta,
            "sections": [s.to_dict() for s in self.sections],
        }


class PDFPaperParser:
    """Import-friendly PDF paper parser (rule-based, no LLM dependency)."""

    def __init__(self, max_pages: int | None = 20) -> None:
        self.max_pages = max_pages
        self.numbered_header_re = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\.)?\s+(.+?)\s*$")
        self.major_header_re = re.compile(
            r"^\s*(ABSTRACT|INTRODUCTION|METHODS?|EXPERIMENTS?|RESULTS?|CONCLUSION|CONCLUSIONS|REFERENCES|KEYWORDS|CCS CONCEPTS)\s*$",
            flags=re.IGNORECASE,
        )
        self.author_hint_re = re.compile(
            r"(@|University|Institute|Department|Laboratory|School|College|Contributed equally)",
            flags=re.IGNORECASE,
        )
        self.author_name_re = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}(?:[∗*†])?$")

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\u00a0", " ")
        text = re.sub(r"\t+", " ", text)
        return text

    @staticmethod
    def _clean_paragraph(text: str) -> str:
        text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
        text = re.sub(r"\n+", " ", text)
        text = re.sub(r"[ ]{2,}", " ", text)
        return text.strip()

    @staticmethod
    def _render_section_md(lines: list[str], node: SectionNode, depth: int = 0) -> None:
        lines.append(f"{'#' * min(6, 3 + depth)} {node.name}")
        if node.paragraphs:
            for p in node.paragraphs:
                lines.append(f"- {p}")
        else:
            lines.append("- (no paragraphs)")
        lines.append("")
        for c in node.children:
            PDFPaperParser._render_section_md(lines, c, depth + 1)

    def parse_pdf(self, pdf_path: str | Path, max_pages: int | None = None) -> ParsedPaper:
        pdf_path = Path(pdf_path)
        reader = PdfReader(str(pdf_path))
        total_pages = len(reader.pages)
        limit_pages = self.max_pages if max_pages is None else max_pages
        pages_used = total_pages if limit_pages is None else min(limit_pages, total_pages)

        page_texts = [self._normalize_text(reader.pages[i].extract_text() or "") for i in range(pages_used)]
        full_text = "\n\n".join(page_texts)
        lines = [ln.rstrip() for ln in full_text.splitlines()]

        headers: list[dict[str, Any]] = []
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue

            m = self.numbered_header_re.match(line)
            if m:
                sec_id = m.group(1)
                title = m.group(2).strip()
                inline_body = ""
                glued = re.match(r"^(.+?\.)(\S.*)$", title)
                if glued:
                    title = glued.group(1).strip()
                    inline_body = glued.group(2).strip()
                headers.append(
                    {
                        "line": i,
                        "type": "section",
                        "id": sec_id,
                        "title": title,
                        "inline_body": inline_body,
                    }
                )
                continue

            m2 = self.major_header_re.match(line)
            if m2:
                key = m2.group(1).upper()
                htype = "meta" if key in ("ABSTRACT", "KEYWORDS", "CCS CONCEPTS") else "section"
                headers.append({"line": i, "type": htype, "id": None, "title": key, "inline_body": ""})

        first_page_lines = [ln.strip() for ln in page_texts[0].splitlines() if ln.strip()] if page_texts else []

        title = ""
        for ln in first_page_lines[:20]:
            if len(ln) < 8 or "@" in ln:
                continue
            if self.major_header_re.match(ln) or self.numbered_header_re.match(ln):
                continue
            title = ln
            break

        authors: list[str] = []
        for ln in first_page_lines[:120]:
            if self.major_header_re.match(ln):
                break
            if self.author_hint_re.search(ln) or self.author_name_re.match(ln):
                authors.append(ln)

        abstract_parts: list[str] = []
        meta: list[str] = []
        flat_sections: list[SectionNode] = []

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

        headers_sorted = sorted(headers, key=lambda x: x["line"])

        for idx, h in enumerate(headers_sorted):
            next_line = headers_sorted[idx + 1]["line"] if idx + 1 < len(headers_sorted) else len(lines)
            body = slice_text(h["line"] + 1, next_line)
            inline_body = str(h.get("inline_body", "")).strip()
            if inline_body:
                body = f"{inline_body}\n{body}".strip() if body else inline_body

            if h["type"] == "meta":
                if h["title"] == "ABSTRACT":
                    if body:
                        abstract_parts.append(body)
                else:
                    meta.append(h["title"])
                    if body:
                        meta.append(body)
                continue

            if h["id"]:
                name = f"{h['id']} {h['title']}".strip()
                sec_id = str(h["id"])
            else:
                name = str(h["title"])
                sec_id = None

            section = SectionNode(id=sec_id, name=name)
            if body:
                raw_paragraphs = re.split(r"\n\s*\n+", body) if "\n\n" in body else [body]
                cleaned = [self._clean_paragraph(p) for p in raw_paragraphs if p.strip()]
                section.paragraphs.extend([p for p in cleaned if p])
            flat_sections.append(section)

        # Build numbered tree
        section_map: dict[str, SectionNode] = {}
        roots: list[SectionNode] = []

        for s in flat_sections:
            if s.id is not None:
                section_map[s.id] = s

        for s in flat_sections:
            if s.id is None:
                roots.append(s)
                continue
            parent_id = s.id.rsplit(".", 1)[0] if "." in s.id else None
            if parent_id and parent_id in section_map:
                section_map[parent_id].children.append(s)
            else:
                roots.append(s)

        authors = list(dict.fromkeys(authors))
        meta = list(dict.fromkeys(meta))

        return ParsedPaper(
            file_path=str(pdf_path.resolve()),
            pages_used=pages_used,
            title=title,
            authors=authors,
            abstract="\n\n".join(abstract_parts).strip(),
            meta=meta,
            sections=roots,
        )

    def parse_directory(self, directory: str | Path) -> list[ParsedPaper]:
        directory = Path(directory)
        pdfs = sorted(directory.glob("*.pdf"))
        return [self.parse_pdf(p) for p in pdfs]

    def to_markdown(self, paper: ParsedPaper) -> str:
        lines: list[str] = [
            "# Paper Structure",
            f"- File: {Path(paper.file_path).name}",
            f"- Pages Used: {paper.pages_used}",
            "",
            "## Title",
            paper.title or "(not found)",
            "",
            "## Authors",
        ]

        lines += [f"- {a}" for a in paper.authors] if paper.authors else ["- (none)"]

        lines += ["", "## Abstract", paper.abstract or "(none)"]

        lines += ["", "## Meta"]
        lines += [f"- {m}" for m in paper.meta] if paper.meta else ["- (none)"]

        lines += ["", "## Sections"]
        if paper.sections:
            for s in paper.sections:
                self._render_section_md(lines, s, 0)
        else:
            lines.append("- (none)")

        return "\n".join(lines).strip() + "\n"


if __name__ == "__main__":
    parser = PDFPaperParser(max_pages=20)
    papers = parser.parse_directory("Papers")
    out_dir = Path("samples")
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in papers:
        out = out_dir / f"{Path(p.file_path).stem}.md"
        out.write_text(parser.to_markdown(p), encoding="utf-8")
        print(f"saved: {out}")
