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
| Investigations DB | Postgres table via Alembic; embedded `pgserver` fallback if `DATABASE_URL` is unset |
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
   | `GOOGLE_API_KEY` | For Gemini (default LLM) and optional Gemini embeddings | Gemini chat + `gemini-embedding-001` when `EMBEDDING_PROVIDER=gemini` |
   | `GROQ_API_KEY` | For `LLM_PROVIDER=groq` | Groq chat (`llama-3.3-70b-versatile` by default) |
   | `LLM_PROVIDER` | Optional (`gemini` default) | `gemini` or `groq` |
   | `EMBEDDING_PROVIDER` | Optional | `onnx` (local) or `gemini`. Unset → gemini if a real `GOOGLE_API_KEY` is set, else onnx. After changing, re-run `python -m app.retrieval.ingest`. |
   | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional | Tracing; leave as placeholders to stay offline |
   | `LANGFUSE_BASE_URL` | Optional | Defaults to `https://cloud.langfuse.com` |
   | `DATABASE_URL` | Optional locally | Real Postgres DSN (Neon in production). If **unset**, embedded `pgserver` is used. If **set**, that DSN is always used (no silent fallback). Comment out any placeholder `DATABASE_URL` in an older `.env` so local runs use embedded Postgres. |
   | `ALLOWED_ORIGINS` | Optional locally | Comma-separated CORS origins. Unset → `http://localhost:5173` and `http://127.0.0.1:5173`. |

   If the chosen provider's key is missing or still a placeholder, the workflow falls back to the other provider, then to offline heuristics — it never hard-fails on a missing key. Transient LLM errors retry with backoff, then degrade to the heuristic for that step.

4. (Frontend) Install npm deps once:

   ```bash
   cd frontend
   npm install
   ```

## Running the backend + frontend

Terminal 1 — API (port **8002**, matching the Vite proxy):

```bash
uvicorn app.api.main:app --reload --port 8002
```

