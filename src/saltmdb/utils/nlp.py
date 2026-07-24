import re

STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "can't", "cannot", "could",
    "did", "do", "does", "doing", "don't", "down", "during", "each", "few", "for",
    "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself",
    "just", "me", "more", "most", "my", "myself", "no", "nor", "not", "of", "off",
    "on", "once", "only", "or", "other", "our", "ours", "out", "over", "own",
    "same", "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why", "with",
    "would", "you", "your", "yours", "yourself", "yourselves"
}

def stem(word: str) -> str:
    """Basic English suffix stemming for fuzzy matching."""
    w = word.lower()
    for suffix in ("ing", "edly", "ed", "es", "s", "ly", "ment", "tion", "ness", "ity", "al"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[:-len(suffix)]
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

import math

FLUFF_PATTERN = re.compile(
    r"^(ok|done|thanks|got it|i have|modified the file|sure|completed|consolidated these files|consolidated|consolidated notes|merged summary)[\.!]?$",
    re.IGNORECASE
)

def calculate_shannon_entropy(text: str) -> float:
    """Calculate character-level Shannon entropy in bits per character."""
    if not text:
        return 0.0
    length = len(text)
    freqs = {}
    for char in text:
        freqs[char] = freqs.get(char, 0) + 1
    entropy = 0.0
    for count in freqs.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy

def calculate_ttr(text: str) -> float:
    """Calculate Type-Token Ratio (Lexical Diversity) based on word tokens."""
    words = re.findall(r"\b\w+\b", text.lower())
    if not words:
        return 0.0
    unique_words = set(words)
    return len(unique_words) / len(words)

def calculate_symbol_ratio(text: str) -> float:
    """Calculate ratio of punctuation/symbols to alphanumeric characters."""
    alpha_count = sum(1 for c in text if c.isalnum())
    if alpha_count == 0:
        return 1.0 if text else 0.0
    symbol_count = sum(1 for c in text if not c.isalnum() and not c.isspace())
    return symbol_count / alpha_count

def calculate_technical_specificity(text: str) -> float:
    """Calculate ratio of specific technical identifiers to total word count."""
    words = re.findall(r"\b\w+\b", text)
    if not words:
        return 0.0
    tech_identifiers = re.findall(r"\b[a-z0-9_]+[A-Z0-9_\.][a-zA-Z0-9_]*\b|\b[A-Z0-9_]{2,}\b|\b\w+\.(py|js|ts|json|md|rs|go|c|cpp|h|yml|yaml|sql|sh|db)\b", text)
    return len(tech_identifiers) / len(words)

def calculate_ngram_duplicate_ratio(text: str, n: int) -> float:
    """Calculate the ratio of duplicate word N-grams in text."""
    words = re.findall(r"\b\w+\b", text.lower())
    if len(words) < n:
        return 0.0
    ngrams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    if not ngrams:
        return 0.0
    unique_count = len(set(ngrams))
    total_count = len(ngrams)
    return 1.0 - (unique_count / total_count)

from saltmdb.utils.perplexity import calculate_transition_perplexity

def extract_prose_content(text: str) -> str:
    """
    Strips code fences, inline backticks, file paths, and URLs to isolate pure prose text.
    Prevents false readability or perplexity quality rejections on raw technical logs.
    """
    if not text:
        return ""
    # Strip triple-backtick code blocks
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)
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
    # Sentences delimited by ., !, or ?
    sentences = [s for s in re.split(r"[.!?]+", prose_text) if s.strip()]
    sentence_count = max(1, len(sentences))
    
    L = (letter_count / word_count) * 100.0
    S = (sentence_count / word_count) * 100.0
    cli = (0.0588 * L) - (0.296 * S) - 15.8
    return round(cli, 2)

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
    formatted_lines = []
    in_code_block = False
    block_buffer = []
    
    for line in lines:
        if line.startswith("```"):
            if not in_code_block:
                in_code_block = True
                block_lang = line[3:].strip()
                block_buffer = [("fence", block_lang)]
            else:
                in_code_block = False
                fence_lang = block_buffer[0][1]
                code_lines = [l for _, l in block_buffer[1:]]
                code_text = "\n".join(code_lines)
                
                # If fence was untyped, apply heuristic keyword detection
                if not fence_lang:
                    if re.search(r"\b(def|import|from|class|elif|self|print)\b", code_text):
                        fence_lang = "python"
                    elif re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|FROM|WHERE)\b", code_text, re.IGNORECASE):
                        fence_lang = "sql"
                    elif re.search(r"^\s*[\{\[]", code_text) and ("\"" in code_text or ":" in code_text):
                        fence_lang = "json"
                    elif re.search(r"\b(function|const|let|var|console\.log|export|import)\b", code_text):
                        fence_lang = "javascript"
                
                formatted_lines.append(f"```{fence_lang}")
                formatted_lines.extend(code_lines)
                formatted_lines.append("```")
                block_buffer = []
        else:
            if in_code_block:
                block_buffer.append(("line", line))
            else:
                formatted_lines.append(line)
                
    # If block was left unclosed, append buffered lines
    if in_code_block and block_buffer:
        fence_lang = block_buffer[0][1]
        formatted_lines.append(f"```{fence_lang}")
        formatted_lines.extend([l for _, l in block_buffer[1:]])
        
    result = "\n".join(formatted_lines)
    # Collapse 3+ consecutive newlines to 2 newlines
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result

