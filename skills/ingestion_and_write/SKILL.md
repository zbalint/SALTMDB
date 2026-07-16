---
name: saltmdb_ingestion_and_write
description: "Instructions and heuristics for losslessly parsing files, capturing structured metadata, and writing granular memories to SALTMDB."
---

# SALTMDB Lossless Ingestion & Knowledge Capture

This skill instructs the agent on how to ingest documentation, codebase guides, rule sheets, and logs without losing technical detail or creating monolithic database entries.

## 1. The "No-Loss" Detail Retention Rule

When capturing knowledge from a file, chat history, or environment state:
* **Verbatim Code & Parameters:** Never summarize, truncate, or omit exact code blocks, version numbers, configuration values, platform requirements, port numbers, or exact error tracebacks.
* **Granular Extraction:** Do not rephrase specialized terminology or replace specific configuration variables with generic summaries. For example, do not replace `journal_mode=WAL` with "WAL enabled".

## 2. The Granularity Rule (Semantic Splitting)

Never store a large file (e.g. over 50-100 lines) or a multi-topic document as a single database memory.
* **Semantic Analysis:** Review the file's structure. Identify independent sections, rules, configurations, or components.
* **Granular Commit:** Write each distinct semantic block as a separate memory entry with its own title and target tags.
* **Context Preservation:** This prevents loading massive chunks of irrelevant text into the context window during keyword searches.

## 3. Parent-Child Relational Linking

When a document is split into multiple granular chunks, you must establish their context in the memory graph:
1. Create or identify a **root anchor memory** that represents the overall file or initiative.
2. Link each granular child memory to this root memory using:
   `store_relation(source_id=child_uuid, target_id=root_uuid, predicate="part_of")`

## 4. Metadata Tagging & Pre-Write Check

Before saving any memory to long-term storage:
* **Duplicate Check:** Run `check_duplicate_memories(title, content, owner_id, tags)` to verify if a near-identical memory exists.
  * If `duplicate_found` is `True`, do **not** write a new memory. Instead, retrieve the existing ID and perform an update (SCD Type 2).
* **Metadata Schema:** Populate the `metadata` dictionary parameter on `store_knowledge` with indexable attributes:
  * `project`: The name of the codebase/project.
  * `source_path`: The relative repository path of the source file.
  * `topic`: The technical category (e.g., `ops`, `auth`, `database`, `build`).
  * `date`: The ISO timestamp of the capture.
* **Tag normalization:** Ensure tags are lowercase, alphanumeric, prefixed with `#`, and do not contain special characters (e.g., use `#auth-error` instead of `#auth_error`). Verify against `get_canonical_tags`.
