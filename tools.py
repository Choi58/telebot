from __future__ import annotations

from pathlib import Path
from typing import Any

from pdf_viewer_core import PDFPaperParser, ParsedPaper


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _open_pdf_impl(path: str, max_pages: int | None = None) -> dict[str, Any]:
    """Internal PDF open/parse implementation."""
    try:
        p = Path(path)
        if not p.exists():
            return _error(f"File not found: {path}")

        parser = PDFPaperParser(max_pages=max_pages)
        paper: ParsedPaper = parser.parse_pdf(p, max_pages=max_pages)
        return _ok(paper.to_dict())
    except Exception as e:
        return _error(f"tool_open_pdf failed: {type(e).__name__}: {e}")


def tool_open_pdf(path: str, max_pages: int | None = None) -> dict[str, Any]:
    """Open one PDF and return structured data."""
    return _open_pdf_impl(path, max_pages=max_pages)


def tool_list_pdfs(directory: str) -> dict[str, Any]:
    """List PDF files in a directory.

    Returns:
        {"ok": True, "data": {"directory":"...", "files":[...]}} or error object.
    """
    try:
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            return _error(f"Directory not found: {directory}")
        files = sorted([p.name for p in d.glob("*.pdf") if p.is_file()])
        return _ok({"directory": str(d), "files": files})
    except Exception as e:
        return _error(f"tool_list_pdfs failed: {type(e).__name__}: {e}")
