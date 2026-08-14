# Data Lineage Investigator

Multi-agent system that investigates data-pipeline incidents — missing revenue, stale dashboards, schema breakages, duplicate rows — by gathering evidence across lineage, SQL models, warehouse tables, ETL job history, and schema metadata, then generating ranked hypotheses and independently validating the strongest hypothesis against the warehouse, SQL artifacts, pipeline metadata, and schema.

Built as a portfolio project: a **9-node LangGraph** workflow, MCP tool servers for warehouse/retrieval access, Langfuse tracing, a **Streamlit** production UI (Neon Postgres for investigation history), four toggleable sandbox incidents, and scored evaluation artifacts (`eval_report.md`, R4/R5 calibration).

**Production path:** Streamlit Community Cloud → LangGraph → Neon (`DATABASE_URL`) + per-instance sandbox warehouse.  
**Alternate path (still in repo):** FastAPI + React for local/API-style demos.

## Architecture

```
START
  → manager                 # keyword routing: which specialists to run this pass
  → lineage_agent           # retrieve relevant SQL models / tables (MCP retrieval)
  → sql_analysis # LLM/heuristic analysis of SQL logic, joins, filters, aggregation, timestamps, deduplication
  → data_quality            # warehouse checks via MCP Postgres tools
  → etl_agent               # pipeline_jobs.json health / staleness
  → schema_agent            # live schema vs. model column references
  → root_cause              # LLM synthesizes ranked hypotheses (structured claim_kind + artifact)
  → validation              # independent warehouse re-check of the top claim
       ├─(not supported, retries left)→ manager   # narrower retry pass
       └─(supported, or retries spent)→ human_review → END
```

Specialists that aren't scheduled on a given pass no-op. Validation can loop back to the manager up to a retry cap.

### Resolution gate (R5)

An investigation becomes `resolved` only when:

```text
validation.confirmed == True
AND confidence_score >= 0.8
```

Otherwise it lands in `needs_human_review`. Confidence alone never resolves; `unknown` / contradicted claims stay in human review even at high confidence.

Supporting layers:


| Layer                          | Role                                                                                                  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------- |
| Streamlit (`streamlit_app.py`) | **Intended UI / Community Cloud deploy** — New Investigation, History, Detail                         |
| FastAPI (`app/api`)            | Alternate `/health` + `POST/GET /investigations` path                                                 |
| React frontend (`frontend/`)   | Alternate Submit / Detail / History UI (Vite → API)                                                   |
| Sandbox warehouse              | SQLite + SQL models + 4 incidents under `app/sandbox_data/`                                           |
| Investigations DB              | Postgres via Alembic; Neon in production; embedded `pgserver` only if `DATABASE_URL` is unset locally |
| Retrieval                      | Chroma index over SQL models / docs (`app/retrieval/`)                                                |
| MCP servers                    | Postgres + retrieval tools (`app/mcp_servers/`)                                                       |
| Langfuse                       | Per-investigation traces, node spans, token usage                                                     |




## Setup

Python **3.11+** recommended (Node.js 18+ only if you use the React alternate UI).

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

  | Variable                                      | Required?                                               | Purpose                                                                                                                                           |
  | --------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `GOOGLE_API_KEY`                              | For Gemini (default LLM) and optional Gemini embeddings | Gemini chat + `gemini-embedding-001` when `EMBEDDING_PROVIDER=gemini`                                                                             |
  | `GROQ_API_KEY`                                | For `LLM_PROVIDER=groq`                                 | Groq chat (`llama-3.3-70b-versatile` by default)                                                                                                  |
  | `LLM_PROVIDER`                                | Optional (`gemini` default)                             | `gemini` or `groq`                                                                                                                                |
  | `EMBEDDING_PROVIDER`                          | Optional                                                | `onnx` (local) or `gemini`. Unset → gemini if a real `GOOGLE_API_KEY` is set, else onnx. After changing, re-run `python -m app.retrieval.ingest`. |
  | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional                                                | Tracing; leave as placeholders to stay offline                                                                                                    |
  | `LANGFUSE_BASE_URL`                           | Optional                                                | Defaults to `https://cloud.langfuse.com`                                                                                                          |
  | `DATABASE_URL`                                | Optional locally                                        | Real Postgres DSN (Neon in production). If **unset**, embedded `pgserver` is used. If **set**, that DSN is always used (no silent fallback).      |
  | `ALLOWED_ORIGINS`                             | Optional (FastAPI + browser UI)                         | Comma-separated CORS origins. Unset → `http://localhost:5173` and `http://127.0.0.1:5173`.                                                        |
  | `ENABLE_SANDBOX_DEBUG`                        | Optional                                                | `true` shows **Debug: Sandbox Control** in the Streamlit sidebar                                                                                  |

   If the chosen provider's key is missing or still a placeholder, the workflow falls back to the other provider, then to offline heuristics — it never hard-fails on a missing key.



## Streamlit (primary local + Cloud UI)

