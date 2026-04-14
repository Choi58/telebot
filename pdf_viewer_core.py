from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
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


class GROBIDPaperParser:
    """GROBID-only parser."""

    def __init__(
        self,
        grobid_url: str,
        grobid_timeout_sec: int = 120,
        max_pages: int | None = None,
    ) -> None:
        self.grobid_url = (grobid_url or "").rstrip("/")
        self.grobid_timeout_sec = max(5, int(grobid_timeout_sec))
        self.max_pages = max_pages
        self.last_backend_used = "grobid"

    @staticmethod
    def _tei_text(el: ET.Element | None) -> str:
        if el is None:
            return ""
        text = " ".join(el.itertext())
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _resolve_pages_used(self, pdf_path: Path, max_pages: int | None = None) -> int:
        reader = PdfReader(str(pdf_path))
        total_pages = len(reader.pages)
        limit = self.max_pages if max_pages is None else max_pages
        return total_pages if limit is None else min(total_pages, int(limit))

    def _parse_div(self, div: ET.Element, ns: dict[str, str], idx_prefix: str) -> SectionNode:
        head = self._tei_text(div.find("tei:head", ns)) or f"Section {idx_prefix}"
        sec_id = div.attrib.get("n") or div.attrib.get("{http://www.w3.org/XML/1998/namespace}id")
        if sec_id is not None:
            sec_id = str(sec_id)

        paragraphs: list[str] = []
        for child in list(div):
            tag = child.tag.split("}")[-1]
            if tag == "p":
                txt = self._tei_text(child)
                if txt:
                    paragraphs.append(txt)

        node = SectionNode(id=sec_id, name=head, paragraphs=paragraphs)

        div_children = [c for c in list(div) if c.tag.split("}")[-1] == "div"]
        for j, child_div in enumerate(div_children, start=1):
            child_idx = f"{idx_prefix}.{j}" if idx_prefix else str(j)
            node.children.append(self._parse_div(child_div, ns, child_idx))
        return node

    def parse_pdf(self, pdf_path: str | Path, max_pages: int | None = None) -> ParsedPaper:
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")
        if not self.grobid_url:
            raise RuntimeError("GROBID_URL is empty")

        endpoint = f"{self.grobid_url}/api/processFulltextDocument"
        with path.open("rb") as f:
            files = {"input": (path.name, f, "application/pdf")}
            data = {
                "consolidateHeader": "0",
                "consolidateCitations": "0",
                "teiCoordinates": "false",
            }
            resp = requests.post(endpoint, files=files, data=data, timeout=self.grobid_timeout_sec)

        if resp.status_code != 200:
            raise RuntimeError(f"GROBID failed: {resp.status_code} {resp.text[:200]}")

        root = ET.fromstring(resp.text)
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}

        title = self._tei_text(root.find(".//tei:titleStmt/tei:title", ns))

        authors: list[str] = []
        for a in root.findall(".//tei:sourceDesc//tei:author", ns):
            name = self._tei_text(a.find("tei:persName", ns)) or self._tei_text(a)
            if name and name not in authors:
                authors.append(name)

        abstract_parts: list[str] = []
        for p in root.findall(".//tei:profileDesc/tei:abstract//tei:p", ns):
            txt = self._tei_text(p)
            if txt:
                abstract_parts.append(txt)
        abstract = "\n\n".join(abstract_parts).strip()

        meta: list[str] = []
        for term in root.findall(".//tei:keywords//tei:term", ns):
            txt = self._tei_text(term)
            if txt:
                meta.append(txt)
        meta = list(dict.fromkeys(meta))

        sections: list[SectionNode] = []
        body = root.find(".//tei:text/tei:body", ns)
        if body is not None:
            divs = [d for d in list(body) if d.tag.split("}")[-1] == "div"]
            for i, d in enumerate(divs, start=1):
                sections.append(self._parse_div(d, ns, str(i)))

        if not sections:
            raise RuntimeError("GROBID returned no sections")

        return ParsedPaper(
            file_path=str(path.resolve()),
            pages_used=self._resolve_pages_used(path, max_pages),
            title=title,
            authors=authors,
            abstract=abstract,
            meta=meta,
            sections=sections,
        )

    def parse_directory(self, directory: str | Path) -> list[ParsedPaper]:
        directory = Path(directory)
        pdfs = sorted(directory.glob("*.pdf"))
        return [self.parse_pdf(p) for p in pdfs]

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
            GROBIDPaperParser._render_section_md(lines, c, depth + 1)

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


class PDFPaperParser(GROBIDPaperParser):
    """Compatibility alias."""

    def __init__(
        self,
        max_pages: int | None = None,
        backend: str | None = None,
        grobid_url: str | None = None,
        grobid_timeout_sec: int = 120,
    ) -> None:
        if backend and backend.strip().lower() not in {"grobid", "auto"}:
            raise ValueError(
                f"Unsupported parser backend '{backend}'. "
                "This project is fixed to GROBID-only."
            )
        super().__init__(
            grobid_url=(grobid_url or ""),
            grobid_timeout_sec=grobid_timeout_sec,
            max_pages=max_pages,
        )
