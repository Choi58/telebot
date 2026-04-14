from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pdf_viewer_core import PDFPaperParser, ParsedPaper


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def tool_open_pdf(path: str, max_pages: int | None = None) -> dict[str, Any]:
    """Open one PDF and return structured data."""
    try:
        p = Path(path)
        if not p.exists():
            return _error(f"File not found: {path}")

        grobid_url = os.getenv("GROBID_URL", "").strip()
        if not grobid_url:
            return _error("GROBID_URL is empty. Set GROBID_URL in your environment.")

        parser = PDFPaperParser(
            max_pages=max_pages,
            backend="grobid",
            grobid_url=grobid_url,
            grobid_timeout_sec=int(os.getenv("GROBID_TIMEOUT_SEC", "120")),
        )
        paper: ParsedPaper = parser.parse_pdf(p, max_pages=max_pages)
        payload = paper.to_dict()
        payload["parser_backend"] = parser.last_backend_used or "unknown"
        return _ok(payload)
    except Exception as e:
        return _error(f"tool_open_pdf failed: {type(e).__name__}: {e}")


def tool_list_pdfs(directory: str) -> dict[str, Any]:
    """List PDF files in a directory."""
    try:
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            return _error(f"Directory not found: {directory}")
        files = sorted([p.name for p in d.glob("*.pdf") if p.is_file()])
        return _ok({"directory": str(d), "files": files})
    except Exception as e:
        return _error(f"tool_list_pdfs failed: {type(e).__name__}: {e}")
