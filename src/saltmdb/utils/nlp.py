import re
from saltmdb.config import (
    QG_MIN_LENGTH,
    QG_MAX_SYMBOL_RATIO,
    QG_MIN_ENTROPY,
    QG_MAX_ENTROPY,
    QG_MAX_3GRAM_DUP,
    QG_MAX_5GRAM_DUP,
    QG_MIN_TTR,
    QG_CLI_MIN,
    QG_CLI_MAX,
    QG_PARAGRAPH_BREAK_MIN_LENGTH,
    QG_HEADING_OR_LIST_MIN_LENGTH,
    QG_MULTI_HEADING_MIN_LENGTH,
    QG_OVERSIZED_PAYLOAD_THRESHOLD,
)

_WORD_TOKEN_RE = re.compile(r"\b\w+\b")


STOP_WORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "aren't",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "can't",
    "cannot",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "don't",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "out",
    "over",
    "own",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "with",
    "would",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
}


# Track A store-time disposition rewrite (see scratch/plans/track_a_disposition_detailed.md §2.1).
# Fixed, documented, deliberately simple/explainable lexicon of correction/update/replacement
# markers -- a heuristic hint feeding evaluate_store_preflight's supersession-signal detection,
# never a determination (the caller sees exactly which phrases matched, not an opaque score).
# Word-boundary-anchored, case-insensitive; checked against the NEW content only, not the target's.
_CORRECTION_LANGUAGE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bactually,",
        r"\bcorrection:",
        r"\bno longer\b",
        r"\binstead of\b",
        r"\bsupersedes\b",
        r"\breplaces\b",
        r"\bwas wrong\b",
        r"\bturns out\b",
        r"\bupdate:",
        r"\bdeprecated\b",
        r"\boutdated\b",
        r"\bcontrary to\b",
        r"\brevised:",
    )
]


def detect_correction_language(text: str) -> list[str]:
    """Returns the distinct correction/update/replacement marker phrases found in `text`.

    Empty list means no signal. Deliberately a plain substring/regex lexicon over a statistical
    classifier -- this feeds an advisory hint an agent must be able to sanity-check at a glance
    (see evaluate_store_preflight's `suggested_label`/`heuristic_note` contract), not a black box.
    """
    if not text:
        return []
    matches = []
    for pattern in _CORRECTION_LANGUAGE_PATTERNS:
        m = pattern.search(text)
        if m:
            matches.append(m.group(0).rstrip(",:"))
    return matches


