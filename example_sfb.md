# Stateful Fact Block (SFB) Standard Format

This template serves as a reference for writing compliant memories in SALTMDB.

```markdown
---
title: "Clean Memory Title"
owner: "owner_id"
scope: "shared" | "private"
tags: ["#tag1", "#tag2"]
project: "project_or_initiative_name"
source_path: "relative_source_path"
date: "YYYY-MM-DD"
---

# Clean Memory Title

## 1. Summary
A brief, 1-2 sentence overview of the fact, constraint, or configuration.

## 2. Core Claims
Bullet points detailing the findings, decisions, or rules. Prefix every claim with UPPERCASE semantic labels:
- `[FACT]` Established truths, constants, or invariants.
- `[DECISION]` Deliberate choices or designs selected.
- `[INFERENCE]` Logical deductions based on facts.
- `[STATUS]` Progress checkpoints or health states.
- `[OPEN]` Pending questions or unresolved issues.
- `[RESOLUTION]` How an open issue was resolved.

## 3. Technical Details
Provide exact code blocks, CLI commands, configuration options, version limits, network ports, or platform requirements in full. Never summarize these.

## 4. Chronological Trace (Why)
- A brief historical description of what attempts were made and why this configuration was established.
```
