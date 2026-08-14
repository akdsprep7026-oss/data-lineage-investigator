# Data Lineage Investigator — Evaluation Report

**Updated:** 2026-08-14  
**Repo tip (at update):** `5beb086` — pool_pre_ping + R5 resolve gate + Streamlit Cloud path  
**Primary LLM used for calibration:** `groq:llama-3.3-70b-versatile`  
**Production UI:** Streamlit Community Cloud → Neon Postgres (`DATABASE_URL`) + per-instance sandbox warehouse  

> This report replaces the stale Step 11 snapshot (2026-08-06). Figures below come from the R4 structured-claim campaign and the R5 threshold change; they are not a fresh 4-incident `tests/run_eval.py` re-run on this date.

---

## Current system (production)

| Area | Status |
|---|---|
| Streamlit Community Cloud | Running; investigations persist on Neon |
| Resolve gate | `validation.confirmed == True` **and** `confidence_score >= 0.8` |
| Safety | Confidence alone never resolves; `unknown` / contradicted claims stay `needs_human_review` |
| Sandbox incidents on Cloud | Independent of Neon; use **Debug: Sandbox Control** when `ENABLE_SANDBOX_DEBUG=true` |
| Idle Neon connections | `pool_pre_ping=True` on SQLAlchemy engines (recovers suspended serverless connections) |
| Unit / integration tests | **208 passed** (`pytest -q`) |

### Resolution rule (R5)

```text
validation.confirmed == True
AND confidence_score >= 0.8
→ resolved
else → needs_human_review
```

R4 offline simulation of this gate: **24/32** claim-correct resolutions, **0/32** false resolutions.  
Previous exclusive gate (`> 0.8`) would have resolved only **8/32**.

---

## R4 structured-claim campaign (authoritative accuracy data)

**Harness:** `python -m tests.run_r4_campaign` / `tests.eval_root_cause`  
**Raw results:** `eval_root_cause_results_r4_fresh.json`  
**Calibration:** `eval_confidence_calibration_r4.json`  
**Provider:** Groq (32/32 direct LLM runs; **0** heuristic/signal fallbacks)

| Metric | Result |
|---|---|
| Runs | 32 (8 × incidents 1–4) |
| Claim-kind accuracy | **32/32** |
| Artifact accuracy | **32/32** |
| Validation confirmed | **32/32** |
| Confirmed + incorrect | **0** |
| False resolutions | **0** |

### Per-incident pattern (R4, as recorded under the then-current `>0.8` gate)

| Incident | Claim kind | Confidence (all 8 runs) | Confirmed | Status under `>0.8` | Status under R5 `>=0.8` |
|---|---|---:|---|---|---|
| #1 Join bug | `join` | 0.90 | Yes | **resolved** (8/8) | **resolved** |
| #2 Stale pipeline | `stale_pipeline` | 0.80 | Yes | needs_human_review | **resolved** |
| #3 Schema change | `schema_change` | 0.80 | Yes | needs_human_review | **resolved** |
| #4 Duplicates | `duplicates` | 0.75 | Yes | needs_human_review | needs_human_review |

Exact **0.80** cases: **16/16** confirmed and claim-correct (incidents 2 + 3). That pattern is why production moved from `>` to `>=`.

### Safety checks (R4 LLM-only)

| Check | Result |
|---|---|
| False resolutions at `>=0.80` | 0 |
| Confidence-only resolutions | 0 |
| Contradicted resolutions | 0 |
| Unconfirmed high-confidence | 0 |

---

## Local isolation check (incident 1, post-R5)

After applying incident 1 to the local sandbox and running:

```bash
$env:LLM_PROVIDER="groq"; python -m app.graph.run_test 1
```

| Field | Value |
|---|---|
| Status | `resolved` |
| Claim kind | `join` |
| Confirmed | `True` |
| Confidence | 0.90 |
| Final root cause | INNER JOIN in `stg_orders_cleaned.sql` drops unmatched new-signup orders |

Confirms the graph/validator path when the sandbox actually has the join bug (frontend issue text alone does not apply the incident).

---

## Historical Step 11 single-pass (2026-08-06, superseded)

Earlier `python tests/run_eval.py` Groq pass (kept for lineage only):

| Incident | Match | Confidence | Status |
|---|---|---:|---|
| #1 Join | Yes | 0.90 | resolved |
| #2 Stale | Yes | 0.90 | resolved |
| #3 Schema | Yes | 0.90 | resolved |
| #4 Duplicates | Partial | 0.80 | needs_human_review |

**Overall then:** 3/4 clean + 1 partial. Prefer the R4 structured campaign above for current accuracy claims.

---

## How to reproduce

```bash
# Structured root-cause campaign (R4-style)
$env:LLM_PROVIDER="groq"; python -m tests.run_r4_campaign --per-incident 8

# Offline threshold / calibration analysis (no LLM)
python -m tests.eval_r4_calibration --input eval_root_cause_results_r4_fresh.json

# Single incident through the graph
$env:LLM_PROVIDER="groq"; python -m app.graph.run_test 1

# Full automated suite
python -m pytest -q
```

On Streamlit Cloud: set `ENABLE_SANDBOX_DEBUG=true`, apply the desired incident in **Debug: Sandbox Control**, then submit that incident’s `issue_description` from the corresponding JSON under `app/sandbox_data/incidents/`.
