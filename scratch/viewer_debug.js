
        let allEntities = [];
        let allEvents = [];
        let currentEntitiesPage = 1;
        let currentEventsPage = 1;

        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.view-content').forEach(view => view.classList.remove('active'));
            
            // Highlight tab button programmatically
            const buttons = document.querySelectorAll('.tab-btn');
            let matchedBtn = null;
            if (tabId === 'entities') matchedBtn = buttons[0];
            else if (tabId === 'events') matchedBtn = buttons[1];
            else if (tabId === 'tags') matchedBtn = buttons[2];
            else if (tabId === 'relations') matchedBtn = buttons[3];
            else if (tabId === 'locks') matchedBtn = buttons[4];
            
            if (matchedBtn) matchedBtn.classList.add('active');
            document.getElementById(`tab-${tabId}`).classList.add('active');
            
            loadTabData(tabId);
        }

        async function loadTabData(tabId) {
            try {
                if (tabId === 'entities') {
                    const res = await fetch(`/api/entities?page=${currentEntitiesPage}`);
                    const data = await res.json();
                    if (data.error) {
                        renderError('entities', data.error);
                        return;
                    }
                    allEntities = data.entities;
                    renderEntities(allEntities);
                    renderPagination('entities', data.pagination);
                } else if (tabId === 'events') {
                    const res = await fetch(`/api/events?page=${currentEventsPage}`);
                    const data = await res.json();
                    if (data.error) {
                        renderError('events', data.error);
                        return;
                    }
                    allEvents = data.events;
                    renderEvents(allEvents);
                    renderPagination('events', data.pagination);
                } else if (tabId === 'tags') {
                    const res = await fetch('/api/tags');
                    const tags = await res.json();
                    if (tags.error) {
                        renderError('tags', tags.error);
                        return;
                    }
                    renderTags(tags);
                } else if (tabId === 'relations') {
                    loadRelationsTab();
                } else if (tabId === 'locks') {
                    const res = await fetch('/api/locks');
                    const locks = await res.json();
                    if (locks.error) {
                        renderError('locks', locks.error);
                        return;
                    }
                    renderLocks(locks);
                }
            } catch (err) {
                console.error(`Failed to load ${tabId} data:`, err);
                renderError(tabId, err.message || err);
            }
        }

        function renderEntities(entities) {
            const list = document.getElementById('entities-list');
            list.innerHTML = '';
            
            if (entities.length === 0) {
                list.innerHTML = '<p style="grid-column: 1/-1; color: var(--text-muted); text-align: center; padding: 2rem;">No long-term memories found.</p>';
                return;
            }

            entities.forEach(e => {
                const card = document.createElement('div');
                card.className = `entity-card ${e.status}`;
                card.onclick = () => showEntityDetail(e.id, e.title);
                
                const tagsHtml = e.tags.map(t => `<span class="tag-pill">${t}</span>`).join('');
                
                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <span class="status-badge ${e.status}">${e.status}</span>
                            ${e.is_core ? '<span class="status-badge" style="background-color:rgba(245,158,11,0.15); color:#f59e0b;">core</span>' : ''}
                        </div>
                        <h3 class="card-title">${escapeHtml(e.title)}</h3>
                        <div class="card-meta">
                            <div class="meta-item"><span class="meta-label">ID:</span><span>${e.id.substring(0, 8)}...</span></div>
                            <div class="meta-item"><span class="meta-label">Weight:</span><span>${e.weight}</span></div>
                            <div class="meta-item"><span class="meta-label">Updated:</span><span>${formatDate(e.updated_at)}</span></div>
                            <div class="meta-item"><span class="meta-label">Accessed:</span><span>${formatDate(e.last_accessed_at)}</span></div>
                        </div>
                    </div>
                    <div class="card-tags">${tagsHtml}</div>
                `;
                list.appendChild(card);
            });
        }

        function renderEvents(events) {
            const list = document.getElementById('events-list');
            list.innerHTML = '';

            if (events.length === 0) {
                list.innerHTML = '<tr><td colspan="5" style="color: var(--text-muted); text-align: center; padding: 2rem;">No short-term events logged.</td></tr>';
                return;
            }

            events.forEach(ev => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="color: var(--text-secondary);">${formatDate(ev.timestamp)}</td>
                    <td style="font-weight: 500;">${escapeHtml(ev.agent_id)}</td>
                    <td><span class="event-type ${ev.type.toLowerCase()}">${ev.type}</span></td>
                    <td style="word-break: break-word;">${escapeHtml(ev.content)}</td>
                    <td>${ev.error_code ? `<span class="code-snippet">${escapeHtml(ev.error_code)}</span>` : '<span style="color:var(--text-muted);">-</span>'}</td>
                `;
                list.appendChild(row);
            });
        }

        function renderTags(tags) {
            const list = document.getElementById('tags-list');
            list.innerHTML = '';

            if (tags.length === 0) {
                list.innerHTML = '<p style="color: var(--text-muted); padding: 2rem;">No folksonomy tags found.</p>';
                return;
            }

            tags.forEach(t => {
                const card = document.createElement('div');
                card.className = 'tag-card';
                card.innerHTML = `
                    <div>
                        <div class="tag-name">${escapeHtml(t.name)}</div>
                        ${t.canonical_id ? `<div class="tag-alias">alias of: <strong>${escapeHtml(t.canonical_name)}</strong></div>` : ''}
                    </div>
                    <span class="tag-badge-count">${t.count}</span>
                `;
                list.appendChild(card);
            });
        }

        function renderLocks(locks) {
            const list = document.getElementById('locks-list');
            list.innerHTML = '';

            locks.forEach(l => {
                const active = l.locked_at !== null;
                const card = document.createElement('div');
                card.className = 'lock-status-card';
                card.innerHTML = `
                    <div class="lock-header">
                        <h3 style="color:#fff;">${escapeHtml(l.task_name)}</h3>
                        <div class="lock-indicator">
                            <span class="indicator-dot ${active ? 'active' : 'inactive'}"></span>
                            <span>${active ? 'Locked' : 'Unlocked'}</span>
                        </div>
                    </div>
                    <div class="card-meta">
                        <div class="meta-item"><span class="meta-label">Locked At:</span><span>${l.locked_at ? formatDate(l.locked_at) : 'N/A'}</span></div>
                        <div class="meta-item"><span class="meta-label">PID:</span><span>${l.locked_by_pid ? l.locked_by_pid : 'N/A'}</span></div>
                        <div class="meta-item"><span class="meta-label">Last Run:</span><span>${l.last_run_at ? formatDate(l.last_run_at) : 'Never'}</span></div>
                    </div>
                `;
                list.appendChild(card);
            });
        }

        function filterEntities() {
            const query = document.getElementById('entity-search').value.toLowerCase();
            const filtered = allEntities.filter(e => 
                e.title.toLowerCase().includes(query) || 
                e.id.toLowerCase().includes(query)
            );
            renderEntities(filtered);
        }

        function filterEvents() {
            const query = document.getElementById('event-search').value.toLowerCase();
            const filtered = allEvents.filter(ev => 
                ev.content.toLowerCase().includes(query) || 
                ev.agent_id.toLowerCase().includes(query) ||
                ev.type.toLowerCase().includes(query)
            );
            renderEvents(filtered);
        }

        async function showEntityDetail(id, title) {
            try {
                const res = await fetch(`/api/entity/${id}`);
                const data = await res.json();
                
                let relationsHtml = '';
                if (data.relations && (data.relations.outgoing.length > 0 || data.relations.incoming.length > 0)) {
                    relationsHtml += '<div class="relations-modal-section" style="margin-top:2rem; border-top:1px solid #334155; padding-top:1.5rem;">';
                    relationsHtml += '<h3 style="color:#f3f4f6; margin-bottom:1rem; font-family:Outfit,sans-serif;">Connected Relationships</h3>';
                    
                    if (data.relations.outgoing.length > 0) {
                        relationsHtml += '<div style="margin-bottom:1rem;">';
                        relationsHtml += '<h4 style="color:#94a3b8; font-size:0.875rem; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.5rem;">Outgoing Relations (This node is Subject)</h4>';
                        data.relations.outgoing.forEach(r => {
                            relationsHtml += `<div class="relation-item" style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span>this --(<strong style="color:#a855f7;">${escapeHtml(r.predicate)}</strong>)--> <a href="#" onclick="closeModal(); showEntityDetail('${r.target_id}', '${escapeHtml(r.target_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.target_title)}</a></span>
                            </div>`;
                        });
                        relationsHtml += '</div>';
                    }
                    
                    if (data.relations.incoming.length > 0) {
                        relationsHtml += '<div style="margin-bottom:1rem;">';
                        relationsHtml += '<h4 style="color:#94a3b8; font-size:0.875rem; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.5rem;">Incoming Relations (This node is Object)</h4>';
                        data.relations.incoming.forEach(r => {
                            relationsHtml += `<div class="relation-item" style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span><a href="#" onclick="closeModal(); showEntityDetail('${r.source_id}', '${escapeHtml(r.source_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.source_title)}</a> --(<strong style="color:#a855f7;">${escapeHtml(r.predicate)}</strong>)--> this</span>
                            </div>`;
                        });
                        relationsHtml += '</div>';
                    }
                    relationsHtml += '</div>';
                }
                
                document.getElementById('modal-title').innerText = title;
                document.getElementById('modal-content').innerHTML = parseMarkdown(data.full_content) + relationsHtml;
                document.getElementById('detail-modal').style.display = 'flex';
            } catch (err) {
                console.error("Failed to fetch entity detail:", err);
            }
        }

        let isGraphDragging = false;
        let draggedNode = null;
        
        async function loadRelationsTab() {
            try {
                const res = await fetch('/api/relations');
                const relations = await res.json();
                
                const sidebar = document.getElementById('relations-sidebar-list');
                sidebar.innerHTML = '';
                
                if (relations.error) {
                    renderError('relations', relations.error);
                    return;
                }
                
                if (relations.length === 0) {
                    sidebar.innerHTML = '<p style="color: var(--text-muted); text-align: center; padding: 2rem;">No relationships found in database.</p>';
                    document.getElementById('relations-svg').innerHTML = '';
                    return;
                }
                
                relations.forEach(r => {
                    const item = document.createElement('div');
                    item.style = 'background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 0.75rem; font-size: 0.9rem;';
                    item.innerHTML = `
                        <div style="display:flex; justify-content:space-between; margin-bottom:0.25rem;">
                            <a href="#" onclick="showEntityDetail('${r.source_id}', '${escapeHtml(r.source_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.source_title)}</a>
                            <span style="color:#a855f7; font-weight:600; font-size:0.8rem; text-transform:uppercase;">${escapeHtml(r.predicate)}</span>
                        </div>
                        <div style="display:flex; justify-content:flex-end;">
                            <a href="#" onclick="showEntityDetail('${r.target_id}', '${escapeHtml(r.target_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.target_title)}</a>
                        </div>
                    `;
                    sidebar.appendChild(item);
                });
                
                // Build Graph Nodes & Edges
                const nodeMap = {};
                const nodes = [];
                const links = [];
                
                relations.forEach(r => {
                    if (!nodeMap[r.source_id]) {
                        nodeMap[r.source_id] = { id: r.source_id, title: r.source_title };
                        nodes.push(nodeMap[r.source_id]);
                    }
                    if (!nodeMap[r.target_id]) {
                        nodeMap[r.target_id] = { id: r.target_id, title: r.target_title };
                        nodes.push(nodeMap[r.target_id]);
                    }
                    links.push({
                        id: r.id,
                        source_id: r.source_id,
                        target_id: r.target_id,
                        predicate: r.predicate
                    });
                });
                
                const svg = document.getElementById('relations-svg');
                const svgWidth = svg.clientWidth || 800;
                const svgHeight = svg.clientHeight || 600;
                
                // Position nodes circular as a starting point
                nodes.forEach((n, idx) => {
                    const angle = (idx / nodes.length) * 2 * Math.PI;
                    n.x = svgWidth / 2 + 180 * Math.cos(angle);
                    n.y = svgHeight / 2 + 180 * Math.sin(angle);
                });
                
                const centerX = svgWidth / 2;
                const centerY = svgHeight / 2;
                
                function runSimulation() {
                    for (let step = 0; step < 100; step++) {
                        // Repulsion
                        for (let i = 0; i < nodes.length; i++) {
                            for (let j = i + 1; j < nodes.length; j++) {
                                const n1 = nodes[i];
                                const n2 = nodes[j];
                                const dx = n2.x - n1.x;
                                const dy = n2.y - n1.y;
                                const dist = Math.hypot(dx, dy) || 1;
                                if (dist < 160) {
                                    const force = (160 - dist) / dist * 0.2;
                                    n1.x -= dx * force;
                                    n1.y -= dy * force;
                                    n2.x += dx * force;
                                    n2.y += dy * force;
                                }
                            }
                        }
                        // Attraction
                        links.forEach(l => {
                            const s = nodeMap[l.source_id];
                            const t = nodeMap[l.target_id];
                            if (s && t) {
                                const dx = t.x - s.x;
                                const dy = t.y - s.y;
                                const dist = Math.hypot(dx, dy) || 1;
                                const force = (dist - 120) / dist * 0.08;
                                s.x += dx * force;
                                s.y += dy * force;
                                t.x -= dx * force;
                                t.y -= dy * force;
                            }
                        });
                        // Gravity to center
                        nodes.forEach(n => {
                            n.x += (centerX - n.x) * 0.02;
                            n.y += (centerY - n.y) * 0.02;
                        });
                    }
                }
                
                runSimulation();
                
                function drawGraph() {
                    svg.innerHTML = '';
                    
                    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
                    defs.innerHTML = `
                        <marker id="arrow" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                            <path d="M 0 2 L 10 5 L 0 8 z" fill="#475569" />
                        </marker>
                    `;
                    svg.appendChild(defs);
                    
                    // Links
                    links.forEach(l => {
                        const s = nodeMap[l.source_id];
                        const t = nodeMap[l.target_id];
                        if (!s || !t) return;
                        
                        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                        line.setAttribute('x1', s.x);
                        line.setAttribute('y1', s.y);
                        line.setAttribute('x2', t.x);
                        line.setAttribute('y2', t.y);
                        line.setAttribute('stroke', '#334155');
                        line.setAttribute('stroke-width', '2');
                        line.setAttribute('marker-end', 'url(#arrow)');
                        svg.appendChild(line);
                        
                        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                        text.setAttribute('x', (s.x + t.x) / 2);
                        text.setAttribute('y', (s.y + t.y) / 2 - 5);
                        text.setAttribute('fill', '#c084fc');
                        text.setAttribute('font-size', '10px');
                        text.setAttribute('font-weight', '600');
                        text.setAttribute('text-anchor', 'middle');
                        text.textContent = l.predicate;
                        svg.appendChild(text);
                    });
                    
                    // Nodes
                    nodes.forEach(n => {
                        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                        g.style.cursor = 'pointer';
                        
                        g.onmousedown = (e) => {
                            e.stopPropagation();
                            draggedNode = n;
                            isGraphDragging = true;
                        };
                        
                        g.onclick = (e) => {
                            e.stopPropagation();
                            showEntityDetail(n.id, n.title);
                        };
                        
                        let color = '#38bdf8'; // Default sky blue
                        if (n.title.startsWith('[ops]')) color = '#f87171'; // Red
                        else if (n.title.startsWith('[tea]')) color = '#34d399'; // Green
                        else if (n.title.toLowerCase().includes('universal')) color = '#fbbf24'; // Gold
                        
                        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                        circle.setAttribute('cx', n.x);
                        circle.setAttribute('cy', n.y);
                        circle.setAttribute('r', '10');
                        circle.setAttribute('fill', color);
                        circle.setAttribute('stroke', '#0f172a');
                        circle.setAttribute('stroke-width', '2');
                        g.appendChild(circle);
                        
                        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                        text.setAttribute('x', n.x);
                        text.setAttribute('y', n.y + 24);
                        text.setAttribute('fill', '#f3f4f6');
                        text.setAttribute('font-size', '11px');
                        text.setAttribute('font-family', "'Outfit', sans-serif");
                        text.setAttribute('text-anchor', 'middle');
                        
                        let trimmedTitle = n.title;
                        if (trimmedTitle.length > 22) {
                            trimmedTitle = trimmedTitle.substring(0, 20) + '...';
                        }
                        text.textContent = trimmedTitle;
                        
                        svg.appendChild(g);
                        
                        // Wait a tick to build rect behind labels
                        setTimeout(() => {
                            try {
                                const bbox = text.getBBox();
                                const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                                rect.setAttribute('x', bbox.x - 4);
                                rect.setAttribute('y', bbox.y - 2);
                                rect.setAttribute('width', bbox.width + 8);
                                rect.setAttribute('height', bbox.height + 4);
                                rect.setAttribute('rx', 4);
                                rect.setAttribute('fill', 'rgba(15, 23, 42, 0.85)');
                                g.insertBefore(rect, text);
                            } catch (e) {}
                        }, 0);
                        g.appendChild(text);
                    });
                }
                
                drawGraph();
                
                svg.onmousemove = (e) => {
                    if (isGraphDragging && draggedNode) {
                        const rect = svg.getBoundingClientRect();
                        draggedNode.x = e.clientX - rect.left;
                        draggedNode.y = e.clientY - rect.top;
                        drawGraph();
                    }
                };
                
                svg.onmouseup = () => {
                    isGraphDragging = false;
                    draggedNode = null;
                };
                svg.onmouseleave = () => {
                    isGraphDragging = false;
                    draggedNode = null;
                };
            } catch (err) {
                console.error("Failed to render relations graph:", err);
            }
        }

        function closeModal(event) {
            document.getElementById('detail-modal').style.display = 'none';
        }

        // Markdown parsing helper
        function parseMarkdown(md) {
            if (!md) return '';
            let html = md;
            
            // Code Blocks
            html = html.replace(/```([\\s\\S]*?)```/g, '<pre><code>$1</code></pre>');
            
            // Inline Code
            html = html.replace(/`([^`]+)`/g, '<code class="code-snippet">$1</code>');
            
            // Headers
            html = html.replace(/^#\\s+(.+)$/gm, '<h1>$1</h1>');
            html = html.replace(/^##\\s+(.+)$/gm, '<h2>$1</h2>');
            html = html.replace(/^###\\s+(.+)$/gm, '<h3>$1</h3>');
            
            // Blockquotes
            html = html.replace(/^>\\s+(.+)$/gm, '<blockquote>$1</blockquote>');
            
            // Lists
            html = html.replace(/^\\*\\s+(.+)$/gm, '<li>$1</li>');
            html = html.replace(/^-\\s+(.+)$/gm, '<li>$1</li>');
            
            // Linebreaks/Paragraphs
            html = html.replace(/^\\s*$/gm, '<br>');
            
            return html;
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        function formatDate(dateStr) {
            if (!dateStr) return 'N/A';
            try {
                const d = new Date(dateStr);
                return d.toLocaleString();
            } catch (e) {
                return dateStr;
            }
        }

        function renderPagination(type, pagination) {
            const container = document.getElementById(`${type}-pagination`);
            if (!container) return;
            
            const { page, pages, total } = pagination;
            if (pages <= 1) {
                container.innerHTML = '';
                return;
            }
            
            container.innerHTML = `
                <button class="pagination-btn" id="${type}-prev" ${page <= 1 ? 'disabled' : ''} onclick="changePage('${type}', ${page - 1})">Prev</button>
                <span class="pagination-info">Page ${page} of ${pages} (${total} total)</span>
                <button class="pagination-btn" id="${type}-next" ${page >= pages ? 'disabled' : ''} onclick="changePage('${type}', ${page + 1})">Next</button>
            `;
        }

        function changePage(type, targetPage) {
            if (type === 'entities') {
                currentEntitiesPage = targetPage;
            } else {
                currentEventsPage = targetPage;
            }
            loadTabData(type);
        }

        function renderError(tabId, errorMsg) {
            const listId = tabId === 'entities' ? 'entities-list' : (tabId === 'events' ? 'events-list' : (tabId === 'tags' ? 'tags-list' : 'locks-list'));
            const element = document.getElementById(listId);
            if (!element) return;
            
            const errorHtml = `
                <div style="grid-column: 1/-1; text-align: center; padding: 3rem; background-color: rgba(239, 68, 68, 0.05); border: 1px dashed var(--accent-error); border-radius: 8px; margin: 1rem 0; width: 100%;">
                    <p style="color: var(--accent-error); font-weight: 600; margin-bottom: 0.5rem; font-size: 1.1rem;">Operational Error</p>
                    <p style="color: var(--text-secondary); font-size: 0.95rem;">${escapeHtml(errorMsg)}</p>
                </div>
            `;
            if (tabId === 'events') {
                element.innerHTML = `<tr><td colspan="5">${errorHtml}</td></tr>`;
            } else {
                element.innerHTML = errorHtml;
            }
        }

        // Init
        loadTabData('entities');
    