Streamlit uses the **same** investigation engine (`create_investigation` → background graph → Postgres). It does **not** call FastAPI over HTTP.

### Prepare retrieval (once)

```bash
python -m app.retrieval.ingest
```

Prefer `EMBEDDING_PROVIDER=onnx` for a local, key-free index.

### Start

```bash
streamlit run streamlit_app.py
```

On process start (once per process, **not** every rerun): Cloud `DATABASE_URL` guard → stale-investigation reaper → seed sandbox warehouse if missing → ingest Chroma if not ready.

Use **New Investigation**, watch `pending` / `investigating`, then **History** / Detail for root cause, hypotheses, and evidence.

### Streamlit Community Cloud (intended deployment)

**Entry point:** `streamlit_app.py`  
**Runtime pin:** `runtime.txt` (`python-3.11`)  
**Config:** `.streamlit/config.toml`

```
Streamlit Community Cloud
  → streamlit_app.py
  → LangGraph / MCP / retrieval / sandbox / LLM / Langfuse
  → external PostgreSQL via DATABASE_URL (e.g. Neon)
```


| Secret / env                                       | Required on Cloud?   | Purpose                                                                      |
| -------------------------------------------------- | -------------------- | ---------------------------------------------------------------------------- |
| `DATABASE_URL`                                     | **Yes**              | Durable investigation history (not the sandbox warehouse)                    |
| `STREAMLIT_CLOUD_DEPLOY`                           | Recommended (`true`) | Explicit Cloud detection                                                     |
| `ENABLE_SANDBOX_DEBUG`                             | Optional             | Sidebar control to apply incidents 1–4 / clean baseline on **this** instance |
| `EMBEDDING_PROVIDER`                               | Recommended: `onnx`  | Avoids Gemini embedding quota                                                |
| `GOOGLE_API_KEY` / `GROQ_API_KEY` / `LLM_PROVIDER` | As needed            | LLM                                                                          |
| Langfuse keys                                      | Optional             | Tracing                                                                      |


**Do not commit** `.streamlit/secrets.toml`.

Before first useful Cloud history:

```bash
alembic upgrade head
```



#### Sandbox vs investigations database


| Store                   | Where                                                                          | Purpose                              |
| ----------------------- | ------------------------------------------------------------------------------ | ------------------------------------ |
| Investigations Postgres | `DATABASE_URL`                                                                 | Durable investigation rows / status  |
| Sandbox warehouse       | `warehouse.db` + SQL models / `pipeline_jobs.json` **on the running instance** | Demo company data the graph inspects |
| Retrieval index         | `app/retrieval/chroma_db/` **on the running instance**                         | Lineage search                       |


Applying an incident **locally** does not change Cloud. On Cloud, enable `ENABLE_SANDBOX_DEBUG` and use **Debug: Sandbox Control** (apply + automatic re-ingest), then submit that incident’s issue text.

SQLAlchemy uses `pool_pre_ping=True` so idle Neon connections that were suspended are detected and replaced instead of failing with SSL closed unexpectedly.

## Incident scenarios

Four toggleable bugs under `app/sandbox_data/incidents/`. Applying one always resets to a clean baseline first.


| #   | Scenario                                                              | Typical claim kind |
| --- | --------------------------------------------------------------------- | ------------------ |
| 1   | INNER JOIN drops orphan new-customer orders (revenue undercount)      | `join`             |
| 2   | Failing `build_fct_daily_revenue` job → stale dashboard               | `stale_pipeline`   |
| 3   | Upstream column rename breaks `stg_orders_cleaned`                    | `schema_change`    |
| 4   | Same transaction re-emitted under new `order_id`s (revenue inflation) | `duplicates`       |


```bash
python -m app.sandbox_data.incidents.manage apply 1   # or 2, 3, 4
python -m app.sandbox_data.incidents.manage status
python -m app.sandbox_data.incidents.manage reset
python -m app.retrieval.ingest                        # after apply/reset locally
python -m app.graph.run_test 1                        # smoke one incident through the graph
```

Use each incident’s `issue_description` from `app/sandbox_data/incidents/incident_0N_*.json` — vague prompts (e.g. “orders look low”) can lead the model toward unclassifiable business-rule guesses such as the intentional `status = 'completed'` revenue filter.

## Evaluation



### Latest production smoke (Streamlit)

With the correct incident applied on the instance under test, **when the corresponding incident is applied to the running sandbox instance**, all four authoritative incidents resolve end-to-end(confirmed + confidence clears the R5 `>= 0.8` gate) with the expected claim kinds.


| Incident               | Expected kind    | Streamlit smoke |
| ---------------------- | ---------------- | --------------- |
| #1 Join bug            | `join`           | **resolved**    |
| #2 Stale pipeline      | `stale_pipeline` | **resolved**    |
| #3 Schema change       | `schema_change`  | **resolved**    |
| #4 Duplicate order_ids | `duplicates`     | **resolved**    |


Details and calibration history: `[eval_report.md](eval_report.md)` and `[eval_report_groq.md](eval_report_groq.md)`.

