---
name: saltmdb-skill-review
description: >
  How to review accumulated SALTMDB failure telemetry and propose skill or hook improvements.
  Activate when reviewing accumulated failure logs or retrieval outcome telemetry, proposing skill
  or hook enhancements, or running a skill review or codify pass to turn recurring issues into
  actionable improvements.
compatibility: Requires the saltmdb MCP server (tools exposed as mcp__saltmdb__*)
metadata:
  author: SALTMDB project
  version: "1.0"
---

# SALTMDB Skill Review Skill

## Overview

This skill defines the "Codify" stage of SALTMDB's closed self-correction loop. While lifecycle hooks continuously detect issues and log retrieval outcomes, this skill mines accumulated telemetry (`issue` events and `retrieval_outcome` logs) to diagnose recurring failure patterns and propose surgical text diffs to `skills/` and `hooks/` files.

**Hard Rule**: This process NEVER auto-applies any file edit or code change. Its sole output is a review-gated proposal stored via `store_memory` for human review and manual application.

## Execution Frequency

- **Manual / Low-Frequency Only**: Trigger this review manually or via cron every few weeks, or when telemetry volume is meaningful.
- **Never Auto-Run Per-Session**: Overfitting a single bad session into permanent skill or hook modifications is a key failure mode. Low frequency ensures proposals represent durable, corroborated patterns rather than transient session noise.

## The 5-Step Codify Procedure

### 1. Mine Telemetry
- Check for prior `[SALTMDB Skill-Review Sweep]` memories using `search_memory` to determine the last review date. If found, only mine events created after that date.
- Retrieve events via `get_events` for `event_type="issue"` and separately `event_type="retrieval_outcome"` (`order="oldest_first"`, limit ~300 each).
- **Noise Filtering**: Evaluate event content to discard synthetic test data or unrelated-domain noise. Do NOT filter strictly by `agent_id` blocklists, as legitimate subagents use fixed role IDs.

### 2. Diagnose Patterns
- Group remaining events by the specific file (`hooks/` or `skills/`) or MCP tool name implicated.
- Require at least **2 corroborating events** before classifying a cluster as a real pattern (single occurrences are treated as noise).
- Formulate a concise causal diagnosis explaining *why* the failure occurred.

### 3. Pair-Check & Confidence Scoring
- Count total `retrieval_outcome` events mined.
- **Low-Volume Degradation (< 20 events)**: Explicitly skip paired comparisons and label every finding as `"single-diagnosis, unpaired -- lower confidence"`.
- **High-Volume Pairing (>= 20 events)**: Compare failure clusters against similar successful retrieval outcomes to sharpen the causal diagnosis.

### 4. Propose Surgical Diffs
- For each actionable pattern, draft a minimal text diff (a few lines, never a full file rewrite) targeting the single implicated file.
- Do NOT invoke any file-editing tool (`Write`, `Edit`, `replace_file_content`, etc.). The diff exists purely as text in the proposal memory.

### 5. Review Gate (Store Proposal)
- Store findings via `store_memory(memory_type="fact", ...)`; the hook's MCP server environment
  must configure `SALTMDB_OWNER_ID=agent_hook_skillreview`.
- Title format: `[SALTMDB Skill-Review Sweep] <ISO date> -- N pattern(s) found, M diff(s) proposed` (or `0 patterns found` if nothing qualified).
- Include mined event counts, date range, causal diagnoses, proposed text diffs, and confidence level (paired vs unpaired).
- Even if no patterns qualify, store a short "no new findings" memory so future runs know where the last review window ended.

## Standalone / Cron Integration

For headless or automated execution outside an interactive conversation, see the portable script `hooks/saltmdb-skill-review-sweep.py`. It executes this exact prompt-driven procedure via `claude -p` or `codex exec`.
