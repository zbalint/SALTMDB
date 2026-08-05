"""Threshold-calibration benchmark for Phase 2 Part B's `rerank_candidates_by_topic`
(search_memory Stage-2 cross-chunk reranking -- see plans/ and SALTMDB memory `5c09effa`).

The `plans/engineering_specification_2.md` pseudocode's illustrative verdict thresholds
(0.75 / 0.55) and candidate-pool size (20) are Gemini-generated placeholders, not measured
against this project's real `bge-small-en-v1.5` similarity distribution. This script computes
`Mean(Max(cosine_similarity))` -- exactly the algorithm `rerank_candidates_by_topic` implements
in SQL via `vec_distance_cosine` -- over real hand-labeled same-topic / related-theme / unrelated
pairs, using the real chunker (`saltmdb.utils.chunking.chunk_text`) and the real batched embedder
(`saltmdb.domain.services.embedding_service.embed_texts`), so the two `RERANK_*` threshold
constants in `config.py` are locked from actual measured separation, not taken on faith from the
spec -- mirroring how CHUNK_SIZE_CHARS/CHUNK_OVERLAP_CHARS were empirically settled across 3
benchmark rounds in Foundation.

Fixture design note: an earlier draft of this script reused whole project markdown docs
(AGENT_GUIDE.md et al.) cross-paired as "related_theme" candidates. That failed to separate from
"same_topic": every doc in this repo shares heavy project-specific vocabulary ("SALTMDB",
"install", "MCP"), so cross-doc similarity came out statistically indistinguishable from
same-doc similarity (same_topic mean 0.6656 vs. related_theme mean 0.6669 -- related_theme was
*higher*). Replaced with hand-crafted domain triplets below, each an independent (query,
same-topic candidate, related-but-different-topic candidate) trio within one technical domain
(e.g. "Python venv setup" vs. "pip dependency management" -- both Python tooling, but genuinely
different specific answers), plus a shared pool of fully unrelated candidate paragraphs. This
gives real control over topic *distance* per pair, which reusing whole heterogeneous docs did not.

Codex correction: `benchmark_embedding_accuracy3.py`'s Mean(Max(...)) math used
`sklearn.metrics.pairwise.cosine_similarity`, and `sklearn` is NOT a declared dependency anywhere
in pyproject.toml. This script is deliberately numpy-only (already a real, declared dependency,
used in librarian_service.py today): vectors are L2-normalized once, then cosine similarity is a
plain dot product.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it, for the same reason as its accuracy-benchmark siblings: it's a one-time
calibration pass, not a regression test, and directly instantiates the real embedding model.
"""

import numpy as np

from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from saltmdb.utils.chunking import chunk_text
from saltmdb.domain.services import embedding_service

