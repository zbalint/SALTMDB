import sqlite3
import json
import os
from saltmdb_server import get_db_path

def main():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    
    # Resolve path dynamically using expansion and env overrides to avoid hardcoded absolute usernames
    brain_dir = os.environ.get("GEMINI_BRAIN_DIR", os.path.expanduser(r"~/.gemini/antigravity-cli/brain/a4a383c5-c4fe-4ecb-a1db-03205bf776f9"))
    report_path = os.path.join(brain_dir, "database_review.md")
    
    try:
        # Fetch entities
        cursor = conn.execute("""
            SELECT id, title, owner_id, scope, status, weight, is_core, valid_from, valid_to, full_content 
            FROM entities
        """)
        entities = []
        for r in cursor.fetchall():
            entities.append({
                "id": r[0],
                "title": r[1],
                "owner_id": r[2],
                "scope": r[3],
                "status": r[4],
                "weight": r[5],
                "is_core": bool(r[6]),
                "valid_from": r[7],
                "valid_to": r[8],
                "content": r[9]
            })
            
        # Fetch relations
        cursor = conn.execute("""
            SELECT r.id, e1.title, e2.title, r.predicate, r.valid_from, r.valid_to, r.source_id, r.target_id 
            FROM relations r
            JOIN entities e1 ON r.source_id = e1.id
            JOIN entities e2 ON r.target_id = e2.id
        """)
        relations = []
        for r in cursor.fetchall():
            relations.append({
                "id": r[0],
                "source": r[1],
                "target": r[2],
                "predicate": r[3],
                "valid_from": r[4],
                "valid_to": r[5],
                "source_id": r[6],
                "target_id": r[7]
            })
            
        # Fetch events
        cursor = conn.execute("SELECT timestamp, agent_id, type, content FROM events ORDER BY timestamp ASC")
        events = []
        for r in cursor.fetchall():
            events.append({
                "timestamp": r[0],
                "agent_id": r[1],
                "type": r[2],
                "content": r[3]
            })
            
        # Filter groups
        core_memories = [e for e in entities if e["is_core"]]
        consolidated = [e for e in entities if e["status"] == "consolidated" and not e["is_core"]]
        raw_skills = [e for e in entities if e["status"] == "raw" and "[skill]" in e["title"]]
        raw_subagents = [e for e in entities if e["status"] == "raw" and "[subagent]" in e["title"]]
        other_raw = [e for e in entities if e["status"] == "raw" and "[skill]" not in e["title"] and "[subagent]" not in e["title"]]

        # Build Markdown Report
        md = []
        md.append("# SALTMDB Database Real-World Usage Review\n")
        md.append("This document analyzes the current schema state, memory topology, and multi-agent partitioning constraints populated in the database by the other agent.\n")
        
        md.append("## 📊 Database Metrics Summary\n")
        md.append("| Metric | Count | Description |")
        md.append("| :--- | :---: | :--- |")
        md.append(f"| **Core Persona Rules** | {len(core_memories)} | Loaded into agent prompt context during bootstrap |")
        md.append(f"| **Consolidated Memory Nodes** | {len(consolidated)} | Distilled structural facts compiled from prior sessions |")
        md.append(f"| **Raw Skill Files** | {len(raw_skills)} | Executable workflows, checks, and commands |")
        md.append(f"| **Raw Subagent Profiles** | {len(raw_subagents)} | Task-specific agent roles and constraints |")
        md.append(f"| **Other Raw Logs** | {len(other_raw)} | Sequential session logs and context records |")
        md.append(f"| **Relational Topology Links** | {len(relations)} | Directed edges representing node dependencies |")
        md.append(f"| **Immutable Audit Log Events** | {len(events)} | Historical ledger of structural operations |")
        md.append("\n---\n")

        # Core memories section
        md.append("## 👑 Core Prompt Guidelines (is_core = 1)\n")
        md.append("These entities are automatically loaded on bootstrap to establish rules of engagement:\n")
        md.append("| Title | Owner | Weight | Scope |")
        md.append("| :--- | :--- | :---: | :--- |")
        for m in core_memories:
            md.append(f"| `{m['title']}` | `{m['owner_id']}` | **{m['weight']}** | `{m['scope']}` |")
        md.append("\n---\n")

        # Multi-agent partitioning section
        md.append("## 👥 Multi-Agent Partitioning & Isolation\n")
        md.append("We observe strict namespace separation by `owner_id`. This allows different agent identities (`tea` vs `ops`) to operate inside the same database file without context leakage:\n")
        
        # Group by owner
        owners = {}
        for e in entities:
            owners.setdefault(e["owner_id"], []).append(e["title"])
            
        for owner, titles in owners.items():
            md.append(f"### 👤 Owner ID: `{owner}` ({len(titles)} memories)\n")
            md.append("Sample of stored memories:")
            for title in sorted(titles)[:6]:
                md.append(f"- `{title}`")
            if len(titles) > 6:
                md.append(f"- *...and {len(titles) - 6} more*")
            md.append("")
        md.append("---\n")

        # Relational topology mapping
        md.append("## 🕸️ Relational Topology Graph\n")
        md.append("By linking memories using `store_relation`, the agent constructs a queryable graph. Downstream dependency trees are traversed recursively during refactors:\n")
        
        # Build a Mermaid diagram of dependencies
        md.append("```mermaid")
        md.append("graph TD")
        # Define some styles
        md.append("    classDef core fill:#f59e0b,stroke:#d97706,stroke-width:2px,color:#fff;")
        md.append("    classDef consolidated fill:#1e293b,stroke:#475569,stroke-width:1px,color:#f3f4f6;")
        md.append("    classDef raw fill:#0f172a,stroke:#334155,stroke-width:1px,color:#94a3b8,stroke-dasharray: 5 5;")
        
        # Group entities for diagram definitions
        for e in entities:
            clean_title = e["title"].replace("[", "").replace("]", "").replace("'", "")
            node_id = f"node_{e['id'][:8]}"
            klass = "core" if e["is_core"] else ("consolidated" if e["status"] == "consolidated" else "raw")
            md.append(f'    {node_id}["{clean_title}"]::: {klass}')
            
        for r in relations:
            src_id = f"node_{r['source_id'][:8]}"
            tgt_id = f"node_{r['target_id'][:8]}"
            md.append(f'    {src_id} -->|"{r["predicate"]}"| {tgt_id}')
            
        md.append("```\n")
        md.append("---\n")

        # Consolidated vs raw details
        md.append("## 🔍 Deep-Dive: Memory States & Lifecycle\n")
        md.append("### 1. Consolidated Memories (`status = 'consolidated'`)\n")
        md.append("These are structural facts synthesized by the agent at the end of sessions:\n")
        for c in consolidated[:4]:
            md.append(f"#### 📄 `{c['title']}` (ID: `{c['id'][:8]}...`)\n")
            md.append(f"> **Metadata:** Owner: `{c['owner_id']}` | Scope: `{c['scope']}` | Weight: `{c['weight']}`\n")
            md.append(c["content"])
            md.append("")
        md.append("\n")
        
        md.append("### 2. Working Skills & Working Subagents (`status = 'raw'`)\n")
        md.append("Raw memories represent temporary context, sequential workflow scripts, and specialized subagent catalogs. For example:\n")
        md.append("| Title | Domain Owner | Created/Updated At |")
        md.append("| :--- | :--- | :--- |")
        for s in (raw_skills + raw_subagents)[:8]:
            md.append(f"| `{s['title']}` | `{s['owner_id']}` | `{s['valid_from']}` |")
        md.append("\n---\n")

        # Audit Ledger Events
        md.append("## 📜 Audit Events Ledger\n")
        md.append("Immutable entries recording structural migrations and administrative locks executions:\n")
        md.append("| Timestamp | Agent | Action Type | Log Payload |")
        md.append("| :--- | :--- | :--- | :--- |")
        for ev in events:
            md.append(f"| `{ev['timestamp']}` | `{ev['agent_id']}` | `{ev['type']}` | `{ev['content']}` |")

        # Write to File
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
            
        print(f"Report written successfully to: {report_path}")
        
    except Exception as e:
        print(f"Error during report compilation: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
