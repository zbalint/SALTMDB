## Technical Memo: Architecture Upgrade for Local-First Semantic Search and Memory Consolidation
To: Core Development Team / AI Coding Agents
Date: August 3, 2026
Subject: Eliminating Length Bias and False Consolidations via Two-Stage Cross-Chunk Semantic Alignment
------------------------------
## 1. Executive Summary
SALTMDB currently uses a single global document vector configuration for long-term memory management and semantic indexing. While efficient at database scale, compressing 10k-token markdown files into isolated 384-dimensional dense vectors (BAAI/bge-small-en-v1.5) averages out critical contextual detail. This introduces structural failure paths in two primary areas:

* Background Maintenance (Librarian Component): Unrelated files or documents sharing common technical syntax (e.g., error logs, system tokens, configuration code) drift together globally, prompting false consolidation requests.
* Foreground Discovery (search_memory Tool): Shorter user queries get mathematically diluted when measured against longer documents, leading to severe "lost-in-the-middle" context degradation.

To resolve these limitations while respecting a strict local hardware runtime ceiling of under 1 GB RAM, we are introducing a unified, two-stage Cross-Chunk Semantic Alignment Matrix methodology across both systems.
------------------------------
## 2. Core Mathematical Solution: Cloud-to-Cloud Intersection
Instead of calculating standard vector distance between two global points (One-to-One), both documents are treated as point clouds of localized concept fragments (Cloud-to-Cloud).

[Document A] ──► Sliced Chunks ──► Array of Vectors (A) ──┐
                                                          ├──► [Cosine Similarity Matrix] ──► Mean(Max(Axis=1))
[Document B] ──► Sliced Chunks ──► Array of Vectors (B) ──┘


   1. Sliding Window Processing: Documents are broken into chunks of 1,200 characters with a 200-character overlapping sliding window (10%–20% allocation). This prevents thought fracturing across boundary lines.
   2. Intersection Logic: For an evaluation pairing, a 2D Cosine Similarity matrix is calculated using NumPy and scikit-learn across every single chunk variation.
   3. Local Target Intersection Scoring:
   $$\text{Topic Score} = \text{Mean}(\text{Max}(\text{CosineSimilarity}(\text{Chunks}_A, \text{Chunks}_B), \text{axis}=1))$$ 
   This measures whether every unique concept in Document A finds an exact localized partner inside Document B.
   4. Asymmetry Protection: If a 1-paragraph error description is fully contained inside a 50-page comprehensive execution log, this matrix returns a near-perfect score ($\ge 0.75$), ignoring the length mismatch.

------------------------------
## 3. Implementation Layouts## Component A: The Background Librarian Post-Cluster Gate
Keep the initial high-velocity global adjacency calculation (np.dot(X_norm, X_norm.T)) to find wide component groups via the BFS walking sequence. However, before the system records a permanent consolidation_request or domain_suggestion ledger entry, it must pass a strict post-cluster confirmation layout:

def verify_cluster_local_integrity(entities_in_cluster: list[dict], embed_service) -> bool:
    if len(entities_in_cluster) < 2: return True
    
    pivot_text = entities_in_cluster[0].get('full_content', '')
    chunks_pivot = _chunk_text(pivot_text)
    embeds_pivot = np.array([embed_service.embed_text(c) for c in chunks_pivot])

    for candidate in entities_in_cluster[1:]:
        cand_chunks = _chunk_text(candidate.get('full_content', ''))
        embeds_cand = np.array([embed_service.embed_text(c) for c in cand_chunks])
        
        matrix = cosine_similarity(embeds_pivot, embeds_cand)
        cross_chunk_score = float(np.mean(np.max(matrix, axis=1)))
        
        if cross_chunk_score < 0.75:  # STRICT CONSOLIDATION BARRIER
            return False
    return True

## Component B: Foreground Two-Stage Hybrid Search
To maintain performance during search_memory scans, do not process chunks database-wide. Instead, implement a Two-Stage Retrieval (Reranking) pipeline:

* Stage 1 (Database Filter - Fast Pass): Execute the current parallelized FTS5 keyword engine combined with pre-computed sqlite-vec global matrix index rows via Reciprocal Rank Fusion (RRF). Extract the Top 20 items with fetch_full=True.
* Stage 2 (Reranker - Precision Pass): If rerank_by_topic=True is provided, chunk the raw query and the top 20 candidate payloads on the fly. Recompute their relevance using the Cross-Chunk score, append an explicit semantic_verdict, and sort descending.

Incoming Request ──► Stage 1: Global FTS5/Vector DB Search (Matches millions in ms)
                           │ (Pulls Top 20 Candidates)
                           ▼
                     Stage 2: On-the-fly Local Cross-Chunk Reranker
                           │ (Sorts out length dilution/keyword anomalies)
                           ▼
                     Final Output Returned to Agent Interface

------------------------------
## 4. Architectural Verification Metrics

| System Attribute | Prior Implementation | Upgraded Implementation |
|---|---|---|
| RAM Footprint | ~150 MB | < 200 MB (Well below 1 GB budget limit) |
| Model Ingestion | Local bge-small-en-v1.5 | Same (No extra overhead or new weights) |
| Varying Text Size Reliability | Unstable (Long files dilute short files) | Stable (Absolute localized topic tracking) |
| Librarian Precision | False-positive matches via common tokens | Precise (Only merges structural duplicates) |
| Compute Context Allocation | One-to-One Vector Metrics | Cloud-to-Cloud Localized Intersections |

------------------------------
## 5. Next Steps for Autonomous Agents (Claude Code / Antigravity)

   1. Inject the specified spec sheets into your local project prompt environment to update repository files under the src/saltmdb/ path.
   2. Ensure that when rerank_by_topic=False, the search function skips the Stage 2 process entirely to guarantee normal query execution speed.
   3. Validate operations by running the integrated unit test framework via python -m unittest discover tests.

------------------------------
File this document inside your system design archives or reference directories to preserve tracking parameters across downstream automation tools.
