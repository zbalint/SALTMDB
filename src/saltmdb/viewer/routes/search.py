"""Hybrid search endpoint: GET /api/search."""

import logging
from typing import TYPE_CHECKING

from saltmdb.config import get_db_path
from saltmdb.domain.services import memory_service

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


class SearchMixin(ViewerHandlerProtocol):
    """Provides get_search(); mixed into the final SALTMDBHandler elsewhere."""

    def get_search(self, query):
        try:
            q = query.get("q", [""])[0].strip()
            if not q:
                self.send_json({"query": "", "mode": "broad", "results": []})
                return
            db_path = getattr(getattr(self.server, "viewer_gateway", None), "db_path", None)
            search_kwargs = {
                "query_keywords": q,
                "limit": 50,
                "include_related": False,
                "mode": "broad",
                "db_path": db_path or get_db_path(),
            }
            is_core_raw = query.get("is_core", [None])[0]
            if is_core_raw:
                normalized_is_core = is_core_raw.strip().lower()
                if normalized_is_core in ("true", "1", "yes"):
                    search_kwargs["is_core"] = True
                elif normalized_is_core in ("false", "0", "no"):
                    search_kwargs["is_core"] = False
                else:
                    raise ValueError("is_core must be one of true, 1, yes, false, 0, no")
            session_id_raw = query.get("agent_session_id", [None])[0]
            if session_id_raw:
                search_kwargs["agent_session_id"] = session_id_raw
            results = memory_service.search_memory(**search_kwargs)
            if not isinstance(results, list):
                message = (
                    results.get("error", "Hybrid search unavailable")
                    if isinstance(results, dict)
                    else str(results)
                )
                raise RuntimeError(message or "Hybrid search unavailable")
            self.send_json({"query": q, "mode": "broad", "results": results})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": str(e) or "Hybrid search unavailable"}, 503)
