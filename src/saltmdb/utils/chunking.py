def chunk_text(text: str, chunk_size: int, overlap: int) -> list[dict]:
    """Sliding-window text chunker with character offsets.

    Returns [{"text": str, "char_start": int, "char_end": int}, ...]. Empty/whitespace-only
    input returns []. Reuses the exact start/end stride math validated in
    scripts/benchmarking/benchmark_embedding_speed.py's original `_chunk_text` helper (start
    advances by chunk_size - overlap each iteration, loop continues while start < len(text)),
    extended to report offsets. Unlike a raw Python slice, char_end is explicitly clamped to
    len(text) rather than left as the raw (possibly out-of-range) start + chunk_size -- these
    offsets get persisted to a DB column now, so they must reflect the real slice bounds, not
    rely on slicing's silent clamping behavior.
    """
    if chunk_size <= overlap:
        raise ValueError(
            f"chunk_size ({chunk_size}) must be greater than overlap ({overlap}); "
            "otherwise the sliding window never advances and chunking never terminates."
        )
    if not text or not text.strip():
        return []

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append({"text": text[start:end], "char_start": start, "char_end": end})
        start += chunk_size - overlap
    return chunks
