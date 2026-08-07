"""Tests for Track A: the store-time disposition rewrite (see
scratch/plans/track_a_disposition_detailed.md and disposition_service.py).
"""

import json
import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.domain.services import disposition_service, memory_service
from saltmdb.domain.services.event_service import get_recent_events


def _near_dup(base: str, suffix: str) -> str:
    """Deterministic near-duplicate content generator -- same shape as the existing test suite's
    proven-to-flag fixtures (e.g. test_advanced_quality_features.py's 0.90-Jaccard pair)."""
    return f"{base} {suffix}".strip()


class DispositionServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title, content, **kwargs):
        return memory_service.store_memory(
            title=title,
            content=content,
            owner_id=kwargs.pop("owner_id", "agent1"),
            db_connection=self.conn,
            **kwargs,
        )

    def _resolve_one(self, review_required, disposition):
        """Convenience: resolve a single-candidate REVIEW_REQUIRED response with `disposition`."""
        cid = review_required["candidates"][0]["candidate_id"]
        return {"candidate_id": cid, "disposition": disposition}


class TestNoCandidatesPath(DispositionServiceTestBase):
    def test_clear_content_stores_immediately_unchanged(self):
        res = self._store(
            "Unique Fact About Kubernetes Autoscaling",
            "Horizontal pod autoscaling adjusts replica count based on observed CPU utilization metrics.",
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Knowledge stored successfully with ID: "))

    def test_no_legacy_events_ever_emitted(self):
        base = "The nofile ulimit setting was configured to 1048576 in etc security limits conf"
        self._store("Ulimit Base", base, skip_duplicate_check=True)
        self._store("Ulimit Near Dup", _near_dup(base, "today"))
        self._store("Ulimit Near Dup Two", _near_dup(base, "recently"))
        events = get_recent_events(db_connection=self.conn, limit=100)
        types = {e["type"] for e in events if isinstance(e, dict)}
        self.assertNotIn("consolidation_request", types)
        self.assertNotIn("supersession_candidate", types)


class TestPreflightSurfacing(DispositionServiceTestBase):
    def test_possible_duplicate_flags_review_required(self):
        base = "Production system nofile ulimit setting was configured to 1048576 in etc security limits conf"
        r1 = self._store("Ulimit Config", base, skip_duplicate_check=True)
        entity_id = r1.split("ID: ")[1].strip()

        r2 = self._store("Ulimit Config Copy", base + " today")
        self.assertIsInstance(r2, dict)
        self.assertEqual(r2["status"], "REVIEW_REQUIRED")
        self.assertIn("review_token", r2)
        self.assertEqual(len(r2["candidates"]), 1)
        cand = r2["candidates"][0]
        self.assertEqual(cand["target_entity_id"], entity_id)
        self.assertIn(cand["suggested_label"], ("possible_duplicate", "possible_supersession"))
        self.assertIn("heuristic_note", cand)
        self.assertIn("not a determination", cand["heuristic_note"])
        self.assertEqual(set(cand["available_dispositions"]), {"distinct", "supersede", "consolidate"})

    def test_correction_language_surfaces_possible_supersession(self):
        r1 = self._store(
            "Nginx Worker Processes Setting",
            "Nginx worker_processes directive is set to 4 in the main configuration block.",
            skip_duplicate_check=True,
        )
        entity_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Nginx Worker Processes Correction",
            "Correction: the nginx worker_processes directive is actually set to auto, not 4, in the main configuration block.",
        )
        self.assertIsInstance(r2, dict)
        cand = r2["candidates"][0]
        self.assertEqual(cand["target_entity_id"], entity_id)
        self.assertEqual(cand["suggested_label"], "possible_supersession")
        self.assertTrue(any("correction_language" in s for s in cand["evidence"]["matched_signals"]))

    def test_weak_similarity_alone_never_flags(self):
        self._store(
            "PostgreSQL Memory Configuration",
            "Configured PostgreSQL max_connections setting to 500 and shared_buffers to 4GB in postgresql.conf",
            skip_duplicate_check=True,
        )
        res = self._store(
            "Kubernetes Eviction Policy",
            "Kubernetes pod eviction policy triggers when memory pressure exceeds the configured node threshold.",
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Knowledge stored successfully"))

    def test_core_target_excludes_consolidate_from_available_dispositions(self):
        r1 = self._store(
            "Core Architecture Invariant",
            "SALTMDB never opens more than one SQLite connection per process by design invariant.",
            is_core=True,
            skip_duplicate_check=True,
        )
        entity_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Core Architecture Invariant Copy",
            "SALTMDB never opens more than one SQLite connection per process by design invariant today.",
        )
        self.assertIsInstance(r2, dict)
        cand = r2["candidates"][0]
        self.assertEqual(cand["target_entity_id"], entity_id)
        self.assertTrue(cand["target_is_core"])
        self.assertEqual(set(cand["available_dispositions"]), {"distinct", "supersede", "elaborate"})
        self.assertNotIn("consolidate", cand["available_dispositions"])


