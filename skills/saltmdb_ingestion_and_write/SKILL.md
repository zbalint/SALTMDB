---
name: saltmdb_ingestion_and_write
description: "Instructions and heuristics for losslessly parsing files, capturing structured metadata, and writing granular memories to SALTMDB."
---

# SALTMDB Lossless Ingestion & Knowledge Capture

This skill instructs the agent on how to ingest documentation, codebase guides, rule sheets, and logs without losing technical detail or creating monolithic database entries.

> [!CAUTION]
> **NO DIRECT DATABASE ACCESS:** Do not run raw SQL or `sqlite3` CLI commands directly on `saltmdb.db`. Bypassing the MCP server tools breaks secrets scrubbing and FTS5 triggers. To scan or audit memories for contradictions, use the `scan_memories` tool.

## 1. The "No-Loss" Detail Retention Rule

When capturing knowledge from a file, chat history, or environment state:
* **Verbatim Code & Parameters:** Never summarize, truncate, or omit exact code blocks, version numbers, configuration values, platform requirements, port numbers, or exact error tracebacks.
* **Granular Extraction:** Do not rephrase specialized terminology or replace specific configuration variables with generic summaries. For example, do not replace `journal_mode=WAL` with "WAL enabled".

### The "Look-Before-Leap" Protocol
Before editing any file, running a shell command, or debugging a compiler error:
- **Search first:** Run `search_memory` with keywords related to the target file name, terminal command, or error stack.
- **Acknowledge constraints:** Confirm if a past resolution, configuration limit, or bug fix is already recorded to prevent repeating mistakes.

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
* **Metadata Schema:** Populate the `metadata` dictionary parameter on `store_memory` with indexable attributes:
  * `project`: The name of the codebase/project.
  * `source_path`: The relative repository path of the source file. (e.g. `CORE.md` instead of absolute paths like `C:/Users/...`).
  * `topic`: The technical category (e.g., `ops`, `auth`, `database`, `build`).
  * `date`: The ISO timestamp of the capture.
* **Clean Titles:** Do not prefix memory titles with filenames or tags (e.g. use `Language Rules` instead of `CORE.md — Language Rules`).
* **Tag normalization:** Ensure tags are lowercase, alphanumeric, prefixed with `#`, and do not contain special characters (e.g., use `#auth-error` instead of `#auth_error`). Verify against `get_canonical_tags`.

## 5. Stateful Fact Block (SFB) Layout Guideline

When writing the markdown `content` parameter for `store_memory`, structure it as:
1. **Title Heading (`#`)**: A concise, tag-free, filename-free title.
2. **Summary**: A brief overview of the memory.
3. **Claims list**: Tagged bullet points using `[FACT]`, `[DECISION]`, `[INFERENCE]`, `[STATUS]`, `[OPEN]`, or `[RESOLUTION]`.
4. **Technical Code/Config blocks**: Verbatim CLI commands, parameter limits, or config details.
5. **Chronological Trace**: A record of actions, attempts, or historical context.
