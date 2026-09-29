# Viewer remediation smoke checklist

Run this against the daemon-hosted local Viewer using an already installed Chromium or Firefox. Do not use a standalone `saltmdb-viewer` process or browser automation dependency.

## Memories

1. Open **Memories** in **Browse / audit list** mode. Confirm the keyword field is labelled **Keyword match in title and memory text**, rather than semantic search.
2. Select each of the four sort options and confirm the table order changes consistently. Select **Created date**, set an identical From/To UTC date, and confirm records created anywhere in that calendar day remain visible. Confirm a reversed range produces an actionable error.
3. Advance to a later page if available. Confirm sort, date range, and the other filters remain in the request and the result summary.
4. Click **Reset filters**. Confirm keyword, ID-prefix, tag, lifecycle, type, core, date controls, and date field clear; sort returns to Updated: newest first; results reload at page 1; and Hybrid Search’s query remains untouched.
5. Switch to **Hybrid search**, enter a non-empty query, and confirm the ranked results state that broad hybrid retrieval is used. Open a result detail. Confirm an empty query requests no search and a no-result response is clearly explained. Switch back to Browse and confirm its filter state is retained.
6. Confirm **Core memories** is visibly and programmatically labeled and checked, then click **Apply filters**. Confirm the request includes `is_core=true` and only core memories appear.
7. Uncheck **Core memories**, change one other filter, and click **Apply filters**. Confirm `is_core` is omitted, the result updates, and the pager returns to page 1.
8. With the same form values, press Enter in a text field. Confirm the same filter request and page-1 result occur.
9. Move focus through a memory title and its **Copy ID** control. Titles must be left aligned, span the available memory column, and both controls must show visible focus. Click **Copy ID** and confirm the live copy feedback.

## Overview

1. Open **Overview** and confirm the *Events & connections* group shows **Traces (last 24 hours)** and **All traces** next to the event and session counts, and that the numbers match the Agent Sessions trace totals.

## Events

1. Open **Events** and activate **View details** on an event. Confirm timestamp, type, agent, event/session/context IDs, error code, and content appear in a read-only dialog, and focus returns to the button on close.
2. For an event with a context ID, activate **Browse this context**. Confirm Memories opens in Browse mode with that context filter and page 1 selected. For an event without context, confirm no misleading memory-navigation control is offered.

## System Health

1. Open **System Health** and confirm Database size uses a readable binary unit appropriate to its magnitude (B, KB, MB, GB, or TB), rather than always appending raw bytes.

## Memory Map

1. Enter a known memory ID and activate **Explore graph** by clicking it. Repeat with Enter in the root-ID field; both paths must use the trimmed ID and return the same neighborhood.
2. Open a memory detail, activate **Explore relationship graph**, and confirm the Memory Map root-ID field contains that ID and receives focus.
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
2. At 1280px wide, review Memories and both Memory Quality tables: titles are left aligned; title, compact ID, type/lifecycle, and tags or quality remain easy to scan; the page has no horizontal scrollbar. Open a detail modal and confirm every standard metadata, custom metadata, and validity label is paired with its value.
3. At 375px wide, verify form controls stack, only table/graph containers scroll horizontally when needed, the page has no horizontal scrollbar, and focus remains visible. Open both detail dialogs: they should use nearly the full viewport with modest gutters, remain at or below 95dvh, scroll internally, retain a visible close control, and preserve focus restoration. Recheck standard, custom, and validity label/value pairing in the memory detail modal.
4. In a memory detail dialog, confirm Created, Updated, Last accessed, Valid from, and Valid to use readable local date/time text with a timezone label, rather than raw ISO-8601 strings. Missing Valid to must still show **Current**.
4. Confirm Memory Quality, Tags, Diagnostics, and metadata remain structured views; raw Markdown inspection and **Copy Markdown** still work; no raw JSON dump or speculative refresh banner appears.

## Conversation traces

1. Open **Agent Sessions** and confirm a **Traces** column shows a count per session. Open a session that has traces and confirm a **conversation traces** section lists them newest first with a status badge, a prompt snippet, and a **View trace** button; a session without traces shows the empty-state sentence.
2. Activate **View trace**. Confirm the dialog shows harness, status, owner, times, copyable trace and session IDs, then the full **User prompt** and **Final assistant message** in scrollable blocks that keep their line breaks. Paste a prompt containing `<script>` or Markdown into a test trace and confirm it is shown as literal text, never rendered.
3. In the trace dialog, activate a title under **Memories written in this turn**. Confirm the trace dialog closes and that memory's detail opens.
4. Open a memory detail and confirm a **Conversation traces** section lists up to five traces that wrote it. Activate **View trace** from there; on close, focus must return to that button.

## Filters, paging, and URL state

1. Open **Events**, filter by event type, agent, session, context, and text, and confirm the total, the rows, and **Page x of y** all follow the filters. Confirm **Previous**/**Next** work and **Reset filters** clears everything.
2. Open **Memory Quality** and confirm **Quality signals** and **Orphaned raw memories** show real totals (create more than 50 signals or check against a known count). Confirm the embedding-status and quality-status filters and the pager work, and that a truncated orphan list says **Showing N of M**.
3. Apply a filter on Events, go to page 2, then reload the browser. Confirm the same view, filters, and page return. Copy the URL into a new tab and confirm the same result.
4. Open a session's detail, then press the browser Back button: the session list returns. Press Forward: the detail returns. Changing only a filter or page must not add a history entry per change.
5. Edit the URL hash to `#view=constructor` and confirm the Viewer ignores it and stays usable.

## Memory Map, System Health, and Diagnostics additions

1. In **Memory Map**, change **Depth** to 2 hops, set a **Predicate**, tick **Include archived**, and set an **As of (UTC)** in the past; confirm each changes the neighborhood as expected and the legend explains node colours.
2. In **System Health**, confirm warnings appear when embeddings have failed (or the text **No warnings** otherwise), and that uptime, version, WAL/SHM size, SQLite pages, free pages, vector availability, and the latest snapshot (or **No snapshot has been written yet.**) are shown.
3. In **Diagnostics**, load the projection. Tab to a point and press Enter and Space: each must open the memory detail, and Space must not scroll the page. If more than 500 embeddings are ready, confirm the sampling note appears.

## Keyboard, focus, and refresh

1. Open **Events**, focus a **View details** button, and wait 30 seconds. The table must not be replaced under the focused control. Move focus out of the view and confirm auto-refresh resumes.
2. With any dialog open, wait 30 seconds on Overview, Events, or System Health and confirm the view behind it does not re-render; after closing the dialog, focus returns to the control that opened it.
3. Inspect the navigation: exactly one item has `aria-current="page"`. In a memory detail, **Show raw** toggles to **Show rendered** and its `aria-pressed` state follows.
4. Stop the daemon's database access (or block a request in DevTools) and submit a filter form or click a pager: an error appears in the notice bar instead of the control silently doing nothing.
