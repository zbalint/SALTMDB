"""Uniform response envelope (agent API redesign plan §4.2).

Not yet wired into any MCP tool's return value -- Phase 1 only builds the shared module and
its tests. Tools adopt it incrementally as each is reshaped in later phases (§6). Building it
now, ahead of any caller, keeps every subsequent phase's response shape identical and drift-
proof: one envelope constructor, never a hand-rolled dict literal per tool.

Shape (§4.2):
    {
      "status": "ok" | "rejected",
      "data": {...},              # tool-specific payload, "ok" only
      "warnings": [...],          # never blocking, present (possibly empty) on "ok"
      "errors": [...],            # present only when status == "rejected"
      "corrected_call": {...},    # present only for mechanically derivable fixes
      "effective": {...},         # present only when supplied (owner_id/context_id/scope actually used)
    }

Zero-side-effect guarantee (§4.2): callers constructing a "rejected" envelope must not have
written anything first -- validation runs before any BEGIN. This module cannot enforce that by
itself (it has no view of what a caller did before calling it); it is asserted by tests at each
call site instead (§8.1).
"""

from typing import Any, Literal

Status = Literal["ok", "rejected"]


def warning(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Builds one entry for the envelope's `warnings` list. Never blocking by construction --
    a warning is informational only and must never be the sole reason a call is rejected."""
    item: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        item["detail"] = detail
    return item


def error(code: str, message: str, field: str | None = None) -> dict[str, Any]:
    """Builds one entry for the envelope's `errors` list. `field` names the offending
    parameter when the error is attributable to exactly one (omitted for whole-call errors)."""
    item: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        item["field"] = field
    return item


def ok(
    data: Any = None,
    *,
    warnings: list[dict[str, Any]] | None = None,
    corrected_call: dict[str, Any] | None = None,
    effective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds a successful envelope. `warnings` defaults to an empty list (always present, per
    §4.2's shape), never omitted -- callers should never need to guard for a missing key."""
    envelope: dict[str, Any] = {"status": "ok", "data": data, "warnings": list(warnings or [])}
    if corrected_call is not None:
        envelope["corrected_call"] = corrected_call
    if effective is not None:
        envelope["effective"] = effective
    return envelope


def rejected(
    errors: list[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
    corrected_call: dict[str, Any] | None = None,
    effective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds a rejected envelope. `errors` must be non-empty -- a rejection with no stated
    reason gives the caller nothing to act on, so this is enforced rather than left to convention."""
    if not errors:
        raise ValueError("rejected() requires at least one error entry")
    envelope: dict[str, Any] = {
        "status": "rejected",
        "errors": list(errors),
        "warnings": list(warnings or []),
    }
    if corrected_call is not None:
        envelope["corrected_call"] = corrected_call
    if effective is not None:
        envelope["effective"] = effective
    return envelope


def is_rejected(envelope: dict[str, Any]) -> bool:
    return isinstance(envelope, dict) and envelope.get("status") == "rejected"


def is_ok(envelope: dict[str, Any]) -> bool:
    return isinstance(envelope, dict) and envelope.get("status") == "ok"
