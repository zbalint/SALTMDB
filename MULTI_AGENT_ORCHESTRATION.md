# 🌐 Antigravity CLI: Multi-Agent Orchestration Protocol

**Target Audience:** The Lead Orchestrator Agent (`antigravity-cli`)
**Objective:** Define the strict procedural rules for spawning sub-agents, passing context, and managing multi-agent database state in SALTMDB without data corruption or domain leakage.

## 1. The Two-Tier Architecture

In a multi-agent workflow, you (the main `antigravity-cli` instance) act as the **Orchestrator**. You hold the global context and the 13 Core Commandments. When you encounter a highly specialized or parallelizable task (e.g., writing tests, auditing security, optimizing CSS), you must spawn **Workers** (sub-agents).

Workers are short-lived, task-scoped, and operate on a strict, token-efficient subset of rules defined in `WORKER_TEMPLATE.md`.

## 2. The Golden Rules of Multi-Agent State

To maintain a coherent database, you must enforce the following data constraints:

### Rule 1: Identity by Role (`owner_id`)

* **The Rule:** `owner_id` is a fixed role, not a temporary instance.
* **Execution:** When spawning a worker, you must assign it a fixed role ID (e.g., `agent_frontend`, `agent_security`, `agent_db`).
* **DO NOT** generate randomized IDs per task (e.g., no `agent_frontend_task99`). This ensures that the next time a frontend worker is spawned, it correctly inherits all `#core` rules previously established by other frontend workers.

### Rule 2: The Context Thread (`context_id`)

* **The Rule:** Every isolated task requires a unique thread identifier.
* **Execution:** Before delegating a task, generate a descriptive `context_id` (e.g., `task_auth_refactor_12`). You must instruct the sub-agent to pass this exact string directly into the `context_id` parameter of every `log_event` and `store_memory` call it makes.

### Rule 3: Strict Epistemic Boundaries

* **The Rule:** Workers cannot overwrite each other. Only the Orchestrator can arbitrate cross-domain disputes.
* **Execution:** If `agent_security` disagrees with a memory authored by `agent_frontend` (which defaults to `scope='shared'`), the worker is instructed to leave the original memory intact, store its own finding, and link them using `manage_relation(predicate="conflicts_with")`. You must resolve this later.

### Rule 4: The `#core` Namespace Scope (Private vs. Shared)

* **The Rule:** Role-specific standing rules must never pollute the global scope.
* **Execution:** If a worker learns a new permanent behavioral rule for its specific domain and tags it `#core`, it MUST store it with `scope="private"`. This ensures `agent_frontend`'s CSS preferences are strictly invisible to `agent_db`. Only you, the Orchestrator, may store `#core` rules with `scope="shared"` (creating global project laws visible to all workers).

---

## 3. The Delegation Lifecycle (How to Spawn a Sub-Agent)

When you determine a task should be delegated, follow this sequence:

1. **Initialize the Task:** Generate your `context_id`.
2. **Draft the Sub-Agent Prompt:** Construct the prompt for the sub-agent. This prompt MUST strictly adhere to the `WORKER_TEMPLATE.md` structure.
3. **Inject Context:** Clearly define the sub-agent's `owner_id`, the assigned `context_id`, and the exact task objective.
4. **Dispatch:** Spawn the agent via your local CLI/execution tools.
5. **Await Completion:** Wait for the sub-agent to report task completion or termination before proceeding to the wrap-up phase.

---

## 4. The Cognitive Sweep & Arbitration (Wrap-Up Phase)

Once all sub-agents have completed their tasks for a specific feature or objective, you must weave their isolated work back into the global truth.

### Step 1: Pull the Thread

Retrieve the entire history of the task across all sub-agents by filtering on the context ID.
**Explicit Syntax:**
`search_memory(context_id="[your_generated_context_id]", fetch_full=True)`

### Step 2: Resolve Cross-Role Conflicts

Scan the retrieved memories and events for any relations marked `conflicts_with` or pending `consolidation_request` events that cross domain boundaries.

* Read the conflicting perspectives.
* Make an authoritative architectural decision.

### Step 3: Explicit Supersession (SCD-Versioning)

Once you have synthesized the findings or resolved a conflict, you must explicitly update the active database state. Do not assume automatic overwrites.

* **Explicit Syntax:** You MUST call `store_memory(entity_id="[old_conflict_memory_id]", content="[your_new_synthesized_decision]", scope="shared", ...)`
* By passing the old `entity_id`, SALTMDB will safely archive the conflicting sub-agent notes and promote your synthesized decision to the active state.

> ⚠️ **WARNING: Avoid Core Pollution:** Do NOT set `is_core=1` and `weight=5` when resolving an ordinary technical conflict or factual dispute. Those flags are strictly reserved for permanent, behavioral standing rules. Promoting standard architectural decisions to `#core` will permanently bloat the context window of all future agents during their bootstrap phase.

### Step 4: Finalize

Once the synthesis is stored, log a final `log_event` under your Orchestrator `owner_id` explicitly stating that the `context_id` thread has been closed and consolidated.