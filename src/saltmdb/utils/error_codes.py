"""Canonical error codes for envelope.rejected()'s error entries (domain/validation layer).

New, generic codes for tools that emit no error code today. Distinct from daemon/protocol.py's
AUTH_FAILED/UNKNOWN_TOOL/MALFORMED_REQUEST/INTERNAL_ERROR, which classify a transport/RPC-level
failure before a request ever reaches a tool's own domain logic -- these classify what a TOOL's
own validation or business rules rejected, once dispatch already let the call through. Does not
replace any code an already envelope-adopted tool already uses (MISSING_TITLE, INVALID_TAGS,
UNKNOWN_ENTITY_ID, AMBIGUOUS_ID_PREFIX, INACTIVE_TARGET, TARGET_CHANGED, INVALID_MEMORY,
MEMORY_QUALITY_REJECTED, LIFECYCLE_WRITE_FAILED, RESERVED_PREDICATE, LEGACY_READONLY_PREDICATE,
NONCANONICAL_PREDICATE, UNKNOWN_PREDICATE, CORE_CAPACITY_EXCEEDED,
IDENTITY_IN_YAML_FRONT_MATTER) -- those are already specific and stay unchanged.
"""

VALIDATION_ERROR = "VALIDATION_ERROR"
"""A required field was missing, or a supplied field was the wrong shape/type, for a tool with
no pre-existing bespoke code for that specific field. Always carries `field` in the error entry."""

NOT_FOUND = "NOT_FOUND"
"""A referenced id (entity, tag, predicate) does not exist, for a tool with no pre-existing
bespoke "unknown X" code. Always carries `field` naming the offending parameter."""

ALREADY_DONE = "ALREADY_DONE"
"""An idempotent no-op: the requested end state already holds (already archived, already
canonical, already non-core). Not an error in the sense of "nothing happened because it
couldn't" -- nothing happened because it was already true. Callers should treat this as success,
not failure, when deciding whether to retry."""

CONFLICT = "CONFLICT"
"""A caller-supplied constraint (e.g. an ownership check) failed against the current state of
the target, for a tool with no pre-existing bespoke code for that specific conflict."""

INTERNAL_ERROR = "INTERNAL_ERROR"
"""An unexpected exception was caught at a tool's own top-level handler -- not a caller-input
problem. Distinguishes an unanticipated server-side fault from every other code above, all of
which describe an anticipated, nameable rejection reason."""