def stem(word: str) -> str:
    """Basic English suffix stemming for fuzzy matching."""
    w = word.lower()
    for suffix in ("ing", "edly", "ed", "es", "s", "ly", "ment", "tion", "ness", "ity", "al"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def tokenize(text: str) -> set:
    """Extract stemmed content tokens excluding stop words."""
    if not text:
        return set()
    words = re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", text.lower())
    return {stem(w) for w in words if w not in STOP_WORDS}


def word_sim(text1: str, text2: str) -> float:
    """Jaccard similarity coefficient based on stemmed token sets."""
    t1 = tokenize(text1)
    t2 = tokenize(text2)
    if not t1 or not t2:
        return 0.0
    inter = len(t1.intersection(t2))
    union = len(t1.union(t2))
    return inter / union if union > 0 else 0.0


import math  # noqa: E402

FLUFF_PATTERN = re.compile(
    r"^(ok|done|thanks|got it|i have|modified the file|sure|completed|consolidated these files|consolidated|consolidated notes|merged summary)[\.!]?$",
    re.IGNORECASE,
)


def calculate_shannon_entropy(text: str) -> float:
    """Calculate character-level Shannon entropy in bits per character."""
    if not text:
        return 0.0
    length = len(text)
    freqs: dict[str, int] = {}
    for char in text:
        freqs[char] = freqs.get(char, 0) + 1
    entropy = 0.0
    for count in freqs.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def calculate_ttr(text: str) -> float:
    """Calculate Type-Token Ratio (Lexical Diversity) based on word tokens."""
    words = re.findall(_WORD_TOKEN_RE, text.lower())
    if not words:
        return 0.0
    unique_words = set(words)
    return len(unique_words) / len(words)


def calculate_symbol_ratio(text: str) -> float:
    """Calculate ratio of punctuation/symbols to alphanumeric characters."""
    alpha_count = sum(1 for c in text if c.isalnum())
    symbol_count = sum(1 for c in text if not c.isalnum() and not c.isspace())
    if alpha_count == 0:
        return 1.0 if symbol_count > 0 else 0.0
    return symbol_count / alpha_count


def calculate_ngram_duplicate_ratio(text: str, n: int) -> float:
    """Calculate the ratio of duplicate word N-grams in text."""
    words = re.findall(_WORD_TOKEN_RE, text.lower())
    if len(words) < n:
        return 0.0
    ngrams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    if not ngrams:
        return 0.0
    unique_count = len(set(ngrams))
    total_count = len(ngrams)
    return 1.0 - (unique_count / total_count)


def extract_prose_content(text: str) -> str:
    """
    Strips code fences, inline backticks, file paths, and URLs to isolate pure prose text.
    Prevents false readability quality rejections on raw technical logs.
    """
    if not text:
        return ""
    # Strip triple-backtick code blocks
    cleaned = re.sub(r"```[\s\S]*?(?:```|\Z)", " ", text)
    # Strip inline backticks
    cleaned = re.sub(r"`[^`\n]+`", " ", cleaned)
    # Strip URLs
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    # Strip file paths
    cleaned = re.sub(r"\b[\w/-]+\.(py|rs|js|ts|json|md|sql|yml|yaml|c|cpp|h|sh|db)\b", " ", cleaned)
    # Strip redundant spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def calculate_coleman_liau_index(prose_text: str) -> float:
    """
    Calculates Coleman-Liau Readability Index (CLI) on prose text:
    CLI = 0.0588 * L - 0.296 * S - 15.8
    (L: avg letters per 100 words, S: avg sentences per 100 words)
    """
    words = re.findall(r"\b[a-zA-Z0-9_-]+\b", prose_text)
    if not words:
        return 0.0
    word_count = len(words)
    letter_count = sum(len(w) for w in words)
    # Sentences delimited by ., !, ?, or newlines \n (for unpunctuated technical lists)
    sentences = [s for s in re.split(r"[.!?\n]+", prose_text) if s.strip()]
    sentence_count = max(1, len(sentences))

    L = (letter_count / word_count) * 100.0
    S = (sentence_count / word_count) * 100.0
    cli = (0.0588 * L) - (0.296 * S) - 15.8
    return round(cli, 2)


def _format_closed_code_block(fence_lang, code_lines):
    # If fence was untyped, apply heuristic keyword detection
    if not fence_lang:
        code_text = "\n".join(code_lines)
        if re.search(r"\b(def|import|from|class|elif|self|print)\b", code_text):
            fence_lang = "python"
        elif re.search(
            r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|FROM|WHERE)\b",
            code_text,
            re.IGNORECASE,
        ):
            fence_lang = "sql"
        elif re.search(r"^\s*[\{\[]", code_text) and ('"' in code_text or ":" in code_text):
            fence_lang = "json"
        elif re.search(r"\b(function|const|let|var|console\.log|export|import)\b", code_text):
            fence_lang = "javascript"
    return [f"```{fence_lang}", *code_lines, "```"]


def _format_unclosed_code_block(block_buffer):
    fence_lang = block_buffer[0][1]
    code_lines = [line for _, line in block_buffer[1:]]  # noqa: E741
    return [f"```{fence_lang}", *code_lines]


