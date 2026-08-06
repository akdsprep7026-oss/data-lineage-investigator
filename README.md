# Data Lineage Investigator

Multi-agent system that investigates data-pipeline incidents — missing revenue, stale dashboards, schema breakages, duplicate rows — by gathering evidence across lineage, SQL models, warehouse tables, ETL job history, and schema metadata, then proposing a validated root cause.

Built as a portfolio project: a **9-node LangGraph** workflow, MCP tool servers for warehouse/retrieval access, Langfuse tracing, a FastAPI + React UI, and a scored evaluation report over four toggleable sandbox incidents.

## Architecture

```
START
  → manager                 # keyword routing: which specialists to run this pass
  → lineage_agent           # retrieve relevant SQL models / tables (MCP retrieval)
  → sql_analysis            # LLM review of SQL for join / dedup / filter bugs
  → data_quality            # warehouse checks via MCP Postgres tools
  → etl_agent               # pipeline_jobs.json health / staleness
  → schema_agent            # live schema vs. model column references
  → root_cause              # LLM synthesizes ranked hypotheses
  → validation              # independent warehouse re-check of the top claim
       ├─(not supported, retries left)→ manager   # narrower retry pass
       └─(supported, or retries spent)→ human_review → END
```

Specialists that aren't scheduled on a given pass no-op. Validation can loop back to the manager up to a retry cap; unresolved or weakly supported cases land in `needs_human_review` rather than a false resolve.

Supporting layers:

| Layer | Role |
|---|---|
| FastAPI (`app/api`) | `/health`, `POST/GET /investigations`, background graph runs |
| React frontend (`frontend/`) | Submit / Detail / History views (Vite proxies to the API) |
| Sandbox warehouse | SQLite warehouse + 4 incident scenarios under `app/sandbox_data/` |
| Investigations DB | Postgres table via Alembic; embedded `pgserver` fallback if `DATABASE_URL` is unset/unreachable |
| Retrieval | Chroma index over SQL models / docs (`app/retrieval/`) |
| MCP servers | Postgres + retrieval tools (`app/mcp_servers/`) |
| Langfuse | Per-investigation traces, node spans, token usage |

## Setup

Python 3.11+ and Node.js 18+ recommended.

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   source .venv/bin/activate       # macOS / Linux
   ```

2. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in credentials:

   ```bash
   cp .env.example .env
   ```

   | Variable | Required? | Purpose |
   |---|---|---|
   | `GOOGLE_API_KEY` | For Gemini (default LLM) and embeddings | Gemini chat + `gemini-embedding-001` |
   | `GROQ_API_KEY` | For `LLM_PROVIDER=groq` | Groq chat (`llama-3.3-70b-versatile` by default) |
   | `LLM_PROVIDER` | Optional (`gemini` default) | `gemini` or `groq` |
   | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional | Tracing; leave as placeholders to stay offline |
   | `LANGFUSE_BASE_URL` | Optional | Defaults to `https://cloud.langfuse.com` |
   | `DATABASE_URL` | Optional | Real Postgres DSN; if unset/unreachable, embedded `pgserver` is used |

   If the chosen provider's key is missing or still a placeholder, the workflow falls back to the other provider, then to offline heuristics — it never hard-fails on a missing key. Transient LLM errors retry with backoff, then degrade to the heuristic for that step.

4. (Frontend) Install npm deps once:

   ```bash
   cd frontend
   npm install
   ```

## Running the backend + frontend

Terminal 1 — API:

```bash
uvicorn app.api.main:app --reload
```

Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

Terminal 2 — UI:

