---
name: saltmdb_lifecycle
description: "Rules for managing memory updates (SCD Type 2), retiring obsolete records (archiving), and promoting permanent behavioral constraints to core."
---

# SALTMDB Memory Lifecycle, Archiving & Core Promotion

This skill governs how long-term memories transition through various life stages: updates, archival, and promotion to core.

## 1. SCD Type 2 Temporal Updates

When a fact, configuration, or codebase component changes:
* **Never write a new independent memory with a conflicting fact.** This introduces contradictions in search results.
* **Update existing records:** Invoke `store_knowledge` with the original `entity_id` and the updated content. The database automatically moves the old version to the history log (by setting `valid_to` to the current timestamp) and inserts the new version as active, maintaining full lineage.

## 2. Archival Rules (Retiring Memories)

When a component, feature, or project has been completely removed:
* **Retire the memory:** Invoke `archive_memory(entity_id, owner_id)`.
* **Archiving vs. Deletion:** Archiving marks the status as `"archived"` so it is excluded from default searches, while preserving historical audit trails in the SQLite file.

## 3. Core Memory Promotion Rules

Standard memories decay or shift out of context based on LRU access. However, **core instructions must never decay.**
* **What defines a Core Memory?** You must promote a memory to core (`is_core = True`, `weight = 5`, tag `#core`) if and only if it is:
  1. **Behavioral Constraints:** Custom instructions or system rules (e.g. formatting constraints, code style policies, forbidden actions).
  2. **Codebase Safeguards:** Permanent validation rules (e.g. "always run lint and unit tests before tagging git commits").
  3. **Global Configurations:** Constants or settings that apply across the entire application lifecycle.
* **Bootstrap Retrieval:** Core memories are retrieved by searching with tag filter `["#core"]` and `query_keywords = None` at bootstrap, ensuring they are loaded into your system prompt on every session.
