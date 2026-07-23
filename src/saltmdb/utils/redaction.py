import re
import os
import logging

logger = logging.getLogger(__name__)

SECRET_PATTERNS = [
    r"\bghp_[a-zA-Z0-9]{36,}\b",                # GitHub personal access token (classic)
    r"\bgithub_pat_[a-zA-Z0-9_]{82,}\b",         # GitHub fine-grained token
    r"\bsk-ant-sid01-[a-zA-Z0-9_-]{20,}\b",     # Anthropic session key
    r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b",            # Anthropic API key
    r"\bsk-[a-zA-Z0-9_-]{48,}\b",                # OpenAI API key
    r"\bsk-proj-[a-zA-Z0-9_-]{20,}\b",           # OpenAI project key
    r"\b[a-zA-Z0-9_]{20,}:[a-zA-Z0-9_]{40,}\b",  # Generic API secret pattern (ID:Secret)
    r"\bAKIA[A-Z0-9]{16}\b",                     # AWS access key ID
    r"\b[M-Q][a-zA-Z0-9_\-]{23}\.[a-zA-Z0-9_\-]{6}\.[a-zA-Z0-9_\-]{27}\b" # Discord token
]

_FAST_PATH_PREFIXES = ("ghp_", "github_pat_", "sk-ant-", "sk-proj-", "sk-", "AKIA")

CUSTOM_REDACT_PATTERNS: list[str] = []
_compiled_regex: re.Pattern | None = None

def _rebuild_compiled_regex():
    global _compiled_regex
    all_patterns = SECRET_PATTERNS + CUSTOM_REDACT_PATTERNS
    _compiled_regex = re.compile("|".join(all_patterns), flags=re.IGNORECASE)

def load_custom_redact_patterns():
    """Load custom developer redaction rules from .saltmdb_redact if present."""
    global CUSTOM_REDACT_PATTERNS
    CUSTOM_REDACT_PATTERNS.clear()
    if os.path.exists(".saltmdb_redact"):
        try:
            with open(".saltmdb_redact", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        try:
                            re.compile(line)
                            CUSTOM_REDACT_PATTERNS.append(line)
                        except re.error as e:
                            logger.warning("Invalid regex pattern in .saltmdb_redact: '%s' (%s)", line, e)
        except Exception as e:
            logger.warning("Failed to read .saltmdb_redact: %s", e)
    _rebuild_compiled_regex()

# Initialize on module import
load_custom_redact_patterns()

def redact_secrets(text: str) -> str:
    """Scrub potential credentials and API keys from text using high-speed precompiled regex."""
    if not isinstance(text, str) or not text:
        return text
    # Fast path guard to bypass regex execution when no secret prefix exists
    if not any(prefix in text for prefix in _FAST_PATH_PREFIXES) and not CUSTOM_REDACT_PATTERNS:
        return text
    return _compiled_regex.sub("[REDACTED_SECRET]", text)
