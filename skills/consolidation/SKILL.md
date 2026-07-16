---
name: saltmdb_consolidation
description: "Guidelines and protocols for lossless cognitive consolidation of raw memories, ensuring crucial details are never dropped."
---

# SALTMDB Lossless Cognitive Consolidation

This skill guides the agent in running background consolidations (merges) of raw memories while strictly preventing the loss of codebase details, parameter limits, version numbers, and setup code blocks.

## 1. Consolidation is NOT Summarization

* **Objective:** The goal of consolidation is to clean up structural clutter and remove grammatical redundancy—**not** to compress technical details.
* **Lossless Synthesis:** If three memories describe different debugging attempts or config parameters of a component, merge them into a single comprehensive memory. Ensure that all distinct configuration paths, error logs, and parameters are preserved in full.

## 2. The Context Preservation Checklist

Before invoking the `commit_consolidation` tool, check that the proposed merged memory contains:
1. **Code and Configuration Verbatim:** Every code snippet, CLI command, and config file line from the parent memories must be present in the consolidated output.
2. **Version & Environment Context:** Retain all platform constraints (OS versions, dependencies, library versions).
3. **Chronological Trace:** If the raw memories document a progression of attempts and issues, preserve the sequence of findings (e.g. "Attempt 1 failed because X. Resolved by applying Y").

## 3. Resolving Event Ledger Signals

* **Bootstrap Scan:** At session start, run `get_recent_events(type_filter="consolidation_request")`.
* **State Check:** Examine the dynamic `"status"` attribute of each fetched event:
  * Only process events that have `"status": "pending"`.
  * Ignore events with `"status": "resolved"`, as their target raw entities have already been consolidated by another librarian session.
* **Commit Actions:** Execute the consolidation by calling `commit_consolidation` with the parent UUID list, title, content, tags, scope, and weight. The database will automatically retire the raw parent nodes, updating the event ledger status to `"resolved"`.
