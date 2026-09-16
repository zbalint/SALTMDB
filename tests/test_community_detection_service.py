import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import numpy as np
import sqlite_vec

from saltmdb.domain.services import community_detection_service
from saltmdb.config import COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S
from saltmdb.db.schema import init_db
from saltmdb.domain.services.community_detection_service import (
    _compute_community_group,
    _run_community_detection_pass_impl,
    recompute_communities,
    trigger_community_detection,
)
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import invalidate_relation, store_relation

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list[float]:
    """Unit basis vector with exactly known cosine similarities."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _memory_id(result) -> str:
    if isinstance(result, dict):
        return result["data"]["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class _ImmediateCoordinator:
    def __init__(self, conn):
        self.conn = conn
        self.submissions = []

    def submit(self, name, operation, *, priority, wait=True):
        self.submissions.append((name, priority, wait))
        return operation(self.conn)


class TestCommunityDetectionService(unittest.TestCase):
    _test_mode: Any = None
    _embedding_jobs: Any = None
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = None  # pyright: ignore[reportAssignmentType]

    def setUp(self):
        self._test_mode = patch.dict(os.environ, {"SALTMDB_TEST_MODE": "1"}, clear=False)
        self._test_mode.start()
        self._embedding_jobs = patch.multiple(
            "saltmdb.domain.services.embedding_service",
            enqueue_embedding_jobs_for_entity=lambda *args, **kwargs: None,
            enqueue_retrieval_embedding_job_for_entity=lambda *args, **kwargs: None,
        )
        self._embedding_jobs.start()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._embedding_jobs.stop()
        self._test_mode.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _new_entity(self, label: str) -> str:
        result = store_memory(
            content=(
                f"Community detection fixture record for {label}. This sentence provides "
                "durable test content for a graph entity."
            ),
            title=f"Community fixture {label}",
            owner_id="community-test",
            db_connection=self.conn,
        )
        return _memory_id(result)

    def _insert_vector(self, entity_id: str, vector: list[float]) -> None:
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, sqlite_vec.serialize_float32(vector)),
        )
        self.conn.commit()

    def _insert_relation(
        self,
        source_id: str,
        target_id: str,
        predicate: str,
        *,
        valid_from: str | None = None,
        valid_to: str | None = None,
        valid_at: str | None = None,
        invalid_at: str | None = None,
    ) -> str:
        relation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO relations
                (id, source_id, target_id, predicate, created_at, valid_from, valid_to,
                 valid_at, invalid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                source_id,
                target_id,
                predicate,
                now,
                valid_from if valid_from is not None else now,
                valid_to,
                valid_at if valid_at is not None else now,
                invalid_at,
            ),
        )
        self.conn.commit()
        return relation_id

    def _store_edge(self, source_id: str, target_id: str, predicate: str) -> str:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertFalse(result.startswith("Error"), result)
        return result

    def _communities(self):
        return self.conn.execute(
            "SELECT id, representative_entity_id, member_count FROM communities ORDER BY id"
        ).fetchall()

    def _membership(self):
        rows = self.conn.execute(
            "SELECT entity_id, community_id FROM community_membership ORDER BY entity_id"
        ).fetchall()
        result = {}
        for entity_id, community_id in rows:
            result.setdefault(community_id, set()).add(entity_id)
        return result

    def _embedding(self, community_id: str) -> np.ndarray:
        blob = self.conn.execute(
            "SELECT embedding FROM community_embeddings WHERE community_id = ?", (community_id,)
        ).fetchone()[0]
        return np.frombuffer(blob, dtype=np.float32)

    def test_scenario_01_empty_graph_clears_community_state(self):
        result = recompute_communities(db_connection=self.conn)
        self.assertEqual(result, {"status": "no_edges", "communities_created": 0})
        for table in ("communities", "community_membership", "community_embeddings"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_scenario_02_two_connected_entities_form_one_community(self):
        a, b = self._new_entity("two-a"), self._new_entity("two-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))
        self._insert_vector(b, _axis_vector(1))

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["status"], "recomputed")
        self.assertEqual(result["communities_created"], 1)
        self.assertEqual(result["nodes_clustered"], 2)
        row = self._communities()[0]
        self.assertEqual(row[2], 2)
        membership = self._membership()
        self.assertEqual(next(iter(membership.values())), {a, b})
        centroid = self._embedding(row[0])
        self.assertAlmostEqual(float(centroid[0]), 1 / np.sqrt(2), places=5)
        self.assertAlmostEqual(float(centroid[1]), 1 / np.sqrt(2), places=5)

    def test_scenario_03_zero_edge_entity_is_not_clustered(self):
        a, b, orphan = (
            self._new_entity("zero-a"),
            self._new_entity("zero-b"),
            self._new_entity("zero-orphan"),
        )
        self._store_edge(a, b, "depends_on")
        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["nodes_clustered"], 2)
        members = next(iter(self._membership().values()))
        self.assertEqual(members, {a, b})
        self.assertNotIn(orphan, members)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM community_membership WHERE entity_id = ?", (orphan,)
            ).fetchone()[0],
            0,
        )

    def test_scenario_04_every_predicate_counts_including_reserved_lifecycle_edges(self):
        pairs = []
        for label, predicate in (
            ("related", "related_to"),
            ("contradictory", "contradicts"),
            ("lifecycle", "supersedes"),
        ):
            a, b = (
                self._new_entity(f"predicate-{label}-a"),
                self._new_entity(f"predicate-{label}-b"),
            )
            if predicate == "supersedes":
                self._insert_relation(a, b, predicate)
            else:
                self._store_edge(a, b, predicate)
            pairs.append({a, b})

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["communities_created"], 3)
        self.assertEqual(
            {frozenset(members) for members in self._membership().values()},
            {frozenset(pair) for pair in pairs},
        )

    def test_scenario_05_multiple_predicates_collapse_to_one_undirected_edge(self):
        a, b = self._new_entity("multi-a"), self._new_entity("multi-b")
        self._store_edge(a, b, "depends_on")
        self._store_edge(a, b, "related_to")

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["communities_created"], 1)
        self.assertEqual(self._communities()[0][2], 2)
        self.assertEqual(next(iter(self._membership().values())), {a, b})

    def test_scenario_06_disconnected_components_form_separate_communities(self):
        a, b, c, d = (
            self._new_entity("component-a"),
            self._new_entity("component-b"),
            self._new_entity("component-c"),
            self._new_entity("component-d"),
        )
        self._store_edge(a, b, "depends_on")
        self._store_edge(c, d, "depends_on")

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["communities_created"], 2)
        self.assertEqual([row[2] for row in self._communities()], [2, 2])
        self.assertEqual(
            {frozenset(members) for members in self._membership().values()},
            {
                frozenset({a, b}),
                frozenset({c, d}),
            },
        )

    def test_scenario_07_hub_is_page_rank_representative(self):
        hub = self._new_entity("hub")
        spokes = [self._new_entity(f"spoke-{i}") for i in range(4)]
        for spoke in spokes:
            self._store_edge(hub, spoke, "depends_on")

        recompute_communities(db_connection=self.conn)

        self.assertEqual(len(self._communities()), 1)
        self.assertEqual(self._communities()[0][1], hub)

    def test_scenario_08_page_rank_tie_uses_cosine_distance_to_centroid(self):
        members = [self._new_entity(f"tie-centroid-{i}") for i in range(3)]
        for left, right in (
            (members[0], members[1]),
            (members[1], members[2]),
            (members[2], members[0]),
        ):
            self._store_edge(left, right, "related_to")
        lowest_id = min(members)
        self._insert_vector(lowest_id, _axis_vector(1))
        for entity_id in members:
            if entity_id != lowest_id:
                self._insert_vector(entity_id, _axis_vector(0))

        recompute_communities(db_connection=self.conn)

        representative_id = self._communities()[0][1]
        expected_representative = min(entity_id for entity_id in members if entity_id != lowest_id)
        self.assertEqual(representative_id, expected_representative)

    def test_scenario_09_lowest_id_breaks_full_representative_tie_and_is_stable(self):
        a, b, c = (
            self._new_entity("tie-id-a"),
            self._new_entity("tie-id-b"),
            self._new_entity("tie-id-c"),
        )
        for left, right in ((a, b), (b, c), (c, a)):
            self._store_edge(left, right, "related_to")
        for entity_id in (a, b, c):
            self._insert_vector(entity_id, _axis_vector(0))

        recompute_communities(db_connection=self.conn)
        first = self._communities()[0]
        first_members = next(iter(self._membership().values()))
        recompute_communities(db_connection=self.conn)
        second = self._communities()[0]
        second_members = next(iter(self._membership().values()))

        self.assertEqual(first[1], min(a, b, c))
        self.assertEqual(second[1], min(a, b, c))
        self.assertEqual(first[1:], second[1:])
        self.assertEqual(first_members, second_members)
        self.assertNotEqual(first[0], second[0])

    def test_scenario_10_singleton_branch_self_assigns_and_normalizes_embedding(self):
        a, b = self._new_entity("singleton-a"), self._new_entity("singleton-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))
        self._insert_vector(b, _axis_vector(1))

        # The locked spec permits directly supplying a partition-shaped singleton to cover this
        # branch because a genuine singleton is not reliably produced by the real algorithm on
        # sparse connected graphs. This calls the production per-community seam directly without
        # mocking igraph or leidenalg.
        import igraph as ig

        graph = ig.Graph(n=2, edges=[(0, 1)], directed=False)
        member_ids, representative_id, centroid = _compute_community_group(
            [0],
            graph,
            [a, b],
            {a: np.asarray(_axis_vector(0), dtype=np.float32)},
        )

        self.assertEqual(member_ids, [a])
        self.assertEqual(representative_id, a)
        assert isinstance(centroid, np.ndarray)
        np.testing.assert_allclose(centroid, np.asarray(_axis_vector(0)), atol=1e-5)

    def test_scenario_11_one_missing_embedding_degrades_gracefully(self):
        a, b = self._new_entity("missing-one-a"), self._new_entity("missing-one-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["communities_missing_embedding"], 0)
        community_id, representative_id, member_count = self._communities()[0]
        self.assertEqual(member_count, 2)
        self.assertEqual(representative_id, a)
        self.assertEqual(next(iter(self._membership().values())), {a, b})
        np.testing.assert_allclose(
            self._embedding(community_id), np.asarray(_axis_vector(0)), atol=1e-5
        )

    def test_scenario_12_all_missing_embeddings_preserve_structure_without_vector(self):
        a, b = self._new_entity("missing-all-a"), self._new_entity("missing-all-b")
        self._store_edge(a, b, "depends_on")

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["communities_created"], 1)
        self.assertEqual(result["communities_missing_embedding"], 1)
        community_id = self._communities()[0][0]
        self.assertEqual(next(iter(self._membership().values())), {a, b})
        self.assertIsNone(
            self.conn.execute(
                "SELECT embedding FROM community_embeddings WHERE community_id = ?",
                (community_id,),
            ).fetchone()
        )

    def test_scenario_13_full_recompute_clears_state_after_invalidation(self):
        a, b = self._new_entity("clear-a"), self._new_entity("clear-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))
        self._insert_vector(b, _axis_vector(1))
        recompute_communities(db_connection=self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0], 1)

        invalidated = invalidate_relation(
            source_id=a,
            target_id=b,
            predicate="depends_on",
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertTrue(invalidated.startswith("Relation invalidated"), invalidated)
        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result, {"status": "no_edges", "communities_created": 0})
        for table in ("communities", "community_membership", "community_embeddings"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_scenario_14_community_id_changes_across_identical_recomputes(self):
        a, b = self._new_entity("unstable-a"), self._new_entity("unstable-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))
        self._insert_vector(b, _axis_vector(1))

        recompute_communities(db_connection=self.conn)
        first_id, first_rep, first_count = self._communities()[0]
        first_members = next(iter(self._membership().values()))
        recompute_communities(db_connection=self.conn)
        second_id, second_rep, second_count = self._communities()[0]
        second_members = next(iter(self._membership().values()))

        self.assertNotEqual(first_id, second_id)
        self.assertEqual((first_rep, first_count), (second_rep, second_count))
        self.assertEqual(first_members, second_members)

    def test_scenario_15_archived_endpoint_is_excluded_from_clustering(self):
        archived, live, other = (
            self._new_entity("archived"),
            self._new_entity("live"),
            self._new_entity("other"),
        )
        self.conn.execute(
            "UPDATE entities SET status = 'archived', embedding_status = 'archived' WHERE id = ?",
            (archived,),
        )
        self.conn.commit()
        self._insert_relation(archived, live, "depends_on")
        self._store_edge(live, other, "depends_on")

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["nodes_clustered"], 2)
        members = next(iter(self._membership().values()))
        self.assertEqual(members, {live, other})
        self.assertNotIn(archived, members)

    def test_scenario_16_bitemporally_invalid_relations_are_excluded(self):
        a, b, c, d = (
            self._new_entity("expired-a"),
            self._new_entity("expired-b"),
            self._new_entity("invalid-a"),
            self._new_entity("invalid-b"),
        )
        past = "2020-01-01T00:00:00+00:00"
        self._insert_relation(a, b, "depends_on", valid_to=past)
        self._insert_relation(c, d, "related_to", invalid_at=past)

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result, {"status": "no_edges", "communities_created": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0], 0)

    def test_scenario_17_test_mode_suppresses_trigger_submission(self):
        with patch(
            "saltmdb.domain.services.librarian_service._librarian_trigger_pool.submit"
        ) as submit:
            trigger_community_detection(self.db_path)
        submit.assert_not_called()

    def test_scenario_18_dedicated_disable_flag_is_independent(self):
        os.environ.pop("SALTMDB_TEST_MODE", None)
        with (
            patch.dict(
                os.environ,
                {"SALTMDB_DISABLE_COMMUNITY_DETECTION": "1"},
                clear=False,
            ),
            patch(
                "saltmdb.domain.services.librarian_service._librarian_trigger_pool.submit"
            ) as submit,
        ):
            trigger_community_detection(self.db_path)
        submit.assert_not_called()

    def test_scenario_19_immediate_worker_calls_collapse_to_one_cooldown_winner(self):
        a, b = self._new_entity("cooldown-a"), self._new_entity("cooldown-b")
        self._store_edge(a, b, "depends_on")

        first = _run_community_detection_pass_impl(self.db_path)
        second = _run_community_detection_pass_impl(self.db_path)

        self.assertIn("Community detection pass complete", first)
        self.assertEqual(second, "Skipped: cooldown not elapsed.")
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
            ).fetchone()[0]
        )

    def test_scenario_20_worker_claim_succeeds_after_cooldown_window(self):
        a, b = self._new_entity("cooldown-expired-a"), self._new_entity("cooldown-expired-b")
        self._store_edge(a, b, "depends_on")
        first = _run_community_detection_pass_impl(self.db_path)
        self.assertIn("Community detection pass complete", first)
        old_timestamp = (
            datetime.now(UTC) - timedelta(seconds=COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S + 1)
        ).isoformat()
        self.conn.execute(
            "UPDATE _system_locks SET last_run_at = ? WHERE task_name = 'community_detection'",
            (old_timestamp,),
        )
        self.conn.commit()

        second = _run_community_detection_pass_impl(self.db_path)

        self.assertIn("Community detection pass complete", second)

    def test_scenario_21_no_edges_skip_precedes_cooldown_claim(self):
        before = self.conn.execute(
            "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
        ).fetchone()[0]

        result = _run_community_detection_pass_impl(self.db_path)

        after = self.conn.execute(
            "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
        ).fetchone()[0]
        self.assertEqual(result, "Skipped: no qualifying relation edges to cluster.")
        self.assertEqual(after, before)

    def test_scenario_25_missing_vector_extension_preserves_structure_and_gates_vec0(self):
        a, b = self._new_entity("extension-missing-a"), self._new_entity("extension-missing-b")
        self._store_edge(a, b, "depends_on")
        self._insert_vector(a, _axis_vector(0))
        self._insert_vector(b, _axis_vector(1))

        with patch(
            "saltmdb.domain.services.community_detection_service.try_load_vector_extension",
            return_value=False,
        ):
            result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["status"], "recomputed")
        self.assertEqual(result["communities_created"], 1)
        self.assertEqual(result["communities_missing_embedding"], result["communities_created"])
        self.assertEqual(next(iter(self._membership().values())), {a, b})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM community_embeddings").fetchone()[0], 0
        )

        invalidate_relation(
            source_id=a,
            target_id=b,
            predicate="depends_on",
            db_connection=self.conn,
            db_path=self.db_path,
        )
        c, d = self._new_entity("extension-existing-c"), self._new_entity("extension-existing-d")
        self._store_edge(c, d, "depends_on")
        self._insert_vector(c, _axis_vector(2))
        self._insert_vector(d, _axis_vector(3))
        recompute_communities(db_connection=self.conn)
        old_community_id = self._communities()[0][0]
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM community_embeddings").fetchone()[0], 1
        )

        invalidate_relation(
            source_id=c,
            target_id=d,
            predicate="depends_on",
            db_connection=self.conn,
            db_path=self.db_path,
        )
        with patch(
            "saltmdb.domain.services.community_detection_service.try_load_vector_extension",
            return_value=False,
        ):
            empty_result = recompute_communities(db_connection=self.conn)

        self.assertEqual(empty_result, {"status": "no_edges", "communities_created": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM community_membership").fetchone()[0], 0
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT embedding FROM community_embeddings WHERE community_id = ?",
                (old_community_id,),
            ).fetchone()
        )

    def test_scenario_26_trigger_without_coordinator_uses_legacy_worker(self):
        with (
            patch.dict(
                os.environ,
                {
                    "SALTMDB_TEST_MODE": "",
                    "SALTMDB_DISABLE_COMMUNITY_DETECTION": "",
                },
                clear=False,
            ),
            patch(
                "saltmdb.domain.services.librarian_service._librarian_trigger_pool.submit"
            ) as submit,
        ):
            trigger_community_detection(self.db_path)

        submit.assert_called_once_with(_run_community_detection_pass_impl, self.db_path)

    def test_scenario_27_trigger_with_coordinator_uses_coordinator_worker(self):
        coordinator = _ImmediateCoordinator(self.conn)
        with (
            patch.dict(
                os.environ,
                {
                    "SALTMDB_TEST_MODE": "",
                    "SALTMDB_DISABLE_COMMUNITY_DETECTION": "",
                },
                clear=False,
            ),
            patch(
                "saltmdb.domain.services.librarian_service._librarian_trigger_pool.submit"
            ) as submit,
        ):
            trigger_community_detection(self.db_path, coordinator=coordinator)

        submit.assert_called_once_with(
            community_detection_service._run_community_detection_with_coordinator,
            self.db_path,
            coordinator,
        )
        worker, submitted_path, submitted_coordinator = submit.call_args.args
        self.assertIs(worker, community_detection_service._run_community_detection_with_coordinator)
        self.assertEqual(submitted_path, self.db_path)
        self.assertIs(submitted_coordinator, coordinator)

        result = worker(submitted_path, submitted_coordinator)
        self.assertEqual(result, "Skipped: no qualifying relation edges to cluster.")
        self.assertEqual(
            coordinator.submissions,
            [("community_detection_mutations", "background", True)],
        )

    def test_scenario_28_connection_worker_runs_pass_and_honors_preconditions(self):
        source, target = (
            self._new_entity("connection-worker-source"),
            self._new_entity("connection-worker-target"),
        )
        self._store_edge(source, target, "depends_on")
        self._insert_vector(source, _axis_vector(0))
        self._insert_vector(target, _axis_vector(1))

        first = community_detection_service._run_community_detection_pass_on_connection(self.conn)
        self.assertTrue(first.startswith("Community detection pass complete:"), first)
        from typing import cast

        first_membership: dict[str, set[str]] = cast(dict[str, set[str]], self._membership())
        first_members: set[frozenset[str]] = {
            frozenset(members) for members in first_membership.values()
        }
        first_embeddings: dict[frozenset[str], np.ndarray] = {
            frozenset(members): self._embedding(community_id)
            for community_id, members in first_membership.items()
        }
        first_counts = sorted(row[2] for row in self._communities())

        equivalent = recompute_communities(db_connection=self.conn)
        self.assertEqual(equivalent["status"], "recomputed")
        self.assertEqual(equivalent["communities_created"], len(first_counts))
        equivalent_membership: dict[str, set[str]] = cast(dict[str, set[str]], self._membership())
        equivalent_embeddings: dict[frozenset[str], np.ndarray] = {
            frozenset(members): self._embedding(community_id)
            for community_id, members in equivalent_membership.items()
        }
        self.assertEqual(
            {frozenset(members) for members in self._membership().values()},
            first_members,
        )
        self.assertEqual(
            sorted(row[2] for row in self._communities()),
            first_counts,
        )
        self.assertEqual(set(first_embeddings), set(equivalent_embeddings))
        for members, first_embedding in first_embeddings.items():
            np.testing.assert_array_equal(first_embedding, equivalent_embeddings[members])

        cooldown = community_detection_service._run_community_detection_pass_on_connection(
            self.conn
        )
        self.assertEqual(cooldown, "Skipped: cooldown not elapsed.")

        invalidated = invalidate_relation(
            source_id=source,
            target_id=target,
            predicate="depends_on",
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertTrue(invalidated.startswith("Relation invalidated"), invalidated)
        lock_before = self.conn.execute(
            "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
        ).fetchone()[0]
        with patch("leidenalg.find_partition") as find_partition:
            empty = community_detection_service._run_community_detection_pass_on_connection(
                self.conn
            )
        find_partition.assert_not_called()
        self.assertEqual(empty, "Skipped: no qualifying relation edges to cluster.")
        self.assertEqual(
            self.conn.execute(
                "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
            ).fetchone()[0],
            lock_before,
        )
        for table in ("communities", "community_membership", "community_embeddings"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

        self._store_edge(source, target, "depends_on")
        recompute_communities(db_connection=self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0], 1)
        lock_before = self.conn.execute(
            "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
        ).fetchone()[0]
        reinvalidated = invalidate_relation(
            source_id=source,
            target_id=target,
            predicate="depends_on",
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertTrue(reinvalidated.startswith("Relation invalidated"), reinvalidated)
        with patch("leidenalg.find_partition") as find_partition:
            legacy_empty = _run_community_detection_pass_impl(self.db_path)
        find_partition.assert_not_called()
        self.assertEqual(legacy_empty, "Skipped: no qualifying relation edges to cluster.")
        self.assertEqual(
            self.conn.execute(
                "SELECT last_run_at FROM _system_locks WHERE task_name = 'community_detection'"
            ).fetchone()[0],
            lock_before,
        )
        for table in ("communities", "community_membership", "community_embeddings"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_scenario_29_oversized_community_recurses_into_real_children(self):
        nodes = sorted(self._new_entity(f"hierarchy-29-{index}") for index in range(6))
        for left, right in (
            (0, 4),
            (1, 3),
            (1, 5),
            (2, 4),
            (2, 5),
            (4, 5),
        ):
            self._store_edge(nodes[left], nodes[right], "related_to")
        for index, entity_id in enumerate(nodes):
            self._insert_vector(entity_id, _axis_vector(index))

        with patch.object(community_detection_service, "COMMUNITY_HIERARCHY_SIZE_THRESHOLD", 3):
            result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["status"], "recomputed")
        roots = self.conn.execute(
            "SELECT id, member_count FROM communities "
            "WHERE level = 0 AND parent_community_id IS NULL AND member_count > 3"
        ).fetchall()
        self.assertEqual(len(roots), 1)
        root_id, root_count = roots[0]
        self.assertEqual(root_count, 4)
        children = self.conn.execute(
            "SELECT id FROM communities WHERE parent_community_id = ? AND level = 1",
            (root_id,),
        ).fetchall()
        self.assertGreaterEqual(len(children), 2)
        child_ids = [row[0] for row in children]
        child_members = self.conn.execute(
            f"SELECT entity_id, level FROM community_membership "
            f"WHERE community_id IN ({','.join('?' for _ in child_ids)})",
            child_ids,
        ).fetchall()
        self.assertEqual(len(child_members), root_count)
        self.assertTrue(all(level == 1 for _, level in child_members))
        member_ids = [entity_id for entity_id, _ in child_members]
        self.assertEqual(
            self.conn.execute(
                f"SELECT COUNT(*) FROM community_membership "
                f"WHERE entity_id IN ({','.join('?' for _ in member_ids)}) AND level = 0",
                member_ids,
            ).fetchone()[0],
            0,
        )

    def test_scenario_30_child_below_threshold_does_not_recurse_further(self):
        nodes = sorted(self._new_entity(f"hierarchy-30-{index}") for index in range(6))
        for left, right in (
            (0, 4),
            (1, 3),
            (1, 5),
            (2, 4),
            (2, 5),
            (4, 5),
        ):
            self._store_edge(nodes[left], nodes[right], "related_to")
        for index, entity_id in enumerate(nodes):
            self._insert_vector(entity_id, _axis_vector(index))

        with patch.object(community_detection_service, "COMMUNITY_HIERARCHY_SIZE_THRESHOLD", 3):
            recompute_communities(db_connection=self.conn)

        root_id = self.conn.execute(
            "SELECT id FROM communities "
            "WHERE level = 0 AND parent_community_id IS NULL AND member_count > 3"
        ).fetchone()[0]
        children = self.conn.execute(
            "SELECT id, member_count FROM communities WHERE parent_community_id = ? AND level = 1",
            (root_id,),
        ).fetchall()
        self.assertGreaterEqual(len(children), 2)
        self.assertTrue(all(member_count <= 3 for _, member_count in children))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM communities WHERE level = 2").fetchone()[0],
            0,
        )
        for child_id, _ in children:
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM communities WHERE parent_community_id = ?",
                    (child_id,),
                ).fetchone()[0],
                0,
            )

    def test_scenario_31_recursion_is_capped_at_max_depth_while_still_oversized(self):
        nodes = sorted(self._new_entity(f"hierarchy-31-{index}") for index in range(4))
        for left, right in (
            (0, 1),
            (0, 2),
            (0, 3),
            (1, 2),
            (1, 3),
            (2, 3),
        ):
            self._store_edge(nodes[left], nodes[right], "related_to")
        for index, entity_id in enumerate(nodes):
            self._insert_vector(entity_id, _axis_vector(index))

        with (
            patch.object(community_detection_service, "COMMUNITY_HIERARCHY_SIZE_THRESHOLD", 3),
            patch.object(community_detection_service, "COMMUNITY_HIERARCHY_MAX_DEPTH", 1),
        ):
            recompute_communities(db_connection=self.conn)

        root_id, root_count = self.conn.execute(
            "SELECT id, member_count FROM communities "
            "WHERE level = 0 AND parent_community_id IS NULL"
        ).fetchone()
        self.assertEqual(root_count, 4)
        deepest = self.conn.execute(
            "SELECT id, member_count FROM communities WHERE level = 1 AND parent_community_id = ?",
            (root_id,),
        ).fetchall()
        self.assertEqual(len(deepest), 1)
        deepest_id, deepest_count = deepest[0]
        self.assertGreater(deepest_count, 3)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM communities WHERE parent_community_id = ?",
                (deepest_id,),
            ).fetchone()[0],
            0,
        )

    def test_scenario_32_representative_and_centroid_are_computed_at_every_level(self):
        nodes = sorted(self._new_entity(f"hierarchy-32-{index}") for index in range(4))
        for left, right in (
            (0, 1),
            (0, 2),
            (0, 3),
            (1, 2),
            (1, 3),
            (2, 3),
        ):
            self._store_edge(nodes[left], nodes[right], "related_to")
        for index, entity_id in enumerate(nodes):
            self._insert_vector(entity_id, _axis_vector(index))

        with (
            patch.object(community_detection_service, "COMMUNITY_HIERARCHY_SIZE_THRESHOLD", 3),
            patch.object(community_detection_service, "COMMUNITY_HIERARCHY_MAX_DEPTH", 1),
        ):
            recompute_communities(db_connection=self.conn)

        rows = self.conn.execute(
            "SELECT id, representative_entity_id, level FROM communities ORDER BY level"
        ).fetchall()
        self.assertEqual([row[2] for row in rows], [0, 1])
        for community_id, representative_id, _ in rows:
            self.assertIsNotNone(representative_id)
            self.assertIsNotNone(
                self.conn.execute(
                    "SELECT embedding FROM community_embeddings WHERE community_id = ?",
                    (community_id,),
                ).fetchone()
            )

    def test_scenario_33_flat_behavior_is_unchanged_at_or_below_threshold(self):
        first, second = (
            self._new_entity("hierarchy-flat-first"),
            self._new_entity("hierarchy-flat-second"),
        )
        self._store_edge(first, second, "related_to")
        self._insert_vector(first, _axis_vector(0))
        self._insert_vector(second, _axis_vector(1))

        result = recompute_communities(db_connection=self.conn)

        self.assertEqual(result["status"], "recomputed")
        self.assertEqual(result["communities_created"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM communities WHERE parent_community_id IS NULL AND level = 0"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM communities WHERE level > 0").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM community_membership WHERE level = 0"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM community_embeddings").fetchone()[0],
            1,
        )

    def test_scenario_34_full_recompute_clears_every_hierarchy_level(self):
        nodes = sorted(self._new_entity(f"hierarchy-clear-{index}") for index in range(4))
        for left, right in (
            (0, 1),
            (0, 2),
            (0, 3),
            (1, 2),
            (1, 3),
            (2, 3),
        ):
            self._store_edge(nodes[left], nodes[right], "related_to")
        for index, entity_id in enumerate(nodes):
            self._insert_vector(entity_id, _axis_vector(index))

        with patch.object(community_detection_service, "COMMUNITY_HIERARCHY_SIZE_THRESHOLD", 3):
            first = recompute_communities(db_connection=self.conn)

        self.assertEqual(first["communities_created"], 3)
        self.assertEqual(
            [
                row[0]
                for row in self.conn.execute(
                    "SELECT level, COUNT(*) FROM communities GROUP BY level ORDER BY level"
                ).fetchall()
            ],
            [0, 1, 2],
        )
        self.conn.execute("DELETE FROM relations")
        self.conn.commit()

        second = recompute_communities(db_connection=self.conn)

        self.assertEqual(second, {"status": "no_edges", "communities_created": 0})
        for table in ("communities", "community_membership", "community_embeddings"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
