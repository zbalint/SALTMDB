### SALTMDB Architecture & Retrieval Enhancement Report

| Enhancement Target | Reason | Implementation Example |
| --- | --- | --- |
| **Configure FTS5 Porter Stemmer** | Integrating the `porter` tokenizer reduces words to root forms, natively capturing pluralizations and verb tense variations without relying on the agent to generate synonyms. | `CREATE VIRTUAL TABLE entities_fts USING fts5(title, content, tokenize='porter');` |
| **Index Metadata for Hidden Aliases** | Allows the agent to inject expected search queries invisibly, separating machine-readable search pathways from human-readable markdown content. | The agent populates a `search_aliases` array in the JSON payload, and the FTS5 index is updated to parse the `metadata` column. |
| **Enforce Explicit Noun Usage** | FTS5 BM25 cannot traverse semantic context or resolve pronouns; exact keywords must be present in the text to register a search hit. | The agent is prompted to write "The Nginx configuration caused a timeout" instead of "It caused a timeout." |
| **Front-Load Keywords in Titles** | SALTMDB applies a 10:1 BM25 weight to the title field. Placing specific technical nouns here significantly boosts the mathematical ranking for those terms. | The agent assigns the title `Docker Container Nginx 500 Gateway Timeout` instead of `Server Error`. |
| **Integrate Relational PageRank** | Memories frequently linked to via the `store_relation` tool act as foundational facts. Boosting their score utilizes the existing graph for ranking validation. | The `search_memory` tool multiplies the base BM25 score by `1.0 + (0.05 * incoming_edges)`. |
| **Automate Tag Expansion** | `[Inference]` Based on observed LLM behavior patterns, agents often default to a narrow categorical scope. Explicitly prompting for broader tags increases the keyword surface area. | The agent stores a memory using the tags `#auth_error`, `#jwt_token`, `#login_flow`, and `#api_security`. |
