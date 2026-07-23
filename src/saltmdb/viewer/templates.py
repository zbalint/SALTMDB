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
            flex-shrink: 0;
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
            width: 340px;
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
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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

        tr.clickable-row {
            cursor: pointer;
        }

        tr.clickable-row:hover {
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

        /* Modal overlay */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(0, 0, 0, 0.75);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease;
        }

        .modal-overlay.active {
            opacity: 1;
            pointer-events: auto;
        }

        .modal-box {
            background-color: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            width: 800px;
            max-width: 90vw;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
        }

        .modal-header {
            padding: 16px 24px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            background-color: var(--bg-card);
        }

        .modal-header h3 {
            font-size: 1.1rem;
            color: var(--text-primary);
        }

        .modal-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
        }

        .modal-close:hover {
            color: var(--text-primary);
        }

        .modal-body {
            padding: 24px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .modal-section-title {
            font-size: 0.85rem;
            font-weight: 700;
            color: var(--accent-primary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 6px;
        }

        .content-code-box {
            background-color: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px;
            font-family: monospace;
            font-size: 0.85rem;
            white-space: pre-wrap;
            color: #e2e8f0;
            max-height: 300px;
            overflow-y: auto;
        }

        .meta-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            background-color: var(--bg-primary);
            padding: 12px;
            border-radius: 6px;
            font-size: 0.8rem;
        }

        .btn-action {
            padding: 6px 12px;
            background-color: var(--accent-primary);
            color: #0f172a;
            border: none;
            border-radius: 4px;
            font-weight: 600;
            cursor: pointer;
            font-size: 0.8rem;
            transition: background 0.2s;
        }

        .btn-action:hover {
            background-color: var(--accent-hover);
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

    <!-- Modal Popup for Entity Detail -->
    <div class="modal-overlay" id="entity-modal">
        <div class="modal-box">
            <div class="modal-header">
                <h3 id="modal-title">Memory Detail</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body" id="modal-body">
                <!-- Injected Detail Body -->
            </div>
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

        function closeModal() {
            document.getElementById('entity-modal').classList.remove('active');
        }

        async function showEntityDetail(id) {
            const data = await fetchAPI('entities/' + id);
            if (!data || data.error) {
                alert("Error loading memory details.");
                return;
            }

            document.getElementById('modal-title').innerText = data.title || 'Untitled Memory';
            
            const rels = data.relations || {};
            const outgoing = rels.outgoing || [];
            const incoming = rels.incoming || [];

            document.getElementById('modal-body').innerHTML = `
                <div class="meta-grid">
                    <div><strong>ID:</strong> <span style="font-family:monospace;">${data.id}</span></div>
                    <div><strong>Owner:</strong> ${data.owner_id || 'system'}</div>
                    <div><strong>Status:</strong> <span class="badge badge-${data.status}">${data.status}</span></div>
                    <div><strong>Scope:</strong> ${data.scope}</div>
                    <div><strong>Weight:</strong> ${data.weight}</div>
                    <div><strong>Is Core:</strong> ${data.is_core ? 'Yes' : 'No'}</div>
                    <div><strong>Context ID:</strong> ${data.context_id || 'N/A'}</div>
                    <div><strong>Project ID:</strong> ${data.project_id || 'N/A'}</div>
                    <div><strong>Created:</strong> ${data.created_at || 'N/A'}</div>
                </div>

                <div>
                    <div class="modal-section-title">Tags</div>
                    <div>${(data.tags || []).map(t => `<span class="tag">${t}</span>`).join('') || '<em>No tags</em>'}</div>
                </div>

                <div>
                    <div class="modal-section-title">Full Markdown Content</div>
                    <div class="content-code-box">${escapeHTML(data.full_content || '')}</div>
                </div>

                <div>
                    <div class="modal-section-title">Relations</div>
                    <div style="font-size:0.85rem;">
                        <strong>Outgoing Dependencies:</strong>
                        ${outgoing.length > 0 ? outgoing.map(r => `<div>↳ <i>${r.predicate}</i> &rarr; <a href="#" onclick="closeModal(); showEntityDetail('${r.target_id}'); return false;" style="color:var(--accent-primary);">${r.target_title}</a></div>`).join('') : '<em>None</em>'}
                        <br>
                        <strong>Incoming Dependents:</strong>
                        ${incoming.length > 0 ? incoming.map(r => `<div>↲ <i>${r.predicate}</i> &larr; <a href="#" onclick="closeModal(); showEntityDetail('${r.source_id}'); return false;" style="color:var(--accent-primary);">${r.source_title}</a></div>`).join('') : '<em>None</em>'}
                    </div>
                </div>

                ${data.parent_ids && data.parent_ids.length > 0 ? `
                    <div>
                        <div class="modal-section-title">Consolidation Lineage</div>
                        <button class="btn-action" onclick="showLineageModal('${data.id}')">Inspect Parent Lineage Ancestry</button>
                    </div>
                ` : ''}
            `;

            document.getElementById('entity-modal').classList.add('active');
        }

        async function showLineageModal(id) {
            const data = await fetchAPI('entities/' + id + '/lineage');
            if (!data || !data.ancestry_tree) return;
            let treeHtml = (data.ancestry_tree || []).map(node => `
                <div style="padding: 8px; background: var(--bg-primary); margin-bottom: 6px; border-radius: 4px; font-size: 0.85rem;">
                    <strong>Depth ${node.generation_depth}:</strong> ${node.title} (<span class="badge badge-${node.status}">${node.status}</span>)
                </div>
            `).join('');
            
            document.getElementById('modal-title').innerText = "Consolidation Ancestry Lineage";
            document.getElementById('modal-body').innerHTML = `<div>${treeHtml}</div>`;
        }

        function escapeHTML(str) {
            return str.replace(/[&<>'"]/g, 
                tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
            );
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
                const recentMem = await fetchAPI('entities?page=1');
                content.innerHTML = `
                    <div class="grid-stats">
                        <div class="stat-card"><div class="title">Raw Memories</div><div class="value">${stats?.raw_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Consolidated</div><div class="value">${stats?.consolidated_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Archived</div><div class="value">${stats?.archived_count || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Events</div><div class="value">${stats?.total_events || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Relations</div><div class="value">${stats?.total_relations || 0}</div></div>
                        <div class="stat-card"><div class="title">Total Tags</div><div class="value">${stats?.total_tags || 0}</div></div>
                    </div>
                    <h3 style="margin-bottom:12px; font-size:1rem;">Recent Memories</h3>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr><th>Title</th><th>Owner</th><th>Status</th><th>Scope</th><th>Tags</th></tr>
                            </thead>
                            <tbody>
                                ${(recentMem?.entities || []).slice(0, 5).map(e => `
                                    <tr class="clickable-row" onclick="showEntityDetail('${e.id}')">
                                        <td><strong>${e.title}</strong></td>
                                        <td>${e.owner_id || 'system'}</td>
                                        <td><span class="badge badge-${e.status}">${e.status}</span></td>
                                        <td>${e.scope}</td>
                                        <td>${(e.tags || []).map(t => `<span class="tag">${t}</span>`).join('')}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
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
                                    <tr class="clickable-row" onclick="showEntityDetail('${e.id}')">
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
            } else if (view === 'events') {
                title.innerText = 'Event Log';
                const data = await fetchAPI('events');
                content.innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr><th>Timestamp</th><th>Agent</th><th>Type</th><th>Content</th></tr>
                            </thead>
                            <tbody>
                                ${(data?.events || []).map(ev => `
                                    <tr>
                                        <td>${ev.timestamp}</td>
                                        <td>${ev.agent_id}</td>
                                        <td><span class="badge badge-consolidated">${ev.type}</span></td>
                                        <td>${escapeHTML(ev.content || '')}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } else if (view === 'relations') {
                title.innerText = 'Relations Graph';
                const data = await fetchAPI('relations');
                content.innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr><th>Source Entity</th><th>Predicate</th><th>Target Entity</th><th>Created At</th></tr>
                            </thead>
                            <tbody>
                                ${(data?.relations || []).map(r => `
                                    <tr>
                                        <td><a href="#" onclick="showEntityDetail('${r.source_id}'); return false;" style="color:var(--accent-primary);">${r.source_title}</a></td>
                                        <td><span class="badge badge-raw">${r.predicate}</span></td>
                                        <td><a href="#" onclick="showEntityDetail('${r.target_id}'); return false;" style="color:var(--accent-primary);">${r.target_title}</a></td>
                                        <td>${r.created_at}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } else if (view === 'tags') {
                title.innerText = 'Tags Folksonomy';
                const data = await fetchAPI('tags');
                content.innerHTML = `
                    <div style="display:grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap:12px;">
                        ${(data?.tags || []).map(t => `
                            <div class="stat-card">
                                <div class="tag" style="font-size:0.9rem;">${t.name}</div>
                                <div style="font-size:0.8rem; color:var(--text-secondary); margin-top:8px;">Usage: ${t.usage_count}</div>
                                ${t.canonical_id ? `<div style="font-size:0.75rem; color:var(--status-raw);">Alias &rarr; Canonical</div>` : ''}
                            </div>
                        `).join('')}
                    </div>
                `;
            } else if (view === 'locks') {
                title.innerText = 'System Locks';
                const data = await fetchAPI('locks');
                content.innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr><th>Task Name</th><th>Locked At</th><th>Locked By PID</th><th>Last Run At</th></tr>
                            </thead>
                            <tbody>
                                ${(data?.locks || []).map(l => `
                                    <tr>
                                        <td><strong>${l.task_name}</strong></td>
                                        <td>${l.locked_at || 'Unlocked'}</td>
                                        <td>${l.locked_by_pid || 'N/A'}</td>
                                        <td>${l.last_run_at || 'N/A'}</td>
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
                                            <tr class="clickable-row" onclick="showEntityDetail('${r.id}')">
                                                <td><strong>${r.title}</strong></td>
                                                <td>${r.snippet}</td>
                                                <td>${r.score}</td>
                                            </tr>
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
