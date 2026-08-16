(() => {
  'use strict';

  const state = {
    view: 'overview', renderController: null, detailController: null, poller: null,
    explorerPreset: {}, explorerPage: 1, explorerMode: 'browse', hybridQuery: '', relationRoot: '', modalInvoker: null,
    focusRelationshipInput: false, skipModalFocusRestore: false,
  };
  const view = document.querySelector('#view');
  const title = document.querySelector('#view-title');
  const status = document.querySelector('#live-status');
  const notice = document.querySelector('#notice');
  const indicator = document.querySelector('#connection-indicator');
  const dialog = document.querySelector('#memory-detail');
  const detail = document.querySelector('#detail-content');
  const eventDialog = document.querySelector('#event-detail');
  const eventDetail = document.querySelector('#event-detail-content');
  const names = {
    overview: 'Overview', explorer: 'Memory Explorer', activity: 'Activity',
    relationships: 'Relationships', quality: 'Quality & Lifecycle', operations: 'Operations',
    tags: 'Tags & Taxonomy', diagnostics: 'Diagnostics',
  };

  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const svgNode = (tag, attributes = {}) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  };
  const setNotice = (text) => { notice.hidden = !text; notice.textContent = text || ''; };
  const connection = (kind, text) => {
    indicator.className = `connection-indicator is-${kind}`;
    indicator.lastElementChild.textContent = text;
  };
  const api = async (path, controller = state.renderController) => {
    try {
      const response = await fetch(path, { signal: controller?.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      connection('online', 'Viewer connected');
      return data;
    } catch (err) {
      if (err.name !== 'AbortError') connection('offline', 'Viewer unavailable');
      throw err;
    }
  };
  const showError = (err) => {
    view.replaceChildren(node('p', err.message || String(err), 'error'));
    status.textContent = 'Unavailable';
  };
  const button = (label, className, handler, type = 'button') => {
    const element = node('button', label, className);
    element.type = type;
    if (handler) element.addEventListener('click', handler);
    return element;
  };
  const setBusy = (element, busy) => element.setAttribute('aria-busy', String(busy));
  const copyText = async (value, label = 'Text') => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(value);
      setNotice(`${label} copied to clipboard.`);
    } catch (_) { setNotice(`Could not copy ${label.toLowerCase()}.`); }
  };
  const select = (label, options, value = '') => {
    const wrap = node('label', undefined, 'field');
    wrap.append(node('span', label, 'field-label'));
    const element = document.createElement('select');
    options.forEach(([optionValue, optionLabel]) => {
      const option = node('option', optionLabel); option.value = optionValue;
      option.selected = optionValue === value; element.append(option);
    });
    wrap.append(element);
    return { wrap, element };
  };
  const inputField = (label, placeholder, value = '') => {
    const wrap = node('label', undefined, 'field');
    wrap.append(node('span', label, 'field-label'));
    const element = document.createElement('input');
    element.placeholder = placeholder; element.value = value;
    wrap.append(element);
    return { wrap, element };
  };
  const checkboxField = (label, checked = false) => {
    const wrap = node('label', undefined, 'field checkbox-field');
    const element = document.createElement('input');
    element.type = 'checkbox'; element.checked = checked; element.setAttribute('aria-label', label);
    wrap.append(element, node('span', label, 'field-label'));
    return { wrap, element };
  };
  const factPair = (label, value) => {
    const pair = node('div');
    pair.append(node('dt', label), node('dd', value));
    return pair;
  };
  const statusBadge = (value) => node('span', value || 'unknown', `status-pill status-${value || 'unknown'}`);
  const tagList = (tags) => {
    const wrap = node('span', undefined, 'tag-list');
    (tags || []).forEach(tag => wrap.append(node('span', tag, 'tag')));
    return wrap;
  };
  const metric = (label, value, tone = '') => {
    const box = node('article', undefined, `card metric-card ${tone}`);
    box.append(node('p', label, 'muted'), node('strong', String(value ?? '—'), 'metric'));
    return box;
  };
  const formatBytes = (value) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return '—';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const scaled = bytes / (1024 ** index);
    return `${scaled.toFixed(scaled >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  };
  const formatTimestamp = (value, fallback = '—') => {
    if (!value) return fallback;
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
    }).format(timestamp);
  };
  const section = (heading, description) => {
    const header = node('div', undefined, 'section-heading');
    header.append(node('h3', heading));
    if (description) header.append(node('p', description, 'muted'));
    return header;
  };
  const renderTable = (columns, rows, empty = 'Nothing to show yet.') => {
    const wrap = node('div', undefined, 'table-wrap');
    const table = node('table'); const head = node('thead'); const headRow = node('tr');
    columns.forEach(column => headRow.append(node('th', column)));
    head.append(headRow); const body = node('tbody');
    if (rows.length) rows.forEach(row => body.append(row));
    else { const row = node('tr'); const cell = node('td', empty, 'muted'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
    table.append(head, body); wrap.append(table); return wrap;
  };
  const titleButton = (entity) => button(entity.title || entity.id, 'row-button', event => openDetail(entity.id, event.currentTarget));
  const memoryCell = (entity) => {
    const memory = node('td'); const idLine = node('div', undefined, 'id-prefix');
    idLine.append(node('code', entity.id.slice(0, 12)), button('Copy ID', 'copy-id', () => copyText(entity.id, 'Memory ID')));
    memory.append(titleButton(entity), idLine); return memory;
  };

  const openDetail = async (id, invoker = document.activeElement) => {
    try {
      state.detailController?.abort(); state.detailController = new AbortController();
      const data = await api(`/api/entities/${encodeURIComponent(id)}`, state.detailController);
      detail.replaceChildren();
      document.querySelector('#detail-title').textContent = data.title || 'Memory';
      const identity = node('div', undefined, 'detail-identity');
      identity.append(statusBadge(data.status), statusBadge(data.memory_type), node('code', data.id, 'memory-id'));
      detail.append(identity);
      const metadata = node('section', undefined, 'metadata-panel');
      metadata.append(section('Metadata'));
      const metadataGrid = node('dl', undefined, 'metadata-grid');
      const metadataEntries = [
        ['Lifecycle', data.status || '—'], ['Memory type', data.memory_type || 'fact'],
        ['Embedding', data.embedding_status || 'pending'], ['Quality', data.quality_status || 'Not evaluated'],
        ['Owner', data.owner_id || 'system'], ['Scope', data.scope || '—'], ['Weight', data.weight ?? '—'],
        ['Core memory', data.is_core ? 'Yes (#core)' : 'No'], ['Created', formatTimestamp(data.created_at)],
        ['Updated', formatTimestamp(data.updated_at)], ['Last accessed', formatTimestamp(data.last_accessed_at)],
        ['Context ID', data.context_id || data.project_id || '—'], ['Entity ID', data.id],
      ];
      metadataEntries.forEach(([label, value]) => metadataGrid.append(factPair(label, value)));
      metadata.append(metadataGrid);
      const topTags = node('div', undefined, 'metadata-tags'); topTags.append(node('strong', 'Tags'), tagList(data.tags)); metadata.append(topTags);
      if (data.metadata && Object.keys(data.metadata).length) {
        const custom = node('div', undefined, 'metadata-custom'); custom.append(node('strong', 'Custom metadata'));
        const customFacts = node('dl', undefined, 'custom-facts');
        Object.entries(data.metadata).forEach(([key, value]) => customFacts.append(factPair(key, typeof value === 'string' ? value : JSON.stringify(value))));
        custom.append(customFacts); metadata.append(custom);
      }
      detail.append(metadata);
      const actions = node('div', undefined, 'detail-actions');
      const raw = node('pre', data.full_content || '', 'raw-markdown'); raw.hidden = true;
      actions.append(button('Copy ID', '', () => copyText(data.id, 'Memory ID')));
      actions.append(button('Copy Markdown', '', () => copyText(data.full_content || '', 'Markdown')));
      actions.append(button('Show raw', '', () => { raw.hidden = !raw.hidden; raw.previousElementSibling.hidden = raw.hidden; }));
      detail.append(actions);
      const markdown = node('div', undefined, 'markdown');
      const parsed = window.marked?.parse(data.full_content || '') || '';
      markdown.innerHTML = window.DOMPurify.sanitize(parsed, {
        USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'svg', 'math'], FORBID_ATTR: ['style'],
      });
      detail.append(markdown, raw);
      const evidence = node('section', undefined, 'evidence'); evidence.append(section('Memory evidence'));
      const facts = node('dl', undefined, 'facts');
      const entries = [['Valid from', formatTimestamp(data.valid_from)], ['Valid to', formatTimestamp(data.valid_to, 'Current')]];
      entries.forEach(([label, value]) => facts.append(factPair(label, value)));
      evidence.append(facts);
      const relationSummary = node('p', `${data.relations.outgoing_count} outgoing · ${data.relations.incoming_count} incoming relations`, 'muted');
      evidence.append(relationSummary, button('Explore relationship graph', '', () => {
        state.relationRoot = data.id; state.focusRelationshipInput = true;
        state.skipModalFocusRestore = true; state.view = 'relationships'; dialog.close(); render();
      }));
      detail.append(evidence); state.modalInvoker = invoker; dialog.showModal();
    } catch (err) { if (err.name !== 'AbortError') setNotice(err.message); }
  };

  const overview = async () => {
    const data = await api('/api/stats'); const fragment = document.createDocumentFragment();
    const heading = section('Memory at a glance', 'A quick read of the current knowledge base.');
    const grid = node('div', undefined, 'grid');
    [['Active memories', data.active_entities, ''], ['Raw memories', data.raw_count, 'raw'], ['Consolidated', data.consolidated_count, 'consolidated'], ['Archived', data.archived_count, 'archived'], ['Pending embeddings', data.embeddings_pending, 'warning']]
      .forEach(item => grid.append(metric(...item)));
    fragment.append(heading, grid);
    const lifecycle = node('div', undefined, 'card lifecycle-summary'); lifecycle.append(section('Lifecycle', 'Open a focused explorer view.'));
    [['All memories', ''], ['Raw', 'raw'], ['Consolidated', 'consolidated'], ['Archived', 'archived']].forEach(([label, value]) => lifecycle.append(button(label, `filter-link ${value ? `status-${value}` : ''}`, () => {
      state.explorerPreset = { status: value }; state.explorerPage = 1; state.view = 'explorer'; render();
    })));
    fragment.append(lifecycle); view.replaceChildren(fragment);
  };

  const explorer = async () => {
    const mode = select('Explorer mode', [['browse', 'Browse / audit list'], ['hybrid', 'Hybrid search']], state.explorerMode);
    mode.element.addEventListener('change', () => { state.explorerMode = mode.element.value; render(); });
    const result = node('div');
    if (state.explorerMode === 'hybrid') {
      const form = node('form', undefined, 'toolbar explorer-toolbar');
      const query = inputField('Hybrid retrieval query', 'Search the memory graph', state.hybridQuery);
      form.append(mode.wrap, query.wrap, button('Search memories', 'primary', undefined, 'submit'));
      form.addEventListener('submit', async event => {
        event.preventDefault(); state.hybridQuery = query.element.value.trim();
        if (!state.hybridQuery) { result.replaceChildren(node('p', 'Enter a query to run hybrid retrieval.', 'muted')); return; }
        setBusy(result, true);
        try {
          const data = await api(`/api/search?q=${encodeURIComponent(state.hybridQuery)}`);
          const rows = data.results.map(item => {
            const row = node('tr'); const score = Number.isFinite(item.score) ? item.score.toFixed(4) : '—';
            const typeCell = node('td'); typeCell.append(statusBadge(item.memory_type || 'fact'));
            row.append(memoryCell(item), node('td', score), typeCell, node('td', item.owner_id || '—')); return row;
          });
          result.replaceChildren(section(`${data.results.length} ranked matches`, 'Broad hybrid retrieval combines the established lexical and semantic backend signals.'), renderTable(['Memory', 'Score', 'Type', 'Owner'], rows, 'No matching active memories.'));
        } finally { setBusy(result, false); }
      });
      view.replaceChildren(section('Explore memories', 'Hybrid Search returns ranked backend retrieval results. Browse / audit list remains the pageable metadata workspace.'), form, result);
      if (state.hybridQuery) form.requestSubmit();
      return;
    }
    const form = node('form', undefined, 'toolbar explorer-toolbar');
    const search = inputField('Keyword match in title and memory text', 'Literal title or content text', state.explorerPreset.q || '');
    const prefixField = inputField('ID prefix', 'ID prefix', state.explorerPreset.id_prefix || '');
    const tagField = inputField('Tag', 'Tag', state.explorerPreset.tag || '');
    const { element: q } = search; const { element: prefix } = prefixField; const { element: tag } = tagField;
    const lifecycle = select('Lifecycle', [['', 'All statuses'], ['raw', 'Raw'], ['consolidated', 'Consolidated'], ['archived', 'Archived']], state.explorerPreset.status || '');
    const type = select('Memory type', [['', 'All types'], ['decision', 'Decision'], ['fact', 'Fact'], ['procedure', 'Procedure'], ['preference', 'Preference'], ['event', 'Event']], state.explorerPreset.memory_type || '');
    const core = checkboxField('Core memories', state.explorerPreset.is_core === 'true');
    const sort = select('Sort', [['updated_desc', 'Updated: newest first'], ['updated_asc', 'Updated: oldest first'], ['created_desc', 'Created: newest first'], ['created_asc', 'Created: oldest first']], state.explorerPreset.sort || 'updated_desc');
    const dateField = select('Date field', [['updated', 'Updated date'], ['created', 'Created date']], state.explorerPreset.date_field || 'updated');
    const dateFrom = inputField('From date (UTC)', 'YYYY-MM-DD', state.explorerPreset.date_from || ''); dateFrom.element.type = 'date';
    const dateTo = inputField('To date (UTC)', 'YYYY-MM-DD', state.explorerPreset.date_to || ''); dateTo.element.type = 'date';
    const resetFilters = button('Reset filters', '', () => {
      state.explorerPreset = {}; state.explorerPage = 1; render();
    });
    form.append(mode.wrap, search.wrap, prefixField.wrap, tagField.wrap, lifecycle.wrap, type.wrap, core.wrap, sort.wrap, dateField.wrap, dateFrom.wrap, dateTo.wrap, button('Apply filters', 'primary', undefined, 'submit'), resetFilters);
    let currentParams = new URLSearchParams(state.explorerPreset);
    const list = async (params, page = 1) => {
      currentParams = new URLSearchParams(params); const requestParams = new URLSearchParams(params); requestParams.set('page', String(page)); requestParams.set('limit', '50');
      setBusy(result, true);
      try {
      const data = await api(`/api/entities?${requestParams}`); const rows = data.entities.map(entity => {
        const row = node('tr'); const memory = memoryCell(entity);
        const lifecycleCell = node('td'); lifecycleCell.append(statusBadge(entity.status)); const typeCell = node('td'); typeCell.append(statusBadge(entity.memory_type));
        const tags = node('td'); tags.append(tagList(entity.tags)); row.append(memory, typeCell, lifecycleCell, tags); return row;
      });
      const pager = node('nav', undefined, 'pagination'); pager.setAttribute('aria-label', 'Memory pages');
      const previous = button('Previous', '', () => list(currentParams, page - 1)); previous.disabled = page <= 1;
      const next = button('Next', '', () => list(currentParams, page + 1)); next.disabled = page >= data.total_pages;
      pager.append(previous, node('span', `Page ${data.page} of ${data.total_pages || 1} · ${data.total_count} memories`, 'muted'), next);
      const dateSummary = data.date_from || data.date_to ? ` · ${data.date_field} dates ${data.date_from || '…'} to ${data.date_to || '…'} (inclusive UTC)` : '';
      result.replaceChildren(section(`${data.total_count} memories`, `${data.sort.replace('_', ' ')}${dateSummary}. Browse filters are applied before paging.`), renderTable(['Memory', 'Type', 'Lifecycle', 'Tags'], rows), pager);
      state.explorerPage = page;
      } finally { setBusy(result, false); }
    };
    form.addEventListener('submit', async event => {
      event.preventDefault(); const params = new URLSearchParams();
      [['q', q.value], ['id_prefix', prefix.value], ['tag', tag.value], ['status', lifecycle.element.value], ['memory_type', type.element.value], ['sort', sort.element.value]].forEach(([key, value]) => { if (value) params.set(key, value); });
      if (core.element.checked) params.set('is_core', 'true');
      if (dateFrom.element.value || dateTo.element.value) {
        params.set('date_field', dateField.element.value);
        if (dateFrom.element.value) params.set('date_from', dateFrom.element.value);
        if (dateTo.element.value) params.set('date_to', dateTo.element.value);
      }
      state.explorerPreset = Object.fromEntries(params); state.explorerPage = 1; await list(params, 1);
    });
    view.replaceChildren(section('Explore memories', 'Browse a pageable audit list with explicit metadata filters. Core-memory filtering is available. Use Hybrid Search for ranked semantic-plus-lexical retrieval.'), form, result);
    const initial = new URLSearchParams(state.explorerPreset); await list(initial, state.explorerPage);
  };

  const openEventDetail = (event, invoker = document.activeElement) => {
    eventDetail.replaceChildren();
    const facts = node('dl', undefined, 'metadata-grid');
    [['Event ID', event.id], ['Timestamp', event.timestamp], ['Type', event.type], ['Agent', event.agent_id || '—'], ['Session ID', event.session_id || '—'], ['Context ID', event.context_id || '—'], ['Error code', event.error_code || '—']].forEach(([label, value]) => facts.append(factPair(label, value)));
    eventDetail.append(section('Event evidence', 'This is a read-only record. Events do not imply a linked memory unless a context is supplied.'), facts);
    const content = node('pre', event.content || '—', 'raw-markdown'); eventDetail.append(section('Event content'), content);
    const actions = node('div', undefined, 'detail-actions');
    actions.append(button('Copy event ID', '', () => copyText(event.id, 'Event ID')));
    if (event.context_id) actions.append(button('Browse this context', '', () => { state.explorerMode = 'browse'; state.explorerPreset = { context_id: event.context_id }; state.explorerPage = 1; state.view = 'explorer'; eventDialog.close(); render(); }));
    eventDetail.append(actions); eventDialog._invoker = invoker; eventDialog.showModal();
  };

  const activity = async () => {
    const data = await api('/api/events?limit=20'); const rows = data.events.map(event => {
      const row = node('tr'); const actions = node('td'); actions.append(button('View details', '', click => openEventDetail(event, click.currentTarget))); row.append(node('td', event.timestamp), node('td', event.type), node('td', event.agent_id || '—'), node('td', event.content), actions); return row;
    }); view.replaceChildren(section('Recent activity', 'Read-only operational evidence. Open an event to inspect its full fields or browse its context when available.'), renderTable(['Time', 'Type', 'Agent', 'Event', 'Action'], rows));
  };

  const normalizeGraph = (data) => {
    const nodes = new Map();
    (data.nodes || []).forEach(item => {
      if (typeof item?.id === 'string' && item.id) nodes.set(item.id, { ...item, title: item.title || item.id });
    });
    const edges = []; let malformedEdges = 0;
    (data.edges || []).forEach(edge => {
      if (typeof edge?.source !== 'string' || !edge.source || typeof edge?.target !== 'string' || !edge.target) {
        malformedEdges += 1; return;
      }
      if (!nodes.has(edge.source)) nodes.set(edge.source, { id: edge.source, title: edge.source, status: 'raw' });
      if (!nodes.has(edge.target)) nodes.set(edge.target, { id: edge.target, title: edge.target, status: 'raw' });
      edges.push(edge);
    });
    return { nodes: [...nodes.values()], edges, malformedEdges };
  };

  const renderGraph = ({ nodes, edges }) => {
    const canvas = node('div', undefined, 'graph-canvas'); const width = 760; const height = 420;
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Relationship neighborhood graph' });
    const radiusX = 270; const radiusY = 145; const positions = new Map();
    nodes.forEach((item, index) => {
      const angle = (Math.PI * 2 * index / Math.max(nodes.length, 1)) - Math.PI / 2;
      positions.set(item.id, { x: width / 2 + Math.cos(angle) * radiusX, y: height / 2 + Math.sin(angle) * radiusY });
    });
    const edgeLayer = svgNode('g', { class: 'graph-edges' });
    edges.forEach(edge => {
      const source = positions.get(edge.source); const target = positions.get(edge.target);
      edgeLayer.append(svgNode('line', { x1: source.x, y1: source.y, x2: target.x, y2: target.y }));
      const label = svgNode('text', { x: (source.x + target.x) / 2, y: (source.y + target.y) / 2 - 6, class: 'graph-edge-label', 'data-predicate': edge.predicate || 'related_to' }); label.textContent = edge.predicate || 'related_to'; edgeLayer.append(label);
    }); svg.append(edgeLayer);
    const nodeLayer = svgNode('g', { class: 'graph-nodes' });
    nodes.forEach(item => {
      const position = positions.get(item.id); const group = svgNode('g', { class: `graph-node status-${item.status}`, tabindex: '0', role: 'button', 'aria-label': `Open ${item.title || item.id}` });
      group.append(svgNode('circle', { cx: position.x, cy: position.y, r: 25 }));
      const label = svgNode('text', { x: position.x, y: position.y + 44, 'text-anchor': 'middle' }); label.textContent = (item.title || item.id).slice(0, 25); group.append(label);
      group.addEventListener('click', () => openDetail(item.id)); group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDetail(item.id); } }); nodeLayer.append(group);
    }); svg.append(nodeLayer); canvas.append(svg); return canvas;
  };

  const relationships = async () => {
    const form = node('form', '', 'toolbar'); const rootField = inputField('Root memory ID', 'Memory ID', state.relationRoot);
    const { element: input } = rootField; form.append(rootField.wrap, button('Explore graph', 'primary', undefined, 'submit')); const result = node('div');
    const load = async root => {
      if (!root) return; setBusy(result, true);
      try {
      const data = await api(`/api/relations/neighborhood?entity_id=${encodeURIComponent(root)}`); state.relationRoot = root;
      const graph = normalizeGraph(data);
      const rows = graph.edges.map(edge => {
        const source = graph.nodes.find(item => item.id === edge.source); const target = graph.nodes.find(item => item.id === edge.target);
        const row = node('tr'); row.append(node('td', edge.predicate || 'related_to', 'predicate-pill'), node('td', edge.source_title || source?.title || edge.source), node('td', edge.target_title || target?.title || edge.target)); return row;
      });
      const contents = [section(`${graph.nodes.length} connected memories`, `${data.returned_edges} relations. Select a node for its full evidence.`)];
      if (data.returned_edges === 0) contents.push(node('p', 'No active relations for this memory', 'muted'));
      if (graph.malformedEdges) contents.push(node('p', `${graph.malformedEdges} malformed relations could not be rendered.`, 'error'));
      if (data.truncated) contents.push(node('p', `Showing ${data.returned_edges} of ${data.total_matching_edges} relations; ${data.omitted_edge_count} omitted by limit.`, 'muted'));
      if (graph.edges.length) contents.push(renderGraph(graph));
      contents.push(renderTable(['Relation', 'Source', 'Target'], rows, data.returned_edges === 0 ? 'No active relations for this memory' : 'No renderable relations.'));
      result.replaceChildren(...contents);
      } finally { setBusy(result, false); }
    };
    form.addEventListener('submit', async event => { event.preventDefault(); await load(input.value.trim()); });
    view.replaceChildren(section('Relationship graph', 'A bounded visual neighborhood centered on one memory.'), form, result);
    if (state.focusRelationshipInput) { state.focusRelationshipInput = false; input.focus(); }
    if (state.relationRoot) await load(state.relationRoot);
  };

  const quality = async () => {
    const data = await api('/api/quality'); const fragment = document.createDocumentFragment();
    const attention = node('div', undefined, 'grid'); attention.append(metric('Quality signals', data.items.length, 'warning'), metric('Orphaned raw memories', data.orphan_raw.length, 'raw'));
    const qualityRows = data.items.map(item => { const row = node('tr'); const lifecycle = node('td'); lifecycle.append(statusBadge(item.status)); const embedding = node('td'); embedding.append(statusBadge(item.embedding_status || 'pending')); const flags = node('td'); flags.append(tagList(item.quality_flags)); row.append(memoryCell(item), lifecycle, embedding, node('td', item.quality_status || 'Not evaluated'), flags); return row; });
    const orphanRows = data.orphan_raw.map(item => { const row = node('tr'); row.append(memoryCell(item)); return row; });
    fragment.append(section('Quality & lifecycle', 'Read-only signals to focus maintenance work.'), attention, section('Embedding and quality signals'), renderTable(['Memory', 'Lifecycle', 'Embedding', 'Quality', 'Flags'], qualityRows), section('Raw memories without relations'), renderTable(['Memory'], orphanRows)); view.replaceChildren(fragment);
  };

  const operations = async () => {
    const data = await api('/api/operations'); const grid = node('div', undefined, 'grid');
    [['Daemon ready', data.daemon.ready ? 'Ready' : 'Not ready', data.daemon.ready ? 'ok' : 'warning'], ['Hello sessions', data.daemon.active_hello_sessions], ['In-flight RPCs', data.daemon.inflight_rpc_dispatches], ['Database size', formatBytes(data.database.files.db_bytes)], ['Schema version', data.database.schema_version]].forEach(item => grid.append(metric(...item)));
    view.replaceChildren(section('Operations', 'Point-in-time daemon and database health supplied by the daemon.'), grid);
  };

  const tags = async () => {
    const data = await api('/api/tags'); const rows = data.tags.map(tag => { const row = node('tr'); const name = node('td'); name.append(button(tag.name, 'row-button', () => { state.explorerPreset = { tag: tag.name }; state.explorerPage = 1; state.view = 'explorer'; render(); })); row.append(name, node('td', String(tag.usage_count)), node('td', tag.canonical_id || 'Canonical')); return row; });
    view.replaceChildren(section('Tags & taxonomy', 'Select a tag to inspect every matching memory.'), renderTable(['Tag', 'Memories', 'Canonical target'], rows));
  };

  const diagnostics = async () => {
    const embeddingData = await api('/api/embeddings_stats'); const fragment = document.createDocumentFragment();
    const grid = node('div', undefined, 'grid'); [['Ready', embeddingData.ready, 'ok'], ['Pending', embeddingData.pending, 'pending'], ['Failed', embeddingData.failed, 'failed'], ['Archived', embeddingData.archived, 'archived']].forEach(item => grid.append(metric(...item)));
    fragment.append(section('Embedding diagnostics', 'A local projection for inspection; it is not a similarity decision.'), grid);
    const projection = node('section', undefined, 'card'); projection.append(section('2D embedding projection', 'Calculates a bounded local projection of ready embeddings only when requested.'));
    const projectionResult = node('div'); projection.append(button('Load projection', '', async () => {
      const scatterData = await api('/api/scatterplot'); projectionResult.replaceChildren();
      if (scatterData.error) projectionResult.append(node('p', scatterData.error, 'muted'));
      else if (!scatterData.points?.length) projectionResult.append(node('p', 'No ready embeddings are available to project yet.', 'muted'));
      else {
        const plot = node('div', undefined, 'scatterplot'); const svg = svgNode('svg', { viewBox: '0 0 760 300', role: 'img', 'aria-label': 'Two dimensional embedding projection' }); const xs = scatterData.points.map(point => point.x); const ys = scatterData.points.map(point => point.y); const minX = Math.min(...xs); const minY = Math.min(...ys); const xSpan = Math.max(...xs) - minX || 1; const ySpan = Math.max(...ys) - minY || 1;
        scatterData.points.forEach(point => { const circle = svgNode('circle', { cx: 30 + (point.x - minX) / xSpan * 700, cy: 270 - (point.y - minY) / ySpan * 240, r: 4, class: `scatter-point status-${point.status}`, tabindex: '0', role: 'button', 'aria-label': point.title }); circle.addEventListener('click', () => openDetail(point.id)); svg.append(circle); }); plot.append(svg); projectionResult.append(plot);
      }
    }), projectionResult); fragment.append(projection);
    view.replaceChildren(fragment);
  };

  const loaders = { overview, explorer, activity, relationships, quality, operations, tags, diagnostics };
  const render = async () => {
    state.renderController?.abort(); state.renderController = new AbortController(); title.textContent = names[state.view];
    document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('is-active', item.dataset.view === state.view));
    setBusy(view, true);
    try { status.textContent = 'Refreshing'; await loaders[state.view](); status.textContent = 'Updated just now'; } catch (err) { if (err.name !== 'AbortError') showError(err); } finally { setBusy(view, false); }
  };
  const schedule = () => {
    clearInterval(state.poller); state.poller = setInterval(() => {
      if (!document.hidden && ['overview', 'activity', 'operations'].includes(state.view)) render();
    }, 10000);
  };
  document.querySelectorAll('.nav-item').forEach(item => item.addEventListener('click', () => { state.view = item.dataset.view; setNotice(''); render(); }));
  document.querySelector('#refresh').addEventListener('click', render);
  document.querySelector('#close-detail').addEventListener('click', () => dialog.close());
  document.querySelector('#close-event-detail').addEventListener('click', () => eventDialog.close());
  dialog.addEventListener('close', () => {
    const invoker = state.modalInvoker; state.modalInvoker = null;
    if (state.skipModalFocusRestore) { state.skipModalFocusRestore = false; return; }
    if (invoker?.isConnected) invoker.focus();
  });
  eventDialog.addEventListener('close', () => {
    const invoker = eventDialog._invoker; eventDialog._invoker = null;
    if (invoker?.isConnected) invoker.focus();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && ['overview', 'activity', 'operations'].includes(state.view)) render(); });
  window.addEventListener('beforeunload', () => { clearInterval(state.poller); state.renderController?.abort(); state.detailController?.abort(); });
  connection('checking', 'Checking connection…'); render(); schedule();
})();
