# Engineering Specification: Upgrade SALTMDB Librarian Component with Cross-Chunk Topic Verification

## Objective
Enhance the background Librarian process (`consolidate_vector_clusters` pass) to prevent false-positive memory consolidations. The current global document vector dot product (`np.dot(X_norm, X_norm.T)`) misgroups variable-sized files or documents sharing common boilerplates (e.g., error codes, token structures). 

You will implement an on-the-fly **Two-Stage Cluster Validation Gate** using local **Cross-Chunk Semantic Alignment Matrix tracking** via NumPy and scikit-learn.

---

## 🛠️ Constraints & Specifications
1. **Memory Budget:** Must stay strictly under 1 GB total RAM footprint during execution.
2. **Model Preservation:** Re-use the existing pre-warmed `EmbeddingService` instance (`BAAI/bge-small-en-v1.5` via fastembed + ONNX runtime). **Do not** introduce new local models or generative LLM API calls.
3. **Lossless Filtering:** Keep the initial fast NumPy global adjacency array logic for Stage 1 candidate generation, but introduce a local cloud-to-cloud precision verification step before committing cluster events to the database.

---

## 💻 Step-by-Step Implementation Blueprint

### Step 1: Add a Sliding-Window Text Chunker
Implement an overlapping text chunker in the Librarian processing service utilities or within the main loop structure to slice full markdown text blocks into contextual parts.

* **Target Window Settings:** 1200 characters per chunk, with a 200-character sliding overlap window.
* **Format:**
```python
def _get_overlapping_text_chunks(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    chunks = []
    start = 0
    if not text or len(text.strip()) == 0:
        return []
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
```

### Step 2: Implement Cross-Chunk Adjacency Matrix Verification
Create a validator function to verify the true conceptual overlap between the core pivot node of a detected cluster component and its neighbor nodes.

* **Mathematical Logic:** For any candidate text compared against the pivot document, compute the 2D Cosine Similarity matrix between their respective chunk sets. Apply a row-wise max extraction (`np.max(matrix, axis=1)`) and compute the arithmetic `np.mean()`. 
* **Strict Gate Threshold:** If the structural mean alignment score drops below **0.75**, drop the cluster node configuration to avoid low-quality data merging.

```python
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

def verify_cluster_local_integrity(entities_in_cluster: list[dict], embed_service) -> bool:
    if len(entities_in_cluster) < 2:
        return True

    # Use the first element as the target topic anchor
    pivot_text = entities_in_cluster[0].get('full_content', '')
    chunks_pivot = _get_overlapping_text_chunks(pivot_text)
    if not chunks_pivot:
        return False
        
    embeds_pivot = np.array([embed_service.embed_text(c) for c in chunks_pivot])

    # Cross-verify every peer node in the current cluster component
    for candidate in entities_in_cluster[1:]:
        cand_text = candidate.get('full_content', '')
        chunks_cand = _get_overlapping_text_chunks(cand_text)
        if not chunks_cand:
            return False
            
        embeds_cand = np.array([embed_service.embed_text(c) for c in chunks_cand])

        # Compute point-cloud cloud-to-cloud mapping matrix
        similarity_matrix = cosine_similarity(embeds_pivot, embeds_cand)
        best_matches = np.max(similarity_matrix, axis=1)
        cross_chunk_score = float(np.mean(best_matches))

        # Reject cluster if any single node lacks high semantic intersection
        if cross_chunk_score < 0.75:
            return False

    return True
```

### Step 3: Patch the Librarian Component Execution Loop
Locate `consolidate_vector_clusters` under the Librarian runtime environment. Find the loop iteration processing discovered BFS connected components. Right after your code groups the database records into `cluster_entities` arrays, inject your validation step.

```python
# ... inside the BFS component discovery evaluation block ...
cluster_entities = [all_raw_entities[idx] for idx in current_bfs_component]

# Retrieve the initialized SALTMDB fastembed ONNX controller
embed_service = EmbeddingService.get_instance()

# NEW TOPIC PRECISION GATE
if not verify_cluster_local_integrity(cluster_entities, embed_service):
    logger.info(f"Skipping consolidation request for loose cluster of size {len(cluster_entities)} due to local cross-chunk semantic check failure.")
    continue

# -> Proceed with the existing c-TF-IDF keyword routing and database event serialization logic.
```

---

## 🧪 Verification Tasks for the AI Agent
1. **Locate the Target Files:** Scan the repository (`src/saltmdb/` folder) to find the exact file definitions for the Librarian engine subprocessing script.
2. **Execute In-Line Code Insertion:** Safely append the chunking engine and matrix validator blocks without destroying existing database state transactions, locks, or logging behaviors.
3. **Run Existing Test Suite:** Execute `python -m unittest discover tests` to verify that existing operational capabilities remain green.
4. **Mock Dynamic Variations:** Ensure that a short document matching a small paragraph within an extensive log report is correctly preserved as a localized match, whereas mismatched overall subjects sharing identical boilerplate strings are bypassed.