def _process_markdown_line(line, in_code_block, block_buffer, formatted_lines):
    if line.strip().startswith("```"):
        if not in_code_block:
            block_lang = line.strip()[3:].strip()
            return True, [("fence", block_lang)], formatted_lines
        code_lines = [line for _, line in block_buffer[1:]]  # noqa: E741
        formatted_lines.extend(_format_closed_code_block(block_buffer[0][1], code_lines))
        return False, [], formatted_lines
    if in_code_block:
        block_buffer.append(("line", line))
        return in_code_block, block_buffer, formatted_lines
    formatted_lines.append(line)
    return in_code_block, block_buffer, formatted_lines


def auto_format_markdown(text: str) -> str:
    """
    Idempotent pre-formatting pipeline: f(f(x)) = f(x).
    Auto-annotates untyped code blocks with language identifiers and normalizes whitespace.
    """
    if not text:
        return ""

    # 1. Normalize line endings and trailing whitespace
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]

    # 2. Auto-annotate untyped code blocks based on syntax heuristics
    formatted_lines: list[str] = []
    in_code_block = False
    block_buffer: list[tuple[str, str]] = []

    for line in lines:
        in_code_block, block_buffer, formatted_lines = _process_markdown_line(
            line, in_code_block, block_buffer, formatted_lines
        )

    # If block was left unclosed, append buffered lines
    if in_code_block and block_buffer:
        formatted_lines.extend(_format_unclosed_code_block(block_buffer))

    result = "\n".join(formatted_lines)
    # Collapse 3+ consecutive newlines to 2 newlines
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def _table_row_update(stripped, table_block_pipes):
    if stripped.startswith("|") and stripped.endswith("|"):
        pipe_count = stripped.count("|")
        if pipe_count < 2:
            return {
                "is_valid": False,
                "error_flag": "BROKEN_MARKDOWN_SYNTAX",
                "reason": "Malformed Markdown table row detected (insufficient pipe separators).",
            }, table_block_pipes
        return None, [*table_block_pipes, pipe_count]
    if table_block_pipes:
        if len(set(table_block_pipes)) > 1:
            return {
                "is_valid": False,
                "error_flag": "BROKEN_MARKDOWN_SYNTAX",
                "reason": "Malformed Markdown table row detected (mismatched column pipe separators).",
            }, []
        return None, []
    return None, table_block_pipes


def _validate_table_rows(text):
    # Table Column Symmetry check
    table_block_pipes: list[int] = []
    for line in text.splitlines():
        error, table_block_pipes = _table_row_update(line.strip(), table_block_pipes)
        if error:
            return error
    if table_block_pipes and len(set(table_block_pipes)) > 1:
        return {
            "is_valid": False,
            "error_flag": "BROKEN_MARKDOWN_SYNTAX",
            "reason": "Malformed Markdown table row detected (mismatched column pipe separators).",
        }
    return None


def _markdown_headers(text):
    text_no_code = re.sub(r"```[\s\S]*?```", "", text)
    headers = re.findall(r"^(#{1,6})\s+(.+)$", text_no_code, re.MULTILINE)
    header_levels = [len(header[0]) for header in headers]
    has_skip = any(
        header_levels[i + 1] > header_levels[i] + 1 for i in range(len(header_levels) - 1)
    )
    return headers, has_skip


def _markdown_metrics(text, headers):
    code_fences = re.findall(r"^\s*```\s*(\S*)", text, re.MULTILINE)
    # Filter only opening fences (even index if formatted properly)
    untyped_blocks = sum(1 for i in range(0, len(code_fences), 2) if not code_fences[i].strip())
    words = re.findall(_WORD_TOKEN_RE, text)
    total_words = len(words)
    header_words = sum(len(re.findall(_WORD_TOKEN_RE, header[1])) for header in headers)
    list_items = re.findall(r"^[^\S\n]*(?:[\-\*\+]|\d+\.)\s+(.+)$", text, re.MULTILINE)
    list_item_words = sum(len(re.findall(_WORD_TOKEN_RE, item)) for item in list_items)
    code_blocks = re.findall(r"```[\s\S]*?```", text)
    code_block_words = sum(len(re.findall(_WORD_TOKEN_RE, block)) for block in code_blocks)
    msdi = (
        (header_words + list_item_words + code_block_words) / total_words
        if total_words > 0
        else 0.0
    )
    return untyped_blocks, round(msdi, 3)


