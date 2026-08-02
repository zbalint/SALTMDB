import unittest
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
import numpy as np

from saltmdb.db.connection import get_connection
from saltmdb.db.schema import init_db
from saltmdb.db.vector_schema import init_vector_schema
from saltmdb.domain.services.librarian_service import (
    extract_c_tfidf_tags,
    find_connected_vector_clusters,
    consolidate_vector_clusters,
)


class TestConnectedComponentsVectorClustering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_saltmdb.db")
        self.conn = get_connection(self.db_path)
        self._init_db()

    def tearDown(self):
        if self.conn:
            self.conn.close()
        self.temp_dir.cleanup()

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                title TEXT,
                full_content TEXT,
                owner_id TEXT DEFAULT 'antigravity',
                status TEXT DEFAULT 'raw',
                is_core INTEGER DEFAULT 0,
                embedding_status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS entity_embeddings (
                entity_id TEXT PRIMARY KEY,
                embedding BLOB,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(entity_id) REFERENCES entities(id)
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp TEXT,
                agent_id TEXT,
                type TEXT,
                content TEXT,
                session_id TEXT,
                context_id TEXT,
                error_code TEXT
            );

            CREATE TABLE IF NOT EXISTS relations (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                target_id TEXT,
                predicate TEXT,
                weight REAL DEFAULT 1.0,
                valid_from TEXT,
                valid_to TEXT,
                valid_at TEXT,
                invalid_at TEXT,
                owner_id TEXT DEFAULT 'antigravity'
            );

            CREATE TABLE IF NOT EXISTS tags (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                is_canonical INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def test_find_connected_vector_clusters_small_batch(self):
        # 3 near-identical vectors (no background noise)
        v1 = np.ones(384, dtype=np.float32)
        v2 = np.ones(384, dtype=np.float32) * 1.01
        v3 = np.ones(384, dtype=np.float32) * 0.99

        valid_ids = ["e1", "e2", "e3"]
        vectors = [v1, v2, v3]

        clusters = find_connected_vector_clusters(
            valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.75
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0][0]), ["e1", "e2", "e3"])
        self.assertGreaterEqual(clusters[0][1], 0.9)

    def test_find_connected_vector_clusters_off_diagonal_mean(self):
        # 3 vectors with known pairwise similarities
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.8, 0.6, 0.0], dtype=np.float32)
        v3 = np.array([0.8, 0.0, 0.6], dtype=np.float32)

        valid_ids = ["e1", "e2", "e3"]
        vectors = [v1, v2, v3]

        clusters = find_connected_vector_clusters(
            valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.6
        )
        self.assertEqual(len(clusters), 1)
        # Off-diagonal mean should be around 0.7467 rather than inflated by 1.0 diagonals
        self.assertAlmostEqual(clusters[0][1], 0.7467, places=3)

    def test_extract_c_tfidf_tags(self):
        self.conn.executemany(
            "INSERT INTO entities (id, title, full_content, status) VALUES (?, ?, ?, 'raw')",
            [
                (
                    "e1",
                    "SQLite WAL Concurrency",
                    "Fixing sqlite wal transaction locking and retries",
                ),
                (
                    "e2",
                    "SQLite Transaction Retries",
                    "Handling wal mode lock contention in sqlite database",
                ),
                (
                    "e3",
                    "SQLite Lock Contention",
                    "Optimizing sqlite wal transaction retries under high concurrency",
                ),
            ],
        )
        self.conn.commit()

        tags, score = extract_c_tfidf_tags(self.conn, ["e1", "e2", "e3"], top_k=3)
        self.assertTrue(len(tags) > 0)
        self.assertIn("#sqlite", [t.lower() for t in tags])
        self.assertGreaterEqual(score, 0.5)

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters(self, mock_trigger):
        v1 = np.ones(384, dtype=np.float32)
        v2 = np.ones(384, dtype=np.float32) * 1.01
        v3 = np.ones(384, dtype=np.float32) * 0.99

        entities = [
            (
                "e1",
                "Docker Container Setup",
                "Configuring Docker containers for WSL2 dev environment",
            ),
            (
                "e2",
                "Docker Container Storage",
                "Setting up persistent Docker volume mounts in WSL2",
            ),
            (
                "e3",
                "Docker Networking WSL2",
                "Debugging Docker container networking ports under WSL2",
            ),
        ]
        vectors = [v1.tobytes(), v2.tobytes(), v3.tobytes()]

        for (eid, title, content), blob in zip(entities, vectors):
            self.conn.execute(
                "INSERT INTO entities (id, title, full_content, status, embedding_status) VALUES (?, ?, ?, 'raw', 'ready')",
                (eid, title, content),
            )
            self.conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                (eid, blob),
            )
        self.conn.commit()

        consolidate_vector_clusters(self.conn)

        cursor = self.conn.execute("SELECT type, content FROM events ORDER BY timestamp ASC")
        rows = cursor.fetchall()
        event_types = [r[0] for r in rows]

        self.assertIn("consolidation_request", event_types)
        self.assertIn("domain_suggestion", event_types)

        for etype, content in rows:
            if etype == "domain_suggestion":
                data = json.loads(content)
                self.assertIn("suggested_tags", data)
                self.assertIn("confidence_score", data)
                self.assertGreaterEqual(data["confidence_score"], 0.5)

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters_default_conn(self, mock_trigger):
        # Rebuilt against the real production schema (init_db + init_vector_schema, which
        # creates entity_embeddings as an actual `vec0` virtual table) instead of setUp's
        # plain-table fixture. A plain table doesn't require the sqlite_vec extension to be
        # loaded on the connection, so it can't exercise -- or catch a regression in -- the
        # self-opened connection's extension-loading path that consolidate_vector_clusters
        # relies on in production.
        self.conn.close()
        real_db_path = os.path.join(self.temp_dir.name, "real_vec0.db")
        conn = init_db(real_db_path)
        init_vector_schema(conn)

        now = datetime.now(timezone.utc).isoformat()
        v1 = np.ones(384, dtype=np.float32)
        v2 = np.ones(384, dtype=np.float32) * 1.01
        v3 = np.ones(384, dtype=np.float32) * 0.99

        entities = [
            (
                "e1",
                "Docker Container Setup",
                "Configuring Docker containers for WSL2 dev environment",
            ),
            (
                "e2",
                "Docker Container Storage",
                "Setting up persistent Docker volume mounts in WSL2",
            ),
            (
                "e3",
                "Docker Networking WSL2",
                "Debugging Docker container networking ports under WSL2",
            ),
        ]
        vectors = [v1.tobytes(), v2.tobytes(), v3.tobytes()]

        for (eid, title, content), blob in zip(entities, vectors):
            conn.execute(
                "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, title, full_content, status, embedding_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 'ready')",
                (eid, now, now, now, "claude", title, content),
            )
            conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                (eid, blob),
            )
        conn.commit()
        conn.close()

        # Call consolidate_vector_clusters with conn=None to exercise the self-opened
        # connection path, against the real vec0 schema this time.
        consolidate_vector_clusters(conn=None, db_path=real_db_path)

        self.conn = get_connection(real_db_path)
        cursor = self.conn.execute("SELECT type, content FROM events ORDER BY timestamp ASC")
        rows = cursor.fetchall()
        event_types = [r[0] for r in rows]

        self.assertIn("consolidation_request", event_types)
        self.assertIn("domain_suggestion", event_types)


if __name__ == "__main__":
    unittest.main()
