"""Uniform success and rejection envelopes for agent-facing APIs.

Successes always contain ``status``, ``data``, ``warnings`` and ``errors``. Rejections
contain ``status``, ``errors`` and ``warnings`` but never ``data``. Structured
``corrected_call`` hints live on the error they correct; alternatives use
``corrected_calls`` instead.
"""

from typing import Any, Literal

Status = Literal["ok", "rejected"]


def warning(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one non-blocking warning entry."""
    item: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        item["detail"] = detail
    return item


def error(
    code: str,
    message: str,
    field: str | None = None,
    *,
    corrected_call: dict[str, Any] | None = None,
    corrected_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one rejection entry, optionally carrying one correction or alternatives."""
    if corrected_call is not None and corrected_calls is not None:
        raise ValueError("corrected_call and corrected_calls are mutually exclusive")
    item: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        item["field"] = field
    if corrected_call is not None:
        item["corrected_call"] = corrected_call
    if corrected_calls is not None:
        item["corrected_calls"] = corrected_calls
    return item


def ok(
    data: Any = None,
    *,
    warnings: list[dict[str, Any]] | None = None,
    effective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a successful envelope with an explicit empty errors list."""
    envelope: dict[str, Any] = {
        "status": "ok",
        "data": data,
        "warnings": list(warnings or []),
        "errors": [],
    }
    if effective is not None:
        envelope["effective"] = effective
    return envelope


def rejected(
    errors: list[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
    effective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a rejection envelope without a ``data`` key."""
    if not errors:
        raise ValueError("rejected() requires at least one error entry")
    envelope: dict[str, Any] = {
        "status": "rejected",
        "errors": list(errors),
        "warnings": list(warnings or []),
    }
    if effective is not None:
        envelope["effective"] = effective
    return envelope


def internal_error() -> dict[str, Any]:
    """Build the fixed rejection for an unexpected fault: no detail reaches the caller."""
    return rejected([error("INTERNAL_ERROR", "Tool execution failed unexpectedly.")])


def is_rejected(envelope: dict[str, Any]) -> bool:
    return isinstance(envelope, dict) and envelope.get("status") == "rejected"


def is_ok(envelope: dict[str, Any]) -> bool:
    return isinstance(envelope, dict) and envelope.get("status") == "ok"
