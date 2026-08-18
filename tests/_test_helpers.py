"""Shared test-only helpers, not a test module itself (no test_*.py prefix, so
`python -m unittest discover -s tests` never collects it directly).

corrected_call invariant harness (agent API redesign plan §4.3): "For every mechanically-
derivable error, submitting the returned corrected_call verbatim must succeed." Phase-specific
test modules import assert_corrected_call_complete from here once they start generating real
corrected_call payloads from live error paths (Phase 5 onward) -- Phase 1 only builds and tests
the generator itself (tests/test_corrected_call.py), since no tool emits corrected_call yet.
"""

from saltmdb.utils.corrected_call import missing_required_fields


def assert_corrected_call_complete(test_case, tool_func, corrected_call: dict) -> None:
    """Fails `test_case` if `corrected_call` is missing any required parameter of `tool_func`.

    This is the structural half of the §4.3 invariant: a corrected_call that supplies every
    required field is *resubmittable* by construction (its keys are already schema-validated by
    build_corrected_call). The live-call half -- actually invoking the tool with the corrected
    call and asserting a non-"rejected" envelope -- belongs in each phase's own test module,
    since it needs that phase's specific backend/fixture wiring.
    """
    missing = missing_required_fields(tool_func, corrected_call)
    test_case.assertEqual(
        missing,
        [],
        f"corrected_call for {tool_func.__name__} is missing required fields {missing}; "
        "submitting it verbatim would not succeed (§4.3 invariant violated)",
    )
