from __future__ import annotations

import re


def cursor_offset(cursor: object, max_offset: int) -> tuple[int, str | None]:
    """Parse the canonical ASCII offset cursor and return an error message when invalid."""
    if cursor is None:
        return 0, None
    if not isinstance(cursor, str):
        return 0, "cursor must be a string."
    max_digits = len(str(max_offset))
    match = re.fullmatch(rf"offset:(0|[1-9]\d{{0,{max_digits - 1}}})", cursor, re.ASCII)
    if match is None:
        return 0, f"cursor must match offset:<0..{max_offset}>."
    offset = int(match.group(1))
    if offset > max_offset:
        return 0, f"cursor offset must be at most {max_offset}."
    return offset, None
