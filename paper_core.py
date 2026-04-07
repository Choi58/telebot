from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter


# Simple sentence split for Korean/English mixed text.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?]|[다요죠]\.)\s+|(?<=[.!?])\n+")


@dataclass
class SentenceSummary:
    sentence: str
    summary: str


@dataclass
class ParagraphSummary:
    paragraph_id: int
    text: str
    summary: str
    sentences: list[SentenceSummary]


@dataclass
class DocumentSummary:
    file_path: str
    overview: str
    paragraphs: list[ParagraphSummary]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_paragraphs(text: str, min_len: int = 80) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    merged: list[str] = []

    for p in paragraphs:
        if not merged:
            merged.append(p)
            continue
        if len(p) < min_len:
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)

    return merged


def _split_sentences(paragraph: str) -> list[str]:
    candidates = [s.strip() for s in _SENTENCE_SPLIT_RE.split(paragraph) if s.strip()]
    if candidates:
        return candidates
    return [paragraph.strip()] if paragraph.strip() else []


class TopDownPaperSummarizer:
    """
    Top-down hierarchical summarizer.
    1) 문서 전체 개요 요약
    2) 개요를 컨텍스트로 문단 요약
    3) 문단 요약을 컨텍스트로 문장 요약
    """

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        api_base: str = "http://host.docker.internal:1234/v1",
        api_key: str = "lm-studio",
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> None:
        self.llm = ChatOpenAI(
            model=model,
            base_url=api_base,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        self.overview_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You summarize academic papers. "
                        "Return concise Korean bullets with section-level structure.",
                    ),
                    (
                        "human",
                        "다음은 논문 일부들입니다. 전체 논문의 구성(문제/방법/실험/결론) 중심으로 8줄 이내 개요를 작성하세요.\n\n{text}",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.paragraph_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You summarize one paragraph in Korean. "
                        "Keep key claims, evidence, and conclusion.",
                    ),
                    (
                        "human",
                        "[문서 개요]\n{overview}\n\n[문단]\n{paragraph}\n\n"
                        "위 문단을 1~2문장으로 요약하세요.",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

        self.sentence_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You summarize one sentence in Korean. Keep original meaning strictly.",
                    ),
                    (
                        "human",
                        "[문서 개요]\n{overview}\n\n[문단 요약]\n{paragraph_summary}\n\n"
                        "[문장]\n{sentence}\n\n"
                        "위 문장을 짧게(최대 1문장) 요약하세요.",
                    ),
                ]
            )
            | self.llm
            | StrOutputParser()
        )

    def load_pdf_text(self, pdf_path: str) -> str:
        loader = PyPDFLoader(pdf_path)
        docs = loader.load()
        text = "\n\n".join(doc.page_content for doc in docs)
        return _clean_text(text)

    def summarize(self, pdf_path: str, *, max_paragraphs: int | None = 40) -> DocumentSummary:
        text = self.load_pdf_text(pdf_path)

        # 1) Overview from coarse chunks.
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=2500,
            chunk_overlap=200,
            separators=["\n\n", "\n", ". ", " "],
        )
        overview_chunks = splitter.split_text(text)
        overview_input = "\n\n".join(overview_chunks[:8])
        overview = self.overview_chain.invoke({"text": overview_input}).strip()

        # 2) Paragraph summaries guided by overview.
        paragraphs = _split_paragraphs(text)
        if max_paragraphs is not None:
            paragraphs = paragraphs[:max_paragraphs]

        paragraph_summaries: list[ParagraphSummary] = []
        for idx, paragraph in enumerate(paragraphs, start=1):
            p_summary = self.paragraph_chain.invoke(
                {"overview": overview, "paragraph": paragraph}
            ).strip()

            # 3) Sentence summaries guided by paragraph+overview.
            sentences = _split_sentences(paragraph)
            sentence_summaries: list[SentenceSummary] = []
            for sentence in sentences:
                s_summary = self.sentence_chain.invoke(
                    {
                        "overview": overview,
                        "paragraph_summary": p_summary,
                        "sentence": sentence,
                    }
                ).strip()
                sentence_summaries.append(
                    SentenceSummary(sentence=sentence, summary=s_summary)
                )

            paragraph_summaries.append(
                ParagraphSummary(
                    paragraph_id=idx,
                    text=paragraph,
                    summary=p_summary,
                    sentences=sentence_summaries,
                )
            )

        return DocumentSummary(
            file_path=str(Path(pdf_path).resolve()),
            overview=overview,
            paragraphs=paragraph_summaries,
        )


def render_markdown(result: DocumentSummary) -> str:
    lines: list[str] = []
    lines.append(f"# Paper Summary: {Path(result.file_path).name}")
    lines.append("")
    lines.append("## 1) 전체 개요")
    lines.append(result.overview)
    lines.append("")
    lines.append("## 2) 문단/문장 요약")

    for p in result.paragraphs:
        lines.append("")
        lines.append(f"### Paragraph {p.paragraph_id}")
        lines.append(f"- 문단 요약: {p.summary}")
        for i, s in enumerate(p.sentences, start=1):
            lines.append(f"- 문장 {i}: {s.summary}")

    return "\n".join(lines).strip()


if __name__ == "__main__":
    # Example:
    # summarizer = TopDownPaperSummarizer(model="google/gemma-3-4b")
    # result = summarizer.summarize("./Papers/sample.pdf", max_paragraphs=30)
    # print(render_markdown(result))
    pass
