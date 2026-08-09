# Viewer remediation smoke checklist

Run this against the daemon-hosted local Viewer using an already installed Chromium or Firefox. Do not use a standalone `saltmdb-viewer` process or browser automation dependency.

## Explorer

1. Open **Memory Explorer**, set a lifecycle and memory-type filter, and advance to a later page if available.
2. Change one filter and click **Apply filters**. Confirm the result count and rows update and the pager returns to page 1.
3. With the same form values, press Enter in a text field. Confirm the same filter request and page-1 result occur.
4. Move focus through a memory title and its **Copy ID** control. Titles must be left aligned, span the available memory column, and both controls must show visible focus. Click **Copy ID** and confirm the live copy feedback.

## Relationships

1. Enter a known memory ID and activate **Explore graph** by clicking it. Repeat with Enter in the root-ID field; both paths must use the trimmed ID and return the same neighborhood.
2. Open a memory detail, activate **Explore relationship graph**, and confirm the Relationships root-ID field contains that ID and receives focus.
3. For a neighborhood with valid edges, count SVG `line` elements, visible predicate labels, and fallback-table rows. Each count must equal the returned valid-edge count.
4. Inspect an entity with no active relations. Confirm the exact text **No active relations for this memory**, no graph canvas, and the fallback-table empty state.
5. For the two-edge `max_edges=1` fixture, use DevTools Console (substitute the fixture ID) to confirm the daemon payload and temporarily feed that real response to the normal Viewer request:

   ```js
   const rootId = 'ROOT_ID';
   const limited = await (await fetch(`/api/relations/neighborhood?entity_id=${encodeURIComponent(rootId)}&max_edges=1`)).json();
   // Verify: 1 returned, 2 total, truncated: true, 1 omitted.
   const nativeFetch = window.fetch.bind(window);
   window.fetch = (resource, options) => String(resource).includes(`/api/relations/neighborhood?entity_id=${encodeURIComponent(rootId)}`)
     ? Promise.resolve(new Response(JSON.stringify(limited), { status: 200, headers: { 'Content-Type': 'application/json' } }))
     : nativeFetch(resource, options);
   ```

   The Viewer deliberately keeps request limits out of URL/UI state, so do not try to set `max_edges` through the browser location. Use **Explore graph** for `ROOT_ID`, confirm the exact text **Showing 1 of 2 relations; 1 omitted by limit.** and the visible fallback table, then restore `window.fetch = nativeFetch`.
6. If a controlled fixture includes an edge whose endpoint node is omitted from `nodes`, confirm the edge still has one SVG line, one label, and one table row. If an edge has no source or target, confirm it is excluded and a count-aware malformed-relation notice appears.

## Detail, layout, and accessibility

1. Open a memory detail from a table and close it normally. Focus must return to the title control that opened it.
2. At 1280px wide, review Explorer and both Quality tables: titles are left aligned; title, compact ID, type/lifecycle, and tags or quality remain easy to scan; the page has no horizontal scrollbar.
3. At 375px wide, verify form controls stack, only table/graph containers scroll horizontally when needed, the page has no horizontal scrollbar, and focus remains visible.
4. Confirm Quality, Tags, Diagnostics, and metadata remain structured views; raw Markdown inspection and **Copy Markdown** still work; no raw JSON dump or speculative refresh banner appears.