class TestCommitDispositions(DispositionServiceTestBase):
    def test_distinct_matches_unflagged_store_shape(self):
        r1 = self._store(
            "Distinct Base Fact",
            "The default HTTP timeout for the internal API gateway is 30 seconds.",
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Distinct Base Fact Similar",
            "The default HTTP timeout for the internal API gateway is 30 seconds today.",
        )
        self.assertIsInstance(r2, dict)

        r3 = self._store(
            "Distinct Base Fact Similar",
            "The default HTTP timeout for the internal API gateway is 30 seconds today.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "distinct")],
        )
        self.assertIsInstance(r3, str)
        self.assertTrue(r3.startswith("Knowledge stored successfully with ID: "))
        new_id = r3.split("ID: ")[1].strip()
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (new_id,)).fetchone()
        self.assertEqual(row[0], "raw")

    def test_supersede_disposition_creates_edge_no_weight_demotion(self):
        r1 = self._store(
            "OAuth Token Lifetime",
            "OAuth2 access tokens are configured to expire after 3600 seconds.",
            weight=5,
            skip_duplicate_check=True,
        )
        old_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "OAuth Token Lifetime Update",
            "OAuth2 access tokens are configured to expire after 3600 seconds now.",
        )
        r3 = self._store(
            "OAuth Token Lifetime Update",
            "OAuth2 access tokens are configured to expire after 3600 seconds now.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "supersede")],
        )
        self.assertTrue(r3.startswith("Knowledge stored successfully"))
        new_id = r3.split("ID: ")[1].strip()

        rel = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
            (new_id, old_id),
        ).fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel[0], "supersedes")

        old_weight = self.conn.execute(
            "SELECT weight FROM entities WHERE id = ?", (old_id,)
        ).fetchone()[0]
        self.assertEqual(old_weight, 5, "supersede must never auto-demote the target's weight")

    def test_elaborate_disposition_on_core_target_creates_elaborates_on_edge(self):
        r1 = self._store(
            "Core Rule About Redaction",
            "Secrets are redacted from all stored content before persistence, no exceptions.",
            is_core=True,
            skip_duplicate_check=True,
        )
        core_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Core Rule About Redaction Detail",
            "Secrets are redacted from all stored content before persistence, no exceptions today.",
        )
        r3 = self._store(
            "Core Rule About Redaction Detail",
            "Secrets are redacted from all stored content before persistence, no exceptions today.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "elaborate")],
        )
        self.assertTrue(r3.startswith("Knowledge stored successfully"))
        new_id = r3.split("ID: ")[1].strip()

        rel = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
            (new_id, core_id),
        ).fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel[0], "elaborates_on")

        core_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (core_id,)
        ).fetchone()[0]
        self.assertEqual(core_status, "raw", "elaborate must never archive/consolidate a core target")

    def test_consolidate_disposition_no_temp_raw_node_ever_created(self):
        r1 = self._store(
            "Redis Eviction Policy",
            "Redis maxmemory-policy is configured to allkeys-lru for the cache cluster.",
            skip_duplicate_check=True,
        )
        old_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Redis Eviction Policy Refresh",
            "Redis maxmemory-policy is configured to allkeys-lru for the cache cluster now.",
        )
        r3 = self._store(
            "Redis Eviction Policy Refresh",
            "Redis maxmemory-policy is configured to allkeys-lru for the cache cluster now.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "consolidate")],
        )
        self.assertTrue(r3.startswith("Successfully committed") or r3.startswith("Knowledge stored"))
        new_id = r3.split("ID: ")[1].strip()

        new_row = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (new_id,)
        ).fetchone()
        self.assertEqual(new_row[0], "consolidated")

        old_row = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (old_id,)
        ).fetchone()
        self.assertEqual(old_row[0], "archived")

        # No stray raw row for the "Redis Eviction Policy Refresh" content ever exists.
        stray = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE title = ? AND status = 'raw'",
            ("Redis Eviction Policy Refresh",),
        ).fetchone()[0]
        self.assertEqual(stray, 0)

    def test_missing_disposition_for_flagged_candidate_rejected(self):
        r1 = self._store(
            "Missing Disposition Base",
            "The deploy pipeline runs integration tests before every production release.",
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Missing Disposition Copy",
            "The deploy pipeline runs integration tests before every production release today.",
        )
        res = self._store(
            "Missing Disposition Copy",
            "The deploy pipeline runs integration tests before every production release today.",
            review_token=r2["review_token"],
            dispositions=[],
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Error"))

    def test_core_restricted_disposition_rejected_even_if_client_forces_it(self):
        """Defense in depth: even if a caller sends 'consolidate' for a core-flagged candidate
        (ignoring the offered available_dispositions), the server rejects it independently."""
        r1 = self._store(
            "Core Invariant Two",
            "Every write transaction uses BEGIN IMMEDIATE to acquire the write lock up front.",
            is_core=True,
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Core Invariant Two Copy",
            "Every write transaction uses BEGIN IMMEDIATE to acquire the write lock up front today.",
        )
        res = self._store(
            "Core Invariant Two Copy",
            "Every write transaction uses BEGIN IMMEDIATE to acquire the write lock up front today.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "consolidate")],
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Error"))


class TestReviewStale(DispositionServiceTestBase):
    def test_stale_on_concurrent_target_modification(self):
        r1 = self._store(
            "Stale Target Base",
            "The build cache is stored under the .cache directory at the repo root.",
            skip_duplicate_check=True,
        )
        old_id = r1.split("ID: ")[1].strip()
        r2 = self._store(
            "Stale Target Copy",
            "The build cache is stored under the .cache directory at the repo root today.",
        )
        # Simulate concurrent modification of the flagged target between preflight and commit.
        self.conn.execute(
            "UPDATE entities SET content_hash = 'deliberately-changed' WHERE id = ?", (old_id,)
        )
        self.conn.commit()

        res = self._store(
            "Stale Target Copy",
            "The build cache is stored under the .cache directory at the repo root today.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "distinct")],
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "REVIEW_STALE")

    def test_stale_on_content_mismatch_at_commit(self):
        r1 = self._store(
            "Fingerprint Base",
            "The scheduler retries a failed job up to 3 times before marking it dead.",
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Fingerprint Copy",
            "The scheduler retries a failed job up to 3 times before marking it dead today.",
        )
        res = self._store(
            "Fingerprint Copy CHANGED",  # different title -> fingerprint mismatch
            "The scheduler retries a failed job up to 3 times before marking it dead today.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "distinct")],
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "REVIEW_STALE")

    def test_stale_on_expired_token(self):
        r1 = self._store(
            "Expiry Base",
            "The connection pool maximum size is set to 20 for the primary database.",
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Expiry Copy",
            "The connection pool maximum size is set to 20 for the primary database today.",
        )
        token_payload = disposition_service._decode_review_token(r2["review_token"])
        token_payload["expires_at"] = "2000-01-01T00:00:00+00:00"
        expired_token = disposition_service._encode_review_token(token_payload)

        res = self._store(
            "Expiry Copy",
            "The connection pool maximum size is set to 20 for the primary database today.",
            review_token=expired_token,
            dispositions=[self._resolve_one(r2, "distinct")],
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "REVIEW_STALE")


class TestCodexImplementationReviewFixes(DispositionServiceTestBase):
    """Regression coverage for findings from the independent Codex implementation review of
    Track A (see scratch/plans/track_a_disposition_detailed.md's implementation history)."""

    def test_review_required_includes_target_status(self):
        self._store(
            "Status Field Base",
            "The rate limiter allows 100 requests per minute per API key by default.",
            skip_duplicate_check=True,
        )
        res = self._store(
            "Status Field Copy",
            "The rate limiter allows 100 requests per minute per API key by default today.",
        )
        self.assertIsInstance(res, dict)
        self.assertIn("target_status", res["candidates"][0])
        self.assertEqual(res["candidates"][0]["target_status"], "raw")

    def test_consolidated_target_with_correction_language_still_flags(self):
        """Finding #1: a consolidated-status target that ALSO clears the correction-language bar
        must still be flagged as possible_supersession, not silently absorbed into the
        integrity-only check (which only fires on cohesion drift)."""
        parent = self._store(
            "Consolidation Parent",
            "The staging environment database uses a single replica for cost reasons.",
            skip_duplicate_check=True,
        )
        parent_id = parent.split("ID: ")[1].strip()

        from saltmdb.domain.services.relation_service import commit_consolidation

        cons_res = commit_consolidation(
            parent_ids=[parent_id],
            title="Consolidated Staging DB Note",
            content="The staging environment database uses a single replica for cost reasons.",
            owner_id="agent1",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", cons_res)
        consolidated_id = cons_res.split("ID: ")[1].strip()

        # Highly cohesive with the consolidated node's own content (so the integrity/drift check
        # alone would NOT fire) but carries explicit correction language.
        res = self._store(
            "Staging DB Correction",
            "Correction: the staging environment database uses a single replica for cost reasons.",
        )
        self.assertIsInstance(res, dict)
        cand = res["candidates"][0]
        self.assertEqual(cand["target_entity_id"], consolidated_id)
        self.assertEqual(cand["suggested_label"], "possible_supersession")
        self.assertTrue(any("correction_language" in s for s in cand["evidence"]["matched_signals"]))

    def test_multi_candidate_mixed_dispositions_one_atomic_commit(self):
        """Two independent flagged candidates in one preflight, resolved to different concrete
        dispositions, committed as one atomic transaction (reconciliation §1.3). Constructed via
        a synthetic token (same technique as the parent-cap test) rather than relying on real
        evidence-gathering to organically surface two candidates in one call -- deterministic,
        not dependent on the embedder's actual similarity behavior."""
        r1 = self._store(
            "Multi Candidate Supersede Target",
            "The CDN cache TTL for static assets is set to 86400 seconds.",
            skip_duplicate_check=True,
        )
        supersede_target = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Multi Candidate Consolidate Target",
            "The load balancer health check interval is configured to 10 seconds.",
            skip_duplicate_check=True,
        )
        consolidate_target = r2.split("ID: ")[1].strip()

        proposed = {
            # Deliberately close to supersede_target's own wording -- store_relation's
            # RELATION_GATE_MIN_SIMILARITY_THRESHOLD gate (a pre-existing, Track-A-unrelated
            # governance check on "strong" predicates like supersedes) requires the two ends of
            # the edge to actually be substantively related; a real flagged candidate always
            # clears this naturally (it already cleared the stricter 0.75 dedup bar to be flagged
            # at all), so this synthetic fixture mirrors that by construction.
            "content": "The CDN cache TTL for static assets is set to 86400 seconds, consolidated with the load balancer health check interval notes.",
            "title": "Multi Candidate Combined",
            "tags": None,
            "owner_id": "agent1",
            "scope": "shared",
            "memory_type": None,
            "context_id": None,
            "is_core": None,
            "weight": 1,
            "metadata": None,
            "resolved_entity_id": None,
        }

        def _hash_of(entity_id):
            return self.conn.execute(
                "SELECT content_hash FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()[0]

        candidates = [
            {
                "candidate_id": "c1",
                "target_entity_id": supersede_target,
                "target_content_hash": _hash_of(supersede_target),
                "target_status": "raw",
                "target_is_core": False,
                "available_dispositions": ["distinct", "supersede", "consolidate"],
            },
            {
                "candidate_id": "c2",
                "target_entity_id": consolidate_target,
                "target_content_hash": _hash_of(consolidate_target),
                "target_status": "raw",
                "target_is_core": False,
                "available_dispositions": ["distinct", "supersede", "consolidate"],
            },
        ]
        token_payload = {
            "v": 1,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "proposed_write_fingerprint": disposition_service._compute_write_fingerprint(proposed),
            "resolved_entity_id": None,
            "candidates": candidates,
        }
        token = disposition_service._encode_review_token(token_payload)

        final = disposition_service.commit_disposed_write(
            self.conn,
            proposed,
            token,
            [
                {"candidate_id": "c1", "disposition": "supersede"},
                {"candidate_id": "c2", "disposition": "consolidate"},
            ],
            self.db_path,
        )
        self.assertIsInstance(final, str)
        self.assertFalse(final.startswith("Error"))
        new_id = final.split("ID: ")[-1].strip()

        rel = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
            (new_id, supersede_target),
        ).fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel[0], "supersedes")

        consolidate_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (consolidate_target,)
        ).fetchone()[0]
        self.assertEqual(consolidate_status, "archived")

        # The single output entity is the consolidated node (since a "consolidate" disposition
        # was present) -- one output entity per call, per the composition rule (Track A plan §3).
        new_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (new_id,)
        ).fetchone()[0]
        self.assertEqual(new_status, "consolidated")

    def test_live_is_core_change_after_preflight_rejects_stale_disposition_choice(self):
        """A candidate that was non-core at preflight time but becomes core before commit must be
        classified REVIEW_STALE at commit time (Codex round-2 implementation-review fix: is_core
        is token state the caller's decision was based on, revalidated the same way as content_
        hash/status -- not merely rejected as an opaque disposition error once discovered)."""
        r1 = self._store(
            "Core Flip Base",
            "The message queue retry backoff starts at 1 second and doubles up to 60 seconds.",
            skip_duplicate_check=True,
        )
        target_id = r1.split("ID: ")[1].strip()

        r2 = self._store(
            "Core Flip Copy",
            "The message queue retry backoff starts at 1 second and doubles up to 60 seconds now.",
        )
        self.assertIsInstance(r2, dict)
        self.assertIn("consolidate", r2["candidates"][0]["available_dispositions"])

        # Flip the target to core AFTER preflight, before commit.
        self.conn.execute("UPDATE entities SET is_core = 1 WHERE id = ?", (target_id,))
        self.conn.commit()

        res = self._store(
            "Core Flip Copy",
            "The message queue retry backoff starts at 1 second and doubles up to 60 seconds now.",
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "consolidate")],
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "REVIEW_STALE")
        self.assertIn(r2["candidates"][0]["candidate_id"], res["stale_candidate_ids"])

        # The now-core target must not have been archived/consolidated.
        status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (target_id,)
        ).fetchone()[0]
        self.assertEqual(status, "raw")

    def test_consolidate_disposition_preserves_metadata_and_memory_type(self):
        """Additional correctness fix: a 'consolidate' disposition must not silently drop the
        proposed write's metadata/memory_type (commit_consolidation previously had no columns
        for either)."""
        r1 = self._store(
            "Metadata Preservation Base",
            "The API gateway enforces a 30 second upstream timeout for all proxied requests.",
            memory_type="procedure",  # must match r2's memory_type for the type-compatibility gate
            skip_duplicate_check=True,
        )
        r2 = self._store(
            "Metadata Preservation Copy",
            "The API gateway enforces a 30 second upstream timeout for all proxied requests now.",
            memory_type="procedure",
            metadata={"runbook_id": "gw-timeout-001"},
        )
        self.assertIsInstance(r2, dict)

        res = self._store(
            "Metadata Preservation Copy",
            "The API gateway enforces a 30 second upstream timeout for all proxied requests now.",
            memory_type="procedure",
            metadata={"runbook_id": "gw-timeout-001"},
            review_token=r2["review_token"],
            dispositions=[self._resolve_one(r2, "consolidate")],
        )
        self.assertTrue(res.startswith("Successfully committed") or res.startswith("Knowledge stored"))
        new_id = res.split("ID: ")[1].strip()

        row = self.conn.execute(
            "SELECT memory_type, metadata FROM entities WHERE id = ?", (new_id,)
        ).fetchone()
        self.assertEqual(row[0], "procedure")
        self.assertIsNotNone(row[1])
        self.assertEqual(json.loads(row[1])["runbook_id"], "gw-timeout-001")


