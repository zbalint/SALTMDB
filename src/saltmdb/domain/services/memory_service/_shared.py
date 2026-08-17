"""Shared singleton state for the memory_service package.

Reached into directly by external callers by dotted path: daemon/server.py accesses
`memory_service._embed_pool` / `memory_service._search_pool` for shutdown/monitoring
wiring, and daemon/dispatch.py accesses `memory_service.RETRIEVAL_TEXT_UNSET` as a
sentinel default. Keep these definitions here as the single source of truth, and
always access them via qualified `_shared.<name>` lookups from other submodules,
never a name captured at import time.

In particular, scripts/benchmarking/build_diverse_test_db.py reassigns
`memory_service._embed_pool` at runtime (shutdown + fresh ThreadPoolExecutor) --
any internal consumer that captured `_embed_pool` via `from ._shared import
_embed_pool` at import time would silently diverge from that reassignment and use a
stale/shut-down pool. No internal code currently submits to the pool, so nothing
breaks today, but this is the rule any future consumer must follow.

The logger name is hardcoded (not `__name__`) so it stays exactly
"saltmdb.domain.services.memory_service" -- matching the pre-split flat module --
regardless of which submodule under this package a given log call physically lives in.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("saltmdb.domain.services.memory_service")
_embed_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saltmdb-embed")
_search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="saltmdb-search")
RETRIEVAL_TEXT_UNSET = object()
