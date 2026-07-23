import sys
import os

src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

import logging
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.db.schema import init_db
from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock
import saltmdb.domain.services.memory_service as memory_service
import saltmdb.domain.services.event_service as event_service
import saltmdb.domain.services.relation_service as relation_service
import saltmdb.domain.services.ephemeral_service as ephemeral_service
import saltmdb.domain.services.librarian_service as librarian_service
import saltmdb.db.backup as backup
import saltmdb.mcp.tools as mcp_tools

DB_PATH = get_db_path()

# Re-export key functions with alias normalization for 100% backward compatibility
def store_memory(*args, **kwargs):
    if "text" in kwargs and "content" not in kwargs:
        kwargs["content"] = kwargs.pop("text")
    if "tag" in kwargs and "tags" not in kwargs:
        tag_val = kwargs.pop("tag")
        kwargs["tags"] = [tag_val] if isinstance(tag_val, str) else tag_val
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.store_memory(*args, **kwargs)

def search_memory(*args, **kwargs):
    if "query" in kwargs and "query_keywords" not in kwargs:
        kwargs["query_keywords"] = kwargs.pop("query")
    if "q" in kwargs and "query_keywords" not in kwargs:
        kwargs["query_keywords"] = kwargs.pop("q")
    if "keywords" in kwargs and "query_keywords" not in kwargs:
        kwargs["query_keywords"] = kwargs.pop("keywords")
    if "tag" in kwargs and "tags_filter" not in kwargs:
        tag_val = kwargs.pop("tag")
        kwargs["tags_filter"] = [tag_val] if isinstance(tag_val, str) else tag_val
    if "tags" in kwargs and "tags_filter" not in kwargs:
        kwargs["tags_filter"] = kwargs.pop("tags")
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.search_memory(*args, **kwargs)

def fetch_memory_chunk(*args, **kwargs):
    return memory_service.fetch_memory_chunk(*args, **kwargs)

def archive_memory(*args, **kwargs):
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.archive_memory(*args, **kwargs)

def detect_orphaned_memories(*args, **kwargs):
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.detect_orphaned_memories(*args, **kwargs)

def check_duplicate_memories(*args, **kwargs):
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.check_duplicate_memories(*args, **kwargs)

def scan_memories(*args, **kwargs):
    if "owner" in kwargs and "owner_id" not in kwargs:
        kwargs["owner_id"] = kwargs.pop("owner")
    return memory_service.scan_memories(*args, **kwargs)

def log_event(*args, **kwargs):
    if "agent" in kwargs and "agent_id" not in kwargs:
        kwargs["agent_id"] = kwargs.pop("agent")
    if "event_type" in kwargs and "type" not in kwargs:
        kwargs["type"] = kwargs.pop("event_type")
    if "message" in kwargs and "content" not in kwargs:
        kwargs["content"] = kwargs.pop("message")
    if "description" in kwargs and "content" not in kwargs:
        kwargs["content"] = kwargs.pop("description")
    return event_service.log_event(*args, **kwargs)

def get_recent_events(*args, **kwargs):
    if "agent" in kwargs and "agent_id" not in kwargs:
        kwargs["agent_id"] = kwargs.pop("agent")
    if "type" in kwargs and "type_filter" not in kwargs:
        kwargs["type_filter"] = kwargs.pop("type")
    return event_service.get_recent_events(*args, **kwargs)

def get_session_summary(*args, **kwargs):
    if "agent" in kwargs and "agent_id" not in kwargs:
        kwargs["agent_id"] = kwargs.pop("agent")
    return event_service.get_session_summary(*args, **kwargs)

def store_relation(*args, **kwargs):
    return relation_service.store_relation(*args, **kwargs)

def analyze_dependencies(*args, **kwargs):
    return relation_service.analyze_dependencies(*args, **kwargs)

def analyze_lineage(*args, **kwargs):
    return relation_service.analyze_lineage(*args, **kwargs)

def commit_consolidation(*args, **kwargs):
    return relation_service.commit_consolidation(*args, **kwargs)

def bulk_commit_consolidation(*args, **kwargs):
    return relation_service.bulk_commit_consolidation(*args, **kwargs)

def bulk_store_relations(*args, **kwargs):
    return relation_service.bulk_store_relations(*args, **kwargs)

def bulk_archive_memory(*args, **kwargs):
    return memory_service.bulk_archive_memory(*args, **kwargs)

def store_ephemeral_memory(*args, **kwargs):
    return ephemeral_service.store_ephemeral_memory(*args, **kwargs)

def get_ephemeral_memory(*args, **kwargs):
    return ephemeral_service.get_ephemeral_memory(*args, **kwargs)

def create_snapshot(*args, **kwargs):
    return backup.create_snapshot(*args, **kwargs)

def get_canonical_tags(*args, **kwargs):
    return memory_service.get_canonical_tags(*args, **kwargs)

def trigger_librarian(*args, **kwargs):
    return librarian_service.trigger_librarian(*args, **kwargs)

if __name__ == "__main__":
    from saltmdb.__main__ import main
    main()