def validate_markdown_structure(text: str) -> dict:
    """
    Validates Markdown syntax integrity, header hierarchy, code block annotations, and MSDI.
    """
    # 1. Syntax Integrity Checks
    # Balanced code fences (allow optional leading whitespace)
    fence_count = len(re.findall(r"^\s*```", text, re.MULTILINE))
    if fence_count % 2 != 0:
        return {
            "is_valid": False,
            "error_flag": "BROKEN_MARKDOWN_SYNTAX",
            "reason": "Unclosed Markdown code block detected (odd count of ``` markers).",
        }

    table_error = _validate_table_rows(text)
    if table_error:
        return table_error

    # 2. Header Hierarchy & Progression Check
    headers, has_skip = _markdown_headers(text)

    # 3. Untyped Code Fences Check (allow leading whitespace and hyphenated language names e.g. docker-compose)
    # 4. MSDI (Markdown Structural Density Index) Calculation
    untyped_blocks, msdi = _markdown_metrics(text, headers)
    return {
        "is_valid": True,
        "header_count": len(headers),
        "has_header_skip": has_skip,
        "untyped_blocks": untyped_blocks,
        "msdi": msdi,
    }


def _contains_explicit_placeholder(text: str) -> bool:
    """Return whether *text* is an authoring placeholder rather than a memory body.

    The check intentionally targets explicit template markers and whole-body placeholders.  A
    normal note mentioning ``TODO`` in passing is not rejected, while a generated ``{{summary}}``
    or ``[placeholder]`` payload is.
    """
    if not text:
        return False
    marker = re.compile(
        r"(?:\{\{[^{}\n]+\}\}|\$\{[^}\n]+\}|<(?:placeholder|todo|tbd|your[- ]?text)>|"
        r"\[(?:placeholder|todo|tbd|insert[^\]]*)\])",
        re.IGNORECASE,
    )
    if marker.search(text):
        return True
    return bool(
        re.fullmatch(
            r"(?:placeholder|todo|tbd|lorem ipsum|n/?a|none|null|to be determined)[.!]?",
            text.strip(),
            re.IGNORECASE,
        )
    )


def _is_extreme_generation_loop(text: str, words: list[str], entropy: float) -> bool:
    """Identify only unmistakable generated repetition as a statistical hard failure.

    Ordinary low-diversity prose is allowed through with a warning.  A short, single-word loop
    (the common ``test test ...`` failure mode) and long near-identical n-gram loops remain hard
    failures because they carry no recoverable memory signal.
    """
    if len(words) < 12:
        return False
    ttr = calculate_ttr(text)
    if entropy < 2.0 and ttr < 0.20:
        return True
    duplicate_3gram = calculate_ngram_duplicate_ratio(text, 3)
    duplicate_5gram = calculate_ngram_duplicate_ratio(text, 5)
    return len(words) >= 24 and (duplicate_3gram >= 0.85 or duplicate_5gram >= 0.80)


def _quality_add_flag(flags, code):
    if code not in flags:
        flags.append(code)


def _quality_add_hard(flags, hard_errors, reasons, code, reason):
    _quality_add_flag(flags, code)
    hard_errors.append(code)
    reasons.append(reason)


def _quality_add_warning(flags, warnings, code):
    _quality_add_flag(flags, code)
    warnings.append(code)


