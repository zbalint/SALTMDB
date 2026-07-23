def get_frontend_html(db_path: str = None) -> str:
    """Returns the single-page application (SPA) HTML dashboard template."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SALTMDB Database Viewer</title>
    <style>
        :root {
            --bg-primary:   #0a0f1e;
            --bg-secondary: #111827;
            --bg-card:      #1e2a3a;
            --bg-hover:     #243044;
            --accent:       #38bdf8;
            --accent-dark:  #0284c7;
            --accent-purple:#a78bfa;
            --text-primary: #f1f5f9;
            --text-secondary:#94a3b8;
            --border:       #1e3248;
            --border-light: #2d4360;
            --green:  #34d399;
            --yellow: #fbbf24;
            --red:    #f87171;
            --blue:   #60a5fa;
            --radius: 8px;
            --shadow: 0 4px 24px rgba(0,0,0,.5);
        }

        *, *::before, *::after {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, sans-serif;
            font-size: 14px;
            display: flex;
            height: 100vh;
            overflow: hidden;
        }

        /* ── Sidebar ─────────────────────────────────── */
        .sidebar {
            width: 240px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            overflow: hidden;
        }

        .logo {
            padding: 20px 18px 12px;
            font-size: 1rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: var(--accent);
            display: flex;
            align-items: center;
            gap: 8px;
            border-bottom: 1px solid var(--border);
        }

        .logo span { color: var(--text-secondary); font-weight: 400; font-size: 0.75rem; }

        .nav-section {
            padding: 12px 10px 4px;
            font-size: 0.68rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: .08em;
            color: var(--text-secondary);
        }

        .nav-list { list-style: none; padding: 4px 8px 8px; }

        .nav-item {
            padding: 9px 12px;
            border-radius: 6px;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all .15s ease;
            font-size: 0.875rem;
            display: flex;
            align-items: center;
            gap: 9px;
            user-select: none;
            margin-bottom: 2px;
        }

        .nav-item:hover { background: var(--bg-card); color: var(--text-primary); }
        .nav-item.active { background: rgba(56,189,248,.12); color: var(--accent); font-weight: 600; }
        .nav-item .nav-icon { width: 18px; text-align: center; font-size: 1rem; }

        .sidebar-footer {
            margin-top: auto;
            padding: 12px 14px;
            border-top: 1px solid var(--border);
            font-size: 0.72rem;
            color: var(--text-secondary);
        }

        /* ── Main ────────────────────────────────────── */
        .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

        .topbar {
            height: 56px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 20px;
            gap: 16px;
            flex-shrink: 0;
        }

        .topbar-left { display: flex; align-items: center; gap: 12px; }
        .page-title { font-size: 1rem; font-weight: 600; }

        .refresh-indicator {
            display: flex; align-items: center; gap: 6px;
            font-size: 0.75rem; color: var(--text-secondary);
        }
        .refresh-dot {
            width: 7px; height: 7px; border-radius: 50%;
            background: var(--green); display: none;
        }
        .refresh-dot.active { display: inline-block; animation: pulse 2s infinite; }

        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

        /* Search */
        .search-wrap { position: relative; }
        .search-wrap input {
            width: 280px;
            padding: 7px 30px 7px 34px;
            background: var(--bg-primary);
            border: 1px solid var(--border-light);
            border-radius: 20px;
            color: var(--text-primary);
            font-size: 0.85rem;
            outline: none;
            transition: border-color .15s;
        }
        .search-wrap input:focus { border-color: var(--accent); }
        .search-wrap .search-icon {
            position: absolute; left: 11px; top: 50%;
            transform: translateY(-50%);
            color: var(--text-secondary); font-size: 0.9rem; pointer-events: none;
        }
        .search-clear {
            position: absolute; right: 10px; top: 50%;
            transform: translateY(-50%);
            background: none; border: none;
            color: var(--text-secondary); cursor: pointer;
            font-size: 1rem; display: none; line-height: 1;
        }

        /* Content */
        .content { flex: 1; padding: 20px 24px; overflow-y: auto; }

        /* ── Stats Grid ──────────────────────────────── */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }

        .stat-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 14px 16px;
            transition: border-color .15s;
        }
        .stat-card:hover { border-color: var(--border-light); }

        .stat-card .sc-label {
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: .06em;
            color: var(--text-secondary);
            margin-bottom: 6px;
            display: flex; align-items: center; gap: 5px;
        }

        .stat-card .sc-value {
            font-size: 1.6rem;
            font-weight: 700;
            line-height: 1;
        }

        .stat-card .sc-sub {
            font-size: 0.72rem;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        /* ── Section header ──────────────────────────── */
        .section-header {
            display: flex; align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }
        .section-title { font-size: 0.9rem; font-weight: 600; }
        .section-link {
            font-size: 0.78rem; color: var(--accent);
            background: none; border: none;
            cursor: pointer; text-decoration: underline;
        }

        /* ── Tables ──────────────────────────────────── */
        .table-wrap {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            overflow: hidden;
        }

        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }

        th {
            background: var(--bg-card);
            color: var(--text-secondary);
            padding: 10px 14px;
            font-weight: 600;
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: .04em;
            border-bottom: 1px solid var(--border);
            text-align: left;
            white-space: nowrap;
        }

        td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
        tr:last-child td { border-bottom: none; }
        tr.clickable { cursor: pointer; }
        tr.clickable:hover td { background: var(--bg-hover); }

        .td-mono { font-family: "Cascadia Code","Consolas",monospace; font-size: 0.78rem; }
        .td-trunc { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

        /* ── Badges ──────────────────────────────────── */
        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.72rem;
            font-weight: 600;
            white-space: nowrap;
        }
        .badge-raw         { background: rgba(251,191, 36,.15); color: var(--yellow); }
        .badge-consolidated{ background: rgba( 52,211,153,.15); color: var(--green);  }
        .badge-archived    { background: rgba(248,113,113,.15); color: var(--red);    }
        .badge-decision    { background: rgba(167,139,250,.15); color: var(--accent-purple); }
        .badge-issue       { background: rgba(248,113,113,.15); color: var(--red);    }
        .badge-fix         { background: rgba( 52,211,153,.15); color: var(--green);  }
        .badge-attempt     { background: rgba( 96,165,250,.15); color: var(--blue);   }
        .badge-ready       { background: rgba( 52,211,153,.15); color: var(--green);  }
        .badge-pending     { background: rgba(251,191, 36,.15); color: var(--yellow); }
        .badge-failed      { background: rgba(248,113,113,.15); color: var(--red);    }
        .badge-default     { background: rgba(148,163,184,.15); color: var(--text-secondary); }

        /* ── Tags ────────────────────────────────────── */
        .tag {
            display: inline-block;
            background: rgba(56,189,248,.1);
            border: 1px solid rgba(56,189,248,.25);
            color: var(--accent);
            padding: 1px 7px;
            border-radius: 4px;
            font-size: 0.72rem;
            margin: 2px;
            cursor: pointer;
            transition: background .12s;
        }
        .tag:hover { background: rgba(56,189,248,.2); }

        /* ── Filter bar ──────────────────────────────── */
        .filter-bar {
            display: flex; gap: 8px; flex-wrap: wrap;
            align-items: center; margin-bottom: 14px;
        }

        .filter-select, .filter-input {
            padding: 6px 10px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 0.82rem;
            outline: none;
            transition: border-color .15s;
        }
        .filter-select:focus, .filter-input:focus { border-color: var(--accent); }

        .filter-label {
            display: flex; align-items: center; gap: 5px;
            font-size: 0.82rem; color: var(--text-secondary);
        }
        .filter-label input[type=checkbox] { cursor: pointer; accent-color: var(--accent); }

        /* ── Pagination ──────────────────────────────── */
        .pagination {
            display: flex; gap: 6px; margin-top: 14px;
            align-items: center; flex-wrap: wrap;
        }
        .page-btn {
            padding: 5px 11px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 0.82rem;
            cursor: pointer;
            transition: all .12s;
        }
        .page-btn:hover:not(:disabled) { background: var(--bg-card); }
        .page-btn:disabled { opacity: .35; cursor: not-allowed; }
        .page-btn.current { background: var(--accent); color: #0a0f1e; border-color: var(--accent); font-weight: 700; }
        .page-info { font-size: 0.78rem; color: var(--text-secondary); padding: 0 6px; }

        /* ── Skeleton ────────────────────────────────── */
        .skeleton-row { padding: 12px 16px; }
        .skeleton {
            background: linear-gradient(90deg,var(--bg-card) 25%,var(--bg-hover) 50%,var(--bg-card) 75%);
            background-size: 200% 100%;
            animation: shimmer 1.4s infinite;
            border-radius: 4px;
            height: 13px;
            margin-bottom: 4px;
        }
        @keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }

        /* ── Empty state ─────────────────────────────── */
        .empty-state {
            text-align: center; padding: 56px 20px;
            color: var(--text-secondary);
        }
        .empty-state .e-icon { font-size: 2.5rem; margin-bottom: 12px; }
        .empty-state .e-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; }
        .empty-state .e-sub { font-size: 0.82rem; }

        /* ── Progress bar ────────────────────────────── */
        .progress-wrap {
            background: var(--bg-card);
            border-radius: 6px; height: 8px; overflow: hidden;
            margin: 8px 0;
        }
        .progress-bar { height: 100%; border-radius: 6px; transition: width .5s ease; }

        /* ── Modal ───────────────────────────────────── */
        .modal-overlay {
            position: fixed; inset: 0;
            background: rgba(0,0,0,.7);
            display: flex; align-items: center; justify-content: center;
            z-index: 1000;
            opacity: 0; pointer-events: none;
            transition: opacity .2s;
        }
        .modal-overlay.active { opacity: 1; pointer-events: auto; }

        .modal {
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: 12px;
            width: 820px; max-width: 95vw; max-height: 88vh;
            display: flex; flex-direction: column;
            box-shadow: var(--shadow);
            overflow: hidden;
        }

        .modal-head {
            padding: 14px 20px;
            border-bottom: 1px solid var(--border);
            display: flex; align-items: center; justify-content: space-between;
            background: var(--bg-card);
            flex-shrink: 0;
        }
        .modal-head h3 { font-size: 1rem; font-weight: 600; }
        .modal-close {
            background: none; border: none;
            color: var(--text-secondary); font-size: 1.4rem;
            cursor: pointer; line-height: 1;
            transition: color .12s;
        }
        .modal-close:hover { color: var(--text-primary); }

        .modal-tabs {
            display: flex; gap: 0;
            border-bottom: 1px solid var(--border);
            background: var(--bg-secondary);
            flex-shrink: 0;
        }
        .modal-tab {
            padding: 9px 16px;
            font-size: 0.82rem; font-weight: 500;
            color: var(--text-secondary);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all .12s;
        }
        .modal-tab.active { color: var(--accent); border-bottom-color: var(--accent); }

        .modal-body { padding: 20px; overflow-y: auto; flex: 1; }

        .meta-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            background: var(--bg-primary);
            border-radius: 6px;
            padding: 12px;
            font-size: 0.82rem;
            margin-bottom: 12px;
        }
        .meta-grid .meta-k { color: var(--text-secondary); font-size: 0.72rem; margin-bottom: 2px; }
        .meta-grid .meta-v { font-weight: 500; word-break: break-all; }

        .modal-section-title {
            font-size: 0.72rem; font-weight: 700;
            text-transform: uppercase; letter-spacing: .06em;
            color: var(--accent); margin-bottom: 8px; margin-top: 14px;
            display: flex; align-items: center; justify-content: space-between;
        }

        .copy-btn {
            background: none; border: 1px solid var(--border-light);
            border-radius: 4px; padding: 2px 8px;
            font-size: 0.72rem; color: var(--text-secondary);
            cursor: pointer; transition: all .12s;
        }
        .copy-btn:hover { background: var(--bg-card); color: var(--text-primary); }
        .copy-btn.copied { color: var(--green); border-color: var(--green); }

        /* Markdown rendered output */
        .md-content { font-size: 0.85rem; line-height: 1.65; }
        .md-content h1 { font-size: 1.15rem; margin: 12px 0 6px; }
        .md-content h2 { font-size: 1rem; margin: 10px 0 5px; }
        .md-content h3 { font-size: 0.9rem; margin: 8px 0 4px; }
        .md-content p  { margin-bottom: 8px; }
        .md-content ul, .md-content ol { padding-left: 18px; margin-bottom: 8px; }
        .md-content li { margin-bottom: 3px; }
        .md-content code {
            background: var(--bg-card); padding: 1px 5px;
            border-radius: 3px; font-size: 0.82rem;
            font-family: "Cascadia Code","Consolas",monospace;
            color: #e2e8f0;
        }
        .md-content pre {
            background: var(--bg-primary); border: 1px solid var(--border);
            border-radius: 6px; padding: 12px; overflow-x: auto;
            font-size: 0.82rem; line-height: 1.5; margin-bottom: 8px;
        }
        .md-content pre code { background: none; padding: 0; }
        .md-content blockquote {
            border-left: 3px solid var(--border-light);
            padding-left: 12px; color: var(--text-secondary);
            margin-bottom: 8px;
        }
        .md-content strong { font-weight: 700; }
        .md-content em { font-style: italic; }
        .md-content hr { border: none; border-top: 1px solid var(--border); margin: 12px 0; }

        /* ── Relations graph canvas ──────────────────── */
        #relations-canvas {
            width: 100%; height: 480px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            cursor: grab; display: block;
        }
        #relations-canvas:active { cursor: grabbing; }

        .graph-controls {
            display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap;
            align-items: center;
        }

        /* ── Embeddings health ───────────────────────── */
        .emb-health-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 12px; margin-bottom: 20px;
        }

        /* ── Action buttons ──────────────────────────── */
        .btn {
            padding: 6px 14px;
            background: var(--accent); color: #0a0f1e;
            border: none; border-radius: 6px;
            font-size: 0.82rem; font-weight: 700;
            cursor: pointer; transition: background .12s;
        }
        .btn:hover { background: var(--accent-dark); }
        .btn-ghost {
            background: none;
            border: 1px solid var(--border-light);
            color: var(--text-primary);
        }
        .btn-ghost:hover { background: var(--bg-card); }
        .btn-danger { background: rgba(248,113,113,.15); color: var(--red); border: 1px solid var(--red); }
        .btn-danger:hover { background: rgba(248,113,113,.25); }

        /* ── Misc ────────────────────────────────────── */
        a { color: var(--accent); text-decoration: none; }
        a:hover { text-decoration: underline; }
        .text-muted { color: var(--text-secondary); }
        .mono { font-family: "Cascadia Code","Consolas",monospace; font-size: 0.82rem; }
        .core-star { color: var(--yellow); font-size: 0.85rem; }

        /* Toast */
        #toast {
            position: fixed; bottom: 20px; right: 20px;
            background: var(--bg-card); border: 1px solid var(--border-light);
            color: var(--text-primary); padding: 10px 16px;
            border-radius: 8px; font-size: 0.82rem;
            box-shadow: var(--shadow);
            transform: translateY(80px); opacity: 0;
            transition: all .25s ease; z-index: 9999;
        }
        #toast.show { transform: translateY(0); opacity: 1; }
    </style>
</head>
<body>

<!-- ── Sidebar ─────────────────────────────────────── -->
<nav class="sidebar">
    <div class="logo">⚡ SALTMDB <span>v0.1.0</span></div>
    <div class="nav-section">Views</div>
    <ul class="nav-list">
        <li class="nav-item active" id="nav-dashboard" onclick="switchView('dashboard')">
            <span class="nav-icon">📊</span>Dashboard
        </li>
        <li class="nav-item" id="nav-entities" onclick="switchView('entities')">
            <span class="nav-icon">🧠</span>Memories
        </li>
        <li class="nav-item" id="nav-events" onclick="switchView('events')">
            <span class="nav-icon">📜</span>Event Log
        </li>
        <li class="nav-item" id="nav-relations" onclick="switchView('relations')">
            <span class="nav-icon">🕸️</span>Relations Graph
        </li>
        <li class="nav-item" id="nav-embeddings" onclick="switchView('embeddings')">
            <span class="nav-icon">🔬</span>Embeddings
        </li>
        <li class="nav-item" id="nav-tags" onclick="switchView('tags')">
            <span class="nav-icon">🏷️</span>Tags
        </li>
        <li class="nav-item" id="nav-locks" onclick="switchView('locks')">
            <span class="nav-icon">🔒</span>Locks
        </li>
    </ul>
    <div class="sidebar-footer">Local-First Memory DB</div>
</nav>

<!-- ── Main ───────────────────────────────────────── -->
<div class="main">
    <div class="topbar">
        <div class="topbar-left">
            <span class="page-title" id="page-title">Dashboard</span>
            <div class="refresh-indicator">
                <span class="refresh-dot" id="refresh-dot"></span>
                <span id="refresh-label"></span>
            </div>
        </div>
        <div class="search-wrap">
            <span class="search-icon">🔍</span>
            <input type="text" id="global-search" placeholder="Search memories..." oninput="handleSearchInput(this.value)" onkeydown="handleSearchKey(event)">
            <button class="search-clear" id="search-clear-btn" onclick="clearSearch()" title="Clear search">×</button>
        </div>
    </div>

    <div class="content" id="content"></div>
</div>

<!-- ── Entity Modal ───────────────────────────────── -->
<div class="modal-overlay" id="entity-modal" onclick="handleModalOverlayClick(event)">
    <div class="modal">
        <div class="modal-head">
            <h3 id="modal-title">Memory Detail</h3>
            <button class="modal-close" onclick="closeModal()">×</button>
        </div>
        <div class="modal-tabs" id="modal-tabs"></div>
        <div class="modal-body" id="modal-body"></div>
    </div>
</div>

<div id="toast"></div>

<script>
// ═══════════════════════════════════════════════════
// State & Constants
// ═══════════════════════════════════════════════════
let currentView = 'dashboard';
let _refreshTimer = null;
let _searchDebounce = null;

const entitiesState = { page: 1, status: '', owner: '', tag: '', core_only: false };

// ═══════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════
async function api(endpoint) {
    try {
        const r = await fetch('/api/' + endpoint);
        return await r.json();
    } catch(e) {
        console.error('API error:', e);
        return null;
    }
}

// ═══════════════════════════════════════════════════
// Toast
// ═══════════════════════════════════════════════════
function toast(msg, ms=2200) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), ms);
}

// ═══════════════════════════════════════════════════
// Skeleton / Empty State
// ═══════════════════════════════════════════════════
function skeleton(rows=6) {
    return '<div class="table-wrap">' +
        Array(rows).fill(`<div class="skeleton-row"><div class="skeleton" style="width:${60+Math.random()*30|0}%"></div></div>`).join('') +
    '</div>';
}

function emptyState(icon, title, sub='') {
    return `<div class="empty-state">
        <div class="e-icon">${icon}</div>
        <div class="e-title">${title}</div>
        ${sub ? `<div class="e-sub">${sub}</div>` : ''}
    </div>`;
}

// ═══════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════
function esc(str) {
    return String(str||'').replace(/[&<>'"]/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]||c));
}

function statusBadge(s) {
    const cls = {'raw':'raw','consolidated':'consolidated','archived':'archived'}[s] || 'default';
    return `<span class="badge badge-${cls}">${esc(s)}</span>`;
}

function embBadge(s) {
    s = s || 'pending';
    const cls = {'ready':'ready','pending':'pending','failed':'failed'}[s] || 'default';
    return `<span class="badge badge-${cls}">${s}</span>`;
}

function eventBadge(t) {
    const cls = {'decision':'decision','issue':'issue','fix':'fix','attempt':'attempt'}[t] || 'default';
    return `<span class="badge badge-${cls}">${esc(t)}</span>`;
}

function fmtDate(d) {
    if (!d) return '—';
    const dt = new Date(d.replace(' ','T')+'Z');
    if (isNaN(dt)) return d;
    return dt.toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}

async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        toast('✅ Copied to clipboard');
    } catch(e) {
        toast('Copy failed (check browser permissions)');
    }
}

// ═══════════════════════════════════════════════════
// Minimal Markdown Renderer (no CDN)
// ═══════════════════════════════════════════════════
function renderMd(raw) {
    if (!raw) return '';
    let s = esc(raw);
    // Restore &amp; for code blocks first pass protection — use placeholder
    // code fences
    const fences = [];
    s = s.replace(/```(?:[a-z]*)\n([\s\S]*?)```/g, (_, code) => {
        fences.push(code);
        return `\x00FENCE${fences.length-1}\x00`;
    });
    // inline code
    const inlines = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => {
        inlines.push(c);
        return `\x00INLINE${inlines.length-1}\x00`;
    });
    // headings
    s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    s = s.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
    s = s.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
    // bold/italic
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*(.+?)\*/g,     '<em>$1</em>');
    // blockquote
    s = s.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    // hr
    s = s.replace(/^---+$/gm, '<hr>');
    // lists
    s = s.replace(/^[-*•] (.+)$/gm, '<li>$1</li>');
    s = s.replace(/(<li>.*?<\/li>\n?)+/gs, m => '<ul>' + m + '</ul>');
    s = s.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
    // paragraphs
    s = s.replace(/\n\n+/g, '</p><p>');
    s = '<p>' + s + '</p>';
    s = s.replace(/<p><h/g, '<h').replace(/<\/h(\d)><\/p>/g, '</h$1>');
    s = s.replace(/<p><ul>/g, '<ul>').replace(/<\/ul><\/p>/g, '</ul>');
    s = s.replace(/<p><hr><\/p>/g, '<hr>');
    s = s.replace(/<p><\/p>/g, '');
    // restore fences
    s = s.replace(/\x00FENCE(\d+)\x00/g, (_, i) =>
        `<pre><code>${fences[i]}</code></pre>`);
    s = s.replace(/\x00INLINE(\d+)\x00/g, (_, i) =>
        `<code>${inlines[i]}</code>`);
    return s;
}

// ═══════════════════════════════════════════════════
// Search
// ═══════════════════════════════════════════════════
function handleSearchInput(val) {
    const clearBtn = document.getElementById('search-clear-btn');
    clearBtn.style.display = val ? 'block' : 'none';
    clearTimeout(_searchDebounce);
    if (!val.trim()) return;
    _searchDebounce = setTimeout(() => runSearch(val.trim()), 350);
}

function handleSearchKey(e) {
    if (e.key === 'Escape') clearSearch();
}

function clearSearch() {
    const inp = document.getElementById('global-search');
    inp.value = '';
    document.getElementById('search-clear-btn').style.display = 'none';
    switchView(currentView);
}

async function runSearch(q) {
    stopAutoRefresh();
    setNav(null);
    document.getElementById('page-title').textContent = `Results for "${q}"`;
    const content = document.getElementById('content');
    content.innerHTML = skeleton();
    const data = await api('search?q=' + encodeURIComponent(q));
    if (!data || !data.results || !data.results.length) {
        content.innerHTML = emptyState('🔍', 'No results found', `No memories matched "${q}"`);
        return;
    }
    content.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead><tr><th>Title</th><th>Owner</th><th>Status</th><th>Snippet</th></tr></thead>
                <tbody>
                    ${data.results.map(r => `
                        <tr class="clickable" onclick="showEntity('${esc(r.id)}')">
                            <td><strong>${esc(r.title)}</strong></td>
                            <td class="text-muted">${esc(r.owner_id||'system')}</td>
                            <td>${statusBadge(r.status)}</td>
                            <td class="td-trunc text-muted">${esc(r.snippet||r.full_content||'').slice(0,120)}</td>
                        </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
}

// ═══════════════════════════════════════════════════
// Auto-refresh
// ═══════════════════════════════════════════════════
function startAutoRefresh(interval=30000) {
    stopAutoRefresh();
    document.getElementById('refresh-dot').classList.add('active');
    document.getElementById('refresh-label').textContent = 'Auto-refresh on';
    _refreshTimer = setInterval(() => {
        if (currentView === 'dashboard') switchView('dashboard');
    }, interval);
}

function stopAutoRefresh() {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    document.getElementById('refresh-dot').classList.remove('active');
    document.getElementById('refresh-label').textContent = '';
}

// ═══════════════════════════════════════════════════
// Navigation
// ═══════════════════════════════════════════════════
function setNav(view) {
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    if (view) {
        const el = document.getElementById('nav-' + view);
        if (el) el.classList.add('active');
    }
}

async function switchView(view) {
    currentView = view;
    setNav(view);
    document.getElementById('page-title').textContent = {
        dashboard:'Dashboard', entities:'Memories', events:'Event Log',
        relations:'Relations Graph', embeddings:'Embeddings Health',
        tags:'Tags Folksonomy', locks:'System Locks'
    }[view] || view;

    const content = document.getElementById('content');
    if (view !== 'dashboard') stopAutoRefresh();

    if (view === 'dashboard')     await renderDashboard(content);
    else if (view === 'entities') await renderEntities(content);
    else if (view === 'events')   await renderEvents(content);
    else if (view === 'relations')await renderRelations(content);
    else if (view === 'embeddings')await renderEmbeddings(content);
    else if (view === 'tags')     await renderTags(content);
    else if (view === 'locks')    await renderLocks(content);
}

// ═══════════════════════════════════════════════════
// DASHBOARD
// ═══════════════════════════════════════════════════
async function renderDashboard(content) {
    content.innerHTML = skeleton(4);
    const [stats, recent] = await Promise.all([api('stats'), api('entities?page=1')]);
    const s = stats || {};
    const total_active = (s.raw_count||0) + (s.consolidated_count||0);
    const emb_total = (s.embeddings_ready||0)+(s.embeddings_pending||0)+(s.embeddings_failed||0);
    const emb_pct = emb_total > 0 ? Math.round(s.embeddings_ready/emb_total*100) : 0;

    content.innerHTML = `
        <div class="stats-grid">
            <div class="stat-card">
                <div class="sc-label">🟡 Raw</div>
                <div class="sc-value">${s.raw_count||0}</div>
                <div class="sc-sub">unprocessed memories</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">✅ Consolidated</div>
                <div class="sc-value" style="color:var(--green)">${s.consolidated_count||0}</div>
                <div class="sc-sub">synthesized facts</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">🗂️ Archived</div>
                <div class="sc-value" style="color:var(--text-secondary)">${s.archived_count||0}</div>
                <div class="sc-sub">historical versions</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">📜 Events</div>
                <div class="sc-value">${s.total_events||0}</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">🕸️ Relations</div>
                <div class="sc-value">${s.total_relations||0}</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">🔬 Embeddings</div>
                <div class="sc-value" style="color:var(--accent-purple)">${emb_pct}%</div>
                <div class="sc-sub">${s.embeddings_ready||0} / ${emb_total} ready</div>
            </div>
        </div>

        <div class="section-header">
            <span class="section-title">Recent Memories</span>
            <button class="section-link" onclick="switchView('entities')">View all →</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Title</th><th>Owner</th><th>Status</th><th>Updated</th><th>Tags</th></tr></thead>
                <tbody>
                    ${(recent?.entities||[]).slice(0,8).map(e => `
                        <tr class="clickable" onclick="showEntity('${esc(e.id)}')">
                            <td>${e.is_core?'<span class="core-star" title="Core memory">⭐</span> ':''}<strong>${esc(e.title)}</strong></td>
                            <td class="text-muted">${esc(e.owner_id||'system')}</td>
                            <td>${statusBadge(e.status)}</td>
                            <td class="text-muted">${fmtDate(e.updated_at)}</td>
                            <td>${(e.tags||[]).slice(0,3).map(t=>`<span class="tag" onclick="filterByTag(event,'${esc(t)}')">${esc(t)}</span>`).join('')}</td>
                        </tr>`).join('') || `<tr><td colspan="5">${emptyState('🧠','No memories yet','Store your first memory via an agent')}</td></tr>`}
                </tbody>
            </table>
        </div>`;

    startAutoRefresh();
}

// ═══════════════════════════════════════════════════
// ENTITIES / MEMORIES
// ═══════════════════════════════════════════════════
async function renderEntities(content, preserveFilters=false) {
    if (!preserveFilters) {
        entitiesState.page = 1;
    }

    content.innerHTML = `
        <div class="filter-bar">
            <select class="filter-select" id="f-status" onchange="entitiesState.status=this.value;entitiesState.page=1;reloadEntities()">
                <option value="">All Statuses</option>
                <option value="raw">Raw</option>
                <option value="consolidated">Consolidated</option>
                <option value="archived">Archived</option>
            </select>
            <input class="filter-input" id="f-owner" placeholder="Filter owner…"
                   value="${esc(entitiesState.owner)}"
                   oninput="entitiesState.owner=this.value;entitiesState.page=1;debouncedReload()">
            <input class="filter-input" id="f-tag" placeholder="Filter tag (e.g. #core)…"
                   value="${esc(entitiesState.tag)}"
                   oninput="entitiesState.tag=this.value;entitiesState.page=1;debouncedReload()">
            <label class="filter-label">
                <input type="checkbox" id="f-core" ${entitiesState.core_only?'checked':''}
                       onchange="entitiesState.core_only=this.checked;entitiesState.page=1;reloadEntities()">
                ⭐ Core only
            </label>
        </div>
        <div id="entities-body">${skeleton()}</div>
    `;

    // Restore select value
    if (entitiesState.status) document.getElementById('f-status').value = entitiesState.status;

    await _fetchAndRenderEntities();
}

let _entityDebounce = null;
function debouncedReload() {
    clearTimeout(_entityDebounce);
    _entityDebounce = setTimeout(reloadEntities, 320);
}

async function reloadEntities() { await _fetchAndRenderEntities(); }

async function _fetchAndRenderEntities() {
    const qs = new URLSearchParams({ page: entitiesState.page });
    if (entitiesState.status) qs.set('status', entitiesState.status);
    if (entitiesState.owner)  qs.set('owner_id', entitiesState.owner);
    if (entitiesState.core_only) qs.set('is_core', '1');
    // Tag filter not natively supported by /api/entities query — we filter client-side for now
    const data = await api('entities?' + qs);
    let entities = data?.entities || [];

    if (entitiesState.tag) {
        const tagLower = entitiesState.tag.toLowerCase();
        entities = entities.filter(e => (e.tags||[]).some(t => t.toLowerCase().includes(tagLower)));
    }

    const container = document.getElementById('entities-body');
    if (!container) return;

    if (!entities.length) {
        container.innerHTML = emptyState('🧠', 'No memories found', 'Try changing your filters');
        return;
    }

    const total = data?.total_count || entities.length;
    const totalPages = data?.total_pages || 1;

    container.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th></th>
                        <th>Title</th>
                        <th>Owner</th>
                        <th>Status</th>
                        <th>Scope</th>
                        <th>Weight</th>
                        <th>Emb.</th>
                        <th>Updated</th>
                        <th>Tags</th>
                    </tr>
                </thead>
                <tbody>
                    ${entities.map(e => `
                        <tr class="clickable" onclick="showEntity('${esc(e.id)}')">
                            <td style="width:20px;">${e.is_core?'<span class="core-star" title="Core memory">⭐</span>':''}</td>
                            <td><strong>${esc(e.title)}</strong></td>
                            <td class="text-muted">${esc(e.owner_id||'system')}</td>
                            <td>${statusBadge(e.status)}</td>
                            <td class="text-muted">${esc(e.scope||'shared')}</td>
                            <td class="text-muted">${e.weight||1}</td>
                            <td>${embBadge(e.embedding_status)}</td>
                            <td class="text-muted" style="white-space:nowrap;">${fmtDate(e.updated_at)}</td>
                            <td>${(e.tags||[]).slice(0,3).map(t=>`<span class="tag" onclick="filterByTag(event,'${esc(t)}')">${esc(t)}</span>`).join('')}</td>
                        </tr>`).join('')}
                </tbody>
            </table>
        </div>
        <div class="pagination">
            <button class="page-btn" onclick="entitiesPage(1)" ${entitiesState.page<=1?'disabled':''}>«</button>
            <button class="page-btn" onclick="entitiesPage(${entitiesState.page-1})" ${entitiesState.page<=1?'disabled':''}>‹ Prev</button>
            <span class="page-info">Page ${entitiesState.page} of ${totalPages} (${total} total)</span>
            <button class="page-btn" onclick="entitiesPage(${entitiesState.page+1})" ${entitiesState.page>=totalPages?'disabled':''}>Next ›</button>
            <button class="page-btn" onclick="entitiesPage(${totalPages})" ${entitiesState.page>=totalPages?'disabled':''}>»</button>
        </div>`;
}

function entitiesPage(p) {
    entitiesState.page = p;
    reloadEntities();
}

function filterByTag(e, tag) {
    e.stopPropagation();
    entitiesState.tag = tag;
    entitiesState.page = 1;
    switchView('entities');
}

// ═══════════════════════════════════════════════════
// EVENTS
// ═══════════════════════════════════════════════════
async function renderEvents(content) {
    content.innerHTML = skeleton();
    const data = await api('events');
    const events = data?.events || [];
    if (!events.length) { content.innerHTML = emptyState('📜','No events logged yet','Events are logged automatically by agents'); return; }
    content.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead><tr><th>Timestamp</th><th>Agent</th><th>Type</th><th>Context</th><th>Content</th></tr></thead>
                <tbody>
                    ${events.map(ev => `
                        <tr>
                            <td class="text-muted" style="white-space:nowrap;">${fmtDate(ev.timestamp)}</td>
                            <td><strong>${esc(ev.agent_id||'system')}</strong></td>
                            <td>${eventBadge(ev.type)}</td>
                            <td class="text-muted">${esc(ev.context_id||'—')}</td>
                            <td class="td-trunc text-muted">${esc((ev.content||'').slice(0,120))}</td>
                        </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
}

// ═══════════════════════════════════════════════════
// RELATIONS GRAPH (pure-JS Canvas force layout)
// ═══════════════════════════════════════════════════
let _graphSimRunning = false;

async function renderRelations(content) {
    content.innerHTML = `
        <div class="graph-controls">
            <button class="btn btn-ghost" onclick="resetGraphView()">⟲ Reset View</button>
            <span class="text-muted" style="font-size:0.78rem;">Drag to pan · Scroll to zoom · Click node to inspect</span>
        </div>
        <canvas id="relations-canvas"></canvas>
        <div id="relations-table-fallback" style="margin-top:16px;display:none;"></div>`;
    const data = await api('relations');
    const rels = data?.relations || [];
    if (!rels.length) {
        content.innerHTML = emptyState('🕸️','No relations yet','Relations are created via store_relation()');
        return;
    }
    initGraph(rels);
}

function initGraph(relations) {
    const canvas = document.getElementById('relations-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // DPI scaling
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const W = rect.width || 800, H = rect.height || 480;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.scale(dpr, dpr);

    // Build node set
    const nodeMap = {};
    relations.forEach(r => {
        if (!nodeMap[r.source_id]) nodeMap[r.source_id] = { id:r.source_id, title:r.source_title||r.source_id, x:W/2+(Math.random()-.5)*300, y:H/2+(Math.random()-.5)*200, vx:0, vy:0, r:22 };
        if (!nodeMap[r.target_id]) nodeMap[r.target_id] = { id:r.target_id, title:r.target_title||r.target_id, x:W/2+(Math.random()-.5)*300, y:H/2+(Math.random()-.5)*200, vx:0, vy:0, r:22 };
    });
    const nodes = Object.values(nodeMap);
    const edges = relations.map(r => ({ s: nodeMap[r.source_id], t: nodeMap[r.target_id], label: r.predicate }));

    // Pan / zoom state
    let ox=0, oy=0, scale=1;
    let dragging=false, dragNode=null, lastMx=0, lastMy=0;

    function toWorld(mx, my) { return { x:(mx-W/2-ox)/scale+W/2, y:(my-H/2-oy)/scale+H/2 }; }
    function nodeAt(mx,my) {
        const w = toWorld(mx,my);
        return nodes.find(n => Math.hypot(n.x-w.x, n.y-w.y) < n.r+4);
    }

    canvas.onmousedown = e => {
        const rect2 = canvas.getBoundingClientRect();
        const mx = e.clientX-rect2.left, my = e.clientY-rect2.top;
        dragNode = nodeAt(mx,my);
        dragging = true; lastMx=mx; lastMy=my;
    };
    canvas.onmousemove = e => {
        if (!dragging) return;
        const rect2 = canvas.getBoundingClientRect();
        const mx = e.clientX-rect2.left, my = e.clientY-rect2.top;
        const dx=mx-lastMx, dy=my-lastMy;
        if (dragNode) { dragNode.x += dx/scale; dragNode.y += dy/scale; dragNode.vx=0; dragNode.vy=0; }
        else { ox+=dx; oy+=dy; }
        lastMx=mx; lastMy=my;
    };
    canvas.onmouseup = e => {
        if (!dragging) return;
        const rect2 = canvas.getBoundingClientRect();
        const mx = e.clientX-rect2.left, my = e.clientY-rect2.top;
        if (Math.hypot(mx-lastMx,my-lastMy)<3 && dragNode) showEntity(dragNode.id);
        dragging=false; dragNode=null;
    };
    canvas.onwheel = e => {
        e.preventDefault();
        scale = Math.max(.25, Math.min(3, scale * (e.deltaY<0?1.12:.89)));
    };

    window._graphReset = () => { ox=0; oy=0; scale=1; };

    // Force simulation
    let frame = 0;
    function tick() {
        if (!document.getElementById('relations-canvas')) { _graphSimRunning=false; return; }
        frame++;
        const cool = Math.max(0.1, 1 - frame/1200);

        // Repulsion
        for (let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++) {
            const dx=nodes[j].x-nodes[i].x, dy=nodes[j].y-nodes[i].y;
            const dist=Math.max(1,Math.hypot(dx,dy));
            const force = 2400/(dist*dist);
            nodes[i].vx -= dx/dist*force; nodes[i].vy -= dy/dist*force;
            nodes[j].vx += dx/dist*force; nodes[j].vy += dy/dist*force;
        }
        // Attraction (springs)
        edges.forEach(({s,t}) => {
            const dx=t.x-s.x, dy=t.y-s.y, dist=Math.max(1,Math.hypot(dx,dy));
            const force=(dist-120)*0.03;
            const fx=dx/dist*force, fy=dy/dist*force;
            s.vx+=fx; s.vy+=fy; t.vx-=fx; t.vy-=fy;
        });
        // Center gravity
        nodes.forEach(n => {
            n.vx += (W/2-n.x)*0.005;
            n.vy += (H/2-n.y)*0.005;
        });
        // Integrate
        nodes.forEach(n => {
            if (dragNode===n) return;
            n.x += n.vx*cool; n.y += n.vy*cool;
            n.vx*=.7; n.vy*=.7;
            n.x=Math.max(n.r,Math.min(W-n.r,n.x));
            n.y=Math.max(n.r,Math.min(H-n.r,n.y));
        });

        // Draw
        ctx.save();
        ctx.clearRect(0,0,W,H);
        ctx.translate(W/2+ox,H/2+oy);
        ctx.scale(scale,scale);
        ctx.translate(-W/2,-H/2);

        // Edges
        edges.forEach(({s,t,label}) => {
            // Arrow
            const ang = Math.atan2(t.y-s.y, t.x-s.x);
            const ex = t.x - Math.cos(ang)*t.r, ey = t.y - Math.sin(ang)*t.r;
            const sx = s.x + Math.cos(ang)*s.r, sy = s.y + Math.sin(ang)*s.r;
            ctx.beginPath(); ctx.moveTo(sx,sy); ctx.lineTo(ex,ey);
            ctx.strokeStyle='rgba(56,189,248,.4)'; ctx.lineWidth=1.5; ctx.stroke();
            // Arrowhead
            ctx.beginPath();
            ctx.moveTo(ex,ey);
            ctx.lineTo(ex-10*Math.cos(ang-.45), ey-10*Math.sin(ang-.45));
            ctx.lineTo(ex-10*Math.cos(ang+.45), ey-10*Math.sin(ang+.45));
            ctx.closePath(); ctx.fillStyle='rgba(56,189,248,.6)'; ctx.fill();
            // Label
            const mx=(sx+ex)/2, my=(sy+ey)/2;
            ctx.save();
            ctx.translate(mx,my); ctx.rotate(ang);
            ctx.font='9px "Segoe UI",system-ui,sans-serif';
            ctx.fillStyle='rgba(148,163,184,.9)';
            ctx.textAlign='center'; ctx.textBaseline='bottom';
            ctx.fillText(label||'',-0,-3);
            ctx.restore();
        });

        // Nodes
        nodes.forEach(n => {
            ctx.beginPath();
            ctx.arc(n.x,n.y,n.r,0,Math.PI*2);
            ctx.fillStyle='#1e2a3a'; ctx.fill();
            ctx.strokeStyle='#38bdf8'; ctx.lineWidth=1.5; ctx.stroke();
            // Label
            ctx.font='bold 10px "Segoe UI",system-ui,sans-serif';
            ctx.fillStyle='#f1f5f9';
            ctx.textAlign='center'; ctx.textBaseline='middle';
            const label = n.title.length>16 ? n.title.slice(0,15)+'…' : n.title;
            ctx.fillText(label, n.x, n.y);
        });

        ctx.restore();
        requestAnimationFrame(tick);
    }
    _graphSimRunning = true;
    tick();
}

function resetGraphView() { if (window._graphReset) window._graphReset(); }

// ═══════════════════════════════════════════════════
// EMBEDDINGS HEALTH
// ═══════════════════════════════════════════════════
async function renderEmbeddings(content) {
    content.innerHTML = skeleton(3);
    const stats = await api('embeddings_stats');
    if (!stats) { content.innerHTML = emptyState('🔬','Could not load embedding stats'); return; }
    const total = (stats.ready||0)+(stats.pending||0)+(stats.failed||0)+(stats.null||0);
    const pct = total>0 ? Math.round(stats.ready/total*100) : 0;

    content.innerHTML = `
        <div class="emb-health-grid">
            <div class="stat-card">
                <div class="sc-label">✅ Ready</div>
                <div class="sc-value" style="color:var(--green)">${stats.ready||0}</div>
                <div class="sc-sub">vectors stored</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">⏳ Pending</div>
                <div class="sc-value" style="color:var(--yellow)">${stats.pending||0}</div>
                <div class="sc-sub">awaiting generation</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">❌ Failed</div>
                <div class="sc-value" style="color:var(--red)">${stats.failed||0}</div>
                <div class="sc-sub">error during embed</div>
            </div>
            <div class="stat-card">
                <div class="sc-label">📊 Coverage</div>
                <div class="sc-value" style="color:var(--accent-purple)">${pct}%</div>
                <div class="sc-sub">${stats.ready||0} / ${total} active</div>
            </div>
        </div>

        <div class="table-wrap" style="padding:20px;">
            <div class="modal-section-title" style="margin-top:0;">Embedding Coverage</div>
            <div style="display:flex;justify-content:space-between;font-size:0.78rem;color:var(--text-secondary);margin-bottom:6px;">
                <span>Ready ${stats.ready||0}</span><span>${pct}%</span>
            </div>
            <div class="progress-wrap">
                <div class="progress-bar" style="width:${pct}%;background:var(--green);"></div>
            </div>

            ${stats.pending>0 ? `
            <div style="margin-top:16px;padding:12px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:6px;font-size:0.82rem;">
                ⚠️ <strong>${stats.pending}</strong> entities have pending embeddings.
                Run the one-time backfill to generate them:
                <pre style="margin-top:8px;background:var(--bg-primary);padding:8px;border-radius:4px;font-size:0.78rem;">python scratch/backfill_embeddings.py</pre>
                Or enable <code>SALTMDB_ENABLE_SEMANTIC=true</code> to start live generation on new writes.
            </div>` : ''}

            ${stats.failed>0 ? `
            <div style="margin-top:12px;padding:12px;background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.2);border-radius:6px;font-size:0.82rem;">
                ❌ <strong>${stats.failed}</strong> entities failed embedding. Check <code>~/.saltmdb/viewer.log</code> for details.
                Failed entities fall back to FTS5-only search automatically.
            </div>` : ''}

            ${pct===100 && !stats.failed ? `
            <div style="margin-top:12px;padding:12px;background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.2);border-radius:6px;font-size:0.82rem;">
                ✅ All active memories have embeddings. Hybrid RRF search is fully operational.
            </div>` : ''}
        </div>`;
}

// ═══════════════════════════════════════════════════
// TAGS
// ═══════════════════════════════════════════════════
async function renderTags(content) {
    content.innerHTML = skeleton(3);
    const data = await api('tags');
    const tags = data?.tags || [];
    if (!tags.length) { content.innerHTML = emptyState('🏷️','No tags yet','Tags are created when storing memories'); return; }
    content.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;">
            ${tags.map(t => `
                <div class="stat-card" style="cursor:pointer;" onclick="filterByTagGlobal('${esc(t.name)}')">
                    <div class="sc-label">${t.canonical_id?'🔀 Alias':'🏷️ Tag'}</div>
                    <div style="font-size:0.95rem;font-weight:700;color:var(--accent);margin-bottom:4px;">${esc(t.name)}</div>
                    <div class="sc-sub">${t.usage_count} memor${t.usage_count===1?'y':'ies'}</div>
                    ${t.canonical_id?`<div style="font-size:0.7rem;color:var(--text-secondary);margin-top:4px;">→ canonical</div>`:''}
                </div>`).join('')}
        </div>`;
}

function filterByTagGlobal(tag) {
    entitiesState.tag = tag;
    entitiesState.page = 1;
    switchView('entities');
}

// ═══════════════════════════════════════════════════
// LOCKS
// ═══════════════════════════════════════════════════
async function renderLocks(content) {
    content.innerHTML = skeleton(2);
    const data = await api('locks');
    const locks = data?.locks || [];
    if (!locks.length) { content.innerHTML = emptyState('🔒','No system locks','Locks are created by the Librarian GC worker'); return; }
    content.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead><tr><th>Task</th><th>Locked At</th><th>PID</th><th>Last Run</th></tr></thead>
                <tbody>
                    ${locks.map(l => `
                        <tr>
                            <td><strong>${esc(l.task_name)}</strong></td>
                            <td class="text-muted">${fmtDate(l.locked_at)||'Unlocked'}</td>
                            <td class="text-muted mono">${l.locked_by_pid||'N/A'}</td>
                            <td class="text-muted">${fmtDate(l.last_run_at)||'N/A'}</td>
                        </tr>`).join('')}
                </tbody>
            </table>
        </div>`;
}

// ═══════════════════════════════════════════════════
// ENTITY DETAIL MODAL
// ═══════════════════════════════════════════════════
let _modalTabData = {};

async function showEntity(id) {
    const data = await api('entities/' + id);
    if (!data || data.error) { toast('⚠️ Error loading memory'); return; }

    _modalTabData = data;
    document.getElementById('modal-title').textContent = data.title || 'Untitled Memory';

    // Build tabs
    const tabs = [
        { id:'overview', label:'Overview' },
        { id:'content',  label:'Content' },
        { id:'relations', label:`Relations (${(data.relations?.all||[]).length})` },
        ...(data.parent_ids?.length ? [{ id:'lineage', label:'Lineage' }] : [])
    ];
    document.getElementById('modal-tabs').innerHTML = tabs.map((t,i) =>
        `<div class="modal-tab${i===0?' active':''}" id="mtab-${t.id}" onclick="switchModalTab('${t.id}')">${t.label}</div>`
    ).join('');

    renderModalTab('overview', data);
    document.getElementById('entity-modal').classList.add('active');
}

function switchModalTab(tabId) {
    document.querySelectorAll('.modal-tab').forEach(el => el.classList.remove('active'));
    const activeTab = document.getElementById('mtab-' + tabId);
    if (activeTab) activeTab.classList.add('active');
    renderModalTab(tabId, _modalTabData);
}

function renderModalTab(tabId, data) {
    const body = document.getElementById('modal-body');
    if (tabId === 'overview') {
        body.innerHTML = `
            <div class="meta-grid">
                <div>
                    <div class="meta-k">ID</div>
                    <div class="meta-v mono" style="font-size:0.72rem;">${esc(data.id)}
                        <button class="copy-btn" id="copy-uuid-btn" onclick="copyId('${esc(data.id)}')">📋 Copy</button>
                    </div>
                </div>
                <div><div class="meta-k">Owner</div><div class="meta-v">${esc(data.owner_id||'system')}</div></div>
                <div><div class="meta-k">Status</div><div class="meta-v">${statusBadge(data.status)}</div></div>
                <div><div class="meta-k">Scope</div><div class="meta-v">${esc(data.scope||'shared')}</div></div>
                <div><div class="meta-k">Weight</div><div class="meta-v">${data.weight||1}</div></div>
                <div><div class="meta-k">Is Core</div><div class="meta-v">${data.is_core?'⭐ Yes':'No'}</div></div>
                <div><div class="meta-k">Context</div><div class="meta-v">${esc(data.context_id||data.project_id||'—')}</div></div>
                <div><div class="meta-k">Embedding</div><div class="meta-v">${embBadge(data.embedding_status)}</div></div>
                <div><div class="meta-k">Created</div><div class="meta-v">${fmtDate(data.created_at)}</div></div>
                <div><div class="meta-k">Updated</div><div class="meta-v">${fmtDate(data.updated_at)}</div></div>
                <div><div class="meta-k">Valid From</div><div class="meta-v">${fmtDate(data.valid_from)||'—'}</div></div>
                <div><div class="meta-k">Valid To</div><div class="meta-v">${fmtDate(data.valid_to)||'∞ (active)'}</div></div>
            </div>
            <div class="modal-section-title">Tags</div>
            <div>${(data.tags||[]).map(t=>`<span class="tag" onclick="closeModal();filterByTagGlobal('${esc(t)}')">${esc(t)}</span>`).join('')||'<span class="text-muted">No tags</span>'}</div>
            ${data.metadata ? `
            <div class="modal-section-title">Metadata JSON</div>
            <pre style="background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:0.78rem;overflow:auto;">${esc(JSON.stringify(data.metadata,null,2))}</pre>
            ` : ''}`;
    } else if (tabId === 'content') {
        body.innerHTML = `
            <div class="modal-section-title" style="margin-top:0;">
                Rendered Markdown
                <button class="copy-btn" onclick="copyMd()">📋 Copy Raw</button>
            </div>
            <div class="md-content">${renderMd(data.full_content||'')}</div>`;
    } else if (tabId === 'relations') {
        const out = data.relations?.outgoing||[];
        const inc = data.relations?.incoming||[];
        body.innerHTML = `
            <div class="modal-section-title" style="margin-top:0;">Outgoing (${out.length})</div>
            ${out.length ? `<div class="table-wrap"><table><thead><tr><th>Predicate</th><th>Target</th></tr></thead><tbody>
                ${out.map(r=>`<tr class="clickable" onclick="showEntity('${esc(r.target_id)}')">
                    <td>${eventBadge(r.predicate)}</td>
                    <td style="color:var(--accent);">${esc(r.target_title||r.target_id)}</td>
                </tr>`).join('')}
            </tbody></table></div>` : '<p class="text-muted">No outgoing relations.</p>'}
            <div class="modal-section-title">Incoming (${inc.length})</div>
            ${inc.length ? `<div class="table-wrap"><table><thead><tr><th>Predicate</th><th>Source</th></tr></thead><tbody>
                ${inc.map(r=>`<tr class="clickable" onclick="showEntity('${esc(r.source_id)}')">
                    <td>${eventBadge(r.predicate)}</td>
                    <td style="color:var(--accent);">${esc(r.source_title||r.source_id)}</td>
                </tr>`).join('')}
            </tbody></table></div>` : '<p class="text-muted">No incoming relations.</p>'}`;
    } else if (tabId === 'lineage') {
        body.innerHTML = `<div class="text-muted" style="padding:12px;">Loading lineage…</div>`;
        api('entities/' + data.id + '/lineage').then(linData => {
            const nodes = linData?.ancestry_tree || [];
            document.getElementById('modal-body').innerHTML = nodes.length
                ? `<div class="modal-section-title" style="margin-top:0;">Consolidation Ancestry (${nodes.length} nodes)</div>` +
                  nodes.map(n=>`<div style="padding:10px;background:var(--bg-primary);border-radius:6px;margin-bottom:8px;font-size:0.82rem;">
                      <strong>Depth ${n.generation_depth}:</strong> ${esc(n.title)} ${statusBadge(n.status)}
                  </div>`).join('')
                : '<p class="text-muted">No lineage data available.</p>';
        });
    }
}

async function copyId(id) {
    await copyToClipboard(id);
    const btn = document.getElementById('copy-uuid-btn');
    if (btn) { btn.textContent='✅ Copied'; btn.classList.add('copied'); setTimeout(()=>{ btn.textContent='📋 Copy'; btn.classList.remove('copied'); }, 2000); }
}

async function copyMd() {
    await copyToClipboard(_modalTabData?.full_content||'');
}

function closeModal() {
    document.getElementById('entity-modal').classList.remove('active');
}

function handleModalOverlayClick(e) {
    if (e.target === document.getElementById('entity-modal')) closeModal();
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
    if (e.key === '/' && !e.target.matches('input,select,textarea')) {
        e.preventDefault();
        document.getElementById('global-search').focus();
    }
});

// Init
switchView('dashboard');
</script>
</body>
</html>"""