def validate_markdown_structure(text: str) -> dict:
    """
    Validates Markdown syntax integrity, header hierarchy, code block annotations, and MSDI.
    """
    # 1. Syntax Integrity Checks
    # Balanced code fences
    fence_count = len(re.findall(r"^```", text, re.MULTILINE))
    if fence_count % 2 != 0:
        return {
            "is_valid": False,
            "error_flag": "BROKEN_MARKDOWN_SYNTAX",
            "reason": "Unclosed Markdown code block detected (odd count of ``` markers)."
        }
        
    # Table Column Symmetry check
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            pipe_count = stripped.count("|")
            if pipe_count < 3: # Must have at least start, divider, end
                return {
                    "is_valid": False,
                    "error_flag": "BROKEN_MARKDOWN_SYNTAX",
                    "reason": "Malformed Markdown table row detected (insufficient pipe separators)."
                }

    # 2. Header Hierarchy & Progression Check
    headers = re.findall(r"^(#{1,6})\s+(.+)$", text, re.MULTILINE)
    header_levels = [len(h[0]) for h in headers]
    has_skip = False
    for i in range(len(header_levels) - 1):
        if header_levels[i + 1] > header_levels[i] + 1:
            has_skip = True
            break

    # 3. Untyped Code Fences Check
    code_fences = re.findall(r"^```(\w*)", text, re.MULTILINE)
    # Filter only opening fences (even index if formatted properly)
    untyped_blocks = 0
    for i in range(0, len(code_fences), 2):
        if not code_fences[i].strip():
            untyped_blocks += 1

    # 4. MSDI (Markdown Structural Density Index) Calculation
    words = re.findall(r"\b\w+\b", text)
    total_words = len(words)
    
    header_words = sum(len(re.findall(r"\b\w+\b", h[1])) for h in headers)
    
    list_items = re.findall(r"^\s*[\-\*\d\.]+\s+(.+)$", text, re.MULTILINE)
    list_item_words = sum(len(re.findall(r"\b\w+\b", item)) for item in list_items)
    
    code_blocks = re.findall(r"```[\s\S]*?```", text)
    code_block_words = sum(len(re.findall(r"\b\w+\b", cb)) for cb in code_blocks)
    
    msdi = (header_words + list_item_words + code_block_words) / total_words if total_words > 0 else 0.0

    return {
        "is_valid": True,
        "header_count": len(headers),
        "has_header_skip": has_skip,
        "untyped_blocks": untyped_blocks,
        "msdi": round(msdi, 3)
    }

