"""Ground-truth transcription of Codex's original 2026-08-08 live-retrieval audit.

Recovered from Codex CLI's own local session transcript (`codex-tui`, session
019fe1db-d3de-7331-a920-5b78cf5f2f31, started 2026-08-08T14:51:48Z), NOT reconstructed
from memory or prose -- a local-file read only, no MCP tools, no live DB access (Phase 0
guardrail-compliant). Raw extracted exec-tool inputs/outputs are archived in this
session's scratchpad as `codex_audit_calls_full.json`; see SALTMDB memory `cd006191`
(Phase 1a completion note, `elaborates_on` handover `fad8df05`) for the full recovery
methodology and the runtime-state findings (SALTMDB_ENABLE_SEMANTIC=true,
SALTMDB_RERANKER_MODEL unset/disabled, no call ever used explain_mode or
use_cross_encoder).

Each entry's `kwargs` dict is exactly what Codex passed to the MCP `search_memory` tool,
minus the MCP-wrapper-only `kwargs` string param (an artifact of the tool's dynamic-kwargs
schema, not a real `search_memory()` Python parameter) and `entity_id`/`fetch_full` calls
(direct entity fetches, not pipeline calls -- listed separately in FETCH_FULL_CALLS for
completeness but not replayed through the ranking pipeline).

`source_exec_call_id` ties each entry back to the originating exec-tool call in
codex_audit_calls_full.json for cross-checking against Codex's own original result
payload (Phase 1d's drift-diff step).
"""

# Known entity ids referenced as "expected" answers in the audit's evidence matrix.
TRACK_B_CLOSURE = "9d3b34ab-cf69-40a0-ad97-fdc37131ead9"  # "Track B Goal Closed"
VIEWER_PID_INCIDENT = "bc8b273c-db6b-41f9-8900-e26e9803c1b0"
WSL_WINDOWS_DB_POLICY = "7a898477-b0d3-44f7-9504-216eacb21076"
DEV_SMOKE_TEST_PROCEDURE = "d9f46925-53d2-45b5-8f03-e5428c998ae0"

RECOVERED_CALLS: list[dict] = []


def _add(source_exec_call_id, label, kwargs, expected_id=None):
    RECOVERED_CALLS.append(
        {
            "source_exec_call_id": source_exec_call_id,
            "label": label,
            "kwargs": kwargs,
            "expected_id": expected_id,
        }
    )


# --- call_5cFsCcSDfF8MfrWa8Z2W1CPN: 5 queries (first exploratory batch) ---------------------
_C1 = "call_5cFsCcSDfF8MfrWa8Z2W1CPN"
_add(
    _C1,
    "explore/daemon-linux-windows",
    dict(
        query_keywords="daemon Linux Windows validation",
        include_related=False,
        limit=8,
        mode="broad",
    ),
)
_add(
    _C1,
    "explore/persistent-daemon-windows-compat+topic",
    dict(
        query_keywords="persistent daemon Windows compatibility",
        include_related=False,
        limit=8,
        mode="broad",
        rerank_by_topic=True,
    ),
)
_add(
    _C1,
    "explore/avoid-live-db-dev-server+strict+topic",
    dict(
        query_keywords="how to ensure development server avoids live default database",
        include_related=False,
        limit=8,
        mode="strict",
        rerank_by_topic=True,
    ),
)
_add(
    _C1,
    "explore/viewer-pid-isolation",
    dict(
        query_keywords="viewer pid file test isolation",
        include_related=False,
        limit=8,
        mode="broad",
    ),
)
_add(
    _C1,
    "explore/negative-quantum-cooking+strict",
    dict(
        query_keywords="unrelated quantum entanglement cooking recipes",
        include_related=False,
        limit=8,
        mode="strict",
    ),
)

