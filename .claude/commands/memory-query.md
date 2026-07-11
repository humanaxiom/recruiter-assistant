# /memory-query <text>

Vector-search Neo4j graph memory for prior artifacts similar to `<text>`,
before implementing anything new — "have we solved something like this before?".

## Steps

1. Ensure the stack is up (`docker compose ps` — `api` and `neo4j` healthy)
2. Query the memory endpoint:
   ```bash
   curl -s "http://localhost:8000/memory/similar?q=<url-encoded text>&k=5" | jq
   ```
3. Present the top matches: kind · score · a content excerpt
4. If a close match exists, read its artifact before writing new code and
   reuse the prior approach; note the reuse in your plan
5. If nothing similar is found, say so and proceed with a fresh implementation

The same retrieval runs automatically inside each subagent via
`BaseAgent._memory_context()`; this command is the manual, ad-hoc entry point.
