from __future__ import annotations

from pathlib import Path
from typing import Any

from pdf_viewer_core import PDFPaperParser, ParsedPaper, SectionNode


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _find_section_node(nodes: list[SectionNode], section_id: str) -> SectionNode | None:
    for node in nodes:
        if node.id == section_id:
            return node
        found = _find_section_node(node.children, section_id)
        if found is not None:
            return found
    return None


def tool_parse_pdf(path: str, max_pages: int | None = None) -> dict[str, Any]:
    """Parse one PDF and return structured data.

    Returns:
        {"ok": True, "data": {...}} or {"ok": False, "error": "..."}
    """
    try:
        p = Path(path)
        if not p.exists():
            return _error(f"File not found: {path}")

        parser = PDFPaperParser(max_pages=max_pages)
        paper: ParsedPaper = parser.parse_pdf(p, max_pages=max_pages)
        return _ok(paper.to_dict())
    except Exception as e:
        return _error(f"tool_parse_pdf failed: {type(e).__name__}: {e}")


def tool_parse_directory(directory: str, max_pages: int | None = None) -> dict[str, Any]:
    """Parse every PDF in a directory.

    Returns:
        {"ok": True, "data": [ ...parsed papers... ]} or error object.
    """
    try:
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            return _error(f"Directory not found: {directory}")

        parser = PDFPaperParser(max_pages=max_pages)
        papers = parser.parse_directory(d)
        return _ok([p.to_dict() for p in papers])
    except Exception as e:
        return _error(f"tool_parse_directory failed: {type(e).__name__}: {e}")


def tool_get_section(path: str, section_id: str, max_pages: int | None = None) -> dict[str, Any]:
    """Parse one PDF and return only one section node by section id.

    Example section_id: "2", "2.1", "2.1.2"
    """
    try:
        p = Path(path)
        if not p.exists():
            return _error(f"File not found: {path}")

        parser = PDFPaperParser(max_pages=max_pages)
        paper = parser.parse_pdf(p, max_pages=max_pages)
        node = _find_section_node(paper.sections, section_id)
        if node is None:
            return _error(f"Section not found: {section_id}")

        return _ok(
            {
                "file_path": paper.file_path,
                "section_id": section_id,
                "section": node.to_dict(),
            }
        )
    except Exception as e:
        return _error(f"tool_get_section failed: {type(e).__name__}: {e}")
