"""Historical benchmark script, not part of the maintained test suite.

Round 3 of the retrieval-accuracy benchmark for the memory-core rework's Foundation phase (see
plans/ and SALTMDB memory `5c09effa`) -- same 30 paraphrased query/doc pairs as
benchmark_embedding_accuracy2.py, this time prepending the BGE model's documented query-
instruction prefix ("Represent this sentence for searching relevant passages: ") to test whether
its absence explained round 2's low Top-1 accuracy. Result: prefix hypothesis ruled out (<0.015
MRR movement, within noise) -- see SALTMDB memory `92a3e17d`.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it -- it depends on scikit-learn, which is NOT a declared project
dependency (see pyproject.toml); committing this under tests/ would ImportError at collection and
break the entire suite's discovery under CI's `pip install .` (no dev extras).
Frozen as historical evidence -- the benchmarking phase is closed (SALTMDB memory `403bc0a7`).
"""

import os
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from saltmdb.domain.services import embedding_service


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    if not text or len(text.strip()) == 0:
        return []
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def run_accuracy_benchmark():  # noqa: PLR0915
    embedding_service.get_model()

    # 10 Documents, 3 Queries each = 30 pairs
    doc_query_map = [
        {
            "path": "AGENT_GUIDE.md",
            "queries": [
                "Where can I find instructions on modifying the assistant's underlying behavior and setting up new capabilities?",
                "I need help understanding how to trigger special background jobs and long-running routines.",
                "Explain the difference between global directives and workspace-specific contextual files.",
            ],
        },
        {
            "path": "MIGRATION.md",
            "queries": [
                "What do I need to install on my Windows machine before I can run the backend services in a Linux environment?",
                "How do I fix the network port forwarding issue between the host OS and the container?",
                "Are there specific terminal commands required to get the orchestrator running inside the virtual machine?",
            ],
        },
        {
            "path": "INSTALL.md",
            "queries": [
                "Which exact version of the language runtime is required to bootstrap this repository?",
                "Tell me the procedure to activate the virtual environment and fetch all third-party libraries.",
                "I want to start developing locally, what script do I run first?",
            ],
        },
        {
            "path": "CONTRIBUTING.md",
            "queries": [
                "What are the rules regarding formatting, linting, and type checking before I submit my code?",
                "Do I need to sign any contributor agreements or follow a specific commit message format?",
                "How should I structure my pull request description so the maintainers will review it?",
            ],
        },
        {
            "path": "README.md",
            "queries": [
                "What exactly does this software do and why was it created?",
                "Give me a high-level architectural summary of the database layer and its primary use cases.",
                "Is this project intended for production use or is it just an experimental prototype?",
            ],
        },
        {
            "path": "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/103/output.txt",
            "name": "Handover: Rework Not Started",
            "queries": [
                "Which file mentions that the team only talked about plans but didn't write any actual logic yet?",
                "I'm looking for the log entry where we finalized the list of pending tasks for the architecture rewrite.",
                "Did we make any progress on the codebase today, or was it entirely focused on planning?",
            ],
        },
        {
            "path": "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/104/output.txt",
            "name": "Handover: Post-Round 9 Continuity",
            "queries": [
                "Where is the document that explains how to resume work after the ninth round of defect resolutions?",
                "I need the reference material that replaces the old context for the ongoing self-improvement track.",
                "Which note tells the next agent how to pick up the thread without starting from scratch?",
            ],
        },
        {
            "path": "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/105/output.txt",
            "name": "Handover: Quota Paused",
            "queries": [
                "We ran out of our API budget. Where is this recorded along with the restart date?",
                "Which entry details the start of the massive database overhaul that had to be temporarily halted?",
                "I need to know why the agent stopped working on the rewrite midway through the week.",
            ],
        },
        {
            "path": "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/106/output.txt",
            "name": "Handover: Consolidation Sweep",
            "queries": [
                "How many fresh knowledge nodes were created after we cleaned up the massive pile of pending requests?",
                "Find the report detailing the synthesis of over a hundred raw developer logs into structured facts.",
                "We finished processing the queue of items waiting to be merged. Which log covers this?",
            ],
        },
        {
            "path": "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/107/output.txt",
            "name": "Handover: Bug-Sweep Round 10-13",
            "queries": [
                "I'm trying to track down the history of when we fixed the database freezing and overlapping write issues.",
                "Which document chronicles the four consecutive sweeps focused on resolving full-text search and permission problems?",
                "Where can I read about the fixes applied to the background task delegator's security sandbox?",
            ],
        },
    ]

    docs = []
    queries = []

    for item in doc_query_map:
        path = item["path"]
        if os.path.exists(path):
            with open(path, "r") as f:
                content = f.read().strip()
                if content:
                    doc_idx = len(docs)
                    name = item.get("name", os.path.basename(path))
                    docs.append({"text": content, "name": name})
                    for q in item["queries"]:
                        queries.append({"query": q, "target_doc_idx": doc_idx})

    print(f"Loaded {len(docs)} documents and {len(queries)} queries for accuracy testing.")

    settings = [(200, 33), (400, 67), (800, 133), (1200, 200), (2000, 333)]

    print("\n=== CHUNKING ACCURACY BENCHMARK (Mean(Max(CosineSimilarity))) WITH QUERY PREFIX ===")

    for size, overlap in settings:
        # Embed document chunks (unprefixed)
        doc_chunk_embs = []
        for d in docs:
            chunks = _chunk_text(d["text"], size, overlap)
            embs = [embedding_service.embed_text(c) for c in chunks]
            doc_chunk_embs.append(embs)

        correct_top1 = 0
        mrr_sum = 0
        avg_score_margin = 0

        for q_obj in queries:
            raw_query = q_obj["query"]
            # APPLIED PREFIX HERE
            q_text = "Represent this sentence for searching relevant passages: " + raw_query
            target_idx = q_obj["target_doc_idx"]

            # Chunk and embed the query
            q_chunks = _chunk_text(q_text, size, overlap)
            if not q_chunks:
                q_chunks = [q_text]
            q_embs = [embedding_service.embed_text(c) for c in q_chunks]
            q_embs_np = np.array(q_embs)

            doc_scores = []
            for d_embs in doc_chunk_embs:
                if len(d_embs) == 0:
                    doc_scores.append(0.0)
                    continue
                d_embs_np = np.array(d_embs)
                # Mean(Max(CosineSimilarity(QueryChunks, DocChunks), axis=1))
                sims = cosine_similarity(q_embs_np, d_embs_np)
                max_sims = np.max(sims, axis=1)
                doc_score = np.mean(max_sims)
                doc_scores.append(doc_score)

            ranked_indices = np.argsort(doc_scores)[::-1]
            rank = np.where(ranked_indices == target_idx)[0][0] + 1

            if rank == 1:
                correct_top1 += 1
                margin = doc_scores[ranked_indices[0]] - doc_scores[ranked_indices[1]]
                avg_score_margin += margin
            mrr_sum += 1.0 / rank

        accuracy = correct_top1 / len(queries)
        mrr = mrr_sum / len(queries)
        margin = avg_score_margin / correct_top1 if correct_top1 > 0 else 0

        print(
            f"Setting {size:4d}/{overlap:3d} | Top-1 Acc: {correct_top1}/{len(queries)} ({accuracy * 100:4.1f}%) | MRR: {mrr:.3f} | Avg Win Margin: {margin:.3f}"
        )


if __name__ == "__main__":
    run_accuracy_benchmark()
