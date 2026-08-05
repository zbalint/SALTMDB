# Engineering Specification: Add Stage 2 Cross-Chunk Semantic Reranking to search_memory Tool

## Objective
Upgrade the `search_memory` MCP tool to optionally execute a secondary **Cross-Chunk Semantic Alignment Reranker**. This eliminates length-dilution biases (e.g., matching a short query against a 10k-token log file, or a long query against a 1-sentence atomic memory) when performing semantic searches on documents of varying sizes.

---

## 🛠️ Constraints & Specifications
1. **Memory Ceiling:** Must stay strictly under 1 GB total RAM footprint.
2. **Reuse Architecture:** Re-use the existing pre-warmed `EmbeddingService` instance (`BAAI/bge-small-en-v1.5` via fastembed + ONNX runtime). Do not initialize new models.
3. **Pipeline Efficiency:** Apply the heavy chunk-matrix math **only** to the Top 20 candidate documents returned by Stage 1 (FTS5 + Vector RRF). Never calculate chunk matrices across the entire database.

---

## 💻 Step-by-Step Implementation Blueprint

### Step 1: Update the MCP Tool Parameter Schema
Locate the file where the `search_memory` tool input arguments are registered (e.g., `src/saltmdb/mcp/server.py`). Add an optional boolean argument called `rerank_by_topic`.

```json
"rerank_by_topic": {
    "type": "boolean",
    "description": "If true, takes the top candidate results from global search, performs localized cross-chunk matrix alignment against the query, and re-orders results by absolute topic relevance.",
    "default": false
}
```

### Step 2: Implement the Search Rerank Processing Logic
Create or append a utility function within your search service layer that handles the chunk processing, embedding, matrix alignment calculation, and sorting.

* **Settings:** Sliding window text chunking at 1,200 characters with a 200-character overlap.
* **Math Formula:** `Mean( Max( CosineSimilarity(Query_Chunks, Candidate_Chunks) ) )`

```python
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

def _search_chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    chunks = []
    start = 0
    if not text or len(text.strip()) == 0:
        return []
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def rerank_search_candidates(query_text: str, candidates: list[dict], embed_service) -> list[dict]:
    if not candidates or not query_text.strip():
        return candidates

    # 1. Chunk and embed the incoming search query text
    query_chunks = _search_chunk_text(query_text)
    if not query_chunks:
        return candidates
    query_embeddings = np.array([embed_service.embed_text(c) for c in query_chunks])

    reranked_results = []

    for item in candidates:
        # Extract full content (ensure your Stage 1 pass included full text extraction)
        candidate_text = item.get("full_content", "") or item.get("content", "")
        if not candidate_text:
            item["topic_score"] = 0.0
            item["semantic_verdict"] = "UNKNOWN_CONTENT"
            reranked_results.append(item)
            continue

        # 2. Chunk and embed the specific candidate record
        cand_chunks = _search_chunk_text(candidate_text)
        cand_embeddings = np.array([embed_service.embed_text(c) for c in cand_chunks])

        # 3. Calculate localized cloud-to-cloud mapping matrix
        similarity_matrix = cosine_similarity(query_embeddings, cand_embeddings)
        best_matches = np.max(similarity_matrix, axis=1)
        topic_score = float(np.mean(best_matches))

        # 4. Map score thresholds into actionable metadata verdicts
        if topic_score >= 0.75:
            verdict = "SAME_SPECIFIC_TOPIC"
        elif topic_score >= 0.55:
            verdict = "BROADLY_RELATED_THEMES"
        else:
            verdict = "DIFFERENT_TOPICS"

        # Append scores onto the existing structure
        updated_item = {**item}
        updated_item["topic_score"] = round(topic_score, 4)
        updated_item["semantic_verdict"] = verdict
        reranked_results.append(updated_item)

    # 5. Perform final precision-rank sort descending by topic score
    reranked_results.sort(key=lambda x: x.get("topic_score", 0.0), reverse=True)
    return reranked_results
```

### Step 3: Inject the Logic Into the `search_memory` Entry Point
Modify your master execution workflow inside `search_memory`. Intercept the path when `rerank_by_topic=True` is provided.

```python
def search_memory(query_keywords=None, rerank_by_topic=False, fetch_full=False, **kwargs):
    # Reranking requires processing raw string segments. Force full text retrieval from database.
    if rerank_by_topic:
        fetch_full = True

    # 1. Execute your existing parallel Stage 1 Engine (FTS5 + Vector RRF)
    # Ensure it returns the top candidates (cap candidate fetching at ~20 rows for reranking)
    initial_results = execute_parallel_fts5_and_vector_rrf(
        query_keywords=query_keywords, 
        fetch_full=fetch_full, 
        **kwargs
    )

    # 2. Conditionally switch to Stage 2 Intersection Reranking
    if rerank_by_topic and query_keywords and len(initial_results) > 0:
        from saltmdb.services.embedding_service import EmbeddingService
        embed_service = EmbeddingService.get_instance()

        # Execute on-the-fly local reranking
        final_ranked_results = rerank_search_candidates(
            query_text=query_keywords,
            candidates=initial_results[:20],  # Restrict to top 20 candidates for computational safety
            embed_service=embed_service
        )
        return final_ranked_results

    return initial_results
```

---

## 🧪 Verification Tasks for the AI Agent
1. **Tool Integrity Check:** Ensure that if `rerank_by_topic=False`, the search engine skips Stage 2 entirely, preventing performance degradation for basic searches.
2. **Context Retention:** Verify that snippet generation, highlighting keys (`<mark>`), and graph-relation lookups (`include_related`) function correctly alongside the new sort layout.
3. **Run Suite:** Execute `python -m unittest discover tests` to check your refactoring constraints.
