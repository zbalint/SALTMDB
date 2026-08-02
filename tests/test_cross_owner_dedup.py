import unittest
import tempfile
import os
import shutil
from unittest.mock import patch
import sqlite_vec
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, embedding_service
from saltmdb.db.connection import write_transaction_retrying


def _seed_ready_embedding(conn, entity_id: str, title: str, full_content: str) -> None:
    """Helper to deterministically seed a 'ready' vector embedding in entity_embeddings."""
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass

    text = f"{title}\n\n{full_content}"
    vector = embedding_service.embed_text(text)

    def _write(c):
        c.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
        c.execute(
            "INSERT INTO entity_embeddings(entity_id, embedding) VALUES (?, ?)",
            (entity_id, sqlite_vec.serialize_float32(vector)),
        )
        c.execute("UPDATE entities SET embedding_status = 'ready' WHERE id = ?", (entity_id,))

    write_transaction_retrying(conn, _write)


class TestCrossOwnerDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "true"

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        if "SALTMDB_ENABLE_SEMANTIC" in os.environ:
            del os.environ["SALTMDB_ENABLE_SEMANTIC"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_shared_scope_memory_visible_across_owners(self):
        """A scope='shared' memory stored by one owner must be surfaced as a dedup candidate for a different owner."""
        memory_service.store_memory(
            content="SALTMDB is a local-first MCP memory database enabling cross-agent shared memory across Claude, Antigravity, and Copilot CLI",
            title="SALTMDB Cross-Agent Design Purpose",
            owner_id="agent_a",
            scope="shared",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        dup_check = memory_service.check_duplicate_memories(
            title="SALTMDB Cross-Agent Purpose Restated",
            content="SALTMDB is a local-first MCP memory database that enables cross-agent shared memory across Claude, Antigravity, and Copilot CLI",
            owner_id="agent_b",
            db_connection=self.conn,
        )

        self.assertTrue(
            dup_check.get("duplicate_found"),
            "Shared-scope memory from a different owner should be detected as a duplicate candidate",
        )
        self.assertEqual(dup_check["potential_duplicates"][0]["scope"], "shared")

    def test_private_scope_memory_stays_isolated_across_owners(self):
        """A scope='private' memory stored by one owner must NOT be surfaced as a dedup candidate for a different owner."""
        memory_service.store_memory(
            content="agent_a private scratch note about a local debugging session that nobody else should see",
            title="agent_a Private Debug Note",
            owner_id="agent_a",
            scope="private",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        dup_check = memory_service.check_duplicate_memories(
            title="agent_a Private Debug Note",
            content="agent_a private scratch note about a local debugging session that nobody else should see",
            owner_id="agent_b",
            db_connection=self.conn,
        )

        self.assertFalse(
            dup_check.get("duplicate_found"),
            "Private-scope memory from a different owner must stay isolated",
        )

    def test_dedup_check_no_per_row_embedding_loop(self):
        """Regression guard: verify check_duplicate_memories embeds query text only once and does not loop per candidate."""
        owner = "agent_dedup"
        for i in range(6):
            eid = f"candidate-entity-{i}"
            t = f"SALTMDB Vector Memory Architecture Candidate {i}"
            c = f"Detailed technical text for candidate memory {i} regarding SALTMDB deduplication service"
            memory_service.store_memory(
                title=t,
                content=c,
                owner_id=owner,
                entity_id=eid,
                skip_duplicate_check=True,
                db_connection=self.conn,
            )
            _seed_ready_embedding(self.conn, eid, t, c)

        real_embed_text = embedding_service.embed_text
        with patch(
            "saltmdb.domain.services.embedding_service.embed_text",
            side_effect=real_embed_text,
        ) as mock_embed:
            dup_check = memory_service.check_duplicate_memories(
                title="SALTMDB Vector Memory Architecture Query",
                content="Detailed technical text for query regarding SALTMDB deduplication service",
                owner_id=owner,
                db_connection=self.conn,
            )

        self.assertNotIn("error", dup_check)
        self.assertTrue(dup_check.get("duplicate_found"))
        # embed_text must be called at most once (for query text itself)
        self.assertEqual(mock_embed.call_count, 1)

    def test_dedup_check_mixed_embedding_readiness(self):
        """Correctness with mixed embedding readiness: candidate A (ready vector, >=0.75), B (lexical fallback), C (dissimilar excluded)."""
        owner = "agent_dedup"
        query_title = "SALTMDB Database System Architecture Performance"
        query_content = "Optimizing local SQLite vector search performance and indexing strategies"

        # Candidate A: ready vector + near-identical text
        eid_a = "mixed-cand-a"
        t_a = "SALTMDB Database System Architecture Performance"
        c_a = "Optimizing local SQLite vector search performance and indexing strategies"
        memory_service.store_memory(
            title=t_a,
            content=c_a,
            owner_id=owner,
            entity_id=eid_a,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        _seed_ready_embedding(self.conn, eid_a, t_a, c_a)

        # Candidate B: NO ready vector (left as default) + lexically near-identical text
        eid_b = "mixed-cand-b"
        t_b = "SALTMDB Database System Architecture Performance"
        c_b = "Optimizing local SQLite vector search performance and indexing strategy"
        memory_service.store_memory(
            title=t_b,
            content=c_b,
            owner_id=owner,
            entity_id=eid_b,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        # Candidate C: NO ready vector + lexically dissimilar text (matching only sparse FTS terms)
        eid_c = "mixed-cand-c"
        t_c = "SALTMDB Database System Network Infrastructure"
        c_c = "Configuring Linux firewall rules and socket buffer sizes for distributed server nodes"
        memory_service.store_memory(
            title=t_c,
            content=c_c,
            owner_id=owner,
            entity_id=eid_c,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        dup_check = memory_service.check_duplicate_memories(
            title=query_title,
            content=query_content,
            owner_id=owner,
            db_connection=self.conn,
        )

        self.assertNotIn("error", dup_check)
        self.assertTrue(dup_check.get("duplicate_found"))
        potential_dups = dup_check.get("potential_duplicates", [])
        dup_map = {d["id"]: d for d in potential_dups}

        # Candidate A: present with similarity_score >= 0.75
        self.assertIn(eid_a, dup_map, "Candidate A (ready vector) should be in potential duplicates")
        self.assertGreaterEqual(
            dup_map[eid_a]["similarity_score"],
            0.75,
            "Candidate A similarity score should meet or exceed 0.75",
        )

        # Candidate B: present via lexical fallback
        self.assertIn(eid_b, dup_map, "Candidate B (lexical fallback) should be in potential duplicates")

        # Candidate C: excluded due to low lexical similarity (< 0.40)
        self.assertNotIn(
            eid_c,
            dup_map,
            "Candidate C (lexically dissimilar) must not be included in potential duplicates",
        )

    def test_dedup_check_fts_fallback_scan_is_bounded(self):
        """FTS-fallback scan is bounded: when FTS candidates are empty against 35+ entities, fallback query uses LIMIT 30."""
        owner = "agent_dedup"
        for i in range(35):
            memory_service.store_memory(
                title=f"Standard System Memory Item {i:02d}",
                content=f"Detailed content for standard system memory entity index {i:02d}",
                owner_id=owner,
                skip_duplicate_check=True,
                db_connection=self.conn,
            )

        executed_sqls = []

        class ConnectionProxy:
            def __init__(self, target):
                self._target = target

            def execute(self, sql, *args, **kwargs):
                executed_sqls.append(str(sql))
                return self._target.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._target, name)

        proxy_conn = ConnectionProxy(self.conn)

        dup_check = memory_service.check_duplicate_memories(
            title="ZzzUnmatchedQueryTermXyz",
            content="ZzzUnmatchedQueryTermXyz",
            owner_id=owner,
            db_connection=proxy_conn,
        )

        self.assertNotIn("error", dup_check)

        fallback_queries = [
            sql for sql in executed_sqls if "FROM entities" in sql and "LIMIT 30" in sql
        ]
        self.assertGreaterEqual(
            len(fallback_queries),
            1,
            "FTS fallback branch must execute a query with 'LIMIT 30' when FTS candidates are empty",
        )


if __name__ == "__main__":
    unittest.main()

