def get_frontend_html(db_path: str = None) -> str:
    """Returns the single-page application (SPA) HTML dashboard template."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SALTMDB Database Viewer</title>
    <style>
        :root {
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-card: #334155;
            --accent-primary: #38bdf8;
            --accent-hover: #0284c7;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --border: #475569;
            --status-raw: #fbbf24;
            --status-consolidated: #34d399;
            --status-archived: #f87171;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        body {
            background-color: var(--bg-primary);
            color: var(--text-primary);
            display: flex;
            height: 100vh;
            overflow: hidden;
        }

        /* Sidebar */
        .sidebar {
            width: 260px;
            background-color: var(--bg-secondary);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            padding: 20px;
        }

        .logo {
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--accent-primary);
            margin-bottom: 30px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .nav-list {
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .nav-item {
            padding: 10px 14px;
            border-radius: 6px;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.2s;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .nav-item:hover, .nav-item.active {
            background-color: var(--bg-card);
            color: var(--text-primary);
        }

        .nav-item.active {
            border-left: 3px solid var(--accent-primary);
        }

        /* Main Content */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .header {
            height: 60px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            background-color: var(--bg-secondary);
        }

        .search-box {
            position: relative;
            width: 320px;
        }

        .search-box input {
            width: 100%;
            padding: 8px 14px 8px 36px;
            background-color: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 20px;
            color: var(--text-primary);
            font-size: 0.85rem;
            outline: none;
        }

        .search-box input:focus {
            border-color: var(--accent-primary);
        }

        .content-area {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
        }

        /* Dashboard Cards */
        .grid-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background-color: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }

        .stat-card .title {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
        }

        .stat-card .value {
            font-size: 1.5rem;
            font-weight: 700;
        }

        /* Table View */
        .table-container {
            background-color: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.85rem;
        }

        th {
            background-color: var(--bg-card);
            color: var(--text-secondary);
            padding: 12px 16px;
            font-weight: 600;
            border-bottom: 1px solid var(--border);
        }

        td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
        }

        tr:hover {
            background-color: var(--bg-card);
        }

        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .badge-raw { background-color: rgba(251, 191, 36, 0.2); color: var(--status-raw); }
        .badge-consolidated { background-color: rgba(52, 211, 153, 0.2); color: var(--status-consolidated); }
        .badge-archived { background-color: rgba(248, 113, 113, 0.2); color: var(--status-archived); }

        .tag {
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            color: var(--accent-primary);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.75rem;
            margin-right: 4px;
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="logo">⚡ SALTMDB Database Viewer</div>
        <ul class="nav-list">
            <li class="nav-item active" onclick="switchView('dashboard')">📊 Dashboard</li>
            <li class="nav-item" onclick="switchView('entities')">🧠 Memories (Entities)</li>
            <li class="nav-item" onclick="switchView('events')">📜 Event Log</li>
            <li class="nav-item" onclick="switchView('relations')">🕸️ Relations Graph</li>
            <li class="nav-item" onclick="switchView('tags')">🏷️ Tags</li>
            <li class="nav-item" onclick="switchView('locks')">🔒 System Locks</li>
        </ul>
    </div>

    <div class="main-content">
        <div class="header">
            <h2 id="view-title">Dashboard Overview</h2>
            <div class="search-box">
                <input type="text" id="global-search" placeholder="Search memories..." onkeyup="handleSearch(event)">
            </div>
        </div>

        <div class="content-area" id="content-area">
            <!-- Dynamic Content Injected Here -->
        </div>
    </div>

    <script>
        async function fetchAPI(endpoint) {
            try {
                const res = await fetch('/api/' + endpoint);
                return await res.json();
            } catch (e) {
                console.error("API error:", e);
                return null;
            }
        }

        async function switchView(view) {
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            const activeNav = Array.from(document.querySelectorAll('.nav-item')).find(el => el.getAttribute('onclick').includes(view));
            if (activeNav) activeNav.classList.add('active');

            const content = document.getElementById('content-area');
            const title = document.getElementById('view-title');

            if (view === 'dashboard') {
                title.innerText = 'Dashboard Overview';
                const stats = await fetchAPI('stats');
                content.innerHTML = `
                    <div class="grid-stats">
                        <div class="stat-card"><div class="title">Raw Memories</div><div class="value">${stats?.raw_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Consolidated</div><div class="value">${stats?.consolidated_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Archived</div><div class="value">${stats?.archived_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Events</div><div class="value">${stats?.total_events || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Relations</div><div class="value">${stats?.total_relations || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Tags</div><div class="value">${stats?.total_tags || 0}</div></div>
                    </div>
                `;
            } else if (view === 'entities') {
                title.innerText = 'Memories (Entities)';
                const data = await fetchAPI('entities');
                content.innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr><th>Title</th><th>Owner</th><th>Status</th><th>Scope</th><th>Weight</th><th>Tags</th></tr>
                            </thead>
                            <tbody>
                                ${(data?.entities || []).map(e => `
                                    <tr>
                                        <td><strong>${e.title}</strong></td>
                                        <td>${e.owner_id || 'system'}</td>
                                        <td><span class="badge badge-${e.status}">${e.status}</span></td>
                                        <td>${e.scope}</td>
                                        <td>${e.weight}</td>
                                        <td>${(e.tags || []).map(t => `<span class="tag">${t}</span>`).join('')}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            }
        }

        function handleSearch(e) {
            if (e.key === 'Enter') {
                const query = e.target.value.trim();
                if (query) {
                    fetchAPI('search?q=' + encodeURIComponent(query)).then(data => {
                        const content = document.getElementById('content-area');
                        document.getElementById('view-title').innerText = `Search Results for "${query}"`;
                        content.innerHTML = `
                            <div class="table-container">
                                <table>
                                    <thead><tr><th>Title</th><th>Snippet</th><th>Score</th></tr></thead>
                                    <tbody>
                                        ${(data?.results || []).map(r => `
                                            <tr><td><strong>${r.title}</strong></td><td>${r.snippet}</td><td>${r.score}</td></tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                            </div>
                        `;
                    });
                }
            }
        }

        // Initialize dashboard on load
        switchView('dashboard');
    </script>
</body>
</html>"""
