"""Schema-derived `corrected_call` generation (agent API redesign plan §4.3).

Engineering constraint from the plan, verbatim: "Generate corrected_call from the SAME schema
definition the tool validates against, never a hand-maintained template. Drift then becomes
structurally impossible rather than a discipline problem."

The "same schema definition" is the tool function's own `inspect.signature` -- from Phase 2
onward every `@mcp.tool()` function has explicit, typed parameters (no `**kwargs`), so its
signature IS the validated contract. Reading it directly, rather than maintaining a parallel
field-list constant, is what makes drift impossible rather than merely unlikely.
"""

import inspect
from typing import Any, Callable


def build_corrected_call(
    tool_func: Callable[..., Any],
    submitted: dict[str, Any] | None,
    fixes: dict[str, Any],
) -> dict[str, Any]:
    """Builds a ready-to-paste corrected_call dict for `tool_func`.

    `submitted` is the caller's original arguments (whatever they actually sent, including
    stale/unknown keys); `fixes` overrides/adds the corrected values for the fields the error
    identified. The result contains only keys that are real parameters of `tool_func`'s current
    signature, so a caller pasting it back verbatim can never hit an unexpected-keyword error --
    and any field the caller sent that ISN'T a real parameter (a typo, a pre-redesign alias, a
    front-matter leftover) is silently dropped rather than carried forward.

    A merged value of None is omitted (never emitted as an explicit null), matching every
    tool's own "omit to mean unset" convention -- except a parameter with no default (a
    genuinely required field), which is ALWAYS included even if the caller didn't send one and
    no fix supplied one either. Such a call is not actually correctable (the caller must still
    author real content), but keeping the key present with a placeholder-free omission would
    silently produce an invalid corrected_call the invariant test below would then rightly fail --
    surfacing the gap immediately rather than emitting a call that looks complete but isn't.
    """
    sig = inspect.signature(tool_func)
    merged = {**(submitted or {}), **(fixes or {})}
    corrected: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in merged and merged[name] is not None:
            corrected[name] = merged[name]
    return corrected


def missing_required_fields(
    tool_func: Callable[..., Any], corrected_call: dict[str, Any]
) -> list[str]:
    """Names every required parameter (no default, not *args/**kwargs) of `tool_func` that
    `corrected_call` does not supply. Used by the invariant test harness (tests/_helpers) to
    fail loudly, at generation time, when a corrected_call would not actually be resubmittable --
    rather than deferring that discovery to a live call against a real backend."""
    sig = inspect.signature(tool_func)
    missing = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.default is inspect.Parameter.empty and name not in corrected_call:
            missing.append(name)
    return missing