def _quality_initial_findings(content):
    flags: list[str] = []
    hard_errors: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content.strip()
    else:
        text = str(content).strip()
        _quality_add_hard(
            flags, hard_errors, reasons, "INVALID_FORMAT", "Payload content must be a text string."
        )
    length = len(text)
    if not text:
        _quality_add_hard(flags, hard_errors, reasons, "EMPTY_CONTENT", "Payload content is empty.")
    if length < QG_MIN_LENGTH:
        _quality_add_hard(
            flags,
            hard_errors,
            reasons,
            "SHORT_LENGTH",
            f"Payload string length ({length} chars) below minimum threshold of {QG_MIN_LENGTH} characters.",
        )
    if text and FLUFF_PATTERN.match(text):
        _quality_add_hard(
            flags,
            hard_errors,
            reasons,
            "CONVERSATIONAL_FLUFF",
            "Conversational fluff phrase detected.",
        )
    if _contains_explicit_placeholder(text):
        _quality_add_hard(
            flags,
            hard_errors,
            reasons,
            "EXPLICIT_PLACEHOLDER",
            "Payload contains an unresolved placeholder.",
        )
    if calculate_symbol_ratio(text) > QG_MAX_SYMBOL_RATIO:
        _quality_add_warning(flags, warnings, "HIGH_SYMBOL_RATIO")
    if length > QG_OVERSIZED_PAYLOAD_THRESHOLD:
        _quality_add_warning(flags, warnings, "OVERSIZED_PAYLOAD")
    return text, length, flags, hard_errors, warnings, reasons


def _quality_entropy_findings(text, words, entropy, flags, hard_errors, warnings, reasons):
    extreme_loop = _is_extreme_generation_loop(text, words, entropy)
    if entropy < QG_MIN_ENTROPY:
        if extreme_loop:
            _quality_add_hard(
                flags,
                hard_errors,
                reasons,
                "EXTREME_GENERATION_LOOP",
                f"Character entropy too low ({entropy:.2f} bits/char) and the payload is an extreme repetition loop.",
            )
        else:
            _quality_add_warning(flags, warnings, "LOW_ENTROPY")
    elif entropy > QG_MAX_ENTROPY:
        _quality_add_warning(flags, warnings, "HIGH_ENTROPY")


def _quality_repetition_findings(text, words, flags, warnings):
    if len(words) >= 20:
        dup_3gram = calculate_ngram_duplicate_ratio(text, 3)
        if dup_3gram > QG_MAX_3GRAM_DUP:
            _quality_add_warning(flags, warnings, "HIGH_3GRAM_REPETITION")
        dup_5gram = calculate_ngram_duplicate_ratio(text, 5)
        if dup_5gram > QG_MAX_5GRAM_DUP:
            _quality_add_warning(flags, warnings, "HIGH_5GRAM_REPETITION")
    if len(words) > 30 and calculate_ttr(text) < QG_MIN_TTR:
        _quality_add_warning(flags, warnings, "LOW_TTR")


def _quality_readability_findings(text, flags, warnings):
    prose_content = extract_prose_content(text)
    prose_words = re.findall(r"\b[a-zA-Z0-9_-]+\b", prose_content)
    if len(prose_words) > 30:
        cli = calculate_coleman_liau_index(prose_content)
        if cli < QG_CLI_MIN or cli > QG_CLI_MAX:
            _quality_add_warning(flags, warnings, "EXTREME_READABILITY_BOUNDS")


def _quality_statistical_findings(text, words, entropy, flags, hard_errors, warnings, reasons):
    _quality_entropy_findings(text, words, entropy, flags, hard_errors, warnings, reasons)
    _quality_repetition_findings(text, words, flags, warnings)
    _quality_readability_findings(text, flags, warnings)