def evaluate_memory_quality(content: str, title: str = None) -> dict:
    """
    Evaluates memory content quality across Tier 1, Tier 2, and Tier 4 quality gates.
    Returns dict with status ('ACCEPT', 'WARN', 'REJECT'), quality_score (0.0 - 1.0), and quality_flags.
    """
    flags = []
    text = (content or "").strip()
    
    # Tier 1: Boundary & Fluff Scanners
    if len(text) < 20:
        flags.append("SHORT_LENGTH")
        return {
            "status": "REJECT",
            "quality_score": 0.0,
            "quality_flags": flags,
            "reason": f"Payload string length ({len(text)} chars) below minimum threshold of 20 characters."
        }
        
    if FLUFF_PATTERN.match(text):
        flags.append("CONVERSATIONAL_FLUFF")
        return {
            "status": "REJECT",
            "quality_score": 0.0,
            "quality_flags": flags,
            "reason": "Conversational fluff phrase detected."
        }
        
    symbol_ratio = calculate_symbol_ratio(text)
    if symbol_ratio > 0.35:
        flags.append("HIGH_SYMBOL_RATIO")
        return {
            "status": "REJECT",
            "quality_score": 0.05,
            "quality_flags": flags,
            "reason": f"Symbol-to-alpha ratio ({symbol_ratio:.2f}) exceeds threshold of 0.35."
        }
        
    tier1_warn = False
    if len(text) > 8000:
        flags.append("OVERSIZED_PAYLOAD")
        tier1_warn = True

    # Tier 1.5: Markdown Syntax Integrity Verification
    md_res = validate_markdown_structure(text)
    if not md_res["is_valid"]:
        flags.append(md_res["error_flag"])
        return {
            "status": "REJECT",
            "quality_score": 0.0,
            "quality_flags": flags,
            "reason": md_res["reason"]
        }

    # Tier 2: Information-Theoretic & Sequence Density Filters
    entropy = calculate_shannon_entropy(text)
    if entropy < 2.5:
        flags.append("LOW_ENTROPY")
        return {
            "status": "REJECT",
            "quality_score": 0.10,
            "quality_flags": flags,
            "reason": f"Character entropy too low ({entropy:.2f} bits/char) - repetitive text loop detected."
        }
    elif entropy > 5.3:
        flags.append("HIGH_ENTROPY")
        tier1_warn = True

    words = re.findall(r"\b\w+\b", text.lower())
    if len(words) >= 20:
        dup_3gram = calculate_ngram_duplicate_ratio(text, 3)
        if dup_3gram > 0.30:
            flags.append("HIGH_3GRAM_REPETITION")
            return {
                "status": "REJECT",
                "quality_score": 0.10,
                "quality_flags": flags,
                "reason": f"High 3-gram sequence repetition detected ({dup_3gram:.1%})."
            }
            
        dup_5gram = calculate_ngram_duplicate_ratio(text, 5)
        if dup_5gram > 0.20:
            flags.append("HIGH_5GRAM_REPETITION")
            return {
                "status": "REJECT",
                "quality_score": 0.10,
                "quality_flags": flags,
                "reason": f"High 5-gram sequence repetition detected ({dup_5gram:.1%})."
            }

    if len(words) > 30:
        ttr = calculate_ttr(text)
        if ttr < 0.35:
            flags.append("LOW_TTR")
            return {
                "status": "REJECT",
                "quality_score": 0.15,
                "quality_flags": flags,
                "reason": f"Type-Token Ratio too low ({ttr:.2f}) - boilerplate repetition detected."
            }

    # Extract pure prose content for Readability and Perplexity evaluation
    prose_content = extract_prose_content(text)
    prose_words = re.findall(r"\b[a-zA-Z0-9_-]+\b", prose_content)

    # Bigram Transition Perplexity Gate (Word-Salad Protection)
    if len(prose_words) > 25:
        perp_res = calculate_transition_perplexity(prose_content)
        if perp_res["validity_ratio"] < 0.15:
            flags.append("WORD_SALAD_PERPLEXITY")
            return {
                "status": "REJECT",
                "quality_score": 0.10,
                "quality_flags": flags,
                "reason": f"Nonsensical word-salad sequence detected (valid transition ratio {perp_res['validity_ratio']:.1%} < 15%)."
            }

    # Coleman-Liau Syntactic Readability Bounds
    if len(prose_words) > 30:
        cli = calculate_coleman_liau_index(prose_content)
        if cli < 2.0 or cli > 26.0:
            flags.append("EXTREME_READABILITY_BOUNDS")
            return {
                "status": "REJECT",
                "quality_score": 0.15,
                "quality_flags": flags,
                "reason": f"Coleman-Liau readability index ({cli:.1f}) outside reasonable bounds [2.0, 26.0]."
            }

    # Tier 4: Technical Specificity & Structural Formatting Scoring
    score = 0.50
    if md_res["header_count"] > 0:
        score += 0.15
        flags.append("HAS_HEADERS")
        
    if "`" in text:
        score += 0.15
        flags.append("HAS_CODE")
        
    if re.search(r"^\s*[\-\*\d\.]+\s+", text, re.MULTILINE):
        score += 0.10
        flags.append("HAS_LIST")
        
    if re.search(r"\b[\w/-]+\.(py|rs|js|ts|json|md|sql|yml|yaml|c|cpp|h|sh)\b|\b[a-z0-9_]+_[a-z0-9_]+\b", text):
        score += 0.10
        flags.append("HAS_PATHS_OR_IDENTIFIERS")

    # MSDI Structure Density Score
    msdi = md_res["msdi"]
    if msdi >= 0.35:
        score += 0.15
        flags.append("HIGH_MSDI")
    elif len(words) > 80 and msdi < 0.10:
        score -= 0.15
        flags.append("MONOLITHIC_TEXT_WALL")

    if md_res["untyped_blocks"] > 0:
        score -= 0.10
        flags.append("UNANNOTATED_CODE_BLOCKS")

    if md_res["has_header_skip"]:
        score -= 0.10
        flags.append("NON_HIERARCHICAL_HEADERS")
        
    if len(words) > 25:
        spec_ratio = calculate_technical_specificity(text)
        if spec_ratio < 0.02:
            score -= 0.15
            flags.append("LOW_SPECIFICITY")

    score = max(0.0, min(1.0, round(score, 2)))
    status = "WARN" if tier1_warn else "ACCEPT"
    
    return {
        "status": status,
        "quality_score": score,
        "quality_flags": flags,
        "reason": None
    }

