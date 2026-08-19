import unittest
import tempfile
import os
import time
import shutil

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory

# The literal query the buried-needle fixture is built around.
QUERY = "What is the default timeout for the database connection retry backoff?"

# "RRF-favored, topically wrong" candidate: short, repeats the query's literal keywords heavily
# (timeout, database, connection, retry, backoff), so it wins on lexical/FTS overlap and on
# whole-document semantic similarity (short document, term-dense) -- but it never actually states
# the real answer; it's about a different specific question in the same vocabulary neighborhood.
WRONG_TITLE = "Database Connection Timeout Retry Backoff Configuration Reference"
WRONG_CONTENT = (
    "This document defines retry, timeout, and backoff behavior for push notification delivery "
    "to a mobile device. If a push notification times out because the target device is "
    "offline, the notification service retries delivery later using an exponential backoff "
    "schedule capped at twenty-four hours before finally giving up and marking that "
    "notification permanently undeliverable on that device. Badge counts and silent background "
    "pushes follow a completely separate, much shorter retry schedule than user-visible alert "
    "pushes do, and neither one is affected by the recipient's mobile carrier or network type. "
    "None of this push notification timeout, retry, or backoff logic applies to any other "
    "delivery channel this platform supports."
)

# "Buried needle" candidate: long, generic, misleadingly-titled document about a broad unrelated
# survey topic, with exactly one narrow paragraph deep inside that actually states the real
# numeric answer. Deliberately avoids repeating the query's literal keywords (timeout/retry/
# backoff) anywhere except inside that one buried paragraph.
NEEDLE_TITLE = "General Notes on Distributed Systems Reliability Patterns"
# Deliberately long and multi-paragraph -- longer than one chunk (CHUNK_SIZE_CHARS=1200) so the
# real answer paragraph below lands mostly isolated in its own chunk rather than being crammed
# together with every distractor paragraph into a single whole-document embedding. This is what
# makes the fixture actually test chunk-level isolation, not just whole-document dilution: a
# short NEEDLE_CONTENT would put 100% of the dilution burden on Stage-1's entity-level embedding
# and let Part B's chunk-level rerank see the exact same undiluted text Stage-1 already saw.
NEEDLE_CONTENT = "\n\n".join(
    [
        (
            "A circuit breaker tracks the failure rate of calls to a downstream dependency and, "
            "once it crosses a threshold, trips into an open state that fails fast without even "
            "attempting the call, giving the struggling downstream service time to recover from "
            "whatever is currently wrong with it, instead of piling on more load during an "
            "active outage that it cannot help. After a cooldown period it moves to a half-open "
            "state and lets exactly one trial request through to decide whether to close the "
            "circuit again or trip back open immediately."
        ),
        (
            "Load balancers distribute incoming traffic across a pool of healthy backend "
            "instances, removing an instance from rotation the moment its health check starts "
            "failing so that new requests are never routed to a node that cannot actually serve "
            "them correctly at all, ever. Weighted round robin and least-connections are the "
            "two most common selection strategies."
        ),
        (
            "Auto-scaling groups add or remove backend instances based on a rolling average of "
            "CPU and request-queue-depth metrics sampled every thirty seconds, smoothing out "
            "brief traffic spikes so the fleet doesn't thrash by scaling up and back down "
            "within the same minute."
        ),
        (
            # The actual answer to QUERY -- deliberately paraphrased (no literal "timeout",
            # "retry", or "backoff") so it doesn't win on lexical/FTS overlap either; it must win
            # the Stage-2 rerank on genuine chunk-level semantic similarity alone.
            "Our internal connection pool waits thirty seconds before it gives up on an "
            "unresponsive database session and schedules another attempt, doubling the wait "
            "before each subsequent attempt so repeated failures don't hammer an "
            "already-struggling database server further than it already is. This doubling "
            "continues for up to five attempts before the caller is finally told the session "
            "could not be established at all, at which point the calling service falls back to "
            "a cached response rather than blocking the end user any further while the database "
            "itself slowly recovers on its own schedule, monitored separately and continuously "
            "by an on-call engineer throughout the rest of that same overnight incident. None of "
            "this behavior is configurable per tenant today, though that has been requested "
            "repeatedly by the platform team, and a design doc proposing per-tenant overrides is "
            "currently under review by the database reliability working group before it can "
            "ship to production, since changing it incorrectly could make a struggling "
            "database's day worse."
        ),
        (
            "Bulkhead isolation partitions a service's resource pools -- threads, connections, "
            "memory -- so that one overwhelmed downstream dependency can't exhaust resources "
            "needed by unrelated request paths running in the same process at the same time, "
            "ever. Each tenant gets its own dedicated thread pool sized proportionally to its "
            "historical usage pattern."
        ),
        (
            "A health check endpoint should be cheap to evaluate and should reflect the actual "
            "ability to serve traffic, not just process liveness -- a process can be running "
            "while every real downstream dependency it needs is completely unreachable behind "
            "the scenes. Shallow liveness checks and deep readiness checks answer two genuinely "
            "different questions."
        ),
    ]
)

