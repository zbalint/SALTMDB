# SALTMDB Skills

Portable, harness-agnostic usage guidance for agents using SALTMDB, packaged so the "how to use
this well" knowledge doesn't have to live only in one user's personal harness config.

## The gap this fills

Today, everything an agent needs to know to use SALTMDB *well* (title/quality standards, search
mode selection, the bootstrap → logging → wrap-up → consolidation lifecycle, the
retrieval-outcome telemetry convention) either sits in `AGENT_GUIDE.md` — a doc an agent has to
be told to go read — or, worse, only in a specific user's personal global instruction file. A new
user installing SALTMDB gets the MCP tools and nothing that teaches good usage patterns unless
they independently reconstruct it. Hooks (`hooks/`) enforce the *did-you-actually-do-it*
at the moments that matter; skills teach the *why/when*. Neither replaces the other.

## Included

| Skill | Covers |
| :--- | :--- |
| [`saltmdb-usage/`](saltmdb-usage/) | Title/memory-quality standards, effective `search_memory` mode usage, the full operational lifecycle (bootstrap/logging/wrap-up/consolidation), and the `retrieval_outcome` telemetry event convention. |
| [`saltmdb-skill-review/`](saltmdb-skill-review/) | Reviewing accumulated failure and retrieval telemetry, diagnosing recurring patterns, and proposing surgical text improvements to skills and hooks (review-gated, never auto-applied). |

## Installing

### Claude Code
Skills are auto-discovered from `.claude/skills/<name>/SKILL.md` (project-scoped) or
`~/.claude/skills/<name>/SKILL.md` (user-scoped, every project). Copy the directory:
```bash
mkdir -p ~/.claude/skills
cp -r saltmdb-usage ~/.claude/skills/
```
Claude Code loads the `SKILL.md` frontmatter's `description` at session start and pulls in the
full body on demand when it's relevant — no further wiring needed.

### Other harnesses (Antigravity, Copilot CLI, etc.)
The `SKILL.md` format (YAML frontmatter + Markdown body) is a Claude Code / Claude Agent SDK
convention; other harnesses don't necessarily auto-discover it the same way. The content is
still fully portable prose with no Claude-Code-specific tool syntax — append the body (everything
after the frontmatter) to your harness's own persistent instruction file, or load it into context
manually at session start, if your harness has no native skill-loading mechanism.

## Why this is separate from `hooks/`

A skill is knowledge an agent *reads*; a hook is a script the *harness* runs and can act on
(deny a tool call, inject text, block a turn close). `saltmdb-stop-retrieval-outcome-gate.py`, for
example, enforces the exact telemetry convention this skill documents — read the skill to know
*why* and *how* to log a retrieval outcome; the hook makes sure you actually do it.