# --- call_9HrARuo2yo4wcMNyToC8zH9w: 6 cases, daemon-completion wording sweep ----------------
_C2 = "call_9HrARuo2yo4wcMNyToC8zH9w"
_DAEMON_EXACT_Q = "Track B Goal Closed daemon validated live both Linux WSL2 native Windows"
_DAEMON_OPWORD_Q = "new daemon based version works on Linux and Windows validation complete"
_add(
    _C2,
    "sweep/exact-completion",
    dict(
        query_keywords=_DAEMON_EXACT_Q,
        include_related=False,
        limit=5,
        mode="broad",
        rerank_by_topic=False,
    ),
)
_add(
    _C2,
    "sweep/exact-completion+topic",
    dict(
        query_keywords=_DAEMON_EXACT_Q,
        include_related=False,
        limit=5,
        mode="broad",
        rerank_by_topic=True,
    ),
)
_add(
    _C2,
    "sweep/operational-wording",
    dict(
        query_keywords=_DAEMON_OPWORD_Q,
        include_related=False,
        limit=5,
        mode="broad",
        rerank_by_topic=False,
    ),
)
_add(
    _C2,
    "sweep/operational-wording+strict+topic",
    dict(
        query_keywords=_DAEMON_OPWORD_Q,
        include_related=False,
        limit=5,
        mode="strict",
        rerank_by_topic=True,
    ),
)
_add(
    _C2,
    "sweep/boundary-question+strict+topic",
    dict(
        query_keywords="can Windows and WSL daemon processes share the same SALTMDB database file",
        include_related=False,
        limit=5,
        mode="strict",
        rerank_by_topic=True,
    ),
)
_add(
    _C2,
    "sweep/negative-sourdough+strict+topic",
    dict(
        query_keywords="How do I bake sourdough bread with a cast iron dutch oven",
        include_related=False,
        limit=5,
        mode="strict",
        rerank_by_topic=True,
    ),
)

# --- call_eGidjigAGcHv27dzdmaoKncf: 1 broad meta-query --------------------------------------
_add(
    "call_eGidjigAGcHv27dzdmaoKncf",
    "meta/precision-false-positives",
    dict(
        query_keywords="search precision false positives strict mode topic rerank evaluation",
        include_related=False,
        limit=8,
        mode="broad",
    ),
)

# --- call_CgVBRaGrfVgpgP1jgsXe3EvT: core 12-case x 4-mode evidence matrix (48 calls) --------
_C4 = "call_CgVBRaGrfVgpgP1jgsXe3EvT"
_MATRIX_CASES = [
    ("A1", "exact", TRACK_B_CLOSURE, _DAEMON_EXACT_Q),
    (
        "A2",
        "paraphrase",
        TRACK_B_CLOSURE,
        "the new daemon works on Linux and Windows and validation is complete",
    ),
    (
        "A3",
        "state",
        TRACK_B_CLOSURE,
        "is the backend daemon implementation still pending review or has it been completed and validated",
    ),
    (
        "B1",
        "exact incident",
        VIEWER_PID_INCIDENT,
        "viewer pid file test isolation leak corrupts live viewer_8080.pid",
    ),
    (
        "B2",
        "paraphrase",
        VIEWER_PID_INCIDENT,
        "tests accidentally modify the production viewer PID file",
    ),
    (
        "C1",
        "policy",
        WSL_WINDOWS_DB_POLICY,
        "can a Windows daemon and WSL daemon share a single SALTMDB database file",
    ),
    (
        "C2",
        "policy paraphrase",
        WSL_WINDOWS_DB_POLICY,
        "is cross filesystem database sharing supported between native Windows and WSL",
    ),
    (
        "D1",
        "procedure",
        DEV_SMOKE_TEST_PROCEDURE,
        "how should I smoke test python -m saltmdb safely during development",
    ),
    (
        "D2",
        "procedure paraphrase",
        DEV_SMOKE_TEST_PROCEDURE,
        "prevent my development server startup check from touching the real default database",
    ),
    ("E1", "negative", None, "How do I bake sourdough bread with a cast iron dutch oven"),
    ("E2", "negative", None, "What is the capital city of Mongolia"),
    (
        "E3",
        "false premise",
        None,
        "Which SALTMDB function calculates the nutritional value of recipes",
    ),
]
_MATRIX_MODES = [
    ("broad_default", dict(mode="broad", rerank_by_topic=False)),
    ("broad_topic", dict(mode="broad", rerank_by_topic=True)),
    ("strict_default", dict(mode="strict", rerank_by_topic=False)),
    ("strict_topic", dict(mode="strict", rerank_by_topic=True)),
]
for _case_id, _kind, _expected, _q in _MATRIX_CASES:
    for _cfg_name, _cfg in _MATRIX_MODES:
        _add(
            _C4,
            f"matrix/{_case_id}/{_cfg_name}",
            dict(query_keywords=_q, include_related=False, limit=5, **_cfg),
            expected_id=_expected,
        )