DISTRACTOR_TITLES_AND_CONTENT = [
    (
        "Weekend Sourdough Starter Maintenance Notes",
        "A well-rested sourdough starter should roughly double in volume within four to six "
        "hours of feeding, and smell pleasantly tangy rather than sharply acidic. Discard half "
        "before each feeding to keep the culture balanced.",
    ),
    (
        "Deep Sea Anglerfish Predation Strategy",
        "The bioluminescent anglerfish lures its prey using a modified dorsal spine tipped with "
        "light-producing bacteria, dangled just in front of its enormous jaws in the crushing "
        "dark of the deep ocean.",
    ),
    (
        "Perennial Garden Border Planning Guide",
        "Perennial borders benefit from a mix of early, mid, and late-season bloomers so the bed "
        "never looks bare. Deadhead spent flowers regularly to encourage a second flush.",
    ),
]


def _extract_id(result: dict) -> str:
    assert result["status"] == "ok", result
    return result["data"]["id"]


class TestE2ETopicRerank(unittest.TestCase):
    """Load-bearing test (Phase 2 Part B6): proves rerank_by_topic actually fixes the diagnosed
    length-dilution problem, not just that the plumbing doesn't crash. Real model, real async
    embedding pool, no mocking -- mirrors test_e2e_hybrid_search.py's scaffold."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title: str, content: str) -> str:
        res = store_memory(
            title=title,
            content=(
                f"# {title}\n\n{content}"
                if title == NEEDLE_TITLE
                else f"{content}\n\nThis note records the document scope."
            ),
            owner_id="test_user",
            db_path=self.db_path,
        )
        return _extract_id(res)

    def _poll_for_chunk_rows(self, entity_id: str, tries: int = 100, interval: float = 0.1):
        for _ in range(tries):
            rows = self.conn.execute(
                "SELECT 1 FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,)
            ).fetchall()
            if rows:
                return True
            from saltmdb.domain.services.embedding_service import process_embedding_jobs_sync

            process_embedding_jobs_sync(self.conn)
            time.sleep(interval)
        return False

    def test_rerank_by_topic_fixes_length_dilution_vs_baseline(self):
        wrong_id = self._store(WRONG_TITLE, WRONG_CONTENT)
        needle_id = self._store(NEEDLE_TITLE, NEEDLE_CONTENT)
        for title, content in DISTRACTOR_TITLES_AND_CONTENT:
            self._store(title, content)

        for eid in (wrong_id, needle_id):
            self.assertTrue(
                self._poll_for_chunk_rows(eid), f"chunk rows never appeared for entity {eid}"
            )
        # Also let entity-level (Stage-1) embeddings settle.
        time.sleep(0.5)

        baseline = search_memory(query_keywords=QUERY, limit=10, db_path=self.db_path)
        baseline_ids = [r["id"] for r in baseline]
        self.assertIn(wrong_id, baseline_ids, "wrong candidate should surface at all")
        self.assertIn(needle_id, baseline_ids, "needle candidate should surface at all")
        wrong_rank_baseline = baseline_ids.index(wrong_id)
        needle_rank_baseline = baseline_ids.index(needle_id)
        self.assertLessEqual(
            wrong_rank_baseline,
            needle_rank_baseline,
            "baseline (rerank_by_topic=False) must reproduce the length-dilution bug: the "
            "lexically-loaded but topically-wrong candidate ranks at or above the buried-needle "
            f"candidate that actually answers the query (baseline order: {baseline_ids})",
        )

        reranked = search_memory(
            query_keywords=QUERY, limit=10, rerank_by_topic=True, db_path=self.db_path
        )
        reranked_ids = [r["id"] for r in reranked]
        self.assertIn(wrong_id, reranked_ids)
        self.assertIn(needle_id, reranked_ids)
        wrong_rank_reranked = reranked_ids.index(wrong_id)
        needle_rank_reranked = reranked_ids.index(needle_id)
        self.assertLess(
            needle_rank_reranked,
            wrong_rank_reranked,
            "rerank_by_topic=True must strictly fix the dilution bug: the buried-needle "
            f"candidate must now outrank the topically-wrong one (reranked order: {reranked_ids})",
        )

        needle_item = next(r for r in reranked if r["id"] == needle_id)
        wrong_item = next(r for r in reranked if r["id"] == wrong_id)
        self.assertIn("topic_score", needle_item)
        self.assertIn("topic_score", wrong_item)
        self.assertGreater(
            needle_item["topic_score"],
            wrong_item["topic_score"],
            "needle candidate must have a strictly higher topic_score than the wrong candidate",
        )
        verdict_strength = {
            "SAME_SPECIFIC_TOPIC": 2,
            "BROADLY_RELATED_THEMES": 1,
            "DIFFERENT_TOPICS": 0,
        }
        self.assertGreaterEqual(
            verdict_strength[needle_item["semantic_verdict"]],
            verdict_strength[wrong_item["semantic_verdict"]],
            "needle candidate must have an equal-or-stronger semantic_verdict tier than the "
            f"wrong candidate (needle={needle_item['semantic_verdict']}, "
            f"wrong={wrong_item['semantic_verdict']})",
        )


if __name__ == "__main__":
    unittest.main()
