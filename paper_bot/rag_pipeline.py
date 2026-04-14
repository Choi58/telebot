from __future__ import annotations

import re
from typing import Any

import requests
from haystack import Document
from haystack.components.rankers import SentenceTransformersSimilarityRanker
from haystack.components.retrievers import InMemoryEmbeddingRetriever
from haystack.document_stores.in_memory import InMemoryDocumentStore


class RagPipeline:
    """Semantic retrieval + rerank + sentence-window expansion for paper QA."""

    def __init__(self, config: Any) -> None:
        self.config = config

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {t for t in re.findall(r"[A-Za-z0-9가-힣]+", (text or "").lower()) if len(t) >= 2}

    def _embedding_endpoint(self) -> str:
        return self.config.embedding_api_base.rstrip("/") + "/embeddings"

    def _nomic_prefix(self, text: str, mode: str) -> str:
        clean = (text or "").strip()
        if not clean:
            return clean
        if mode == "search_query":
            return f"search_query: {clean}"
        return f"search_document: {clean}"

    def _embed_texts(self, texts: list[str], mode: str) -> list[list[float]]:
        if not texts:
            return []
        inputs = [self._nomic_prefix(t, mode) if "nomic-embed-text" in self.config.embedding_model else t for t in texts]
        payload: dict[str, Any] = {
            "model": self.config.embedding_model,
            "input": inputs,
        }
        if self.config.embedding_dimensions > 0:
            payload["dimensions"] = int(self.config.embedding_dimensions)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.embedding_api_key}",
        }
        resp = requests.post(self._embedding_endpoint(), json=payload, headers=headers, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"embedding request failed: {resp.status_code} {resp.text[:240]}")
        obj = resp.json()
        data = obj.get("data", [])
        if not isinstance(data, list) or not data:
            raise RuntimeError("embedding response missing data")

        out: list[list[float]] = []
        for item in sorted(data, key=lambda x: int(x.get("index", 0))):
            emb = item.get("embedding")
            if not isinstance(emb, list) or not emb:
                raise RuntimeError("invalid embedding vector")
            out.append([float(v) for v in emb])
        return out

    def _build_runtime(self, paper_index: dict[str, Any]) -> dict[str, Any]:
        chunk_pool = paper_index.get("chunks", []) or []
        if not chunk_pool:
            raise RuntimeError("No chunks available for retrieval.")

        doc_store = InMemoryDocumentStore(embedding_similarity_function="cosine")
        hay_docs: list[Document] = []
        chunks_by_id: dict[str, dict[str, Any]] = {}

        for chunk in chunk_pool:
            chunk_id = str(chunk.get("chunk_id", "")).strip()
            if not chunk_id:
                continue
            chunks_by_id[chunk_id] = chunk
            hay_docs.append(
                Document(
                    content=str(chunk.get("text", "")),
                    meta={
                        "chunk_id": chunk_id,
                        "section_id": str(chunk.get("section_id", "")),
                        "section_name": str(chunk.get("section_name", "")),
                        "chunk_index": int(chunk.get("chunk_index", 0)),
                        "page_start": chunk.get("page_start"),
                        "page_end": chunk.get("page_end"),
                    },
                )
            )

        doc_embeddings = self._embed_texts([d.content for d in hay_docs], mode="search_document")
        for doc, emb in zip(hay_docs, doc_embeddings):
            doc.embedding = emb
        doc_store.write_documents(hay_docs)

        retriever = InMemoryEmbeddingRetriever(
            document_store=doc_store,
            top_k=max(1, int(self.config.retriever_top_k)),
        )

        ranker = None
        if self.config.reranker_model.strip():
            try:
                ranker = SentenceTransformersSimilarityRanker(
                    model=self.config.reranker_model,
                    top_k=max(1, int(self.config.rerank_top_k)),
                )
            except Exception:
                ranker = None

        chunks_by_section: dict[str, dict[int, dict[str, Any]]] = {}
        for chunk in chunks_by_id.values():
            sid = str(chunk.get("section_id", ""))
            cidx = int(chunk.get("chunk_index", 0))
            chunks_by_section.setdefault(sid, {})[cidx] = chunk

        return {
            "retriever": retriever,
            "ranker": ranker,
            "chunks_by_id": chunks_by_id,
            "chunks_by_section": chunks_by_section,
        }

    def _fallback_rerank(self, question: str, docs: list[Document]) -> list[Document]:
        q_tokens = self._tokenize(question)
        if not q_tokens:
            return docs[: self.config.rerank_top_k]

        def _score(d: Document) -> float:
            blob = f"{d.meta.get('section_name', '')} {d.content}".lower()
            return float(sum(1 for t in q_tokens if t in blob))

        ranked = sorted(docs, key=_score, reverse=True)
        return ranked[: self.config.rerank_top_k]

    def _retrieve_documents(self, question: str, runtime: dict[str, Any]) -> list[Document]:
        q_emb = self._embed_texts([question], mode="search_query")[0]
        retrieved = runtime["retriever"].run(query_embedding=q_emb).get("documents", []) or []
        if not retrieved:
            return []

        ranker = runtime.get("ranker")
        if ranker is not None:
            try:
                reranked = ranker.run(query=question, documents=retrieved).get("documents", []) or []
            except Exception:
                reranked = self._fallback_rerank(question, retrieved)
        else:
            reranked = self._fallback_rerank(question, retrieved)
        return reranked[: self.config.rerank_top_k]

    def _expand_sentence_window(self, docs: list[Document], runtime: dict[str, Any]) -> list[dict[str, Any]]:
        chunks_by_section: dict[str, dict[int, dict[str, Any]]] = runtime["chunks_by_section"]
        chunks_by_id: dict[str, dict[str, Any]] = runtime["chunks_by_id"]

        selected_ids: list[str] = []
        for d in docs:
            chunk_id = str(d.meta.get("chunk_id", "")).strip()
            sec_id = str(d.meta.get("section_id", "")).strip()
            cidx = int(d.meta.get("chunk_index", 0))
            if chunk_id and chunk_id not in selected_ids:
                selected_ids.append(chunk_id)

            neighbors = chunks_by_section.get(sec_id, {})
            for delta in range(-self.config.sentence_window_size, self.config.sentence_window_size + 1):
                nidx = cidx + delta
                if nidx < 0:
                    continue
                neighbor = neighbors.get(nidx)
                if not neighbor:
                    continue
                nid = str(neighbor.get("chunk_id", "")).strip()
                if nid and nid not in selected_ids:
                    selected_ids.append(nid)

        expanded: list[dict[str, Any]] = []
        for cid in selected_ids:
            chunk = chunks_by_id.get(cid)
            if chunk:
                expanded.append(chunk)

        if expanded:
            return expanded

        for d in docs:
            cid = str(d.meta.get("chunk_id", "")).strip()
            chunk = chunks_by_id.get(cid)
            if chunk:
                expanded.append(chunk)
        return expanded

    def _build_qa_context(self, paper_index: dict[str, Any], expanded_chunks: list[dict[str, Any]]) -> tuple[str, list[str], list[str], list[str]]:
        title = str(paper_index.get("title", "")).strip()
        lines = [f"Title: {title}"]
        used_sections: list[str] = []
        used_chunk_ids: list[str] = []
        citation_lines: list[str] = []

        for i, chunk in enumerate(expanded_chunks, start=1):
            sec = str(chunk.get("section_name", "")).strip() or "Unknown"
            cid = str(chunk.get("chunk_id", "")).strip()
            ps = chunk.get("page_start")
            pe = chunk.get("page_end")
            page = f"p.{ps}" if isinstance(ps, int) and ps == pe else (f"p.{ps}-{pe}" if isinstance(ps, int) and isinstance(pe, int) else "unknown")
            txt = str(chunk.get("text", "")).strip()[: self.config.max_chars_per_chunk]

            if sec and sec not in used_sections:
                used_sections.append(sec)
            if cid:
                used_chunk_ids.append(cid)

            lines.append(f"[{i}] section={sec} | page={page} | chunk={cid}\n{txt}")
            citation_lines.append(f"[{i}] section={sec} | page={page} | chunk={cid}")

        context = "\n\n".join(lines).strip()
        if len(context) > self.config.max_context_chars:
            context = context[: self.config.max_context_chars]
        return context, used_sections, used_chunk_ids, citation_lines

    def retrieve_context(self, question: str, paper_index: dict[str, Any]) -> tuple[str, list[str], list[str], list[str]]:
        runtime = self._build_runtime(paper_index)
        retrieved_docs = self._retrieve_documents(question, runtime)
        expanded_chunks = self._expand_sentence_window(retrieved_docs, runtime)
        return self._build_qa_context(paper_index, expanded_chunks)
