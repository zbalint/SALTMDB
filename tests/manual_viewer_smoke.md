# Viewer remediation smoke checklist

Run this against the daemon-hosted local Viewer using an already installed Chromium or Firefox. Do not use a standalone `saltmdb-viewer` process or browser automation dependency.

## Explorer

1. Open **Memory Explorer** in **Browse / audit list** mode. Confirm the keyword field is labelled **Keyword match in title and memory text**, rather than semantic search.
2. Select each of the four sort options and confirm the table order changes consistently. Select **Created date**, set an identical From/To UTC date, and confirm records created anywhere in that calendar day remain visible. Confirm a reversed range produces an actionable error.
3. Advance to a later page if available. Confirm sort, date range, and the other filters remain in the request and the result summary.
4. Switch to **Hybrid search**, enter a non-empty query, and confirm the ranked results state that broad hybrid retrieval is used. Open a result detail. Confirm an empty query requests no search and a no-result response is clearly explained. Switch back to Browse and confirm its filter state is retained.
5. Confirm **Core memories** is visibly and programmatically labeled and checked, then click **Apply filters**. Confirm the request includes `is_core=true` and only core memories appear.
6. Uncheck **Core memories**, change one other filter, and click **Apply filters**. Confirm `is_core` is omitted, the result updates, and the pager returns to page 1.
7. With the same form values, press Enter in a text field. Confirm the same filter request and page-1 result occur.
8. Move focus through a memory title and its **Copy ID** control. Titles must be left aligned, span the available memory column, and both controls must show visible focus. Click **Copy ID** and confirm the live copy feedback.

## Activity

1. Open **Activity** and activate **View details** on an event. Confirm timestamp, type, agent, event/session/context IDs, error code, and content appear in a read-only dialog, and focus returns to the button on close.
2. For an event with a context ID, activate **Browse this context**. Confirm Memory Explorer opens in Browse mode with that context filter and page 1 selected. For an event without context, confirm no misleading memory-navigation control is offered.

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
2. At 1280px wide, review Explorer and both Quality tables: titles are left aligned; title, compact ID, type/lifecycle, and tags or quality remain easy to scan; the page has no horizontal scrollbar. Open a detail modal and confirm every standard metadata, custom metadata, and validity label is paired with its value.
3. At 375px wide, verify form controls stack, only table/graph containers scroll horizontally when needed, the page has no horizontal scrollbar, and focus remains visible. Open both detail dialogs: they should use nearly the full viewport with modest gutters, remain at or below 95dvh, scroll internally, retain a visible close control, and preserve focus restoration. Recheck standard, custom, and validity label/value pairing in the memory detail modal.
4. Confirm Quality, Tags, Diagnostics, and metadata remain structured views; raw Markdown inspection and **Copy Markdown** still work; no raw JSON dump or speculative refresh banner appears.