# --- call_EmPAQbg4X2ToWI5AkQNSMs0N: re-run of C1/C2/D1/D2 subset (16 calls) -----------------
_C5 = "call_EmPAQbg4X2ToWI5AkQNSMs0N"
_SUBSET_CASES = [
    (
        "C1",
        WSL_WINDOWS_DB_POLICY,
        "can a Windows daemon and WSL daemon share a single SALTMDB database file",
    ),
    (
        "C2",
        WSL_WINDOWS_DB_POLICY,
        "is cross filesystem database sharing supported between native Windows and WSL",
    ),
    (
        "D1",
        DEV_SMOKE_TEST_PROCEDURE,
        "how should I smoke test python -m saltmdb safely during development",
    ),
    (
        "D2",
        DEV_SMOKE_TEST_PROCEDURE,
        "prevent my development server startup check from touching the real default database",
    ),
]
_SUBSET_MODES = [
    ("B", dict(mode="broad", rerank_by_topic=False)),
    ("BT", dict(mode="broad", rerank_by_topic=True)),
    ("S", dict(mode="strict", rerank_by_topic=False)),
    ("ST", dict(mode="strict", rerank_by_topic=True)),
]
for _case_id, _expected, _q in _SUBSET_CASES:
    for _cfg_name, _cfg in _SUBSET_MODES:
        _add(
            _C5,
            f"resweep/{_case_id}/{_cfg_name}",
            dict(query_keywords=_q, include_related=False, limit=5, **_cfg),
            expected_id=_expected,
        )

# --- call_yZzv4IL6PvDTAdBxpFLMQooK: 8 variants (supersession/type-bias/related checks) ------
_C6 = "call_yZzv4IL6PvDTAdBxpFLMQooK"
_BLOAT_Q = "live database 425MB bloat root cause investigation"
_add(
    _C6,
    "variant/R1-bloat-no-flags",
    dict(
        query_keywords=_BLOAT_Q,
        limit=8,
        mode="broad",
        rerank_by_topic=False,
        prefer_durable_types=False,
        demote_superseded=False,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/R2-bloat-flags-on",
    dict(
        query_keywords=_BLOAT_Q,
        limit=8,
        mode="broad",
        rerank_by_topic=False,
        prefer_durable_types=True,
        demote_superseded=True,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/R3-bloat-history",
    dict(
        query_keywords=_BLOAT_Q,
        limit=8,
        mode="history",
        rerank_by_topic=False,
        prefer_durable_types=False,
        demote_superseded=False,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/R4-bloat-strict",
    dict(
        query_keywords=_BLOAT_Q,
        limit=8,
        mode="strict",
        rerank_by_topic=False,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/I1-daemon-related-true",
    dict(
        query_keywords=_DAEMON_EXACT_Q,
        limit=8,
        mode="broad",
        rerank_by_topic=False,
        include_related=True,
    ),
)
_add(
    _C6,
    "variant/I2-daemon-related-false",
    dict(
        query_keywords=_DAEMON_EXACT_Q,
        limit=8,
        mode="broad",
        rerank_by_topic=False,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/N1-negative-kimchi+strict",
    dict(
        query_keywords="how do I make kimchi at home",
        limit=8,
        mode="strict",
        rerank_by_topic=False,
        include_related=False,
    ),
)
_add(
    _C6,
    "variant/N2-negative-laplace+strict",
    dict(
        query_keywords="differential equations Laplace transform initial value problem",
        limit=8,
        mode="strict",
        rerank_by_topic=False,
        include_related=False,
    ),
)

# --- call_eciPBcbaubuzpXdSCC20TjXU: fetch_full entity lookups (not pipeline calls) ----------
FETCH_FULL_CALLS = [
    {"source_exec_call_id": "call_eciPBcbaubuzpXdSCC20TjXU", "entity_id": eid}
    for eid in (
        TRACK_B_CLOSURE,
        "59a4f77b-577e-4193-ad95-e020d34da2c9",
        "3a168aac-a203-4955-a1c7-dc1356d5e91d",
        "0d973822-ee91-474a-82a4-05c7b00b4d96",
    )
]

assert len(RECOVERED_CALLS) == 5 + 6 + 1 + 48 + 16 + 8, len(RECOVERED_CALLS)  # = 84
