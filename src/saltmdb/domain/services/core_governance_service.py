"""Core-memory governance: the single authority for is_core lifecycle, capacity, and rendering.

SALTMDB rework (see plans/core_memory_bootstrap_governance_detailed.md and the resolved-gaps
addendum plans/core_memory_bootstrap_governance_review.md). Redefines `is_core=True` as a scarce,
temporary bootstrap-delivery mechanism -- never a general "important knowledge" tier -- governed
by hard, independent limits:

    active injectable core count       <= CORE_MAX_ACTIVE
    each core full_content length      <= CORE_MAX_CONTENT_CHARS Unicode code points
    exact rendered bootstrap digest    <= CORE_MAX_RENDERED_CHARS Unicode code points

Every write path that can create, promote, enlarge, or review a core memory (store_memory,
commit_consolidation/bulk_commit_consolidation, review_core_memory) routes through this module --
constants, is_core parsing, lifecycle-field validation, capacity admission, and canonical
rendering live here exactly once so CLI/memory/consolidation/bootstrap code never re-implements
(and re-diverges on) the same rules.

Deliberately import-light at module scope: no `saltmdb.domain.services.*` imports at the top of
this file, even though `reconcile_detail_relations`/`review_core_memory` need relation_service and
memory_service.lifecycle at call time. Both of those already import (directly or transitively)
back into this module's natural callers (memory_service.write, relation_service), so a top-level
Every cross-service import below is therefore deferred (function-local), keeping this module safely
importable from anywhere.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from saltmdb.config import (
    CORE_MAX_ACTIVE,
    CORE_MAX_CONTENT_CHARS,
    CORE_MAX_RENDERED_CHARS,
    CORE_REASON_MIN_CHARS,
    CORE_REASON_MAX_CHARS,
    CORE_EXIT_MIN_CHARS,
    CORE_EXIT_MAX_CHARS,
    CORE_REVIEW_RATIONALE_MIN_CHARS,
    CORE_REVIEW_RATIONALE_MAX_CHARS,
    CORE_MAX_DETAIL_MEMORY_IDS,
    CORE_DEFAULT_REVIEW_DAYS,
    CORE_MAX_REVIEW_DAYS,
    CORE_BOOTSTRAP_ERROR_MAX_CHARS,
)
from saltmdb.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CoreGovernanceRejected(Exception):
    """Raised inside a write transaction when the AUTHORITATIVE in-transaction re-check (plan
    step 7) finds the write no longer admissible -- concurrent state changed since the
    side-effect-free pre-transaction check (step 5/6) ran. Caught just outside
    write_transaction_retrying and converted back into a caller-appropriate return value; never
    escapes a domain-service public function.

    `payload` is either a REJECTED dict (capacity violation, same shape check_capacity_admission
    returns) or a plain string (a lifecycle/validation error) -- callers that only return strings
    (commit_consolidation) format it via `str(exc)`; callers with a dict-capable contract
    (store_memory) return `payload` as-is when it is a dict.
    """

    def __init__(self, payload: dict | str):
        self.payload = payload
        message = (
            payload
            if isinstance(payload, str)
            else payload.get("message", "core capacity exceeded")
        )
        super().__init__(message)


# ---------------------------------------------------------------------------
# is_core strict parsing
# ---------------------------------------------------------------------------


def parse_is_core(value: Any) -> bool | None:
    """Strict tri-state parse: None (omitted) stays None, True/False pass through, anything else
    is rejected with an actionable error -- never silently coerced to False (resolved gap #6:
    the old `1 if value in (True, 1, "true", "1", "True") else 0` shape treated an unrecognized
    value like "yes" as an implicit False)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"is_core must be a boolean (true/false) or omitted -- got {value!r}. Ambiguous values "
        'such as "yes" or integers are rejected outright, never silently coerced to false.'
    )


# ---------------------------------------------------------------------------
# Lifecycle free-text validation (redact + CRLF/CR->LF normalize before length checks)
# ---------------------------------------------------------------------------


def _normalize_lifecycle_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return redact_secrets(normalized)


def _validate_bounded_text(value: str | None, *, field: str, min_chars: int, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field} is required for a core memory ({min_chars}-{max_chars} characters)."
        )
    normalized = _normalize_lifecycle_text(value.strip())
    if not (min_chars <= len(normalized) <= max_chars):
        raise ValueError(
            f"{field} must be {min_chars}-{max_chars} characters after redaction/normalization "
            f"(got {len(normalized)})."
        )
    return normalized


def validate_core_reason(value: str | None) -> str:
    return _validate_bounded_text(
        value, field="core_reason", min_chars=CORE_REASON_MIN_CHARS, max_chars=CORE_REASON_MAX_CHARS
    )


def validate_core_exit_condition(value: str | None) -> str:
    return _validate_bounded_text(
        value,
        field="core_exit_condition",
        min_chars=CORE_EXIT_MIN_CHARS,
        max_chars=CORE_EXIT_MAX_CHARS,
    )


def validate_core_review_rationale(value: str | None) -> str:
    return _validate_bounded_text(
        value,
        field="core_review_rationale",
        min_chars=CORE_REVIEW_RATIONALE_MIN_CHARS,
        max_chars=CORE_REVIEW_RATIONALE_MAX_CHARS,
    )


def validate_core_content_length(content: str) -> None:
    if len(content or "") > CORE_MAX_CONTENT_CHARS:
        raise ValueError(
            f"Core memory content exceeds {CORE_MAX_CONTENT_CHARS} Unicode characters "
            f"(got {len(content)}). Move rationale/chronology/evidence/examples into a linked "
            "normal memory (see detail_memory_ids) and keep the core itself directly actionable."
        )


