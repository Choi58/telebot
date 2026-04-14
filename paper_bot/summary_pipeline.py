from __future__ import annotations

from typing import Any


class SummaryPipeline:
    """Hierarchical (section -> final) summarization pipeline."""

    def __init__(self, config: Any, section_summary_chain: Any, final_summary_chain: Any) -> None:
        self.config = config
        self.section_summary_chain = section_summary_chain
        self.final_summary_chain = final_summary_chain

    def build_hierarchical_summary(self, question: str, paper_index: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        section_buckets: dict[str, dict[str, Any]] = {}
        for chunk in paper_index.get("chunks", []) or []:
            sec = str(chunk.get("section_name", "")).strip() or "Unknown"
            bucket = section_buckets.setdefault(sec, {"section_name": sec, "texts": [], "chunk_ids": []})
            bucket["texts"].append(str(chunk.get("text", "")))
            bucket["chunk_ids"].append(str(chunk.get("chunk_id", "")))

        sections = list(section_buckets.values())[: max(1, int(self.config.summary_max_sections))]
        section_outputs: list[str] = []
        used_sections: list[str] = []
        used_chunk_ids: list[str] = []

        for sec in sections:
            sec_name = str(sec.get("section_name", "Unknown"))
            sec_text = "\n".join(sec.get("texts", []))[: self.config.summary_section_chars]
            if not sec_text.strip():
                continue
            sec_sum = self.section_summary_chain.invoke(
                {
                    "title": str(paper_index.get("title", "")),
                    "section_name": sec_name,
                    "section_text": sec_text,
                }
            ).strip()
            if not sec_sum:
                continue
            section_outputs.append(f"[Section: {sec_name}]\n{sec_sum}")
            used_sections.append(sec_name)
            used_chunk_ids.extend([str(x) for x in sec.get("chunk_ids", []) if str(x).strip()][:2])

        if not section_outputs:
            return "요약할 섹션을 찾지 못했습니다.", used_sections, used_chunk_ids

        final_summary = self.final_summary_chain.invoke(
            {"question": question, "section_summaries": "\n\n".join(section_outputs)}
        ).strip()
        if not final_summary:
            final_summary = "\n\n".join(section_outputs)

        references = "\n".join(f"- {name}" for name in used_sections[:8])
        return f"{final_summary}\n\n근거 섹션:\n{references}".strip(), used_sections, used_chunk_ids
