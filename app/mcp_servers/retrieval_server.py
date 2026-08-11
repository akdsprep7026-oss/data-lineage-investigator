"""MCP server exposing the Step 4 retrieval layer as an MCP tool.

Run it as a standalone MCP server over stdio:

    python -m app.mcp_servers.retrieval_server

One tool is exposed:

    retrieve(query, filter_type, n_results)

It is a thin wrapper over app.retrieval.retriever.retrieve() -- the
similarity search over the Chroma index of the sandbox's SQL models,
pipeline jobs and dashboard widgets. The wrapper deliberately adds no
logic of its own beyond packaging the hits into a JSON object, so the
tool returns exactly what a direct call to the Step 4 function returns.

Building the index stays outside MCP on purpose. `ingest()` writes to
the vector store, and this server exists to give agents a read-only
window onto it; re-indexing after an incident is applied is a fixture
step performed by whoever set up the scenario (see
app/graph/evaluate.py), not something an investigating agent should be
able to trigger.

One operational note: embeddings are selected by
app/retrieval/embeddings.py (`EMBEDDING_PROVIDER` / `GOOGLE_API_KEY`).
Ingest persists the chosen function on the Chroma collection; query
reuses that persisted function so an ONNX-built index is never queried
with Gemini (and vice versa). `app/mcp_servers/client.py` still mirrors
`GOOGLE_API_KEY` / `EMBEDDING_PROVIDER` into this process so a fresh
collection created here would match the parent's config.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from app.retrieval.retriever import retrieve as retrieve_documents

SERVER_NAME = "sandbox-retrieval"

DEFAULT_N_RESULTS = 5

# The metadata "type" values the index actually carries (see
# app/retrieval/ingest.py). Documented in the tool schema so a client
# knows what's worth filtering on.
DOCUMENT_TYPES = ("sql_model", "pipeline_job", "dashboard_widget")

server = MCPServer(
    SERVER_NAME,
    instructions=(
        "Similarity search over the sandbox's SQL models, pipeline job "
        "definitions and dashboard configuration. Use it to find which "
        "artifacts are relevant to a reported data issue."
    ),
)


@server.tool()
def retrieve(
    query: str,
    filter_type: Optional[str] = None,
    n_results: int = DEFAULT_N_RESULTS,
) -> dict[str, Any]:
    """Search the sandbox knowledge index for artifacts matching a query.

    MCP tool schema
    ---------------
    Input:
      query       (string, required) -- natural-language search text,
                  e.g. the reported issue description.
      filter_type (string, optional) -- restrict results to one kind of
                  document: "sql_model", "pipeline_job" or
                  "dashboard_widget". Omit to search across all three.
      n_results   (integer, optional, default 5) -- maximum number of
                  hits to return.

    Output (JSON object):
      query        (string)  the query that was searched.
      filter_type  (string)  the filter applied, or null.
      result_count (integer) len(results).
      results      (array)   hits ordered most-relevant first, each
                             {document, metadata, distance}:
                               document -- the indexed text (the SQL
                                 model's statement, the job definition,
                                 or the widget definition).
                               metadata -- {type, ...} plus the fields
                                 for that type: sql_model carries
                                 table_name/file_path/chunk_index,
                                 pipeline_job carries job_name,
                                 dashboard_widget carries
                                 widget_name/source_table.
                               distance -- similarity distance, lower is
                                 more similar.

    Returns an empty `results` array if the index hasn't been built yet;
    that's a "nothing indexed" answer rather than an error.
    """
    hits = retrieve_documents(query, filter_type=filter_type, n_results=n_results)
    return {
        "query": query,
        "filter_type": filter_type,
        "result_count": len(hits),
        "results": hits,
    }


if __name__ == "__main__":
    server.run("stdio")
