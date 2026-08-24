"""SALTMDB Viewer HTTP request handler, composed from per-feature mixins.

`routes.py` used to be a single 1432-line module holding one
`SALTMDBHandler(http.server.BaseHTTPRequestHandler)` class with every endpoint
as a method. It's now a package: each endpoint group lives in its own module as
a plain mixin class (methods reference `self.send_json`/`self.get_db_connection`/
etc., supplied by `ViewerHandlerBase` at composition time below), and this file
wires them all together into the same `SALTMDBHandler` name at the same import
path (`saltmdb.viewer.routes.SALTMDBHandler`) so nothing outside this package
has to change.

`memory_service`, `relation_service`, and `STATIC_ASSETS` are re-exported here
(not just used internally by search.py/entity_detail.py/_shared.py) because
existing tests reach into this module's namespace directly:
tests/test_viewer_routes.py patches "saltmdb.viewer.routes.memory_service.search_memory"
and tests/test_viewer_rework_contracts.py does
`from saltmdb.viewer.routes import SALTMDBHandler, STATIC_ASSETS`.
"""

from saltmdb.domain.services import memory_service, relation_service
from saltmdb.viewer.routes._shared import STATIC_ASSETS
from saltmdb.viewer.routes.base import ViewerHandlerBase
from saltmdb.viewer.routes.entities import EntitiesMixin
from saltmdb.viewer.routes.entity_detail import EntityDetailMixin
from saltmdb.viewer.routes.events import EventsMixin
from saltmdb.viewer.routes.relations import RelationsMixin
from saltmdb.viewer.routes.scatterplot import ScatterplotMixin
from saltmdb.viewer.routes.search import SearchMixin
from saltmdb.viewer.routes.stats import StatsMixin

__all__ = ["STATIC_ASSETS", "SALTMDBHandler", "memory_service", "relation_service"]


class SALTMDBHandler(
    EntitiesMixin,
    EventsMixin,
    RelationsMixin,
    StatsMixin,
    SearchMixin,
    ScatterplotMixin,
    EntityDetailMixin,
    ViewerHandlerBase,
):
    """Zero-dependency HTTP Request Handler for the SALTMDB Dashboard Viewer."""
