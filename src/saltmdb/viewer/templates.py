"""The small static shell for the zero-build SALTMDB Viewer."""


def get_frontend_html(db_path: str = None) -> str:
    """Return a CSP-compatible shell; all dynamic UI lives in local static assets."""
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SALTMDB Viewer</title><link rel="stylesheet" href="/static/viewer.css"></head>
<body><a class="skip-link" href="#main">Skip to content</a><div class="app-shell">
<aside class="sidebar"><div><h1>SALTMDB</h1><p class="sidebar-kicker">Memory workspace</p><nav aria-label="Viewer">
<button data-view="overview" class="nav-item is-active">Overview</button>
<button data-view="explorer" class="nav-item">Memory Explorer</button>
<button data-view="activity" class="nav-item">Activity</button>
<button data-view="relationships" class="nav-item">Relationships</button>
<button data-view="quality" class="nav-item">Quality</button>
<button data-view="operations" class="nav-item">Operations</button>
<button data-view="tags" class="nav-item">Tags</button>
<button data-view="diagnostics" class="nav-item">Diagnostics</button>
</nav></div><div id="connection-indicator" class="connection-indicator" role="status" aria-live="polite"><span class="connection-dot" aria-hidden="true"></span><span>Checking connection…</span></div></aside><main id="main"><header><div><p class="eyebrow">Knowledge operations</p><h2 id="view-title">Overview</h2></div>
<div class="connection"><span id="live-status" role="status">Loading</span><button id="refresh" type="button">Refresh</button></div></header>
<div id="notice" class="notice" role="status" aria-live="polite" hidden></div><section id="view" aria-live="polite"></section></main></div>
<dialog id="memory-detail" aria-labelledby="detail-title"><article><header><h2 id="detail-title">Memory</h2><button id="close-detail" aria-label="Close memory detail">Close</button></header><div id="detail-content"></div></article></dialog>
<script src="/static/vendor/marked-18.0.7.umd.js"></script><script src="/static/vendor/dompurify-3.4.10.min.js"></script><script src="/static/viewer.js"></script></body></html>"""
