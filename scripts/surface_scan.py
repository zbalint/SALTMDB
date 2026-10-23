"""Scan documentation and hook text against the live MCP public vocabulary."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from saltmdb.mcp.guard import REMOVED_TOOLS, RENAMED_FIELDS
from saltmdb.mcp.server import mcp

MAX_FILE_BYTES = 1024 * 1024
SCANNED_SUFFIXES = frozenset({".md", ".py", ".json"})
REMOVED_FIELDS: dict[str, tuple[str, ...]] = {
    # S2: store_memory is create-only; core fields and weight moved out of the public shape.
    "store_memory": (
        "entity_id",
        "is_core",
        "core_reason",
        "core_exit_condition",
        "core_review_after",
        "detail_memory_ids",
        "weight",
    ),
    # S3a: consolidation is batch-only and no longer accepts the single form or weight.
    "consolidate_memories": ("parent_ids", "weight"),
    # S2b: relation and archive calls are list-only.
    "manage_relation": ("source_id", "target_id", "predicate"),
    "archive_memory": ("entity_id",),
    # S4c/S4d: retrieve_context is id-anchored; get_events uses cursor paging.
    "retrieve_context": ("strategy", "query"),
    "get_events": ("offset",),
    # S4d: trace search uses memory_id/query and bounded cursor paging.
    "search_traces": ("entity_id", "query_keywords"),
}
# Amendment 2: explicit response/public-token list; old field names remain rule-(b) checks.
RESPONSE_RENAMED_TOKENS = frozenset(
    {
        "inspect_memory",
        "include_inspect",
        "old_id",
        "new_id",
        "total_dependencies_found",
        "total_related_found",
        "query_keywords",
    }
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str
    text: str


@dataclass(frozen=True)
class Vocabulary:
    registered_tools: dict[str, frozenset[str]]
    removed_tools: frozenset[str]
    removed_fields: dict[str, frozenset[str]]
    removed_tokens: frozenset[str]


def build_vocabulary() -> Vocabulary:
    """Build the scanner vocabulary from the registered FastMCP server object."""
    tools = asyncio.run(mcp.list_tools())
    registered = {tool.name: frozenset(tool.inputSchema.get("properties", {})) for tool in tools}
    removed_tokens = set(RESPONSE_RENAMED_TOKENS)
    removed_fields = {name: frozenset(fields) for name, fields in REMOVED_FIELDS.items()}
    return Vocabulary(
        registered, frozenset(REMOVED_TOOLS), removed_fields, frozenset(removed_tokens)
    )


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _excluded(relative_path: Path) -> bool:
    parts = relative_path.parts
    if relative_path.name == "MIGRATION.md":
        return True
    for left, right in zip(parts, parts[1:]):
        if (left, right) in (("docs", "specs"), ("hooks", "tests")):
            return True
    return any(part in {"src", "tests", "scripts"} for part in parts)


def _candidate_paths(root: Path) -> list[Path]:
    candidates = [root] if root.is_file() else sorted(root.rglob("*"))
    return [path for path in candidates if not path.is_symlink()] if root.is_dir() else candidates


def _read_scan_target(path: Path, relative_path: Path) -> tuple[Path, Finding | None] | None:
    if path.is_symlink() or not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
        return None
    if _excluded(relative_path):
        return None
    if path.stat().st_size > MAX_FILE_BYTES:
        return path, Finding(_display_path(path), 1, "skipped: too large", "")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    return path, None


def _iter_files(paths: Sequence[str]) -> Iterable[tuple[Path, Finding | None]]:
    for raw in paths:
        root = Path(raw).resolve()
        for candidate in _candidate_paths(root):
            relative_path = candidate.name if root.is_file() else candidate.relative_to(root)
            target = _read_scan_target(candidate, Path(relative_path))
            if target is not None:
                yield target


def _line_at(text: str, index: int) -> tuple[int, str]:
    line = text.count("\n", 0, index) + 1
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return line, text[start:] if end < 0 else text[start:end]


def _allowed(path: str, token: str, line: str, allow_list) -> bool:
    return any(
        token == allowed_token
        and (path == allowed_path or path.endswith("/" + allowed_path))
        and context in line
        for allowed_path, allowed_token, context, _reason in allow_list
    )


def _finding(
    path: str, text: str, index: int, reason: str, allow_list, token: str | None = None
) -> Finding | None:
    line_number, line = _line_at(text, index)
    matched_token = token or ""
    if _allowed(path, matched_token, line, allow_list):
        return None
    return Finding(path, line_number, reason, line)


def _parenthesis_step(
    char: str, quote: str, escaped: bool, depth: int
) -> tuple[str, bool, int, bool]:
    closed = False
    if quote:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = ""
    elif char in "'\"":
        quote = char
    elif char == "(":
        depth += 1
    elif char == ")":
        depth -= 1
        closed = depth == 0
    return quote, escaped, depth, closed


def _scan_parentheses(text: str, start: int, line_end: int) -> str:
    depth = 1
    quote = ""
    escaped = False
    index = start
    while index < line_end:
        quote, escaped, depth, closed = _parenthesis_step(text[index], quote, escaped, depth)
        if closed:
            return text[start:index]
        index += 1
    return text[start:line_end]


def _argument_slice(text: str, start: int) -> str:
    line_end = text.find("\n", start)
    return _scan_parentheses(text, start, len(text) if line_end < 0 else line_end)


def _quoted_token(arguments: str, start: int) -> tuple[str | None, int]:
    quote = arguments[start]
    index = start + 1
    escaped = False
    while index < len(arguments):
        char = arguments[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            break
        index += 1
    end = index + 1
    if quote == '"':
        cursor = end
        while cursor < len(arguments) and arguments[cursor].isspace():
            cursor += 1
        if cursor < len(arguments) and arguments[cursor] == ":":
            return arguments[start + 1 : index], end
    return None, end


def _identifier_token(arguments: str, start: int) -> tuple[str | None, int]:
    end = start + 1
    while end < len(arguments) and (arguments[end].isalnum() or arguments[end] == "_"):
        end += 1
    cursor = end
    while cursor < len(arguments) and arguments[cursor].isspace():
        cursor += 1
    if cursor < len(arguments) and arguments[cursor] == "=":
        return arguments[start:end], end
    return None, end


def _argument_step(arguments: str, index: int, depth: int) -> tuple[str | None, int, int]:
    char = arguments[index]
    if char in "'\"":
        token, next_index = _quoted_token(arguments, index)
        return token if depth == 0 else None, next_index, depth
    if char in "[{(":
        return None, index + 1, depth + 1
    if char in "]})":
        return None, index + 1, max(0, depth - 1)
    if depth == 0 and (char.isalpha() or char == "_"):
        return (*_identifier_token(arguments, index), depth)
    return None, index + 1, depth


def _argument_names(arguments: str) -> Iterable[str]:
    index = 0
    depth = 0
    while index < len(arguments):
        token, index, depth = _argument_step(arguments, index, depth)
        if token is not None:
            yield token


def _scan_tool_names(path: str, text: str, vocabulary: Vocabulary, allow_list) -> list[Finding]:
    findings = []
    pattern = re.compile(r"mcp__saltmdb__([A-Za-z_]+)(\*)?")
    for match in pattern.finditer(text):
        if match.group(2) or match.group(1) in vocabulary.registered_tools:
            continue
        item = _finding(path, text, match.start(), "unknown tool name", allow_list, match.group(1))
        if item:
            findings.append(item)
    return findings


def _scan_calls(path: str, text: str, vocabulary: Vocabulary, allow_list) -> list[Finding]:
    findings = []
    names = sorted(set(vocabulary.registered_tools) | set(vocabulary.removed_tools))
    if not names:
        return findings
    pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, names)) + r")\(")
    for match in pattern.finditer(text):
        tool = match.group(1)
        known = vocabulary.registered_tools.get(tool, frozenset())
        forbidden = vocabulary.removed_fields.get(tool, frozenset()) | frozenset(
            RENAMED_FIELDS.get(tool, {})
        )
        for field in _argument_names(_argument_slice(text, match.end())):
            if field in known and field not in forbidden:
                continue
            item = _finding(
                path, text, match.start(), f"invalid field for {tool}: {field}", allow_list, field
            )
            if item:
                findings.append(item)
    return findings


def _scan_backticks(path: str, text: str, vocabulary: Vocabulary, allow_list) -> list[Finding]:
    findings = []
    for match in re.finditer(r"`([^`]+)`", text):
        token = match.group(1)
        if token not in vocabulary.removed_tokens:
            continue
        item = _finding(path, text, match.start(), "removed public token", allow_list, token)
        if item:
            findings.append(item)
    return findings


def _json_entries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if value.get("type") == "mcp_tool":
            yield value
        for item in value.values():
            yield from _json_entries(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_entries(item)


def _settings_entry_findings(
    path: str, text: str, entry: dict[str, Any], vocabulary: Vocabulary, allow_list
) -> list[Finding]:
    tool = entry.get("tool")
    if tool not in vocabulary.registered_tools:
        index = text.find(f'"tool": "{tool}"')
        item = _finding(path, text, max(index, 0), "unknown settings tool", allow_list, str(tool))
        return [item] if item else []
    input_value = entry.get("input", {})
    if not isinstance(input_value, dict):
        return []
    findings = []
    for field in input_value:
        if field in vocabulary.registered_tools[tool]:
            continue
        index = text.find(f'"{field}"')
        item = _finding(
            path,
            text,
            max(index, 0),
            f"invalid settings field for {tool}: {field}",
            allow_list,
            field,
        )
        if item:
            findings.append(item)
    return findings


def _scan_settings(path: str, text: str, vocabulary: Vocabulary, allow_list) -> list[Finding]:
    if not path.endswith(".json"):
        return []
    try:
        entries = list(_json_entries(json.loads(text)))
    except (json.JSONDecodeError, TypeError):
        return []
    findings = []
    for entry in entries:
        findings.extend(_settings_entry_findings(path, text, entry, vocabulary, allow_list))
    return findings


def scan_text(
    path: str, text: str, vocabulary: Vocabulary, allow_list=(), *, validate_allow_list: bool = True
) -> list[Finding]:
    """Scan one decoded file and return deterministic findings."""
    findings = (
        _scan_tool_names(path, text, vocabulary, allow_list)
        + _scan_calls(path, text, vocabulary, allow_list)
        + _scan_backticks(path, text, vocabulary, allow_list)
        + _scan_settings(path, text, vocabulary, allow_list)
    )
    if validate_allow_list:
        for allowed_path, token, context, _reason in allow_list:
            if (
                not (path == allowed_path or path.endswith("/" + allowed_path))
                or token not in text
                or context not in text
            ):
                findings.append(Finding(path, 1, "allow-list entry did not match", ""))
    return findings


def _scan_path(
    path: Path, vocabulary: Vocabulary, allow_list
) -> tuple[list[Finding], str, set[int]]:
    display = _display_path(path)
    text = path.read_text(encoding="utf-8")
    matched = {
        index
        for index, (allowed_path, token, context, _reason) in enumerate(allow_list)
        if (display == allowed_path or display.endswith("/" + allowed_path))
        and token in text
        and context in text
    }
    return (
        scan_text(display, text, vocabulary, allow_list, validate_allow_list=False),
        display,
        matched,
    )


def _stale_allow_findings(
    allow_list, scanned_paths: set[str], matched_allow: set[int]
) -> list[Finding]:
    findings = []
    for index, (allowed_path, _token, _context, _reason) in enumerate(allow_list):
        if any(path == allowed_path or path.endswith("/" + allowed_path) for path in scanned_paths):
            if index not in matched_allow:
                findings.append(Finding(allowed_path, 1, "allow-list entry did not match", ""))
    return findings


def scan_paths(paths: Sequence[str], vocabulary: Vocabulary, allow_list=()) -> list[Finding]:
    """Read and scan regular files below each supplied root."""
    findings = []
    matched_allow = set()
    scanned_paths = set()
    for path, skipped in _iter_files(paths):
        if skipped is not None:
            findings.append(skipped)
            continue
        path_findings, display, matched = _scan_path(path, vocabulary, allow_list)
        findings.extend(path_findings)
        scanned_paths.add(display)
        matched_allow.update(matched)
    return findings + _stale_allow_findings(allow_list, scanned_paths, matched_allow)