```bash
cd frontend
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite proxies `/investigations` to the API. Submit an issue description, watch status move through the graph, and browse history.

## Incident scenarios

Four toggleable bugs live under `app/sandbox_data/incidents/`. Applying one always resets to a clean baseline first, so only one incident is active at a time.

| # | Scenario |
|---|---|
| 1 | INNER JOIN drops orphan new-customer orders (revenue undercount) |
| 2 | Failing `build_fct_daily_revenue` job → stale dashboard |
| 3 | Upstream column rename breaks `stg_orders_cleaned` |
| 4 | Same transaction re-emitted under new `order_id`s (revenue inflation) |

```bash
python -m app.sandbox_data.incidents.manage apply 1   # or 2, 3, 4
python -m app.sandbox_data.incidents.manage status
python -m app.sandbox_data.incidents.manage reset
```

After applying an incident, re-ingest retrieval so the index matches the buggy state:

```bash
python -m app.retrieval.ingest
```

Smoke-test one incident through the full graph:

```bash
python -m app.graph.run_test 1
```

## Evaluation

End-to-end harness (all 4 incidents, sandbox reset between runs, markdown report):

```bash
# Official portfolio run for this repo (Groq — see Known limitations):
$env:LLM_PROVIDER="groq"; python tests/run_eval.py          # Windows PowerShell
LLM_PROVIDER=groq python tests/run_eval.py                  # macOS / Linux

# Default provider (Gemini), when quota allows:
python tests/run_eval.py
```

Writes `eval_report.md` at the project root with predicted vs. ground-truth root cause, confidence, retry count, evidence count, duration, and tokens (from Langfuse when available). Match / overall accuracy are filled in by hand after review.

### Latest results (`eval_report.md`)

Run against **Groq (`llama-3.3-70b-versatile`)**:

| Incident | Match | Confidence | Status |
|---|---|---:|---|
| #1 Join bug | Yes | 0.90 | resolved |
| #2 Stale pipeline | Yes | 0.90 | resolved |
| #3 Schema change | Yes | 0.90 | resolved |
| #4 Duplicate order_ids | Partial | 0.80 | needs_human_review |

**Overall: 3/4 clean matches + 1 partial.**

## Observability (Langfuse)

When Langfuse keys are set, each investigation is one session/trace keyed by `investigation_id`. Every node records inputs/outputs/duration; Gemini/Groq calls go through Langfuse's LangChain `CallbackHandler` so token usage appears on generations. Set `LANGFUSE_TRACING=false` to disable without removing keys.

## MCP tool servers

| Server | Tools | Wraps |
|---|---|---|
| `app/mcp_servers/postgres_server.py` | `get_schema`, `check_row_count`, `query_table` | Read-only sandbox warehouse |
| `app/mcp_servers/retrieval_server.py` | `retrieve` | Chroma similarity search |

```bash
python -m app.mcp_servers.postgres_server
python -m app.mcp_servers.retrieval_server
```

`lineage_agent_node` and `data_quality_node` call them through `app/mcp_servers/client.py`. `validation_node` reads source artifacts directly so its re-check stays independent of the specialists' tool path. The warehouse server is SELECT-only, with table/column names validated against the live schema.

## Project structure

```
app/
  api/            # FastAPI (health + investigations)
  db/             # Investigations table + embedded Postgres fallback
  graph/          # LangGraph workflow, nodes, LLM, tracing, smoke/eval helpers
  mcp_servers/    # MCP Postgres + retrieval servers and client
  retrieval/      # Chroma ingest / retrieve
  sandbox_data/   # SQLite warehouse, SQL models, incident scenarios
frontend/         # Vite + React + TypeScript UI
alembic/          # Migrations for the investigations DB
tests/            # Pytest suite + run_eval.py (Step 11 harness)
eval_report.md    # Scored evaluation report (portfolio artifact)
```

## Tests

```bash
pytest
```

## Known limitations

- **Incident #4 is a partial match.** The system correctly surfaces the symptom (same transaction re-emitted under a new `order_id`) and conservatively lands in `needs_human_review` at 0.80 confidence, but the LLM's stated mechanism is wrong — it blames window-function row selection instead of the real gap (dedup only partitions by `order_id`, so a brand-new id bypasses it entirely). This is an LLM reasoning miss on the evidence, not a missing specialist or broken validation loop.
- **Gemini free-tier quota is unstable for this workload** (daily `generate_content` cap of 20). The official `eval_report.md` was therefore run against Groq. Prefer `LLM_PROVIDER=groq` for iterative development; use Gemini when you specifically want that provider and have quota left.
- **Sandbox, not production.** The warehouse, SQL models, pipeline job log, and incidents are a simulated local environment designed for reproducible demos and evaluation — not a connection to a real production data platform.