# Each domain: a short search-memory-style query, a candidate paragraph specifically answering
# that query (same_topic), and a candidate paragraph from the SAME broad technical domain but
# addressing a genuinely different specific question (related_theme).
DOMAIN_TRIPLETS = [
    {
        "query": "How do I create and activate a Python virtual environment for a new project?",
        "same_topic": (
            "Run `python -m venv .venv` inside the project root to create an isolated virtual "
            "environment, then activate it with `source .venv/bin/activate` on Linux/macOS or "
            "`.venv\\Scripts\\activate` on Windows. Once activated, your shell prompt is "
            "prefixed with the environment name, and `pip install` writes packages into that "
            "isolated directory instead of the system-wide Python installation."
        ),
        "related_theme": (
            "Pin exact package versions in `requirements.txt` using `pip freeze > requirements.txt` "
            "so a teammate's `pip install -r requirements.txt` reproduces an identical dependency "
            "set. For libraries, prefer loose version ranges in `pyproject.toml`'s `dependencies` "
            "list instead, since a strict pin there can create unresolvable conflicts for whoever "
            "installs your package alongside other pinned dependencies."
        ),
    },
    {
        "query": "What SQL transaction isolation level prevents one transaction from seeing another's uncommitted dirty writes?",
        "same_topic": (
            "READ COMMITTED isolation guarantees a transaction never observes another "
            "transaction's uncommitted (dirty) writes -- each query only sees rows committed "
            "before that query began. This is the default isolation level in PostgreSQL and most "
            "production relational databases, trading off against the weaker READ UNCOMMITTED "
            "level, which permits dirty reads entirely."
        ),
        "related_theme": (
            "A composite B-tree index on (owner_id, created_at) lets the query planner satisfy "
            "both an equality filter on owner_id and a range/ORDER BY on created_at using a single "
            "index scan, avoiding a separate sort step. Column order in a composite index matters: "
            "putting the range-filtered column first would force the planner to scan a much wider "
            "slice of the index before applying the equality filter."
        ),
    },
    {
        "query": "How do I resolve a Git merge conflict when the same lines were changed on both branches?",
        "same_topic": (
            "When `git merge` reports a conflict, Git inserts `<<<<<<<`, `=======`, and `>>>>>>>` "
            "markers directly into the affected file showing both versions of the conflicting "
            "lines. Manually edit the file to keep the correct content, delete the conflict "
            "markers, then run `git add <file>` to mark it resolved and `git commit` to finish "
            "the merge."
        ),
        "related_theme": (
            "A feature-branch workflow keeps `main` always deployable: create a new branch off "
            "`main` for each unit of work, open a pull request when it's ready for review, and "
            "delete the branch after it merges. Long-lived branches that diverge from `main` for "
            "weeks tend to accumulate exactly the kind of overlapping changes that make merges "
            "painful later."
        ),
    },
    {
        "query": "Why is my HTTP API request failing with a 401 Unauthorized error?",
        "same_topic": (
            "A 401 Unauthorized response means the request is missing valid authentication "
            "credentials, most commonly an expired or malformed `Authorization: Bearer <token>` "
            "header. Check that the access token hasn't expired, that it's being sent on every "
            "request (not just the initial login call), and that the header name and `Bearer` "
            "prefix are spelled exactly as the API expects."
        ),
        "related_theme": (
            "An `ETag` response header lets a client send `If-None-Match` on a subsequent request "
            "so the server can return a cheap `304 Not Modified` instead of re-transmitting an "
            "unchanged resource body. This conditional-caching pattern significantly cuts bandwidth "
            "for clients that poll the same endpoint repeatedly, at the cost of one extra header "
            "round-trip per request."
        ),
    },
    {
        "query": "What causes a race condition when two threads write to the same shared variable without synchronization?",
        "same_topic": (
            "A race condition occurs when two threads read-modify-write a shared variable without "
            "a lock, and their operations interleave unpredictably: one thread's write can be lost "
            "if another thread's stale read overwrites it. Wrapping the critical section in a "
            "mutex ensures only one thread executes the read-modify-write sequence at a time, "
            "eliminating the interleaving."
        ),
        "related_theme": (
            "An `async`/`await` event loop runs coroutines cooperatively on a single thread: a "
            "coroutine only yields control at an explicit `await` point, so two coroutines never "
            "truly execute simultaneously the way OS threads can. This makes certain classes of "
            "data races structurally impossible, but a coroutine that never awaits can still "
            "starve every other coroutine sharing that event loop."
        ),
    },
    {
        "query": "How do I mock an external API call so a unit test doesn't hit the real network?",
        "same_topic": (
            "Use `unittest.mock.patch` to replace the function that performs the outbound HTTP "
            "call with a stand-in that returns a canned response object, so the test exercises "
            "your code's handling logic without ever touching the network. Patch the name as it's "
            "imported in the module under test, not where the function is originally defined, or "
            "the patch silently misses the real call site."
        ),
        "related_theme": (
            "Code coverage tools like `coverage.py` instrument the interpreter to record which "
            "lines actually executed during a test run, then report the percentage of the "
            "codebase exercised. A high coverage percentage doesn't guarantee correctness -- it "
            "only proves a line ran, not that its output was ever asserted against -- so coverage "
            "is best used to find completely untested code paths, not as a quality target on its "
            "own."
        ),
    },
    {
        "query": "What's the difference between a shallow copy and a deep copy of a nested Python object?",
        "same_topic": (
            "A shallow copy (`copy.copy`) creates a new outer container but still references the "
            "same nested objects as the original -- mutating a nested list inside the copy also "
            "mutates the original's nested list. A deep copy (`copy.deepcopy`) recursively "
            "duplicates every nested object too, so the copy and original share no mutable state "
            "at any depth."
        ),
        "related_theme": (
            "Python's garbage collector primarily relies on reference counting: an object is freed "
            "as soon as its reference count drops to zero. A supplementary generational cycle "
            "collector periodically scans for reference cycles (objects that reference each other "
            "but are unreachable from any root), which reference counting alone can never detect "
            "and would otherwise leak memory indefinitely."
        ),
    },
    {
        "query": "How do I configure a retry policy with exponential backoff for a flaky network call?",
        "same_topic": (
            "Wrap the network call in a retry loop that doubles the delay after each failed "
            "attempt -- e.g. 100ms, 200ms, 400ms -- up to a fixed maximum number of attempts, and "
            "add random jitter to the delay so many clients retrying simultaneously don't all "
            "collide on the same backoff schedule and overwhelm the server the moment it recovers."
        ),
        "related_theme": (
            "A circuit breaker tracks the failure rate of calls to a downstream dependency and, "
            "once it crosses a threshold, trips into an 'open' state that fails fast without even "
            "attempting the call, giving the struggling downstream service time to recover. After "
            "a cooldown period it moves to 'half-open' and lets a single trial request through to "
            "decide whether to close the circuit again."
        ),
    },
]

