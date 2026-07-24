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

    # Tier 2: Information-Theoretic Density Filters
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
    if len(words) > 30:
        ttr = calculate_ttr(text)
        if ttr < 0.20:
            flags.append("LOW_TTR")
            return {
                "status": "REJECT",
                "quality_score": 0.15,
                "quality_flags": flags,
                "reason": f"Type-Token Ratio too low ({ttr:.2f}) - boilerplate repetition detected."
            }

    # Tier 4: Technical Specificity & Structural Formatting Scoring
    score = 0.50
    if re.search(r"^#{1,6}\s+", text, re.MULTILINE):
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

