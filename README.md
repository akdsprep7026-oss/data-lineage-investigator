# Data Lineage Investigator

Data Lineage Investigator is an AI-powered agent system that traces the origin, transformation, and flow of data across a data pipeline or database, helping engineers and analysts answer questions like "where did this value come from?" and "what downstream tables/reports does this column affect?" by combining a LangGraph-orchestrated multi-agent workflow, retrieval over schema/documentation stores, and direct querying of a Postgres-backed database through a FastAPI service. (More details to be added.)

## Setup

1. Create and activate a virtual environment (Python 3.11+):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # macOS/Linux
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

4. Run the API:

   ```bash
   uvicorn app.api.main:app --reload
   ```

5. Check the health endpoint at [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

6. Run the frontend (separate terminal):

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

   Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite proxies `/investigations` to the API.

## LLM providers

The `sql_analysis` and `root_cause` steps call an LLM. Which one is set
by `LLM_PROVIDER` in `.env`:

| `LLM_PROVIDER` | Uses | Notes |
| --- | --- | --- |
| `gemini` (default) | `GOOGLE_API_KEY` | Free tier is capped at 20 requests/day |
| `groq` | `GROQ_API_KEY` | Much larger free tier; good for dev/test iterations |
| — | none | Offline heuristics, no network calls |

If the requested provider's key is missing (or still holds the
placeholder from `.env.example`), the workflow falls back to the other
provider and then to the offline heuristics. It never hard-fails on a
missing key.

Individual calls retry transient failures (429s, 5xx, timeouts) with
exponential backoff, and after that degrade to the heuristic for that
one step rather than aborting the investigation. Note that backoff only
helps with per-minute throttling — a daily quota won't clear by waiting,
which is why the give-up path exists.

Retrieval embeddings are chosen separately in
`app/retrieval/embeddings.py`, since Groq has no embeddings endpoint.

## Observability (Langfuse)

When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set in `.env`,
each `run_investigation` opens one Langfuse trace/session keyed by the
`investigation_id`. Every graph node records its inputs, outputs, and
duration; Gemini/Groq calls go through Langfuse's LangChain
CallbackHandler so token usage (and cost, when Langfuse knows the model)
show up on those generations. Set `LANGFUSE_TRACING=false` to disable
without removing the keys.

## MCP tool servers

The tools the agents use to look at the world are exposed over the Model
Context Protocol rather than called as Python functions, so they can be
driven by any MCP client and carry a declared schema:

| Server | Tools | Wraps |
| --- | --- | --- |
| `app/mcp_servers/postgres_server.py` | `get_schema`, `check_row_count`, `query_table` | Read-only access to the sandbox warehouse |
| `app/mcp_servers/retrieval_server.py` | `retrieve` | Similarity search over the Chroma index |

Both speak MCP over stdio and can be run standalone:

```bash
python -m app.mcp_servers.postgres_server
python -m app.mcp_servers.retrieval_server
```

`lineage_agent_node` and `data_quality_node` call them through
`app/mcp_servers/client.py`, which spawns each server on first use and
bridges the async SDK to the synchronous graph nodes. `validation_node`
still reads the source artifacts directly, deliberately: it exists to
re-check a hypothesis *independently* of the path the specialists took.

The warehouse server is read-only by construction — SELECT only, with
table and filter-column names validated against the live schema and
filter values bound as parameters.

## Project Structure

```
/app
  /api           # FastAPI application (health + investigations API)
  /db            # Database models and access (the investigations table)
  /graph         # LangGraph workflow, nodes, Langfuse tracing, evaluation harness
  /mcp_servers   # MCP tool servers + the client the nodes call them through
  /retrieval     # Retrieval / vector store logic
  /sandbox_data  # Sandbox warehouse, SQL models and incident scenarios
/frontend        # Vite + React + TypeScript UI (Submit / Detail / History)
/alembic         # Database migrations
/tests           # Test suite
```