# ---------------------------------------------------------------------------
# core_review_after
# ---------------------------------------------------------------------------


def _parse_iso_utc(value: str) -> datetime:
    """Parses an ISO-8601 timestamp into a canonical UTC-aware datetime (resolved review finding
    #7). A naive timestamp (no offset) is treated as UTC -- a deliberate, documented convention,
    not a silent guess: every other SALTMDB timestamp field (created_at, updated_at, ...) is
    generated via `datetime.now(UTC).isoformat()`, so an omitted offset unambiguously means UTC
    here too. An aware timestamp carrying a non-UTC offset is converted, never left in its
    original offset, so every value that reaches `_parse_iso_utc` collapses to one representation
    regardless of what the caller supplied."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_core_review_after(
    value: str | None, *, default_days: int | None = None, now: datetime | None = None
) -> str:
    """Returns a canonical timezone-aware UTC ISO timestamp string, strictly in the future
    relative to `now` and never more than CORE_MAX_REVIEW_DAYS ahead of it -- `value=None` fills
    in `default_days` from `now` (required when value is None). Both bounds apply identically
    whether the value came from the caller or from the default, and this single function is now
    the only place that enforces them -- used uniformly by create, promote, consolidate, and
    review_core_memory(outcome='retain') (resolved review finding #7: previously only `retain`
    checked the future-only bound, so a new/promoted/consolidated core could be born already
    overdue). Equality with `now` counts as due (`is_overdue`'s own boundary rule), so it is not a
    valid next-review timestamp either."""
    now = now or datetime.now(UTC)
    if value is None:
        if default_days is None:
            raise ValueError("core_review_after is required.")
        dt = now + timedelta(days=default_days)
    else:
        if not isinstance(value, str):
            raise ValueError("core_review_after must be an ISO-8601 UTC timestamp string.")
        try:
            dt = _parse_iso_utc(value)
        except ValueError as exc:
            raise ValueError(
                f"core_review_after is not a valid ISO-8601 timestamp: {value!r}"
            ) from exc
    if dt <= now:
        raise ValueError(
            f"core_review_after must be strictly in the future (got {dt.isoformat()}, "
            f"now={now.isoformat()})."
        )
    max_dt = now + timedelta(days=CORE_MAX_REVIEW_DAYS)
    if dt > max_dt:
        raise ValueError(
            f"core_review_after may not be more than {CORE_MAX_REVIEW_DAYS} days in the future "
            f"(got {dt.isoformat()})."
        )
    return dt.isoformat()


def _validate_preserved_review_after(value: str) -> str:
    """Validates a PRESERVED (not newly supplied) `core_review_after`: it must still parse as a
    canonical ISO-8601 timestamp, but is exempt from `parse_core_review_after`'s future-only/
    max-days admission bounds -- those are admission rules for setting a NEW review date, not a
    reason to block an otherwise-valid, non-expanding repair of an already-overdue core (resolved
    follow-up review finding #3). A malformed/unparseable value is never silently carried
    forward -- it must be repaired (supply a fresh `core_review_after`) or the core demoted/
    archived."""
    try:
        return _parse_iso_utc(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Existing core_review_after is malformed and cannot be preserved: {value!r} -- "
            "supply a valid core_review_after to repair it, or demote/archive this core."
        ) from exc


def is_overdue(core_review_after: str | None, *, now: datetime | None = None) -> bool:
    """Equality-at-due-boundary counts as due (plan verification-plan bullet)."""
    if not core_review_after:
        return True  # missing/malformed -- treated as due/invalid, surfaced separately as an
        # invariant violation by find_invariant_violations, not silently accepted here.
    now = now or datetime.now(UTC)
    try:
        dt = _parse_iso_utc(core_review_after)
    except ValueError:
        return True
    return dt <= now


# ---------------------------------------------------------------------------
# Detail memory declarations
# ---------------------------------------------------------------------------


def validate_detail_memory_ids(conn, detail_memory_ids: list | None, *, content: str) -> list[str]:
    """`detail_memory_ids=None` is the caller's job to interpret as "preserve" before calling
    this -- by the time this runs it is always an explicit list (possibly empty)."""
    if not isinstance(detail_memory_ids, list):
        raise ValueError("detail_memory_ids must be a list of full UUID strings.")
    if len(detail_memory_ids) > CORE_MAX_DETAIL_MEMORY_IDS:
        raise ValueError(
            f"A core memory may declare at most {CORE_MAX_DETAIL_MEMORY_IDS} detail_memory_ids "
            f"(got {len(detail_memory_ids)})."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in detail_memory_ids:
        if not isinstance(raw_id, str) or not _UUID_RE.match(raw_id.strip()):
            raise ValueError(
                f"detail_memory_ids must be full UUIDs, not prefixes or other references (got "
                f"{raw_id!r})."
            )
        uid = raw_id.strip().lower()
        if uid in seen:
            continue
        seen.add(uid)
        row = conn.execute(
            "SELECT title, scope, is_core FROM entities WHERE id = ?", (uid,)
        ).fetchone()
        if not row:
            raise ValueError(f"detail_memory_ids references a memory that does not exist: {uid}")
        title, scope, is_core_val = row
        if scope != "shared":
            raise ValueError(
                f"detail memory {uid} must be shared -- private core details are prohibited."
            )
        if is_core_val:
            raise ValueError(
                f"detail memory {uid} is itself core -- a core cannot declare another core as a "
                "detail memory."
            )
        if title not in content or uid not in content:
            raise ValueError(
                f"Core content must mention both the canonical title and the full UUID of "
                f"declared detail memory {uid} (title: {title!r}) -- a core must stay directly "
                "actionable even if a lazy or weaker agent never follows the link."
            )
        normalized.append(uid)
    return normalized


def reconcile_detail_relations(
    conn,
    *,
    core_id: str,
    owner_id: str | None,
    new_detail_ids: list[str],
    previous_detail_ids: list[str],
) -> None:
    """Atomically maintains `detail --elaborates_on--> core` edges to match the JSON declaration.
    Must run inside the caller's own open write transaction (memory_service.write's
    _store_raw_entity, or relation_service.commit_consolidation's _do_commit)."""
    from saltmdb.domain.services.relation_service import store_relation, invalidate_relation

    for detail_id in new_detail_ids or []:
        res = store_relation(
            source_id=detail_id,
            target_id=core_id,
            predicate="elaborates_on",
            owner_id=owner_id,
            db_connection=conn,
            _in_transaction=True,
            _allow_core_elaborates_on=True,
        )
        if isinstance(res, str) and res.startswith("Error"):
            raise RuntimeError(f"Failed to create detail relation for {detail_id}: {res}")

    removed = set(previous_detail_ids or []) - set(new_detail_ids or [])
    for detail_id in removed:
        # Best-effort: the edge may already be absent (e.g. detail archived independently);
        # invalidate_relation's "not found"/"already invalidated" no-ops are not fatal here.
        invalidate_relation(
            source_id=detail_id,
            target_id=core_id,
            predicate="elaborates_on",
            db_connection=conn,
            _in_transaction=True,
        )


# ---------------------------------------------------------------------------
# Canonical rendering
# ---------------------------------------------------------------------------


def _escape_yaml_line(value: str) -> str:
    # Backslashes must be escaped FIRST, before the quote/newline escapes below introduce new
    # ones -- otherwise a literal `\n`/`\t`/`\u...` sequence already present in caller text would
    # be re-interpreted by a real YAML double-quoted-scalar parser reading this rendered digest
    # (secondary review finding: backslashes were previously left unescaped).
    escaped = (value or "").replace("\\", "\\\\")
    return escaped.replace('"', '\\"').replace("\n", " ")


def render_core_entry(row: dict, *, due: bool) -> str:
    """row: {id, title, memory_type, core_reason, core_exit_condition, core_review_after,
    full_content}."""
    title = _escape_yaml_line(row["title"])
    reason = _escape_yaml_line(row.get("core_reason") or "")
    exit_condition = _escape_yaml_line(row.get("core_exit_condition") or "")
    content = (row.get("full_content") or "").replace("</memory>", "&lt;/memory&gt;")
    memory_type = row.get("memory_type") or "fact"
    due_str = "true" if due else "false"
    return "\n".join(
        [
            f'<memory id="{row["id"]}" type="{memory_type}" is_core="true" review_due="{due_str}">',
            "---",
            f'title: "{title}"',
            f"type: {memory_type}",
            "is_core: true",
            f'core_reason: "{reason}"',
            f'core_exit_condition: "{exit_condition}"',
            f"core_review_after: {row.get('core_review_after')}",
            f"review_due: {due_str}",
            "---",
            "",
            content,
            "</memory>",
        ]
    )


def _render_digest(rows: list[dict], *, pessimistic: bool) -> str:
    """pessimistic=True always renders the longer ("false") due-flag representation, giving a
    safe upper bound the real digest (whichever the actual due states turn out to be) can never
    exceed -- becoming overdue only ever shortens a rendered entry ("true" < "false"). The
    <core-rules> section itself is omitted entirely when there are no active cores to inject."""
    if not rows:
        return "<saltmdb-digest>\n\n</saltmdb-digest>"
    lines = ["<saltmdb-digest>", "", "<core-rules>"]
    for row in rows:
        due = False if pessimistic else is_overdue(row.get("core_review_after"))
        lines.append("")
        lines.append(render_core_entry(row, due=due))
    lines.append("")
    lines.append("</core-rules>")
    lines.append("")
    lines.append("</saltmdb-digest>")
    return "\n".join(lines)


def render_bootstrap_digest(rows: list[dict]) -> str:
    return _render_digest(rows, pessimistic=False)


def build_inventory(rows: list[dict]) -> list[dict]:
    """Balanced rejection/error inventory: ID, title, type, owner, review timestamp, due state,
    rendered contribution -- never full content (plan rule 18)."""
    inventory = []
    for row in rows:
        due = is_overdue(row.get("core_review_after"))
        try:
            rendered_chars = len(render_core_entry(row, due=due))
        except (
            Exception
        ):  # pragma: no cover -- defensive, rendering a malformed row must not crash reporting
            rendered_chars = -1
        inventory.append(
            {
                "id": row["id"],
                "title": row.get("title"),
                "memory_type": row.get("memory_type") or "fact",
                "owner_id": row.get("owner_id"),
                "core_review_after": row.get("core_review_after"),
                "review_due": due,
                "rendered_chars": rendered_chars,
            }
        )
    return inventory


_BOOTSTRAP_ERROR_RESERVE_CHARS = 300  # headroom, on top of the exact footer length, for the two
# "omitted_*_count=N" lines -- generous enough that no plausible count ever overflows it.


def render_bootstrap_error(rows: list[dict], violations: list[str]) -> str:
    """Bounded, fail-closed error report (resolved review finding #6): the returned string never
    exceeds CORE_BOOTSTRAP_ERROR_MAX_CHARS -- comfortably below the CORE_MAX_RENDERED_CHARS
    envelope this feature exists to protect -- no matter how many corrupt rows or violations
    exist. Both the violations list and the compact inventory are appended only while they still
    fit the budget; anything that would overflow it is summarized by an `omitted_*_count=N` line
    instead of being silently dropped or left unbounded. Every user-controlled value (violation
    text, titles) is YAML-line-escaped the same way the real digest escapes them."""
    inventory = build_inventory(rows)
    header = [
        "<saltmdb-digest>",
        "",
        "<core-bootstrap-error>",
        "The active core-memory set fails one or more invariants and cannot be safely injected.",
        "No core content was omitted or truncated to make a partial set fit -- this is a full-stop failure.",
        "",
        "Violations:",
    ]
    footer = [
        "",
        "Rebalance autonomously: demote/archive/shorten/consolidate active core memories via "
        "review_core_memory/store_memory/commit_consolidation until every invariant above holds, "
        "then retry -- capacity management never requires a human decision. Do not guess a "
        "partial working set; fix the violations and let the next bootstrap call re-validate the "
        "full set.",
        "",
        "</core-bootstrap-error>",
        "",
        "</saltmdb-digest>",
    ]
    footer_text = "\n".join(footer)
    budget = CORE_BOOTSTRAP_ERROR_MAX_CHARS - len(footer_text) - _BOOTSTRAP_ERROR_RESERVE_CHARS

    body: list[str] = []
    used = len("\n".join(header))

    omitted_violations = 0
    for i, v in enumerate(violations):
        line = f"  - {_escape_yaml_line(str(v))}"
        if used + len(line) + 1 > budget:
            omitted_violations = len(violations) - i
            break
        body.append(line)
        used += len(line) + 1
    body.append(f"omitted_violation_count={omitted_violations}")
    used += len(body[-1]) + 1

    body.append("")
    used += 1
    body.append(f"Active core count: {len(rows)} (limit: {CORE_MAX_ACTIVE})")
    used += len(body[-1]) + 1
    body.append("Compact inventory (no content):")
    used += len(body[-1]) + 1

    omitted_cores = 0
    for i, item in enumerate(inventory):
        title = _escape_yaml_line(str(item["title"]))
        line = (
            f"  - id={item['id']} title={title!r} type={item['memory_type']} "
            f"owner={item['owner_id']} review_after={item['core_review_after']} "
            f"due={item['review_due']} rendered_chars={item['rendered_chars']}"
        )
        if used + len(line) + 1 > budget:
            omitted_cores = len(inventory) - i
            break
        body.append(line)
        used += len(line) + 1
    body.append(f"omitted_core_count={omitted_cores}")

    result = "\n".join(header + body + footer)
    if len(result) > CORE_BOOTSTRAP_ERROR_MAX_CHARS:
        # Defensive fallback (required by review finding #6): must never trigger given the budget
        # accounting above, but guarantees the hard cap regardless of any estimate drift -- the
        # closing tags/instructions in `footer` are always preserved verbatim, only the body is
        # ever hard-truncated.
        allowed_body = CORE_BOOTSTRAP_ERROR_MAX_CHARS - len(footer_text) - 1
        result = result[: max(allowed_body, 0)].rstrip() + "\n" + footer_text
    return result


# ---------------------------------------------------------------------------
# Active-core loading, ordering, invariant checking, bootstrap response
# ---------------------------------------------------------------------------


def load_active_cores(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, title, memory_type, core_reason, core_exit_condition, core_review_after,
               full_content, owner_id, created_at, scope, core_detail_memory_ids
        FROM entities
        WHERE is_core = 1 AND status != 'archived'
        """
    ).fetchall()
    dicts = [
        {
            "id": r[0],
            "title": r[1],
            "memory_type": r[2] or "fact",
            "core_reason": r[3],
            "core_exit_condition": r[4],
            "core_review_after": r[5],
            "full_content": r[6],
            "owner_id": r[7],
            "created_at": r[8],
            "scope": r[9],
            # Raw JSON string as persisted (or None) -- kept unparsed here, same shape the DB
            # column itself has, matching resolve_store_core_state's own `json.loads(row[4]) if
            # row[4] else []` convention. find_invariant_violations (resolved review finding #5)
            # is the one place this list gets parsed+validated during bootstrap.
            "core_detail_memory_ids": r[10],
        }
        for r in rows
    ]

    def _sort_key(d):
        due = is_overdue(d["core_review_after"])
        # overdue first, then earliest upcoming review, then creation time, then id tie-break.
        return (0 if due else 1, d["core_review_after"] or "", d["created_at"] or "", d["id"])

    return sorted(dicts, key=_sort_key)


def _detail_id_violation(conn, row: dict) -> str | None:
    """Read-only check of one row's declared `core_detail_memory_ids` -- returns a violation
    string, or None if there's nothing to check (no `conn`, or the row declares no details)."""
    raw_detail_ids = row.get("core_detail_memory_ids")
    if conn is None or not raw_detail_ids:
        return None
    try:
        parsed_detail_ids = json.loads(raw_detail_ids)
    except (TypeError, ValueError):
        return f"{row['id']}: core_detail_memory_ids is not valid JSON"
    try:
        validate_detail_memory_ids(conn, parsed_detail_ids, content=row.get("full_content") or "")
    except ValueError as e:
        return f"{row['id']}: core_detail_memory_ids invalid -- {e}"
    return None


def find_invariant_violations(rows: list[dict], conn=None) -> list[str]:
    """`conn` is needed only to validate each row's declared `core_detail_memory_ids` (JSON
    shape, per-core cap, full-UUID format, referenced-entity existence, shared+non-core detail
    state, and required title/UUID mentions) against live entity state -- resolved review finding
    #5: bootstrap previously never validated this declaration at all. When `conn` is omitted
    (default None) or a row has no `core_detail_memory_ids` key, that row's detail-declaration
    check is simply skipped -- safe for unit tests constructing synthetic rows without a real
    database; the sole production caller (render_bootstrap_response) always passes a real
    connection, so this can never silently no-op there. This check is read-only: it must never
    mutate or reconcile relations during bootstrap, and archived detail memories remain valid
    (validate_detail_memory_ids's exact-ID lookup is intentionally status-agnostic)."""
    violations: list[str] = []
    if len(rows) > CORE_MAX_ACTIVE:
        violations.append(
            f"active core count {len(rows)} exceeds CORE_MAX_ACTIVE={CORE_MAX_ACTIVE}"
        )
    for row in rows:
        # Routed through the same canonical bounded-text validators store/consolidation use
        # (secondary review finding), not a raw length check -- so a legacy row whose reason/exit
        # text would fail today's redaction/control-character/CRLF handling is caught the same
        # way it would be caught on a fresh write, not differently at bootstrap-read time.
        try:
            validate_core_reason(row.get("core_reason"))
        except ValueError:
            violations.append(f"{row['id']}: core_reason missing or out of bounds")
        try:
            validate_core_exit_condition(row.get("core_exit_condition"))
        except ValueError:
            violations.append(f"{row['id']}: core_exit_condition missing or out of bounds")
        review_after = row.get("core_review_after")
        if not review_after:
            violations.append(f"{row['id']}: core_review_after missing")
        else:
            try:
                _parse_iso_utc(review_after)
            except ValueError:
                violations.append(f"{row['id']}: core_review_after malformed")
        if len(row.get("full_content") or "") > CORE_MAX_CONTENT_CHARS:
            violations.append(
                f"{row['id']}: full_content exceeds {CORE_MAX_CONTENT_CHARS} characters"
            )
        if row.get("scope") != "shared":
            violations.append(
                f"{row['id']}: scope must be shared -- private core memories are prohibited"
            )
        detail_violation = _detail_id_violation(conn, row)
        if detail_violation:
            violations.append(detail_violation)
    rendered = render_bootstrap_digest(rows)
    if len(rendered) > CORE_MAX_RENDERED_CHARS:
        violations.append(
            f"rendered digest {len(rendered)} exceeds CORE_MAX_RENDERED_CHARS={CORE_MAX_RENDERED_CHARS}"
        )
    return violations


def render_bootstrap_response(conn) -> str:
    """Fails closed: any malformed active-core invariant returns a bounded
    <core-bootstrap-error> report instead of an arbitrary subset, truncated digest, or oversized
    digest (plan rules 49-51). An overdue-but-otherwise-valid core is NOT an invariant failure."""
    rows = load_active_cores(conn)
    violations = find_invariant_violations(rows, conn)
    if violations:
        logger.warning("Core bootstrap invariant violation(s): %s", violations)
        return render_bootstrap_error(rows, violations)
    return render_bootstrap_digest(rows)


# ---------------------------------------------------------------------------
# Capacity admission
# ---------------------------------------------------------------------------


def check_capacity_admission(
    conn, *, exclude_ids: list[str] | None, new_entry: dict | None
) -> dict | None:
    """Side-effect-free. Returns None if admitted, or a REJECTED payload dict otherwise.
    `exclude_ids`: active cores removed from the set as part of this write (the update target, or
    consolidation parents about to be archived). `new_entry`: the proposed core row replacing/
    joining the set, or None if the final state has no new/changed core row."""
    exclude_set = set(exclude_ids or [])
    current = load_active_cores(conn)
    prospective = [r for r in current if r["id"] not in exclude_set]
    if new_entry is not None:
        prospective.append(new_entry)

    current_rendered_len = len(_render_digest(current, pessimistic=True))
    new_content_len = len(new_entry.get("full_content") or "") if new_entry is not None else 0
    rendered_len = len(_render_digest(prospective, pessimistic=True))

    violations = []
    if len(prospective) > CORE_MAX_ACTIVE:
        violations.append("count")
    if new_entry is not None and new_content_len > CORE_MAX_CONTENT_CHARS:
        violations.append("content_length")
    if rendered_len > CORE_MAX_RENDERED_CHARS:
        violations.append("rendered_size")

    if not violations:
        return None

    # required_reduction: the exact amount by which each violated dimension must shrink for this
    # write to admit -- secondary review finding, agents were previously left to guess.
    required_reduction: dict[str, int] = {}
    if "count" in violations:
        required_reduction["count"] = len(prospective) - CORE_MAX_ACTIVE
    if "content_length" in violations:
        required_reduction["content_chars"] = new_content_len - CORE_MAX_CONTENT_CHARS
    if "rendered_size" in violations:
        required_reduction["rendered_chars"] = rendered_len - CORE_MAX_RENDERED_CHARS

    return {
        "status": "REJECTED",
        "error_code": "CORE_CAPACITY_EXCEEDED",
        "violated_dimensions": violations,
        "limits": {
            "max_active": CORE_MAX_ACTIVE,
            "max_content_chars": CORE_MAX_CONTENT_CHARS,
            "max_rendered_chars": CORE_MAX_RENDERED_CHARS,
        },
        "current_totals": {"active_count": len(current), "rendered_chars": current_rendered_len},
        "proposed_totals": {
            "active_count": len(prospective),
            "rendered_chars": rendered_len,
        },
        "required_reduction": required_reduction,
        "inventory": build_inventory(current),
        "message": (
            "Core capacity would be exceeded by this write -- no memory, relation, or other state "
            "was created. Demote, archive, shorten, or consolidate existing core memories to make "
            "room (see the inventory below), then retry."
        ),
    }


def has_overdue_core(conn) -> str | None:
    """Every active core counts, including the one being updated in place -- a core being
    updated/enlarged in place must never be self-excluded: its own overdue state has to block
    its own enlargement (resolved review finding #1). A core-producing consolidation's own
    resolved parents are likewise never excluded, even the ones this same transaction is about
    to archive (resolved follow-up review finding #2: replacing an overdue parent with a fresh
    core silently reset the lifecycle without recording review provenance through
    review_core_memory)."""
    for row in load_active_cores(conn):
        if is_overdue(row.get("core_review_after")):
            return row["id"]
    return None


def enforce_overdue_boundary(
    conn,
    *,
    entity_id: str | None,
    effective_is_core: bool,
    is_new_core: bool,
    review_after_changed: bool,
    prospective_entry: dict | None = None,
) -> None:
    """Raises ValueError if this write is blocked by an overdue-core boundary. Demote/archive/
    review/non-expanding edits and any non-core write are always allowed regardless of overdue
    state (plan rules 26-29).

    `entity_id` is the update target for the in-place-edit branch's own lookup below -- it is
    NEVER used to exclude the target (or, for consolidation, any resolved parent) from the
    overdue scan itself; `has_overdue_core` always considers every active core (resolved review
    finding #1, resolved follow-up review finding #2).

    `prospective_entry` is the complete candidate row (id/title/memory_type/core_reason/
    core_exit_condition/core_review_after/full_content) this write would leave in place. The
    in-place-update branch compares its full canonical PESSIMISTIC rendered contribution
    (`render_core_entry(..., due=False)`, the same longer-form upper bound
    check_capacity_admission's rendered-size gate uses) against the existing row's, never
    `full_content` length alone (resolved follow-up review finding #1: a title-only, or
    core_reason/core_exit_condition-only, enlargement previously bypassed this boundary entirely
    because only `len(content)` was compared). Only read on that branch -- is_new_core and
    review_after_changed always reject first when they apply, so `prospective_entry` is optional
    and unused for a new-core write."""
    if not effective_is_core:
        return
    overdue_id = has_overdue_core(conn)
    if not overdue_id:
        return
    if is_new_core:
        raise ValueError(
            f"Cannot create or promote a new core memory while core {overdue_id} is overdue for "
            "review -- resolve the overdue review first (review_core_memory), or demote/archive/"
            "shorten an existing core to make room."
        )
    if review_after_changed:
        raise ValueError(
            f"Cannot change core_review_after directly while core {overdue_id} is overdue -- use "
            "review_core_memory(outcome='retain') to extend a review date."
        )
    existing_row = conn.execute(
        "SELECT id, title, memory_type, core_reason, core_exit_condition, core_review_after, "
        "full_content FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    existing_rendered_len = 0
    if existing_row is not None:
        existing_entry = {
            "id": existing_row[0],
            "title": existing_row[1],
            "memory_type": existing_row[2] or "fact",
            "core_reason": existing_row[3],
            "core_exit_condition": existing_row[4],
            "core_review_after": existing_row[5],
            "full_content": existing_row[6],
        }
        existing_rendered_len = len(render_core_entry(existing_entry, due=False))
    prospective_rendered_len = (
        len(render_core_entry(prospective_entry, due=False)) if prospective_entry else 0
    )
    if prospective_rendered_len > existing_rendered_len:
        raise ValueError(
            f"Cannot enlarge a core memory's rendered bootstrap contribution (title, core_reason, "
            f"core_exit_condition, or content) while core {overdue_id} is overdue for review -- "
            "shrink the rendered contribution, demote, or archive an existing core, or resolve "
            "the overdue review first."
        )


# ---------------------------------------------------------------------------
# store_memory core-state resolution
# ---------------------------------------------------------------------------


def resolve_effective_memory_type(
    conn, *, entity_id: str | None, requested_memory_type: str | None
) -> str:
    """Resolves the `memory_type` this write will actually persist, mirroring the SQL write's own
    `COALESCE(?, entities.memory_type)` update semantics exactly (resolved third-round review
    finding: both prospective-row builders sized an update with `memory_type or "fact"`, silently
    ignoring the existing row's persisted type whenever the caller omitted `memory_type` -- correct
    for a fresh insert, but wrong for an update, which preserves the existing type). A brand-new
    entity (no existing row) with an omitted type resolves to 'fact'; an update to an existing row
    with an omitted type resolves to that row's own `memory_type` (falling back to 'fact' only for
    a legacy NULL); a supplied valid type always wins outright. Callers must use this identical
    value everywhere a row's memory_type feeds governance sizing (overdue rendered-delta
    comparison, capacity admission) or the eventual persisted `prospective_entry`, so preview,
    in-transaction admission, and the committed row can never diverge."""
    if requested_memory_type is not None:
        return requested_memory_type
    if entity_id:
        row = conn.execute("SELECT memory_type FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is not None:
            return row[0] or "fact"
    return "fact"


def _non_core_state(*, previous_detail_ids: list[str]) -> dict:
    return {
        "is_core": False,
        "is_new_core": False,
        "review_after_changed": False,
        "core_reason": None,
        "core_exit_condition": None,
        "core_review_after": None,
        "core_detail_memory_ids": [],
        "previous_detail_memory_ids": previous_detail_ids,
    }


def resolve_store_core_state(  # noqa: C901, PLR0912
    conn,
    *,
    entity_id: str | None,
    is_core_requested: bool | None,
    content: str,
    scope: str,
    core_reason: str | None,
    core_exit_condition: str | None,
    core_review_after: str | None,
    detail_memory_ids: list | None,
) -> dict:
    """Side-effect-free. Raises ValueError on a validation failure (callers convert to their own
    "Error: ..." convention). Returns the effective final core state to persist, including
    `is_new_core`/`review_after_changed` for enforce_overdue_boundary and
    `previous_detail_memory_ids` for reconcile_detail_relations."""
    existing = None
    if entity_id:
        row = conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, core_review_after, "
            "core_detail_memory_ids FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row:
            existing = {
                "is_core": bool(row[0]),
                "core_reason": row[1],
                "core_exit_condition": row[2],
                "core_review_after": row[3],
                "core_detail_memory_ids": json.loads(row[4]) if row[4] else [],
            }
    previous_detail_ids = existing["core_detail_memory_ids"] if existing else []

    effective_is_core = (
        existing["is_core"] if (is_core_requested is None and existing) else bool(is_core_requested)
    )

    any_core_field_supplied = (
        core_reason is not None
        or core_exit_condition is not None
        or core_review_after is not None
        or detail_memory_ids is not None
    )
    if not effective_is_core:
        if any_core_field_supplied:
            raise ValueError(
                "core_reason/core_exit_condition/core_review_after/detail_memory_ids were "
                "supplied but the effective memory is not core -- these fields are rejected, "
                "never silently ignored. Pass is_core=True to create/keep a core memory, or omit "
                "them entirely."
            )
        return _non_core_state(previous_detail_ids=previous_detail_ids)

    if scope != "shared":
        raise ValueError(
            "A core memory must have scope='shared' -- private core memories are prohibited."
        )

    is_new_core = not (existing and existing["is_core"])

    # Resolved follow-up review finding #3: omission means "preserve the value," never "skip
    # validation of the effective value" -- the same class of bug already fixed for
    # detail_memory_ids below. A preserved reason/exit_condition is re-run through the identical
    # canonical validator a freshly-supplied one gets, so a legacy/corrupted row can no longer be
    # carried forward malformed just because this write happened to omit the field.
    if core_reason is not None:
        reason = validate_core_reason(core_reason)
    elif existing and existing.get("core_reason"):
        reason = validate_core_reason(existing["core_reason"])
    else:
        raise ValueError("core_reason is required to create or promote a core memory.")

    if core_exit_condition is not None:
        exit_condition = validate_core_exit_condition(core_exit_condition)
    elif existing and existing.get("core_exit_condition"):
        exit_condition = validate_core_exit_condition(existing["core_exit_condition"])
    else:
        raise ValueError("core_exit_condition is required to create or promote a core memory.")

    review_after_changed = False
    if core_review_after is not None:
        review_after = parse_core_review_after(core_review_after)
        if existing and existing.get("core_review_after") not in (None, review_after):
            review_after_changed = True
    elif existing and existing.get("core_review_after"):
        # Preserved, not newly supplied: structurally revalidated but exempt from the
        # future-only bound (an overdue-but-parseable timestamp may still be preserved during a
        # non-expanding edit -- see _validate_preserved_review_after).
        review_after = _validate_preserved_review_after(existing["core_review_after"])
    else:
        review_after = parse_core_review_after(None, default_days=CORE_DEFAULT_REVIEW_DAYS)

    validate_core_content_length(content)

    # Resolved review finding #4: `detail_memory_ids=None` means "preserve the existing
    # declaration," never "skip validation." The effective list -- whether just-supplied or
    # preserved verbatim from `previous_detail_ids` -- is ALWAYS revalidated against the
    # PROSPECTIVE content below, so an update that rewrites content and drops a declared detail's
    # title/UUID mention (while omitting detail_memory_ids) can no longer persist a stale,
    # now-invalid declaration. Called from both the pre-transaction advisory preview and this
    # same function's authoritative in-transaction re-run, so both checkpoints share one path.
    ids_to_validate = previous_detail_ids if detail_memory_ids is None else detail_memory_ids
    effective_detail_ids = validate_detail_memory_ids(conn, ids_to_validate, content=content)

    return {
        "is_core": True,
        "is_new_core": is_new_core,
        "review_after_changed": review_after_changed,
        "core_reason": reason,
        "core_exit_condition": exit_condition,
        "core_review_after": review_after,
        "core_detail_memory_ids": effective_detail_ids,
        "previous_detail_memory_ids": previous_detail_ids,
    }


# ---------------------------------------------------------------------------
# Consolidation core-state resolution
# ---------------------------------------------------------------------------


def resolve_consolidation_core_state(
    conn,
    *,
    resolved_parents: list[str],
    is_core_requested: bool | None,
    content: str,
    scope: str,
    core_reason: str | None,
    core_exit_condition: str | None,
    core_review_after: str | None,
    detail_memory_ids: list | None,
) -> dict:
    """Consolidation never inherits core status implicitly (plan rules 52-53): if any resolved
    parent is currently an ACTIVE core (is_core=1, status != 'archived') and is_core is omitted,
    this raises. Must be called fresh inside commit_consolidation's in-transaction TOCTOU
    revalidation, not only pre-transaction, since parent core/status can change between preflight
    and commit (resolved gap #2)."""
    if resolved_parents:
        placeholders = ",".join("?" for _ in resolved_parents)
        rows = conn.execute(
            f"SELECT id, is_core, status FROM entities WHERE id IN ({placeholders})",
            resolved_parents,
        ).fetchall()
    else:
        rows = []
    any_active_core_parent = any(bool(r[1]) and r[2] != "archived" for r in rows)

    if is_core_requested is None:
        if any_active_core_parent:
            raise ValueError(
                "One or more parents is an active core memory and is_core was omitted -- "
                "consolidation must never silently inherit or drop core status. Pass explicit "
                "is_core=True (with core_reason/core_exit_condition/core_review_after) to keep "
                "the result core, or is_core=False to let it become an ordinary memory."
            )
        return _non_core_state(previous_detail_ids=[])

    if is_core_requested is False:
        return _non_core_state(previous_detail_ids=[])

    if scope != "shared":
        raise ValueError(
            "A core memory must have scope='shared' -- private core memories are prohibited."
        )
    reason = validate_core_reason(core_reason)
    exit_condition = validate_core_exit_condition(core_exit_condition)
    review_after = parse_core_review_after(core_review_after, default_days=CORE_DEFAULT_REVIEW_DAYS)
    validate_core_content_length(content)
    if detail_memory_ids:
        effective_detail_ids = validate_detail_memory_ids(conn, detail_memory_ids, content=content)
    else:
        effective_detail_ids = []

    return {
        "is_core": True,
        "is_new_core": True,
        "review_after_changed": False,
        "core_reason": reason,
        "core_exit_condition": exit_condition,
        "core_review_after": review_after,
        "core_detail_memory_ids": effective_detail_ids,
        "previous_detail_memory_ids": [],
    }


# ---------------------------------------------------------------------------
# review_core_memory
# ---------------------------------------------------------------------------


def review_core_memory(  # noqa: C901
    conn,
    *,
    entity_id: str,
    outcome: str,
    review_rationale: str,
    owner_id: str,
    core_review_after: str | None = None,
) -> str:
    """Direct, synchronous operation -- not a request/queue/event (plan rule 59). `owner_id`
    identifies the REVIEWING agent; it need not match the entity's own owner and never transfers
    ownership (plan rule: reviewer identity, not an ownership permission)."""
    from saltmdb.db.connection import write_transaction_retrying
    from saltmdb.utils.text import resolve_entity_id

    if outcome not in ("retain", "demote", "archive"):
        return "Error: outcome must be one of 'retain', 'demote', 'archive'."
    if not owner_id:
        return "Error: owner_id is mandatory."
    if outcome in ("demote", "archive") and core_review_after is not None:
        return f"Error: core_review_after must not be supplied for outcome='{outcome}'."
    try:
        rationale = validate_core_review_rationale(review_rationale)
    except ValueError as e:
        return f"Error: {e}"

    resolved_id = resolve_entity_id(conn, entity_id)
    if not resolved_id:
        return f"Error: Could not resolve entity '{entity_id}'."

    result_holder: dict[str, str] = {}

    def _write(c):  # noqa: C901, PLR0911
        row = c.execute(
            "SELECT is_core, status FROM entities WHERE id = ?", (resolved_id,)
        ).fetchone()
        if not row:
            result_holder["msg"] = f"Error: Memory '{resolved_id}' not found."
            return
        is_core_now, status = bool(row[0]), row[1]
        now = datetime.now(UTC).isoformat()

        if outcome == "retain":
            if not is_core_now or status == "archived":
                result_holder["msg"] = (
                    f"Error: review_core_memory(outcome='retain') requires an active core "
                    f"memory; '{resolved_id}' is "
                    f"{'archived' if status == 'archived' else 'not core'}."
                )
                return
            try:
                # parse_core_review_after itself now enforces the future-only lower bound
                # uniformly (resolved review finding #7) -- no separate re-check needed here.
                if core_review_after is None:
                    next_review = parse_core_review_after(
                        None, default_days=CORE_DEFAULT_REVIEW_DAYS
                    )
                else:
                    next_review = parse_core_review_after(core_review_after)
            except ValueError as e:
                result_holder["msg"] = f"Error: {e}"
                return
            c.execute(
                "UPDATE entities SET core_review_after = ?, core_last_reviewed_at = ?, "
                "core_last_reviewed_by = ?, core_review_rationale = ?, updated_at = ? WHERE id = ?",
                (next_review, now, owner_id, rationale, now, resolved_id),
            )
            result_holder["msg"] = (
                f"Memory '{resolved_id}' retained as core; next review at {next_review}."
            )
            return

        if outcome == "demote":
            if not is_core_now:
                result_holder["msg"] = f"Memory '{resolved_id}' is already non-core (no-op)."
                return
            core_tag_row = c.execute("SELECT id FROM tags WHERE name = '#core'").fetchone()
            c.execute(
                "UPDATE entities SET is_core = 0, core_review_after = NULL, "
                "core_last_reviewed_at = ?, core_last_reviewed_by = ?, core_review_rationale = ?, "
                "updated_at = ? WHERE id = ?",
                (now, owner_id, rationale, now, resolved_id),
            )
            if core_tag_row:
                c.execute(
                    "DELETE FROM entity_tags WHERE entity_id = ? AND tag_id = ?",
                    (resolved_id, core_tag_row[0]),
                )
            result_holder["msg"] = f"Memory '{resolved_id}' demoted from core to a normal memory."
            return

        # archive. Resolved review finding #3: review_core_memory reviews a CORE memory -- it
        # must never become a second, ownership-neutral general-purpose archive API for ordinary
        # memories. is_core is never cleared by an archive outcome (only demote clears it), so
        # is_core=1 reliably identifies a memory that either currently is, or formerly was, a
        # core -- the one signal usable for legacy rows with no separate "ever was core" column.
        if not is_core_now:
            result_holder["msg"] = (
                f"Error: review_core_memory(outcome='archive') requires a core memory; "
                f"'{resolved_id}' is not a core memory. Use archive_memory for ordinary memories."
            )
            return
        if status == "archived":
            result_holder["msg"] = f"Memory '{resolved_id}' is already archived (no-op)."
            return
        from saltmdb.domain.services.memory_service.lifecycle import _archive_entity_unchecked

        _archive_entity_unchecked(c, resolved_id)
        c.execute(
            "UPDATE entities SET core_last_reviewed_at = ?, core_last_reviewed_by = ?, "
            "core_review_rationale = ? WHERE id = ?",
            (now, owner_id, rationale, resolved_id),
        )
        result_holder["msg"] = f"Memory '{resolved_id}' was reviewed and archived."

    write_transaction_retrying(conn, _write)
    return result_holder["msg"]
