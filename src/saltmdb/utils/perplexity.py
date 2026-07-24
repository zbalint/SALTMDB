"""
Zero-dependency Pure-Python Bigram Transition Matrix and Perplexity Gate for SALTMDB.
Evaluates word-pair transition validity to detect nonsensical word-salad payloads.
"""
import re

# Comprehensive bigram transition set combining English stop-word transitions,
# standard technical verbs/prepositions, and domain-specific tokens.
TOP_BIGRAM_TRANSITIONS = {
    # Standard English transitions
    ("the", "system"), ("the", "database"), ("the", "model"), ("the", "file"),
    ("the", "code"), ("the", "user"), ("the", "query"), ("the", "data"),
    ("the", "following"), ("the", "memory"), ("the", "vector"), ("the", "search"),
    ("the", "table"), ("the", "index"), ("the", "service"), ("the", "function"),
    ("the", "api"), ("the", "result"), ("the", "event"), ("the", "tag"),
    ("in", "the"), ("on", "the"), ("for", "the"), ("to", "the"), ("with", "the"),
    ("by", "the"), ("from", "the"), ("at", "the"), ("into", "the"), ("of", "the"),
    ("this", "is"), ("that", "is"), ("it", "is"), ("there", "is"), ("there", "are"),
    ("to", "be"), ("should", "be"), ("can", "be"), ("will", "be"), ("must", "be"),
    ("used", "to"), ("used", "for"), ("used", "in"), ("based", "on"), ("according", "to"),
    ("such", "as"), ("as", "well"), ("well", "as"), ("due", "to"), ("in", "order"),
    ("order", "to"), ("so", "that"), ("such", "that"), ("set", "to"), ("refers", "to"),
    ("consists", "of"), ("composed", "of"), ("designed", "to"), ("built", "with"),
    ("implemented", "in"), ("written", "in"), ("created", "by"), ("stored", "in"),
    ("saved", "to"), ("added", "to"), ("removed", "from"), ("retrieved", "from"),
    ("searched", "by"), ("filtered", "by"), ("sorted", "by"), ("grouped", "by"),
    
    # Technical & Domain Vocabulary Transitions (SALTMDB domain)
    ("sqlite", "database"), ("sqlite", "fts5"), ("sqlite", "vec"), ("vector", "search"),
    ("dense", "vector"), ("hybrid", "search"), ("rrf", "search"), ("onnx", "model"),
    ("onnx", "runtime"), ("fastembed", "model"), ("mcp", "server"), ("mcp", "tool"),
    ("database", "schema"), ("database", "viewer"), ("quality", "gate"), ("quality", "score"),
    ("quality", "status"), ("text", "quality"), ("shannon", "entropy"), ("type", "token"),
    ("token", "ratio"), ("sequence", "repetition"), ("markdown", "structure"),
    ("markdown", "syntax"), ("markdown", "density"), ("code", "block"), ("code", "fence"),
    ("exact", "hash"), ("sha", "256"), ("content", "hash"), ("duplicate", "detection"),
    ("vector", "cluster"), ("consolidation", "request"), ("librarian", "service"),
    ("event", "log"), ("knowledge", "graph"), ("relation", "edge"), ("supersedes", "relation"),
    ("derived", "from"), ("consolidated", "from"), ("temporal", "history"), ("scd", "type"),
    ("rest", "api"), ("http", "request"), ("json", "payload"), ("async", "task"),
    ("python", "module"), ("unit", "test"), ("test", "suite"), ("function", "call"),
    ("parameter", "alias"), ("metadata", "filter"), ("context", "id"), ("owner", "id"),
    ("is", "core"), ("scope", "shared"), ("scope", "private"), ("active", "memories"),
    
    # Common verb-noun & noun-noun technical transitions
    ("returns", "a"), ("returns", "the"), ("accepts", "a"), ("accepts", "the"),
    ("takes", "a"), ("takes", "the"), ("provides", "a"), ("provides", "the"),
    ("creates", "a"), ("creates", "the"), ("updates", "the"), ("deletes", "the"),
    ("fetches", "the"), ("executes", "the"), ("parses", "the"), ("evaluates", "the"),
    ("verifies", "the"), ("validates", "the"), ("calculates", "the"), ("extracts", "the"),
    ("data", "structure"), ("source", "code"), ("binary", "file"), ("plain", "text"),
    ("user", "request"), ("system", "prompt"), ("error", "message"), ("log", "file")
}

def normalize_text_for_perplexity(text: str) -> list[str]:
    """
    Normalizes text into clean lowercase token array.
    Strips non-alphanumeric punctuation except hyphens and underscores.
    """
    if not text:
        return []
    # Replace non-alphanumeric characters (except hyphens and underscores) with spaces
    cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
    # Split into words
    words = re.findall(r"\b[a-z0-9_-]+\b", cleaned)
    return words

def calculate_transition_perplexity(prose_text: str) -> dict:
    """
    Calculates word-pair transition validity on prose text.
    Returns dictionary with transition count, recognized count, and validity ratio.
    """
    words = normalize_text_for_perplexity(prose_text)
    if len(words) < 2:
        return {
            "total_bigrams": 0,
            "valid_bigrams": 0,
            "validity_ratio": 1.0
        }
        
    total_bigrams = len(words) - 1
    valid_bigrams = 0
    
    for i in range(total_bigrams):
        w1, w2 = words[i], words[i+1]
        pair = (w1, w2)
        if pair in TOP_BIGRAM_TRANSITIONS:
            valid_bigrams += 1
        else:
            # Secondary check: if both words are known technical/structural tokens or share structural link
            if len(w1) > 2 and len(w2) > 2 and (
                w1 in {"the", "a", "an", "in", "on", "at", "for", "to", "of", "with", "by", "and", "or", "is", "are"} or
                w2 in {"the", "a", "an", "in", "on", "at", "for", "to", "of", "with", "by", "and", "or", "is", "are"}
            ):
                valid_bigrams += 1

    validity_ratio = valid_bigrams / total_bigrams if total_bigrams > 0 else 1.0
    return {
        "total_bigrams": total_bigrams,
        "valid_bigrams": valid_bigrams,
        "validity_ratio": round(validity_ratio, 3)
    }
