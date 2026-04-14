from __future__ import annotations

import re
from pathlib import Path

from paper_bot.paper_service import PaperService


def _extract_block(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    m2 = re.search(r"^##\s+.+$", text[start:], flags=re.MULTILINE)
    end = start + m2.start() if m2 else len(text)
    return text[start:end].strip()


def _split_chunks(text: str, chunk_chars: int = 950, overlap: int = 160) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    out: list[str] = []
    n = len(cleaned)
    i = 0
    while i < n:
        j = min(n, i + chunk_chars)
        if j < n:
            floor = min(n, i + int(chunk_chars * 0.6))
            sp = cleaned.rfind(" ", floor, j)
            if sp > i:
                j = sp
        piece = cleaned[i:j].strip()
        if piece:
            out.append(piece)
        if j >= n:
            break
        i = max(i + 1, j - overlap)
    return out


def build_index_from_parsed_md(md_path: Path) -> dict:
    raw = md_path.read_text(encoding="utf-8")

    title_block = _extract_block(raw, "Title")
    title = next((ln.strip() for ln in title_block.splitlines() if ln.strip()), md_path.stem)
    abstract = "\n".join(
        ln.strip() for ln in _extract_block(raw, "Abstract").splitlines() if ln.strip() and not ln.strip().startswith("- ")
    ).strip()

    sections: list[dict] = []
    current_name = ""
    current_paragraphs: list[str] = []
    for line in raw.splitlines():
        if line.startswith("### "):
            if current_name:
                sections.append({"name": current_name, "paragraphs": current_paragraphs[:]})
            current_name = line[4:].strip()
            current_paragraphs = []
            continue
        if line.startswith("- ") and current_name:
            txt = line[2:].strip()
            if txt and txt != "(no paragraphs)":
                current_paragraphs.append(txt)
    if current_name:
        sections.append({"name": current_name, "paragraphs": current_paragraphs[:]})

    chunks: list[dict] = []
    if abstract:
        chunks.append(
            {
                "chunk_id": "abstract:0",
                "section_id": "abstract",
                "section_name": "Abstract",
                "chunk_index": 0,
                "page_start": None,
                "page_end": None,
                "text": abstract[:2200],
            }
        )

    for s_idx, sec in enumerate(sections, start=1):
        sec_id = f"sec_{s_idx}"
        sec_name = sec["name"] or sec_id
        body = "\n".join(sec["paragraphs"])
        for c_idx, piece in enumerate(_split_chunks(body)):
            chunks.append(
                {
                    "chunk_id": f"{sec_id}:{c_idx}",
                    "section_id": sec_id,
                    "section_name": sec_name,
                    "chunk_index": c_idx,
                    "page_start": None,
                    "page_end": None,
                    "text": piece[:2200],
                }
            )

    return {
        "version": 2,
        "title": title,
        "abstract": abstract,
        "pages_used": 0,
        "chunks": chunks,
    }


def main() -> None:
    md_path = Path("samples/3582560.parsed.md")
    if not md_path.exists():
        raise FileNotFoundError(f"샘플 파일이 없습니다: {md_path}")

    index_doc = build_index_from_parsed_md(md_path)
    service = PaperService(auto_bootstrap=False)
    service.set_parsed_paper_index(paper_index=index_doc, source_label=str(md_path))

    print("\n=== Step 1: Summary ===")
    summary_result = service.answer("이 논문의 핵심을 요약해줘")
    print(summary_result["answer"])

    print("\n=== Step 2: QA (type 'exit' to quit) ===")
    while True:
        q = input("\n질문> ").strip()
        if not q:
            continue
        if q.lower() in {"exit", "quit", "q"}:
            print("종료합니다.")
            break
        out = service.answer(q)
        print("\n답변:")
        print(out["answer"])


if __name__ == "__main__":
    main()
