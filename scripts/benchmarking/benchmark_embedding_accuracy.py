"""Historical benchmark script, not part of the maintained test suite.

Round 1 of the retrieval-accuracy benchmark for the memory-core rework's Foundation phase (see
plans/ and SALTMDB memory `5c09effa`). Superseded by benchmark_embedding_accuracy2.py/3.py in
this same directory (this round's queries were lifted near-verbatim from target document titles
-- trivial lexical match, not genuine semantic retrieval; see SALTMDB memory `9d5090dd`).
References absolute paths under a different agent's local working directory
(/home/zbalint/.gemini/antigravity-cli/brain/...) that will not exist on another machine or in a
fresh checkout -- not runnable as-is, kept only as a historical record of method and findings.

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


def run_accuracy_benchmark():  # noqa: C901, PLR0912
    embedding_service.get_model()

    # Define documents and their corresponding queries
    docs = []

    # Handover memories from earlier outputs
    mcp_outputs = [
        (
            "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/103/output.txt",
            "rework not yet started backlog fleshed out pure discussion",
        ),
        (
            "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/104/output.txt",
            "handover session continuity guide post bugfix round 9 self improvement initiative",
        ),
        (
            "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/105/output.txt",
            "memory core rework kickoff paused until wednesday due to weekly quota",
        ),
        (
            "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/106/output.txt",
            "consolidation backlog sweep complete 37 new memories referenced raw entities",
        ),
        (
            "/home/zbalint/.gemini/antigravity-cli/brain/38fbfa13-7845-47c9-8094-54787eb9ae33/.system_generated/steps/107/output.txt",
            "bug sweep history round 10 to 13 cadet driven concurrency lock stalls security",
        ),
    ]

    for path, query in mcp_outputs:
        if os.path.exists(path):
            with open(path, "r") as f:
                content = f.read().strip()
                if content:
                    docs.append(
                        {
                            "text": content,
                            "query": query,
                            "name": os.path.basename(path) + "_" + query[:10],
                        }
                    )

    # Big markdown files
    md_files = [
        (
            "AGENT_GUIDE.md",
            "How do I configure the agent's behavior and settings, slash commands, rules and skills?",
        ),
        (
            "MIGRATION.md",
            "What are the exact steps and requirements to migrate to WSL2 and Docker?",
        ),
        (
            "INSTALL.md",
            "How do I install the python package, dependencies, and set up the local environment?",
        ),
        (
            "CONTRIBUTING.md",
            "What are the coding standards, PR requirements, and guidelines for contributing?",
        ),
        ("README.md", "What is the high level overview and purpose of this project?"),
    ]

    for path, query in md_files:
        if os.path.exists(path):
            with open(path, "r") as f:
                content = f.read().strip()
                if content:
                    docs.append({"text": content, "query": query, "name": path})

    if not docs:
        print("No documents found!")
        return

    print(f"Loaded {len(docs)} documents for accuracy testing.")

    settings = [(200, 33), (400, 67), (800, 133), (1200, 200), (2000, 333)]

    # Embed queries
    queries = [d["query"] for d in docs]
    query_embs = [embedding_service.embed_text(q) for q in queries]

    print("\n=== CHUNKING ACCURACY BENCHMARK ===")

    for size, overlap in settings:
        # Embed chunks for all documents
        doc_chunk_embs = []
        for d in docs:
            chunks = _chunk_text(d["text"], size, overlap)
            embs = [embedding_service.embed_text(c) for c in chunks]
            doc_chunk_embs.append(embs)

        correct_top1 = 0
        mrr_sum = 0
        avg_score_margin = 0

        for i, q_emb in enumerate(query_embs):
            q_emb_np = np.array([q_emb])
            doc_scores = []
            for d_embs in doc_chunk_embs:
                if len(d_embs) == 0:
                    doc_scores.append(0.0)
                    continue
                # Score is max cosine similarity across all chunks
                sims = cosine_similarity(q_emb_np, np.array(d_embs))[0]
                doc_scores.append(np.max(sims))

            # Sort documents by score descending
            ranked_indices = np.argsort(doc_scores)[::-1]

            rank = np.where(ranked_indices == i)[0][0] + 1
            if rank == 1:
                correct_top1 += 1
                margin = doc_scores[ranked_indices[0]] - doc_scores[ranked_indices[1]]
                avg_score_margin += margin
            mrr_sum += 1.0 / rank

        accuracy = correct_top1 / len(docs)
        mrr = mrr_sum / len(docs)
        margin = avg_score_margin / correct_top1 if correct_top1 > 0 else 0

        print(
            f"Setting {size:4d}/{overlap:3d} | Top-1 Acc: {accuracy * 100:5.1f}% | MRR: {mrr:.3f} | Avg Win Margin: {margin:.3f}"
        )


if __name__ == "__main__":
    run_accuracy_benchmark()