Health check: [http://127.0.0.1:8002/health](http://127.0.0.1:8002/health).

Terminal 2 — UI:

```bash
cd frontend
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite proxies `/investigations` and `/health` to the API on port 8002. Leave `VITE_API_URL` unset locally so those relative paths keep using the proxy. Submit an issue description, watch status move through the graph, and browse history.

## Streamlit

The Streamlit UI is an alternate local entry point over the **same** investigation engine (`create_investigation` → background `run_investigation` → Postgres + LangGraph + retrieval/MCP). It does **not** call FastAPI over HTTP.

Python **3.11+** recommended. Install deps once (`pip install -r requirements.txt`; includes `streamlit`).

### Prepare the retrieval index

Chroma under `app/retrieval/chroma_db/` is gitignored. Build it once from sandbox SQL models / pipeline / dashboard metadata:

```bash
python -m app.retrieval.ingest
```

Prefer `EMBEDDING_PROVIDER=onnx` for a local, key-free index. After changing embedding provider, re-run ingest. The Streamlit sidebar shows **Retrieval index: Ready / Not initialized** and will not rebuild the index on every rerun.

### Environment

Copy `.env.example` → `.env`. Streamlit uses the same names as the rest of the app:

| Variable | Required? | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | Optional | Gemini LLM (default); Gemini embeddings only if `EMBEDDING_PROVIDER=gemini` |
| `GROQ_API_KEY` | Optional | Groq LLM when selected / as fallback when Gemini is unavailable |
| `LLM_PROVIDER` | Optional | `gemini` (default) or `groq` |
| `GEMINI_MODEL` / `GROQ_MODEL` | Optional | Model overrides |
| `EMBEDDING_PROVIDER` | Optional | `onnx` or `gemini` (must match how the Chroma index was built) |
| `DATABASE_URL` | Optional locally | Unset → embedded Postgres under `app/db/.pgdata`. Set → that DSN only (no silent fallback). |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional | Tracing (`app/graph/tracing.py`); placeholders or missing keys keep runs offline |
| `LANGFUSE_BASE_URL` | Optional | Defaults to `https://cloud.langfuse.com` |
| `LANGFUSE_TRACING` | Optional | Set `false` to disable even when keys are present |
| `STALE_INVESTIGATION_MINUTES` | Optional | Startup reaper threshold (default 30) |

Not required for Streamlit: `ALLOWED_ORIGINS`, `VITE_API_URL`.

If LLM keys are missing or still placeholders, sql/root-cause steps use the existing offline heuristics — the app does not hard-fail.

### Start

```bash
streamlit run streamlit_app.py
```

On process start (once per Streamlit process, **not** every rerun), the app:

1. Validates Cloud-required config when Community Cloud is detected (`STREAMLIT_CLOUD_DEPLOY=true` or the conventional `appuser` runtime).
2. Calls `reap_stale_investigations()` (same idea as FastAPI lifespan).
3. Seeds `warehouse.db` **only if** the sandbox warehouse is missing/uninitialized (`python -m app.sandbox_data.seed`).
4. Builds the Chroma index **only if** retrieval is not ready (`python -m app.retrieval.ingest` via existing `ingest()`).

Use **New Investigation** to create a run, watch status while `pending` / `investigating`, then open **History** / detail for root cause, hypotheses, and evidence.

### Streamlit Community Cloud (intended deployment)

**Entry point:** `streamlit_app.py`  
**Runtime pin:** `runtime.txt` (`python-3.11`)  
**Config:** `.streamlit/config.toml` (headless + disable usage stats)

Architecture:

```
Streamlit Community Cloud
  → streamlit_app.py
  → existing service/graph layer (LangGraph, MCP, retrieval, sandbox, LLM, Langfuse)
  → external PostgreSQL via DATABASE_URL
```

Rebuildable local assets (ephemeral Cloud filesystem):

| Asset | Bootstrap |
|---|---|
| Sandbox SQLite `warehouse.db` | Seed once when missing (`app.sandbox_data.seed`) |
| Chroma under `app/retrieval/chroma_db/` | Ingest once when not ready (`app.retrieval.ingest`) |

**Render and Vercel are not required** for this deployment path. The FastAPI + React stack remains in the repo for local/alternate use; it is not the Community Cloud path.

**External PostgreSQL is required** for durable investigation history. Embedded `pgserver` is for **local development only** when `DATABASE_URL` is unset — it is **not** durable storage on Community Cloud. Keep the DSN provider-agnostic: set `DATABASE_URL` to any Postgres URL (do not hard-code a vendor). Before first useful Cloud traffic, run migrations once against that database:

```bash
alembic upgrade head
```

**Do not commit Streamlit secrets.** Never add `.streamlit/secrets.toml` to git (it is gitignored). Configure secrets in the Streamlit Cloud dashboard. Root-level secret keys are exposed as environment variables with the **same names** the app already reads via `os.getenv` — no second config system.

| Secret / env name | Required on Cloud? | Purpose |
|---|---|---|
| `DATABASE_URL` | **Yes** | External Postgres for investigations |
| `STREAMLIT_CLOUD_DEPLOY` | Recommended (`true`) | Explicit Cloud detection so embedded pgserver is never used silently |
| `ENABLE_SANDBOX_DEBUG` | Optional (`true` to enable) | Shows **Debug: Sandbox Control** in the sidebar so you can apply incidents 1–4 (or clean baseline) on the **live** process sandbox + re-ingest Chroma |
| `GOOGLE_API_KEY` | Recommended | Gemini LLM (default) |
| `GROQ_API_KEY` | Optional | Groq when selected / fallback |
| `LLM_PROVIDER` | Optional | `gemini` (default) or `groq` |
| `GEMINI_MODEL` / `GROQ_MODEL` | Optional | Model overrides |
| `EMBEDDING_PROVIDER` | **Recommended: `onnx`** | Avoids Gemini embedding quota on Cloud; ONNX is already supported via Chroma’s bundled MiniLM |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional | Tracing |
| `LANGFUSE_BASE_URL` | Optional | Defaults to `https://cloud.langfuse.com` |
| `LANGFUSE_TRACING` | Optional | Set `false` to disable |
| `LANGFUSE_PUBLIC_TRACES` | Optional | Shareable dashboard links |
| `STALE_INVESTIGATION_MINUTES` | Optional | Startup reaper threshold (default 30) |

MCP stdio servers continue to launch as `sys.executable -m app.mcp_servers.*` from the project root — no MCP redesign for Community Cloud.

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

## Deployment

### Intended path: Streamlit Community Cloud

See **[Streamlit Community Cloud (intended deployment)](#streamlit-community-cloud-intended-deployment)** under [Streamlit](#streamlit). Summary:

- Deploy **`streamlit_app.py`** (not FastAPI/React).
- Provide an **external PostgreSQL** `DATABASE_URL` via Streamlit Secrets (provider-agnostic).
- Prefer **`EMBEDDING_PROVIDER=onnx`** on Cloud.
- Warehouse seed + Chroma ingest run **once per process** when assets are missing — never on every Streamlit rerun.
- **`DATABASE_URL` covers investigation state only**, not the sandbox warehouse. Each deployed instance has its own independent `warehouse.db` / SQL models / `chroma_db` on that process filesystem. To put a live Cloud app onto incident 1–4 (or clean baseline), set `ENABLE_SANDBOX_DEBUG=true` in Secrets and use **Debug: Sandbox Control** in the sidebar (off by default).
- **Render / Vercel are not required.** Embedded `pgserver` is local-only, not durable Community Cloud storage.
- **Do not commit** `.streamlit/secrets.toml`.

#### Sandbox vs investigations database

| Store | Where | Purpose |
|---|---|---|
| Investigations Postgres | `DATABASE_URL` (Neon/etc. via Secrets) | Durable investigation history / status |
| Sandbox warehouse | SQLite `app/sandbox_data/warehouse.db` + SQL models / `pipeline_jobs.json` **on the running instance** | Demo “company” data the graph inspects |
| Retrieval index | `app/retrieval/chroma_db/` **on the running instance** | Lineage search over those sandbox files |

Applying an incident locally does **not** change a Community Cloud instance. Use the debug sidebar on that deployment (or re-seed/re-ingest in that environment) so validators see the INNER JOIN / stale job / etc. that you intend to test.

Run once against the external Postgres before relying on Cloud history:

```bash
alembic upgrade head
```

### Alternate path: FastAPI + React (still in repo)

The FastAPI API and React frontend remain fully supported for local development and optional separate hosting. They are **not** the Streamlit Community Cloud deployment path.

| Service (optional) | Role |
|---|---|
| FastAPI backend | `uvicorn app.api.main:app --host 0.0.0.0 --port $PORT` |
| React frontend | Vite build (`frontend/`) |
| PostgreSQL | Investigations table via `DATABASE_URL` |

The sandbox warehouse remains the SQLite dataset under `app/sandbox_data/` (demo/eval data), not the investigations Postgres database.

#### Backend environment variables (FastAPI hosting)

| Variable | Required? | Purpose |
|---|---|---|
| `DATABASE_URL` | **Yes** in durable hosting | Postgres connection string (include `sslmode=require` when the host requires SSL) |
| `ALLOWED_ORIGINS` | **Yes** with a browser frontend | Comma-separated frontend origins |
| `GOOGLE_API_KEY` | Recommended | Gemini LLM; also Gemini embeddings when `EMBEDDING_PROVIDER=gemini` |
| `GROQ_API_KEY` | Optional | Alternate LLM when `LLM_PROVIDER=groq` |
| `LLM_PROVIDER` | Optional | `gemini` (default) or `groq` |
| `EMBEDDING_PROVIDER` | Optional | `onnx` or `gemini` (keep consistent with how the Chroma index was built; re-ingest after changing) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional | Tracing |
| `LANGFUSE_BASE_URL` | Optional | Defaults to `https://cloud.langfuse.com` |
| `LANGFUSE_TRACING` | Optional | Set `false` to disable tracing |
| `STALE_INVESTIGATION_MINUTES` | Optional | Minutes without `updated_at` progress before startup reaper marks pending/investigating rows `needs_human_review` (default **30**) |

Example FastAPI start:

```bash
uvicorn app.api.main:app --host 0.0.0.0 --port $PORT
```

Build/install: `pip install -r requirements.txt` (includes `pgserver` for local fallback; it is not used when `DATABASE_URL` is set).

#### Frontend environment variables (React hosting)

| Variable | Required? | Purpose |
|---|---|---|
| `VITE_API_URL` | **Yes** when the UI is hosted separately from the API | Public FastAPI base URL, no trailing slash |

Locally, leave `VITE_API_URL` unset so `npm run dev` continues to use the Vite proxy to `http://127.0.0.1:8002`. The proxy only forwards API `fetch` calls; browser document navigations / hard refreshes of `/investigations/:id` still receive the SPA (`frontend/vite.config.ts`). `frontend/vercel.json` rewrites SPA routes (`/`, `/history`, `/investigations/:id`, …) to `index.html` if you host on Vercel.

#### First-deployment commands (investigations DB + optional local index)

With production `DATABASE_URL` available in the environment:

```bash
alembic upgrade head
python -m app.retrieval.ingest
```

- `alembic upgrade head` creates/updates the investigations table on the external Postgres.
- `python -m app.retrieval.ingest` builds Chroma under `app/retrieval/chroma_db/` on the machine that runs it. On ephemeral filesystems, re-run (or rely on Streamlit’s once-per-process bootstrap) if the index is missing. Query always reuses the embedding function persisted on the collection.

### Local-development variables

See `.env.example` (backend) and `frontend/.env.example`. Typical local Streamlit/FastAPI setup: leave `DATABASE_URL` and `ALLOWED_ORIGINS` unset; leave `VITE_API_URL` unset.

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
streamlit_app.py  # Intended Community Cloud / local Streamlit entry point
runtime.txt       # Python 3.11 pin for Streamlit Community Cloud
.streamlit/       # Community Cloud config (do not commit secrets.toml)
app/
  api/            # FastAPI (health + investigations; alternate path)
  db/             # Investigations table + embedded Postgres fallback (local)
  graph/          # LangGraph workflow, nodes, LLM, tracing, smoke/eval helpers
  mcp_servers/    # MCP Postgres + retrieval servers and client
  retrieval/      # Chroma ingest / retrieve
  sandbox_data/   # SQLite warehouse, SQL models, incident scenarios
  streamlit_support.py
  investigation_runner.py
frontend/         # Vite + React + TypeScript UI (alternate path)
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




