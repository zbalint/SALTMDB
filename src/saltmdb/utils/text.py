import re

UUID_REGEX = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

def resolve_entity_id(conn, input_val: str) -> str | None:
    """Helper to flexibly resolve an entity ID from a raw UUID/ID, a status string containing a UUID, or an entity title."""
    if not input_val or not isinstance(input_val, str):
        return input_val
    input_val = input_val.strip()
    
    # 0. Check if input_val is already an exact entity ID in the database
    try:
        cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (input_val,))
        if cursor.fetchone():
            return input_val
    except Exception:
        pass
    
    # 1. Exact UUID pattern
    if UUID_REGEX.fullmatch(input_val):
        return input_val
        
    # 2. Status string containing UUID (e.g. 'Knowledge stored successfully with ID: <uuid>')
    match = UUID_REGEX.search(input_val)
    if match:
        return match.group(0)
        
    # 3. Entity title resolution
    try:
        cursor = conn.execute("SELECT id FROM entities WHERE title = ? AND status != 'archived' ORDER BY updated_at DESC LIMIT 1", (input_val,))
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
        
    return input_val

def extract_title_and_snippet(markdown_text: str):
    """Heuristic helper to extract a clean title and snippet from markdown text."""
    if not markdown_text:
        return "Untitled", ""
    lines = [line.strip() for line in markdown_text.splitlines() if line.strip()]
    title = "Untitled"
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
            
    if title == "Untitled" and lines:
        title = lines[0]
        if len(title) > 60:
            title = title[:57] + "..."
            
    text_lines = []
    for line in lines:
        if not line.startswith("#"):
            text_lines.append(line)
            if len(text_lines) >= 3:
                break
                
    snippet = " ".join(text_lines)
    if len(snippet) > 150:
        snippet = snippet[:147] + "..."
    return title, snippet

def sanitize_fts_query(query: str) -> str:
    """Sanitizes raw query string for FTS5, escaping special characters and balancing quotes."""
    if not query:
        return ""
    if query.count('"') % 2 != 0:
        query = query.replace('"', ' ')
    cleaned = re.sub(r'[\-+<>:/*\\?^$|#@`~!%&(){}[\]]', ' ', query)
    return " ".join(cleaned.split())

def normalize_search_query(query: str) -> str:
    """Normalizes input search queries by lowercasing and stripping punctuation."""
    if not query:
        return ""
    q = query.lower()
    q = re.sub(r'[^\w\s]', ' ', q)
    return " ".join(q.split())