# Fully unrelated candidate paragraphs -- no shared vocabulary or domain with any triplet above.
UNRELATED_TEXTS = [
    "Preheat the oven to 350 degrees Fahrenheit before you begin. Cream the butter and sugar "
    "together until light and fluffy, then fold in the flour a little at a time. Chocolate chip "
    "cookies bake best on a lightly greased sheet for about eleven minutes, until the edges turn "
    "golden brown but the centers still look slightly underdone. Let them cool on a wire rack.",
    "The bioluminescent anglerfish lures its prey using a modified dorsal spine tipped with "
    "light-producing bacteria, dangled just in front of its enormous jaws in the crushing dark of "
    "the deep ocean. Most species live below a thousand meters, where sunlight never penetrates "
    "and pressure would crumple an unadapted body.",
    "Perennial borders benefit from a mix of early, mid, and late-season bloomers so the bed never "
    "looks bare. Deadhead spent flowers regularly to encourage a second flush, and divide "
    "overcrowded clumps of perennials like hostas or daylilies every three to four years in "
    "early spring or fall to keep them vigorous.",
    "Voyager 1 crossed the heliopause in August 2012, becoming the first human-made object to "
    "enter interstellar space. It still transmits data back to Earth using a 23-watt radio "
    "transmitter, though its plutonium power source continues to decay and onboard instruments "
    "are being shut down one by one to conserve the dwindling supply.",
    "A well-rested sourdough starter should roughly double in volume within four to six hours of "
    "feeding, and smell pleasantly tangy rather than sharply acidic. Discard half before each "
    "feeding to keep the culture balanced, and always feed with equal parts flour and water by "
    "weight for a predictable, repeatable rise.",
    "The Battle of Hastings in 1066 marked the last successful invasion of England, when William "
    "the Conqueror's Norman forces defeated King Harold II's army near the English coast. Harold "
    "was reportedly killed by an arrow to the eye, and the battle's outcome reshaped English "
    "aristocracy, language, and land ownership for centuries afterward.",
    "A cold front forms when a cooler, denser air mass advances and wedges beneath a warmer air "
    "mass, forcing it to rise rapidly -- this abrupt lifting often produces narrow bands of "
    "intense thunderstorms right along the frontal boundary, in contrast to the broader, gentler "
    "precipitation typically associated with a slower-moving warm front.",
    "A cello's four strings are tuned in fifths -- C, G, D, and A from lowest to highest -- and "
    "the instrument is played standing on the floor between the seated musician's knees, "
    "supported by an adjustable metal endpin. Its range overlaps the lower half of the violin's "
    "and the upper half of the double bass's, anchoring the middle and bass voices in a string "
    "quartet.",
]