def _quality_structure_findings(text, length, md_res, flags, hard_errors, reasons):
    has_list = bool(re.search(r"^\s*(?:[\-\*\+]|\d+\.)\s+", text, re.MULTILINE))
    has_paragraph_break = "\n\n" in text
    header_count = md_res.get("header_count", 0)
    if QG_PARAGRAPH_BREAK_MIN_LENGTH <= length < QG_HEADING_OR_LIST_MIN_LENGTH:
        if not has_paragraph_break:
            _quality_add_hard(
                flags,
                hard_errors,
                reasons,
                "MISSING_PARAGRAPH_BREAK",
                f"Payloads from {QG_PARAGRAPH_BREAK_MIN_LENGTH} characters require a paragraph break.",
            )
    if length >= QG_HEADING_OR_LIST_MIN_LENGTH and header_count == 0 and not has_list:
        _quality_add_hard(
            flags,
            hard_errors,
            reasons,
            "MISSING_HEADING_OR_LIST",
            f"Payloads over {QG_HEADING_OR_LIST_MIN_LENGTH} characters require a heading or list.",
        )
    if length > QG_MULTI_HEADING_MIN_LENGTH and header_count <= 1:
        _quality_add_hard(
            flags,
            hard_errors,
            reasons,
            "INSUFFICIENT_HEADINGS",
            f"Payloads over {QG_MULTI_HEADING_MIN_LENGTH} characters require more than one heading.",
        )
    return has_list, header_count


def _quality_score(md_res, words, has_list, header_count, flags):
    score = 0.50
    if header_count > 0:
        score += 0.15
        _quality_add_flag(flags, "HAS_HEADERS")
    if has_list:
        score += 0.10
        _quality_add_flag(flags, "HAS_LIST")
    msdi = md_res.get("msdi", 0.0)
    if msdi >= 0.35:
        score += 0.15
        _quality_add_flag(flags, "HIGH_MSDI")
    elif len(words) > 80 and msdi < 0.10:
        score -= 0.15
        _quality_add_flag(flags, "MONOLITHIC_TEXT_WALL")
    if md_res.get("untyped_blocks", 0) > 0:
        score -= 0.10
        _quality_add_flag(flags, "UNANNOTATED_CODE_BLOCKS")
    if md_res.get("has_header_skip", False):
        score -= 0.10
        _quality_add_flag(flags, "NON_HIERARCHICAL_HEADERS")
    return max(0.0, min(1.0, round(score, 2)))


def _quality_finalize(score, hard_errors, warnings):
    if hard_errors:
        status = "REJECT"
        if any(
            code in hard_errors
            for code in (
                "EMPTY_CONTENT",
                "SHORT_LENGTH",
                "INVALID_FORMAT",
                "BROKEN_MARKDOWN_SYNTAX",
            )
        ):
            score = 0.0
        elif "EXTREME_GENERATION_LOOP" in hard_errors:
            score = 0.10
    elif warnings:
        status = "WARN"
    else:
        status = "ACCEPT"
    return status, score


def evaluate_memory_quality(content: str) -> dict:
    """Evaluate content while reporting every blocking and advisory quality finding.

    ``quality_flags`` remains the compatibility view used by persistence and the viewer.  The
    explicit ``hard_errors`` and ``warnings`` lists make the decision auditable and avoid the old
    first-failure behavior.  Statistical heuristics are advisory except for unmistakable
    generation loops; malformed/empty/placeholder bodies and missing long-form structure remain
    hard failures.
    """
    text, length, flags, hard_errors, warnings, reasons = _quality_initial_findings(content)
    md_res = validate_markdown_structure(text)
    if not md_res["is_valid"]:
        _quality_add_hard(flags, hard_errors, reasons, md_res["error_flag"], md_res["reason"])
    words = re.findall(_WORD_TOKEN_RE, text.lower())
    entropy = calculate_shannon_entropy(text)
    _quality_statistical_findings(text, words, entropy, flags, hard_errors, warnings, reasons)
    has_list, header_count = _quality_structure_findings(
        text, length, md_res, flags, hard_errors, reasons
    )
    score = _quality_score(md_res, words, has_list, header_count, flags)
    status, score = _quality_finalize(score, hard_errors, warnings)
    return {
        "status": status,
        "quality_score": score,
        "quality_flags": flags,
        "hard_errors": hard_errors,
        "warnings": warnings,
        "reason": " ".join(reasons) if reasons else None,
    }