class TestConsolidationParentCap(DispositionServiceTestBase):
    def test_exceeding_max_consolidation_parents_rejected(self):
        from saltmdb.config import MAX_CONSOLIDATION_REQUEST_SIZE

        base = "Distinct fixture entity number"
        ids = []
        for i in range(MAX_CONSOLIDATION_REQUEST_SIZE + 1):
            r = self._store(f"Cap Fixture {i}", f"{base} {i} with unique padding text to satisfy the quality gate.", skip_duplicate_check=True)
            ids.append(r.split("ID: ")[1].strip())

        candidates = [
            {
                "candidate_id": f"c{i + 1}",
                "target_entity_id": tid,
                "target_content_hash": self.conn.execute(
                    "SELECT content_hash FROM entities WHERE id = ?", (tid,)
                ).fetchone()[0],
                "target_status": "raw",
                "target_is_core": False,
                "available_dispositions": ["distinct", "supersede", "consolidate"],
            }
            for i, tid in enumerate(ids)
        ]
        proposed = {
            "content": "Synthesized merge content for the cap test.",
            "title": "Cap Fixture Merge",
            "tags": None,
            "owner_id": "agent1",
            "scope": "shared",
            "memory_type": None,
            "context_id": None,
            "is_core": None,
            "weight": 1,
            "metadata": None,
            "resolved_entity_id": None,
        }
        token_payload = {
            "v": 1,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "proposed_write_fingerprint": disposition_service._compute_write_fingerprint(proposed),
            "resolved_entity_id": None,
            "candidates": candidates,
        }
        token = disposition_service._encode_review_token(token_payload)
        dispositions = [{"candidate_id": c["candidate_id"], "disposition": "consolidate"} for c in candidates]

        res = disposition_service.commit_disposed_write(
            self.conn, proposed, token, dispositions, self.db_path
        )
        self.assertIsInstance(res, str)
        self.assertIn("REJECT_TOO_MANY_CONSOLIDATION_PARENTS", res)


if __name__ == "__main__":
    unittest.main()