def _mean_max_cosine(query_text: str, candidate_text: str) -> float:
    """Mean(Max(cosine_similarity(query_chunks, candidate_chunks))) -- numpy only, mirrors the
    exact aggregation rerank_candidates_by_topic implements via SQL's MIN(vec_distance_cosine)
    per query chunk (max-similarity-over-candidate-chunks), averaged across query chunks."""
    q_chunks = chunk_text(query_text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS) or [
        {"text": query_text}
    ]
    c_chunks = chunk_text(candidate_text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    if not c_chunks:
        c_chunks = [{"text": candidate_text}]

    q_vecs = np.array(embedding_service.embed_texts([c["text"] for c in q_chunks]))
    c_vecs = np.array(embedding_service.embed_texts([c["text"] for c in c_chunks]))

    q_norm = q_vecs / np.linalg.norm(q_vecs, axis=1, keepdims=True)
    c_norm = c_vecs / np.linalg.norm(c_vecs, axis=1, keepdims=True)

    sims = q_norm @ c_norm.T  # (num_query_chunks, num_candidate_chunks)
    max_per_query_chunk = np.max(sims, axis=1)  # "max" over candidate chunks
    return float(np.mean(max_per_query_chunk))  # "mean" over query chunks


def _bucket_stats(name: str, scores: list) -> None:
    arr = np.array(scores)
    print(
        f"  {name:15s} n={len(arr):2d}  min={arr.min():.4f}  mean={arr.mean():.4f}  "
        f"max={arr.max():.4f}  std={arr.std():.4f}"
    )


def run_threshold_calibration() -> dict:
    embedding_service.get_model()

    same_topic_scores = []
    related_theme_scores = []
    unrelated_scores = []

    for i, triplet in enumerate(DOMAIN_TRIPLETS):
        query = triplet["query"]
        same_topic_scores.append(_mean_max_cosine(query, triplet["same_topic"]))
        related_theme_scores.append(_mean_max_cosine(query, triplet["related_theme"]))
        unrelated_scores.append(_mean_max_cosine(query, UNRELATED_TEXTS[i % len(UNRELATED_TEXTS)]))

    print("\n=== RERANK THRESHOLD CALIBRATION (Mean(Max(CosineSimilarity))) ===")
    print(f"{len(DOMAIN_TRIPLETS)} hand-labeled domain triplets\n")
    _bucket_stats("same_topic", same_topic_scores)
    _bucket_stats("related_theme", related_theme_scores)
    _bucket_stats("unrelated", unrelated_scores)

    same_topic_min = min(same_topic_scores)
    related_theme_min = min(related_theme_scores)
    related_theme_max = max(related_theme_scores)
    unrelated_max = max(unrelated_scores)

    # Pick separators at the midpoint between adjacent buckets' observed extremes -- the same
    # "measured separation, not faith" approach used to settle CHUNK_SIZE_CHARS/CHUNK_OVERLAP_CHARS.
    same_topic_threshold = (same_topic_min + related_theme_max) / 2
    broad_theme_threshold = (related_theme_min + unrelated_max) / 2

    print("\n--- Derived thresholds ---")
    print(f"RERANK_SAME_TOPIC_THRESHOLD  = {same_topic_threshold:.4f}")
    print(f"RERANK_BROAD_THEME_THRESHOLD = {broad_theme_threshold:.4f}")

    return {
        "same_topic_scores": same_topic_scores,
        "related_theme_scores": related_theme_scores,
        "unrelated_scores": unrelated_scores,
        "same_topic_threshold": same_topic_threshold,
        "broad_theme_threshold": broad_theme_threshold,
    }


if __name__ == "__main__":
    run_threshold_calibration()
