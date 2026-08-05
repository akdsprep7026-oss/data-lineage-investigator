"""MCP (Model Context Protocol) servers wrapping the tools the
investigation agents use, plus the client the graph nodes call them
through.

Before Step 8 the specialist nodes reached their tools by importing
Python functions directly (`run_basic_checks`, `retrieve`). That works,
but it hard-wires the agents to this process: the tools can't be reused
by any other MCP-speaking client, and there's no declared schema for
what an agent is allowed to ask for. Step 8 puts the two tools an agent
uses to *look at the world* behind MCP servers instead:

    postgres_server.py   read-only access to the sandbox warehouse
                         (get_schema, check_row_count, query_table)
    retrieval_server.py  similarity search over the Chroma index
                         (retrieve)

Each server is a standalone process speaking MCP over stdio, so it can
be pointed at any MCP client (Claude Desktop, Cursor, etc.), not just
this codebase:

    python -m app.mcp_servers.postgres_server
    python -m app.mcp_servers.retrieval_server

client.py is what the graph nodes use: it spawns each server on first
use, keeps the session alive, and turns an async MCP call into an
ordinary synchronous function call.

This is deliberately an architecture change and not a behaviour change.
The tools do exactly what the direct function calls did, and the
Step 7 evaluation is re-run after the refactor to confirm the
investigation results are unchanged (see eval_report_step8_mcp.md).
"""