### R4 structured-claim campaign (Groq, measurement)

32 direct LLM runs (8 per incident), **0** fallbacks:


| Metric                                                    | Result          |
| --------------------------------------------------------- | --------------- |
| Claim-kind accuracy                                       | 32/32           |
| Artifact accuracy                                         | 32/32           |
| Validation confirmed                                      | 32/32           |
| False resolutions                                         | 0               |
| SSimulated resolution under R5 gate (`confidence >= 0.8`) | 24/32 (0 false) |


That campaign motivated the R5 inclusive confidence gate (`>= 0.8` instead of `>`).  
[ Note:- `0.7` is used only for validation routing/retry decisions; it does not resolve an investigation. Final resolution requires confirmed validation and confidence ≥ 0.8. ]

### Harnesses

```bash
# Structured campaign (R4-style)
$env:LLM_PROVIDER="groq"; python -m tests.run_r4_campaign --per-incident 8

# Offline calibration (no LLM)
python -m tests.eval_r4_calibration --input eval_root_cause_results_r4_fresh.json

# Classic 4-incident pass (writes narrative detail; prefer reports above for portfolio claims)
$env:LLM_PROVIDER="groq"; python tests/run_eval.py
```



## Alternate path: FastAPI + React

Still fully supported for local/API demos; **not** the Community Cloud path.

```bash
# Terminal 1
uvicorn app.api.main:app --reload --port 8002

# Terminal 2
cd frontend && npm install && npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Leave `VITE_API_URL` unset locally so the Vite proxy is used.


| Variable          | Notes                                             |
| ----------------- | ------------------------------------------------- |
| `DATABASE_URL`    | Yes for durable hosting                           |
| `ALLOWED_ORIGINS` | Yes when the browser UI is separate               |
| `VITE_API_URL`    | Yes when the UI is hosted separately from the API |


```bash
alembic upgrade head
python -m app.retrieval.ingest
```



## Observability (Langfuse)

When Langfuse keys are set, each investigation is one session/trace keyed by `investigation_id`. Node spans and LLM generations (Gemini/Groq via LangChain callbacks) record duration and token usage. Set `LANGFUSE_TRACING=false` to disable without removing keys.

## MCP tool servers


| Server                                | Tools                                          | Wraps                       |
| ------------------------------------- | ---------------------------------------------- | --------------------------- |
| `app/mcp_servers/postgres_server.py`  | `get_schema`, `check_row_count`, `query_table` | Read-only sandbox warehouse |
| `app/mcp_servers/retrieval_server.py` | `retrieve`                                     | Chroma similarity search    |


`lineage_agent_node` and `data_quality_node` call them through `app/mcp_servers/client.py`. `validation_node` reads source artifacts directly so its re-check stays independent.

## Project structure

```
streamlit_app.py           # Community Cloud / local Streamlit entry point
runtime.txt                # Python 3.11 pin for Community Cloud
.streamlit/                # Cloud config (do not commit secrets.toml)
app/
  api/                     # FastAPI (alternate path)
  db/                      # Investigations Postgres + embedded fallback (local)
  graph/                   # LangGraph workflow, nodes, LLM, tracing, eval helpers
  mcp_servers/             # MCP Postgres + retrieval
  retrieval/               # Chroma ingest / retrieve
  sandbox_data/            # SQLite warehouse, SQL models, incidents
  streamlit_support.py
  investigation_runner.py
frontend/                  # Vite + React (alternate path)
alembic/                   # Migrations for investigations DB
tests/                     # Pytest + eval / R4 campaign harnesses
eval_report.md             # Current evaluation + deployment summary
eval_report_groq.md        # Groq / R4–R5 calibration notes
```



## Tests

```bash
pytest -q
```

Expected **208+** passed on the current tree (see current run for the exact count though.)

## Known limitations

- **Sandbox, not a live enterprise platform.** The warehouse, SQL models, pipeline job log, and incidents are a simulated environment for reproducible demos and evaluation — not a connection to a customer’s production data stack.
- **Incident apply is environment-local.** Submitting an incident’s issue text without applying that incident (CLI or Cloud debug panel) leaves a clean LEFT JOIN / healthy jobs baseline; validation will correctly refuse false INNER JOIN claims.
- **Intentional business rules are not auto-bugs.** `fct_daily_revenue`’s `WHERE status = 'completed'` filter is documented as intentional recognized revenue. Claims that treat it as the root cause stay `unknown` / unconfirmed by design.
- **LLM variance.** Confidence and prose can vary by provider/run. The hard safety invariant is: no resolve without `validation.confirmed`, and no resolve for unclassifiable / contradicted claims.
- **Gemini free-tier quota** can be tight for iterative eval; Groq was used for the main calibration campaign; when the provider quota is exhausted, the system degrades to evidence-backed signal classification and ultimately heuristic fallback while preserving the same independent validation gate (Prefer `LLM_PROVIDER=groq` for heavy local loops when Gemini quota is exhausted).

