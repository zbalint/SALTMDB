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
            --bg-primary:    #080c14;
            --bg-secondary:  #0f172a;
            --bg-card:       rgba(18, 26, 42, 0.7);
            --bg-card-hover: rgba(30, 41, 59, 0.85);
            --bg-solid:      #1e293b;
            --accent:        #38bdf8;
            --accent-glow:   rgba(56, 189, 248, 0.25);
            --accent-dark:   #0284c7;
            --purple:        #c084fc;
            --purple-glow:   rgba(192, 132, 252, 0.25);
            --text-primary:  #f8fafc;
            --text-secondary:#94a3b8;
            --text-muted:    #64748b;
            --border:        rgba(255, 255, 255, 0.08);
            --border-light:  rgba(255, 255, 255, 0.15);
            --green:         #34d399;
            --green-glow:    rgba(52, 211, 153, 0.2);
            --yellow:        #fbbf24;
            --yellow-glow:   rgba(251, 191, 36, 0.2);
            --red:           #f87171;
            --red-glow:      rgba(248, 113, 113, 0.2);
            --blue:          #60a5fa;
            --radius:        12px;
            --shadow:        0 8px 32px rgba(0, 0, 0, 0.6);
            --glass-border:  1px solid rgba(255, 255, 255, 0.08);
        }

        *, *::before, *::after {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background: var(--bg-primary);
            background-image: 
                radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.08) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(192, 132, 252, 0.08) 0px, transparent 50%);
            color: var(--text-primary);
            font-family: "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, sans-serif;
            font-size: 14px;
            display: flex;
            height: 100vh;
            overflow: hidden;
        }

        /* ── Sidebar ─────────────────────────────────── */
        .sidebar {
            width: 250px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(20px);
            border-right: var(--glass-border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            overflow: hidden;
            z-index: 10;
        }

        .logo {
            padding: 22px 20px 16px;
            font-size: 1.1rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: var(--accent);
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: var(--glass-border);
        }

        .logo-badge {
            background: var(--accent-glow);
            border: 1px solid var(--accent);
            color: var(--accent);
            font-size: 0.65rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            letter-spacing: 0.05em;
        }

        .logo span { color: var(--text-secondary); font-weight: 400; font-size: 0.8rem; }

        .nav-section {
            padding: 16px 14px 6px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-muted);
        }

        .nav-list { list-style: none; padding: 4px 10px; }

        .nav-item {
            padding: 10px 14px;
            border-radius: 8px;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            font-size: 0.875rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            user-select: none;
            margin-bottom: 3px;
        }

        .nav-item-left {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .nav-item:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
            transform: translateX(2px);
        }

        .nav-item.active {
            background: var(--accent-glow);
            color: var(--accent);
            font-weight: 600;
            border: 1px solid rgba(56, 189, 248, 0.3);
            box-shadow: 0 0 16px var(--accent-glow);
        }

        .nav-item .nav-icon { width: 18px; text-align: center; font-size: 1rem; }
        .nav-key {
            font-size: 0.65rem;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border);
            padding: 1px 5px;
            border-radius: 4px;
            color: var(--text-muted);
        }

        .sidebar-footer {
            margin-top: auto;
            padding: 14px 16px;
            border-top: var(--glass-border);
            font-size: 0.75rem;
            color: var(--text-muted);
            display: flex;
            flex-direction: column;
            gap: 6px;
            background: rgba(0, 0, 0, 0.2);
        }

        .sidebar-footer .status-indicator {
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--green);
            font-weight: 600;
        }

        .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 8px var(--green);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.85); }
        }

        /* ── Main Area ────────────────────────────────── */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            background: transparent;
        }

        .topbar {
            height: 60px;
            background: rgba(15, 23, 42, 0.7);
            backdrop-filter: blur(20px);
            border-bottom: var(--glass-border);
            padding: 0 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }

        .page-title {
            font-size: 1.15rem;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .topbar-actions {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .cmd-palette-btn {
            background: rgba(255, 255, 255, 0.05);
            border: var(--glass-border);
            color: var(--text-secondary);
            padding: 7px 14px;
            border-radius: 8px;
            font-size: 0.8rem;
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .cmd-palette-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text-primary);
            border-color: var(--border-light);
        }

        .view-container {
            flex: 1;
            overflow-y: auto;
            padding: 24px;
            display: none;
        }

        .view-container.active { display: block; }

        /* ── Bento Grid Dashboard ──────────────────────── */
        .bento-grid {
            display: grid;
            grid-template-columns: repeat(12, 1fr);
            gap: 18px;
            margin-bottom: 24px;
        }

        .bento-card {
            background: var(--bg-card);
            backdrop-filter: blur(16px);
            border: var(--glass-border);
            border-radius: var(--radius);
            padding: 20px;
            box-shadow: var(--shadow);
            transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
            position: relative;
            overflow: hidden;
        }

        .bento-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, var(--accent), transparent);
            opacity: 0;
            transition: opacity 0.3s ease;
        }

        .bento-card:hover {
            border-color: var(--border-light);
            transform: translateY(-2px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.7);
        }

        .bento-card:hover::before { opacity: 1; }

        .col-3 { grid-column: span 3; }
        .col-4 { grid-column: span 4; }
        .col-6 { grid-column: span 6; }
        .col-8 { grid-column: span 8; }
        .col-12 { grid-column: span 12; }

        @media (max-width: 1200px) {
            .col-3, .col-4 { grid-column: span 6; }
            .col-8 { grid-column: span 12; }
        }
        @media (max-width: 768px) {
            .col-3, .col-4, .col-6, .col-8 { grid-column: span 12; }
        }

        .stat-label {
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .stat-value {
            font-size: 2.1rem;
            font-weight: 800;
            color: var(--text-primary);
            letter-spacing: -0.03em;
            line-height: 1;
            margin-bottom: 6px;
        }

        .stat-desc {
            font-size: 0.75rem;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        /* ── Badge & Pill Component ───────────────────── */
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 3px 9px;
            border-radius: 20px;
            font-size: 0.72rem;
            font-weight: 600;
            line-height: 1;
        }

        .badge-green  { background: var(--green-glow); color: var(--green); border: 1px solid rgba(52, 211, 153, 0.3); }
        .badge-yellow { background: var(--yellow-glow); color: var(--yellow); border: 1px solid rgba(251, 191, 36, 0.3); }
        .badge-red    { background: var(--red-glow); color: var(--red); border: 1px solid rgba(248, 113, 113, 0.3); }
        .badge-blue   { background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); }
        .badge-purple { background: var(--purple-glow); color: var(--purple); border: 1px solid rgba(192, 132, 252, 0.3); }

        /* ── Table Styling ───────────────────────────── */
        .data-table-container {
            background: var(--bg-card);
            backdrop-filter: blur(16px);
            border: var(--glass-border);
            border-radius: var(--radius);
            overflow: hidden;
            box-shadow: var(--shadow);
        }

        .data-table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }

        .data-table th {
            background: rgba(15, 23, 42, 0.8);
            padding: 14px 18px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            border-bottom: var(--glass-border);
        }

        .data-table td {
            padding: 14px 18px;
            border-bottom: var(--glass-border);
            color: var(--text-primary);
            font-size: 0.85rem;
        }

        .data-table tr:last-child td { border-bottom: none; }
        .data-table tbody tr { transition: background 0.15s ease; cursor: pointer; }
        .data-table tbody tr:hover { background: rgba(255, 255, 255, 0.03); }

        /* ── Filter Toolbar ──────────────────────────── */
        .toolbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }

        .search-box {
            position: relative;
            flex: 1;
            min-width: 260px;
        }

        .search-box input {
            width: 100%;
            background: var(--bg-card);
            border: var(--glass-border);
            border-radius: 8px;
            padding: 9px 14px 9px 36px;
            color: var(--text-primary);
            font-size: 0.85rem;
            outline: none;
            transition: all 0.2s ease;
        }

        .search-box input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 12px var(--accent-glow);
        }

        .search-box .search-icon {
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
        }

        .btn {
            background: rgba(255, 255, 255, 0.06);
            border: var(--glass-border);
            color: var(--text-primary);
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.825rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .btn:hover {
            background: rgba(255, 255, 255, 0.12);
            border-color: var(--border-light);
        }

        .btn-primary {
            background: var(--accent-dark);
            border-color: var(--accent);
            color: #fff;
        }

        .btn-primary:hover {
            background: #0369a1;
            box-shadow: 0 0 16px var(--accent-glow);
        }

        /* ── Modal & Drawer ──────────────────────────── */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(12px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 100;
            padding: 24px;
        }

        .modal-overlay.active { display: flex; }

        .modal-card {
            background: #0f172a;
            border: var(--glass-border);
            border-radius: 16px;
            width: 100%;
            max-width: 800px;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 24px 64px rgba(0, 0, 0, 0.8);
            overflow: hidden;
        }

        .modal-header {
            padding: 20px 24px;
            border-bottom: var(--glass-border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .modal-body {
            padding: 24px;
            overflow-y: auto;
            flex: 1;
        }

        .close-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 1.2rem;
            cursor: pointer;
            padding: 4px;
        }

        .close-btn:hover { color: var(--text-primary); }

        pre {
            background: #080c14;
            border: var(--glass-border);
            padding: 16px;
            border-radius: 8px;
            color: #e2e8f0;
            font-family: "Cascadia Code", "Fira Code", Consolas, monospace;
            font-size: 0.825rem;
            overflow-x: auto;
            white-space: pre-wrap;
        }

        /* ── Donut Chart ─────────────────────────────── */
        .donut-chart-container {
            display: flex;
            align-items: center;
            justify-content: space-around;
            gap: 20px;
            padding: 10px 0;
        }

        .donut-chart {
            width: 130px;
            height: 130px;
            transform: rotate(-90deg);
        }

        .donut-segment {
            fill: transparent;
            stroke-width: 16;
            transition: stroke-dasharray 0.5s ease;
        }

        /* ── SVG Graph Topology ───────────────────────── */
        #graph-canvas {
            width: 100%;
            height: 620px;
            background: #080c14;
            border: var(--glass-border);
            border-radius: var(--radius);
            cursor: grab;
        }

        #graph-canvas:active { cursor: grabbing; }

        .node circle {
            fill: #1e293b;
            stroke: var(--accent);
            stroke-width: 2px;
            transition: all 0.2s ease;
            cursor: pointer;
        }

        .node:hover circle, .node.highlight circle {
            fill: var(--accent-dark);
            stroke: #fff;
            filter: drop-shadow(0 0 12px var(--accent));
        }

        .node.dimmed { opacity: 0.15; }
        .link.dimmed { opacity: 0.05; }

        .node text {
            fill: var(--text-primary);
            font-size: 11px;
            font-weight: 600;
            pointer-events: none;
            transition: opacity 0.2s ease;
        }

        .link {
            stroke: rgba(255, 255, 255, 0.18);
            stroke-width: 1.5px;
            transition: stroke 0.2s ease, opacity 0.2s ease;
        }

        .link.highlight {
            stroke: var(--accent);
            stroke-width: 2.5px;
        }

        .link-label {
            fill: var(--text-muted);
            font-size: 9px;
            font-weight: 600;
        }
    </style>
</head>
<body>

    <!-- ── Sidebar Navigation ────────────────────────────── -->
    <div class="sidebar">
        <div class="logo">
            ⚡ SALTMDB <span class="logo-badge">v0.1.0</span>
        </div>
        <div class="nav-section">Dashboard & Views</div>
        <ul class="nav-list">
            <li class="nav-item active" id="nav-dashboard" onclick="switchView('dashboard')">
                <div class="nav-item-left"><span class="nav-icon">📊</span> Overview</div>
                <span class="nav-key">1</span>
            </li>
            <li class="nav-item" id="nav-entities" onclick="switchView('entities')">
                <div class="nav-item-left"><span class="nav-icon">🧠</span> Memories</div>
                <span class="nav-key">2</span>
            </li>
            <li class="nav-item" id="nav-events" onclick="switchView('events')">
                <div class="nav-item-left"><span class="nav-icon">📜</span> Event Ledger</div>
                <span class="nav-key">3</span>
            </li>
            <li class="nav-item" id="nav-relations" onclick="switchView('relations')">
                <div class="nav-item-left"><span class="nav-icon">🕸️</span> Graph Topology</div>
                <span class="nav-key">4</span>
            </li>
            <li class="nav-item" id="nav-lineage" onclick="switchView('lineage')">
                <div class="nav-item-left"><span class="nav-icon">🌳</span> Lineage</div>
                <span class="nav-key">5</span>
            </li>
            <li class="nav-item" id="nav-embeddings" onclick="switchView('embeddings')">
                <div class="nav-item-left"><span class="nav-icon">🔍</span> Vector Playground</div>
                <span class="nav-key">6</span>
            </li>
            <li class="nav-item" id="nav-tags" onclick="switchView('tags')">
                <div class="nav-item-left"><span class="nav-icon">🏷️</span> Folksonomy Tags</div>
                <span class="nav-key">7</span>
            </li>
            <li class="nav-item" id="nav-locks" onclick="switchView('locks')">
                <div class="nav-item-left"><span class="nav-icon">🔒</span> System Locks</div>
                <span class="nav-key">8</span>
            </li>
        </ul>

        <div class="sidebar-footer">
            <div class="status-indicator">
                <div class="dot"></div> Server Connected
            </div>
            <div id="db-size-label">Database Size: Loading...</div>
        </div>
    </div>

    <!-- ── Main Workspace ────────────────────────────────── -->
    <div class="main-content">
        <!-- Topbar -->
        <div class="topbar">
            <div class="page-title" id="page-header-title">
                📊 Executive Dashboard Overview
            </div>
            <div class="topbar-actions">
                <button class="cmd-palette-btn" onclick="toggleCmdPalette()">
                    <span>🔍 Quick Search / Command Palette</span>
                    <span class="nav-key">Ctrl+K</span>
                </button>
                <button class="btn" onclick="refreshCurrentView()">🔄 Refresh</button>
            </div>
        </div>

        <!-- 1. Bento Dashboard View -->
        <div class="view-container active" id="view-dashboard">
            <div class="bento-grid">
                <!-- Stat Card 1 -->
                <div class="bento-card col-3">
                    <div class="stat-label">Total Long-Term Memories <span>🧠</span></div>
                    <div class="stat-value" id="dash-stat-entities">0</div>
                    <div class="stat-desc" id="dash-stat-entities-desc">Active knowledge records</div>
                </div>
                <!-- Stat Card 2 -->
                <div class="bento-card col-3">
                    <div class="stat-label">Vector Embeddings Ready <span>⚡</span></div>
                    <div class="stat-value" id="dash-stat-embeddings">0%</div>
                    <div class="stat-desc" id="dash-stat-embeddings-desc">Indexed in sqlite-vec</div>
                </div>
                <!-- Stat Card 3 -->
                <div class="bento-card col-3">
                    <div class="stat-label">Event Ledger Writes <span>📜</span></div>
                    <div class="stat-value" id="dash-stat-events">0</div>
                    <div class="stat-desc">Immutable audit entries</div>
                </div>
                <!-- Stat Card 4 -->
                <div class="bento-card col-3">
                    <div class="stat-label">Knowledge Graph Edges <span>🕸️</span></div>
                    <div class="stat-value" id="dash-stat-relations">0</div>
                    <div class="stat-desc">Typed directional relations</div>
                </div>

                <!-- Bento Donut Chart Card -->
                <div class="bento-card col-6">
                    <div class="stat-label">Memory Status & Scope Distribution</div>
                    <div class="donut-chart-container">
                        <svg class="donut-chart" viewBox="0 0 100 100">
                            <circle class="donut-segment" cx="50" cy="50" r="38" stroke="#34d399" stroke-dasharray="70 100" stroke-dashoffset="0"></circle>
                            <circle class="donut-segment" cx="50" cy="50" r="38" stroke="#fbbf24" stroke-dasharray="20 100" stroke-dashoffset="-70"></circle>
                            <circle class="donut-segment" cx="50" cy="50" r="38" stroke="#f87171" stroke-dasharray="10 100" stroke-dashoffset="-90"></circle>
                        </svg>
                        <div style="display:flex; flex-direction:column; gap:8px;">
                            <div class="stat-desc"><span class="badge badge-green">Raw</span> <span id="donut-raw-val">0</span></div>
                            <div class="stat-desc"><span class="badge badge-yellow">Consolidated</span> <span id="donut-cons-val">0</span></div>
                            <div class="stat-desc"><span class="badge badge-red">Archived</span> <span id="donut-arch-val">0</span></div>
                            <div style="margin-top:8px;" class="stat-desc"><span class="badge badge-blue">Shared</span> <span id="donut-shared-val">0</span></div>
                        </div>
                    </div>
                </div>

                <!-- Embedding Health Widget -->
                <div class="bento-card col-6">
                    <div class="stat-label">Vector Embedding Pipeline Health</div>
                    <div style="margin: 14px 0;">
                        <div style="display:flex; justify-content:space-between; margin-bottom:6px; font-size:0.8rem;">
                            <span>Ready: <strong id="emb-ready-count">0</strong></span>
                            <span>Pending: <strong id="emb-pending-count">0</strong></span>
                            <span>Failed: <strong id="emb-failed-count">0</strong></span>
                        </div>
                        <div style="height:8px; background:rgba(255,255,255,0.06); border-radius:4px; overflow:hidden; display:flex;">
                            <div id="emb-bar-ready" style="width:0%; background:var(--green); transition:width 0.5s ease;"></div>
                            <div id="emb-bar-pending" style="width:0%; background:var(--yellow); transition:width 0.5s ease;"></div>
                            <div id="emb-bar-failed" style="width:0%; background:var(--red); transition:width 0.5s ease;"></div>
                        </div>
                    </div>
                    <button class="btn btn-primary" style="width:100%; justify-content:center;" onclick="triggerEmbeddingBackfill()">
                        ⚡ Queue Pending Embedding Backfill
                    </button>
                </div>

                <!-- Live Event Ticker -->
                <div class="bento-card col-12">
                    <div class="stat-label">
                        <span>Real-Time Operational Ledger Stream</span>
                        <button class="btn" style="padding: 2px 8px; font-size: 0.7rem;" onclick="loadDashboardTicker()">🔄 Refresh Stream</button>
                    </div>
                    <div id="dashboard-ticker-list" style="display:flex; flex-direction:column; gap:8px; margin-top:10px;">
                        <div style="color:var(--text-muted);">Loading recent operational events...</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 2. Knowledge Entities View -->
        <div class="view-container" id="view-entities">
            <div class="toolbar">
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input type="text" id="entity-search-input" placeholder="Search by title, content, or tag..." onkeyup="handleEntitySearch(event)">
                </div>
                <div style="display:flex; gap:8px;">
                    <select id="entity-status-filter" class="btn" onchange="loadEntities(1)">
                        <option value="">All Statuses</option>
                        <option value="raw">Raw</option>
                        <option value="consolidated">Consolidated</option>
                        <option value="archived">Archived</option>
                    </select>
                </div>
            </div>
            <div class="data-table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Title</th>
                            <th>Status</th>
                            <th>Embed Status</th>
                            <th>Scope</th>
                            <th>Owner</th>
                            <th>Updated</th>
                        </tr>
                    </thead>
                    <tbody id="entities-table-body">
                        <tr><td colspan="6" style="text-align:center; color:var(--text-muted);">Loading entities...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 3. Event Ledger View -->
        <div class="view-container" id="view-events">
            <div class="data-table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Agent ID</th>
                            <th>Type</th>
                            <th>Content</th>
                        </tr>
                    </thead>
                    <tbody id="events-table-body">
                        <tr><td colspan="4" style="text-align:center; color:var(--text-muted);">Loading events...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 4. Graph Topology View -->
        <div class="view-container" id="view-relations">
            <div class="toolbar" style="margin-bottom:14px; background:var(--bg-card); padding:12px 16px; border-radius:var(--radius); border:var(--glass-border);">
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; flex:1;">
                    <div class="search-box" style="min-width:180px; flex:0.8;">
                        <span class="search-icon">🔍</span>
                        <input type="text" id="graph-search-input" placeholder="Highlight node title..." onkeyup="filterGraphNodes()">
                    </div>
                    <select id="graph-status-filter" class="btn" onchange="loadGraphTopology()">
                        <option value="true" selected>Active Memories Only (Hide Archived)</option>
                        <option value="false">All Memories (Inc. Archived)</option>
                    </select>
                    <select id="graph-degree-filter" class="btn" onchange="filterGraphNodes()">
                        <option value="1" selected>Connected Only (Degree ≥ 1)</option>
                        <option value="2">Hubs Only (Degree ≥ 2)</option>
                        <option value="0">All Nodes (Inc. Isolated)</option>
                    </select>
                    <select id="graph-predicate-filter" class="btn" onchange="loadGraphTopology()">
                        <option value="">All Predicates</option>
                        <option value="consolidated_from">consolidated_from</option>
                        <option value="derived_from">derived_from</option>
                        <option value="complements">complements</option>
                        <option value="resolves">resolves</option>
                    </select>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <span id="graph-stats-badge" class="badge badge-blue">0 nodes | 0 edges</span>
                    <button class="btn" onclick="loadGraphTopology()">🔄 Reload</button>
                </div>
            </div>
            <div style="display:flex; gap:12px; margin-bottom:10px; align-items:center; font-size:0.8rem; color:var(--text-secondary);">
                <span>Legend:</span>
                <span class="badge badge-purple">consolidated_from</span>
                <span class="badge badge-blue">derived_from</span>
                <span class="badge badge-green">complements</span>
                <span class="badge badge-yellow">resolves</span>
                <span style="margin-left:auto; color:var(--text-muted);">Hover node to highlight 1-hop connections • Drag node • Click for details</span>
            </div>
            <svg id="graph-canvas"></svg>
        </div>

        <!-- 5. Lineage View -->
        <div class="view-container" id="view-lineage">
            <div class="toolbar">
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input type="text" id="lineage-search-input" placeholder="Enter entity ID or exact title to inspect ancestry..." onkeyup="if(event.key==='Enter') loadLineage()">
                </div>
                <button class="btn btn-primary" onclick="loadLineage()">Inspect Ancestry</button>
            </div>
            <div id="lineage-tree-container" class="bento-card">
                <div style="color:var(--text-muted); text-align:center; padding:40px;">Select an entity to render multi-generation consolidation tree.</div>
            </div>
        </div>

        <!-- 6. Vector Search Playground View -->
        <div class="view-container" id="view-embeddings">
            <div class="bento-card" style="margin-bottom:20px;">
                <div class="stat-label">Dense Vector & Hybrid RRF Search Test Bench</div>
                <div style="display:flex; gap:12px; margin-top:10px;">
                    <div class="search-box" style="flex:1;">
                        <span class="search-icon">🔍</span>
                        <input type="text" id="vector-query-input" placeholder="Type a test query (e.g. 'molecular ground state energy simulation')..." onkeyup="if(event.key==='Enter') runVectorSearch()">
                    </div>
                    <button class="btn btn-primary" onclick="runVectorSearch()">Execute Search</button>
                </div>
            </div>
            <div id="vector-results-container" class="data-table-container" style="padding:16px;">
                <div style="color:var(--text-muted); text-align:center; padding:30px;">Enter a query above to test live dense vector matching.</div>
            </div>
        </div>

        <!-- 7. Folksonomy Tags View -->
        <div class="view-container" id="view-tags">
            <div id="tags-cloud-container" style="display:flex; flex-wrap:wrap; gap:10px;">
                <div style="color:var(--text-muted);">Loading tags...</div>
            </div>
        </div>

        <!-- 8. System Locks View -->
        <div class="view-container" id="view-locks">
            <div class="data-table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Task Name</th>
                            <th>Locked At</th>
                            <th>PID</th>
                            <th>Last Run</th>
                        </tr>
                    </thead>
                    <tbody id="locks-table-body">
                        <tr><td colspan="4" style="text-align:center; color:var(--text-muted);">Loading system locks...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ── Entity Detail Modal Drawer ───────────────────── -->
    <div class="modal-overlay" id="entity-modal">
        <div class="modal-card">
            <div class="modal-header">
                <h3 id="modal-entity-title" style="color:var(--accent);">Entity Detail</h3>
                <button class="close-btn" onclick="closeModal('entity-modal')">✖</button>
            </div>
            <div class="modal-body" id="modal-entity-body">
                Loading...
            </div>
        </div>
    </div>

    <!-- ── Command Palette Modal ────────────────────────── -->
    <div class="modal-overlay" id="cmd-modal" onclick="if(event.target===this) toggleCmdPalette()">
        <div class="modal-card" style="max-width: 600px;">
            <div class="modal-header">
                <h3>Command Palette & Jump</h3>
                <button class="close-btn" onclick="toggleCmdPalette()">✖</button>
            </div>
            <div class="modal-body">
                <input type="text" id="cmd-input" placeholder="Type a command or view name (e.g. 'memories', 'vector', 'graph')..." style="width:100%; padding:12px; background:#080c14; border:var(--glass-border); border-radius:8px; color:#fff; margin-bottom:14px;" onkeyup="handleCmdInput(event)">
                <div style="display:flex; flex-direction:column; gap:6px;" id="cmd-options-list">
                    <div class="nav-item" onclick="switchView('dashboard'); toggleCmdPalette();">📊 Jump to Overview Dashboard</div>
                    <div class="nav-item" onclick="switchView('entities'); toggleCmdPalette();">🧠 Jump to Memories</div>
                    <div class="nav-item" onclick="switchView('embeddings'); toggleCmdPalette();">🔍 Jump to Vector Playground</div>
                    <div class="nav-item" onclick="switchView('relations'); toggleCmdPalette();">🕸️ Jump to Graph Topology</div>
                    <div class="nav-item" onclick="triggerEmbeddingBackfill(); toggleCmdPalette();">⚡ Queue Pending Embedding Backfill</div>
                </div>
            </div>
        </div>
    </div>

    <!-- ── Dashboard Logic JS ──────────────────────────── -->
    <script>
        let currentView = 'dashboard';
        let rawGraphData = { nodes: [], edges: [] };

        function switchView(viewId) {
            currentView = viewId;
            document.querySelectorAll('.view-container').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            
            const targetView = document.getElementById('view-' + viewId);
            const targetNav = document.getElementById('nav-' + viewId);
            if (targetView) targetView.classList.add('active');
            if (targetNav) targetNav.classList.add('active');

            const headerTitle = document.getElementById('page-header-title');
            const titles = {
                dashboard: '📊 Executive Dashboard Overview',
                entities: '🧠 Long-Term Knowledge Memories',
                events: '📜 Operational Events Ledger',
                relations: '🕸️ Knowledge Graph Topology Network',
                lineage: '🌳 Consolidation Lineage & Ancestry',
                embeddings: '🔍 Dense Vector & Hybrid RRF Playground',
                tags: '🏷️ Folksonomy Tag Registry',
                locks: '🔒 System Mutex & Task Locks'
            };
            if (headerTitle) headerTitle.innerHTML = titles[viewId] || 'SALTMDB Viewer';

            refreshCurrentView();
        }

        function refreshCurrentView() {
            loadDashboardStats();
            if (currentView === 'dashboard') loadDashboardTicker();
            else if (currentView === 'entities') loadEntities(1);
            else if (currentView === 'events') loadEvents(1);
            else if (currentView === 'relations') loadGraphTopology();
            else if (currentView === 'embeddings') runVectorSearch();
            else if (currentView === 'tags') loadTags();
            else if (currentView === 'locks') loadLocks();
        }

        async function loadDashboardStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                
                document.getElementById('dash-stat-entities').innerText = data.total_entities || 0;
                document.getElementById('dash-stat-events').innerText = data.total_events || 0;
                document.getElementById('dash-stat-relations').innerText = data.total_relations || 0;
                
                const ready = data.embeddings_ready || 0;
                const total = data.total_entities || 1;
                const readyPct = Math.round((ready / Math.max(1, total)) * 100);
                document.getElementById('dash-stat-embeddings').innerText = readyPct + '%';
                
                document.getElementById('db-size-label').innerText = 'Database Size: ' + (data.db_size_mb || 0) + ' MB';

                document.getElementById('donut-raw-val').innerText = data.raw_count || 0;
                document.getElementById('donut-cons-val').innerText = data.consolidated_count || 0;
                document.getElementById('donut-arch-val').innerText = data.archived_count || 0;
                document.getElementById('donut-shared-val').innerText = data.scope_shared || 0;

                const pending = data.embeddings_pending || 0;
                const failed = data.embeddings_failed || 0;
                document.getElementById('emb-ready-count').innerText = ready;
                document.getElementById('emb-pending-count').innerText = pending;
                document.getElementById('emb-failed-count').innerText = failed;

                const sum = Math.max(1, ready + pending + failed);
                document.getElementById('emb-bar-ready').style.width = (ready / sum * 100) + '%';
                document.getElementById('emb-bar-pending').style.width = (pending / sum * 100) + '%';
                document.getElementById('emb-bar-failed').style.width = (failed / sum * 100) + '%';
            } catch (err) {
                console.error("Error loading stats:", err);
            }
        }

        async function loadDashboardTicker() {
            try {
                const res = await fetch('/api/events?limit=5');
                const data = await res.json();
                const container = document.getElementById('dashboard-ticker-list');
                if (!data.events || data.events.length === 0) {
                    container.innerHTML = '<div style="color:var(--text-muted);">No operational events logged yet.</div>';
                    return;
                }
                container.innerHTML = data.events.map(ev => `
                    <div style="display:flex; align-items:center; justify-content:space-between; background:rgba(255,255,255,0.03); padding:8px 12px; border-radius:6px; border:var(--glass-border);">
                        <div style="display:flex; align-items:center; gap:10px;">
                            <span class="badge ${getEventBadgeClass(ev.type)}">${ev.type}</span>
                            <span style="font-weight:600;">${escapeHtml(ev.agent_id)}</span>
                            <span style="color:var(--text-secondary);">${escapeHtml(ev.content)}</span>
                        </div>
                        <div style="font-size:0.75rem; color:var(--text-muted);">${ev.timestamp}</div>
                    </div>
                `).join('');
            } catch (err) {
                console.error("Error loading ticker:", err);
            }
        }

        function getEventBadgeClass(type) {
            if (type === 'decision') return 'badge-purple';
            if (type === 'fix') return 'badge-green';
            if (type === 'issue') return 'badge-red';
            return 'badge-blue';
        }

        async function triggerEmbeddingBackfill() {
            try {
                const res = await fetch('/api/embeddings/backfill', { method: 'POST' });
                const data = await res.json();
                alert(data.message || "Backfill triggered!");
                loadDashboardStats();
            } catch (err) {
                alert("Error triggering backfill: " + err);
            }
        }

        async function loadEntities(page = 1) {
            const status = document.getElementById('entity-status-filter').value;
            const search = document.getElementById('entity-search-input').value;
            let url = `/api/entities?page=${page}`;
            if (status) url += `&status=${status}`;
            if (search) url += `&tag=${encodeURIComponent(search)}`;

            try {
                const res = await fetch(url);
                const data = await res.json();
                const tbody = document.getElementById('entities-table-body');
                if (!data.entities || data.entities.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted);">No entities found matching filters.</td></tr>';
                    return;
                }
                tbody.innerHTML = data.entities.map(e => `
                    <tr onclick="openEntityDetail('${e.id}')">
                        <td><strong>${escapeHtml(e.title || 'Untitled')}</strong></td>
                        <td><span class="badge ${e.status==='raw'?'badge-green':(e.status==='consolidated'?'badge-yellow':'badge-red')}">${e.status}</span></td>
                        <td><span class="badge ${e.embedding_status==='ready'?'badge-green':'badge-yellow'}">${e.embedding_status}</span></td>
                        <td><span class="badge badge-blue">${e.scope}</span></td>
                        <td>${escapeHtml(e.owner_id || 'system')}</td>
                        <td style="color:var(--text-muted); font-size:0.78rem;">${e.updated_at}</td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error("Error loading entities:", err);
            }
        }

        async function openEntityDetail(id) {
            try {
                const res = await fetch(`/api/entities/${id}`);
                const data = await res.json();
                document.getElementById('modal-entity-title').innerText = data.title || 'Entity Detail';
                document.getElementById('modal-entity-body').innerHTML = `
                    <div style="margin-bottom:14px; display:flex; gap:8px;">
                        <span class="badge badge-green">${data.status}</span>
                        <span class="badge badge-blue">${data.scope}</span>
                        <span class="badge badge-purple">Owner: ${data.owner_id || 'system'}</span>
                    </div>
                    <h4 style="margin-bottom:6px;">Content Markdown:</h4>
                    <pre>${escapeHtml(data.full_content || '')}</pre>
                    <h4 style="margin:14px 0 6px;">Tags:</h4>
                    <div>${(data.tags||[]).map(t => `<span class="badge badge-blue" style="margin-right:4px;">${t}</span>`).join('')}</div>
                `;
                document.getElementById('entity-modal').classList.add('active');
            } catch (err) {
                alert("Error fetching detail: " + err);
            }
        }

        async function runVectorSearch() {
            const input = document.getElementById('vector-query-input');
            const query = input ? input.value.trim() : '';
            const container = document.getElementById('vector-results-container');
            if (!query) {
                container.innerHTML = '<div style="color:var(--text-muted); text-align:center; padding:30px;">Enter a query above to test live dense vector matching.</div>';
                return;
            }
            container.innerHTML = '<div style="color:var(--text-muted); text-align:center; padding:20px;">Executing dense vector search...</div>';
            try {
                const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                const data = await res.json();
                if (!data.results || data.results.length === 0) {
                    container.innerHTML = '<div style="color:var(--text-muted); text-align:center; padding:30px;">No matching vector entities found.</div>';
                    return;
                }
                container.innerHTML = `
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Rank</th>
                                <th>Matched Entity Title</th>
                                <th>Score (RRF)</th>
                                <th>Snippet Preview</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${data.results.map((r, i) => `
                                <tr onclick="openEntityDetail('${r.id}')">
                                    <td>#${i+1}</td>
                                    <td><strong>${escapeHtml(r.title)}</strong></td>
                                    <td><span class="badge badge-green">${r.score}</span></td>
                                    <td style="color:var(--text-secondary);">${escapeHtml(r.snippet)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            } catch (err) {
                container.innerHTML = '<div style="color:var(--red);">Search error: ' + err + '</div>';
            }
        }

        async function loadGraphTopology() {
            const excludeArchived = document.getElementById('graph-status-filter').value;
            const predicate = document.getElementById('graph-predicate-filter').value;
            let url = `/api/relations/graph?exclude_archived=${excludeArchived}&limit=250`;
            if (predicate) url += `&predicate=${encodeURIComponent(predicate)}`;

            try {
                const res = await fetch(url);
                rawGraphData = await res.json();
                renderGraph();
            } catch (err) {
                console.error("Error loading topology:", err);
            }
        }

        function filterGraphNodes() {
            renderGraph();
        }

        function renderGraph() {
            const svg = document.getElementById('graph-canvas');
            svg.innerHTML = '';
            if (!rawGraphData.nodes || rawGraphData.nodes.length === 0) {
                document.getElementById('graph-stats-badge').innerText = '0 nodes | 0 edges';
                return;
            }

            const minDegree = parseInt(document.getElementById('graph-degree-filter').value || '1');
            const searchStr = (document.getElementById('graph-search-input').value || '').toLowerCase().trim();

            // Filter nodes by min degree
            let filteredNodes = rawGraphData.nodes.filter(n => n.degree >= minDegree);
            const validNodeIds = new Set(filteredNodes.map(n => n.id));
            let filteredEdges = rawGraphData.edges.filter(e => validNodeIds.has(e.source) && validNodeIds.has(e.target));

            document.getElementById('graph-stats-badge').innerText = `${filteredNodes.length} nodes | ${filteredEdges.length} edges`;

            const width = svg.clientWidth || 800;
            const height = 600;
            const cx = width / 2;
            const cy = height / 2;

            // Clustered force layout simulation
            const nodes = filteredNodes;
            const radius = Math.min(width, height) / 2.6;

            nodes.forEach((n, i) => {
                const angle = (i / nodes.length) * 2 * Math.PI;
                n.x = cx + (radius + (i % 3) * 30) * Math.cos(angle);
                n.y = cy + (radius + (i % 3) * 30) * Math.sin(angle);
            });

            // Iterative repulsion & edge attraction physics simulation (20 iterations)
            for (let iter = 0; iter < 25; iter++) {
                // Repulsion between nodes
                for (let i = 0; i < nodes.length; i++) {
                    for (let j = i + 1; j < nodes.length; j++) {
                        let dx = nodes[j].x - nodes[i].x;
                        let dy = nodes[j].y - nodes[i].y;
                        let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                        if (dist < 120) {
                            let force = (120 - dist) / dist * 0.4;
                            nodes[i].x -= dx * force;
                            nodes[i].y -= dy * force;
                            nodes[j].x += dx * force;
                            nodes[j].y += dy * force;
                        }
                    }
                }
                // Attraction along edges
                filteredEdges.forEach(e => {
                    let s = nodes.find(n => n.id === e.source);
                    let t = nodes.find(n => n.id === e.target);
                    if (s && t) {
                        let dx = t.x - s.x;
                        let dy = t.y - s.y;
                        let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                        let force = (dist - 140) * 0.04;
                        s.x += (dx / dist) * force;
                        s.y += (dy / dist) * force;
                        t.x -= (dx / dist) * force;
                        t.y -= (dy / dist) * force;
                    }
                });
                // Constrain within bounding box
                nodes.forEach(n => {
                    n.x = Math.max(40, Math.min(width - 40, n.x));
                    n.y = Math.max(40, Math.min(height - 40, n.y));
                });
            }

            // Draw Edges
            const edgeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            filteredEdges.forEach(e => {
                const s = nodes.find(n => n.id === e.source);
                const t = nodes.find(n => n.id === e.target);
                if (s && t) {
                    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                    line.setAttribute('x1', s.x);
                    line.setAttribute('y1', s.y);
                    line.setAttribute('x2', t.x);
                    line.setAttribute('y2', t.y);
                    line.setAttribute('class', 'link');
                    line.dataset.source = s.id;
                    line.dataset.target = t.id;
                    edgeGroup.appendChild(line);
                }
            });
            svg.appendChild(edgeGroup);

            // Draw Nodes
            const nodeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            nodes.forEach(n => {
                const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                g.setAttribute('class', 'node');
                g.dataset.id = n.id;
                g.onclick = () => openEntityDetail(n.id);

                // Hover 1-hop connection highlight
                g.onmouseenter = () => highlightNodeNeighbors(n.id);
                g.onmouseleave = () => resetGraphHighlight();

                // Dynamic node radius based on connection degree
                const nodeRadius = 8 + Math.min((n.degree || 1) * 3, 16);

                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', n.x);
                circle.setAttribute('cy', n.y);
                circle.setAttribute('r', nodeRadius);

                const isMatch = searchStr && n.title.toLowerCase().includes(searchStr);
                if (isMatch) {
                    circle.setAttribute('stroke', '#34d399');
                    circle.setAttribute('stroke-width', '3.5');
                    circle.setAttribute('filter', 'drop-shadow(0 0 10px #34d399)');
                }

                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', n.x + nodeRadius + 4);
                text.setAttribute('y', n.y + 4);
                text.textContent = n.title.length > 22 ? n.title.substring(0, 22) + '...' : n.title;

                g.appendChild(circle);
                g.appendChild(text);
                nodeGroup.appendChild(g);
            });
            svg.appendChild(nodeGroup);
        }

        function highlightNodeNeighbors(nodeId) {
            const svg = document.getElementById('graph-canvas');
            const connectedIds = new Set([nodeId]);

            // Find all connected edges and targets
            svg.querySelectorAll('.link').forEach(link => {
                if (link.dataset.source === nodeId || link.dataset.target === nodeId) {
                    link.classList.add('highlight');
                    connectedIds.add(link.dataset.source);
                    connectedIds.add(link.dataset.target);
                } else {
                    link.classList.add('dimmed');
                }
            });

            svg.querySelectorAll('.node').forEach(node => {
                if (connectedIds.has(node.dataset.id)) {
                    node.classList.add('highlight');
                } else {
                    node.classList.add('dimmed');
                }
            });
        }

        function resetGraphHighlight() {
            const svg = document.getElementById('graph-canvas');
            svg.querySelectorAll('.link').forEach(link => {
                link.classList.remove('highlight', 'dimmed');
            });
            svg.querySelectorAll('.node').forEach(node => {
                node.classList.remove('highlight', 'dimmed');
            });
        }

        async function loadEvents(page = 1) {
            try {
                const res = await fetch(`/api/events?page=${page}`);
                const data = await res.json();
                const tbody = document.getElementById('events-table-body');
                tbody.innerHTML = (data.events || []).map(ev => `
                    <tr>
                        <td style="color:var(--text-muted); font-size:0.78rem;">${ev.timestamp}</td>
                        <td><strong>${escapeHtml(ev.agent_id)}</strong></td>
                        <td><span class="badge ${getEventBadgeClass(ev.type)}">${ev.type}</span></td>
                        <td style="color:var(--text-secondary);">${escapeHtml(ev.content)}</td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error("Error loading events:", err);
            }
        }

        async function loadTags() {
            try {
                const res = await fetch('/api/tags');
                const data = await res.json();
                const container = document.getElementById('tags-cloud-container');
                container.innerHTML = (data.tags || []).map(t => `
                    <div class="bento-card" style="padding:10px 16px; cursor:pointer;" onclick="searchByTag('${escapeHtml(t.name)}')">
                        <span style="font-weight:700; color:var(--accent);">${escapeHtml(t.name)}</span>
                        <span class="badge badge-blue" style="margin-left:8px;">${t.usage_count} memories</span>
                    </div>
                `).join('');
            } catch (err) {
                console.error("Error loading tags:", err);
            }
        }

        function searchByTag(tagName) {
            switchView('entities');
            document.getElementById('entity-search-input').value = tagName;
            loadEntities(1);
        }

        async function loadLocks() {
            try {
                const res = await fetch('/api/locks');
                const data = await res.json();
                const tbody = document.getElementById('locks-table-body');
                if (!data.locks || data.locks.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-muted);">No system locks currently held.</td></tr>';
                    return;
                }
                tbody.innerHTML = data.locks.map(l => `
                    <tr>
                        <td><strong>${escapeHtml(l.task_name)}</strong></td>
                        <td>${l.locked_at || 'N/A'}</td>
                        <td>${l.locked_by_pid || 'N/A'}</td>
                        <td>${l.last_run_at || 'N/A'}</td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error("Error loading locks:", err);
            }
        }

        function closeModal(id) {
            document.getElementById(id).classList.remove('active');
        }

        function toggleCmdPalette() {
            const modal = document.getElementById('cmd-modal');
            modal.classList.toggle('active');
            if (modal.classList.contains('active')) {
                setTimeout(() => document.getElementById('cmd-input').focus(), 50);
            }
        }

        function escapeHtml(str) {
            if (!str) return '';
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        // Global Keyboard Shortcuts
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                toggleCmdPalette();
            } else if (e.key >= '1' && e.key <= '8' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
                const viewKeys = ['dashboard', 'entities', 'events', 'relations', 'lineage', 'embeddings', 'tags', 'locks'];
                const idx = parseInt(e.key) - 1;
                if (viewKeys[idx]) switchView(viewKeys[idx]);
            } else if (e.key === 'Escape') {
                closeModal('entity-modal');
                closeModal('cmd-modal');
            }
        });

        // Initialize dashboard on page load
        window.onload = () => {
            loadDashboardStats();
            loadDashboardTicker();
        };
    </script>
</body>
</html>
"""
