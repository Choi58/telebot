from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pdf_viewer_core import PDFPaperParser
from settings import load_env_file


def parse_all_pdfs(
    *,
    papers_dir: Path,
    out_dir: Path,
    max_pages: int | None,
    write_json: bool,
) -> None:
    if not papers_dir.exists() or not papers_dir.is_dir():
        raise FileNotFoundError(f"papers directory not found: {papers_dir}")

    pdfs = sorted(p for p in papers_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"[INFO] no pdf files found in {papers_dir}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    grobid_url = os.getenv("GROBID_URL", "").strip()
    if not grobid_url:
        raise RuntimeError("GROBID_URL is empty. Set GROBID_URL in .env or environment.")

    parser = PDFPaperParser(
        backend="grobid",
        max_pages=max_pages,
        grobid_url=grobid_url,
        grobid_timeout_sec=int(os.getenv("GROBID_TIMEOUT_SEC", "120")),
    )

    print(f"[START] parsing {len(pdfs)} pdf(s) from {papers_dir}")
    ok = 0
    failed = 0
    for i, pdf in enumerate(pdfs, start=1):
        stem = pdf.stem
        md_path = out_dir / f"{stem}.parsed.md"
        json_path = out_dir / f"{stem}.parsed.json"
        try:
            paper = parser.parse_pdf(pdf, max_pages=max_pages)
            md_path.write_text(parser.to_markdown(paper), encoding="utf-8")
            if write_json:
                json_path.write_text(
                    json.dumps(paper.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            ok += 1
            try:
                display_path = md_path.resolve().relative_to(Path.cwd())
            except ValueError:
                display_path = md_path
            print(f"[OK] ({i}/{len(pdfs)}) {pdf.name} -> {display_path}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] ({i}/{len(pdfs)}) {pdf.name}: {type(e).__name__}: {e}")

    print(f"[DONE] success={ok}, failed={failed}, out_dir={out_dir}")


def main() -> None:
    load_env_file(".env")

    ap = argparse.ArgumentParser(
        description="Parse all PDFs in Papers/ and save parsed markdown under samples/parsed/."
    )
    ap.add_argument("--papers-dir", default="Papers", help="directory that contains PDF files")
    ap.add_argument("--out-dir", default="samples", help="output directory for parsed files")
    ap.add_argument("--max-pages", type=int, default=None, help="optional max pages per PDF")
    ap.add_argument(
        "--no-json",
        action="store_true",
        help="if set, do not write parsed json files",
    )
    args = ap.parse_args()

    parse_all_pdfs(
        papers_dir=Path(args.papers_dir),
        out_dir=Path(args.out_dir),
        max_pages=args.max_pages,
        write_json=not args.no_json,
    )


if __name__ == "__main__":
    main()
