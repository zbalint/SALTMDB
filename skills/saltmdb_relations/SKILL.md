---
name: saltmdb_relations
description: "Guidelines for semantic dependency mapping, recursive relationship tracing, and resolving orphaned memory nodes."
---

# SALTMDB Graph Relations & Topology Management

This skill governs how memories are connected to build a semantic knowledge graph, enabling agents to run dependency analysis and fix isolated data.

## 1. When to Map Relations

Whenever you create or update a memory, ask yourself: *how does this relate to what is already stored?*
Establish links immediately using `store_relation(source_id, target_id, predicate)`.
* **Standard Predicates:**
  * `replaces`: Linking a new design or fix to the obsolete item it resolves.
  * `depends_on`: Linking a file, module, or package to its prerequisite configurations.
  * `implements`: Linking a concrete script/implementation to its higher-level design guide.
  * `part_of`: Linking semantic sub-sections back to their parent document anchor.

## 2. Dependency Tracing (Impact Analysis)

Before executing refactoring tasks or modifying a core service:
* **Trace Downstream:** Run `analyze_dependencies(root_entity_id)` starting from the component's memory ID.
* **Review Traversal:** This queries the database using a recursive SQL CTE to trace all downstream dependents. Inspect these memories to see which other components, tests, or configurations will be impacted by your changes.

## 3. Resolving Orphaned Memories

An "orphan" is a memory node with zero incoming or outgoing relation edges. Orphans are hard to discover and lack contextual grounding.
* **Scan for Orphans:** Run `detect_orphaned_memories(owner_id)` during system maintenance or bootstrap.
* **Apply Recommendations:** The tool compares tags of orphaned memories against other active memories and returns connection suggestions. Review these recommendations and execute `store_relation` calls to link orphans to their corresponding parent anchors or sibling components.
