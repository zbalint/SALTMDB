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
