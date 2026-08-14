# Groq evaluation report — current (R4 / R5)

**Updated:** 2026-08-14  
**LLM:** `LLM_PROVIDER=groq` → `llama-3.3-70b-versatile`  
**Role of this file:** Document Groq as the **primary measured** evaluation path used through structured-claim calibration (R4) and the resolve-gate change (R5).  

> Older content below the historical section described this file as “provisional Step 7” with Gemini still outstanding. That framing is obsolete: Groq carried the R4 32-run campaign (0 fallbacks), and production Streamlit is verified against the same graph.

---

## Why Groq

Gemini free-tier quota was repeatedly exhausted during development. Groq was used for:

- Step 7 / Step 11 dogfood evals  
- R1–R4 root-cause / confidence calibration  
- Local `run_test` isolation after R5  

Production may still use Gemini or Groq via Secrets (`LLM_PROVIDER` / API keys). Evaluation numbers cited here are **Groq** unless noted.

---

## R4 campaign summary (Groq, 32 direct LLM runs)

| | |
|---|---|
| Source | `eval_root_cause_results_r4_fresh.json` |
| Analyzer | `eval_confidence_calibration_r4.json` |
| Claim-kind / artifact / confirm | **32/32** each |
| False resolutions | **0** |
| Fallbacks | **0** |

Confidence pattern under Groq:

| Incident | Kind | Typical confidence | Confirmed |
|---|---|---:|---|
| #1 | join | 0.90 | yes |
| #2 | stale_pipeline | 0.80 | yes |
| #3 | schema_change | 0.80 | yes |
| #4 | duplicates | 0.75 | yes |

### Gate impact (same Groq dataset, offline)

| Gate | Resolutions | False resolutions |
|---|---|---|
| Old `confidence > 0.8` | 8/32 | 0/32 |
| New `confidence >= 0.8` (R5 production) | **24/32** | **0/32** |

Exact 0.80 confirmed+correct runs (16/16) drove the inclusive threshold.

---

## Production alignment

- Resolve only when **confirmed** and **confidence >= 0.8**  
- Streamlit Cloud + Neon for investigation state; sandbox incidents are per-instance (debug sidebar when enabled)  
- See `eval_report.md` for the full current evaluation + deployment summary  

---

## Historical: Step 7 provisional Groq pass (unchanged narrative)

The following was the original Step 7 dogfood (`python -m app.graph.evaluate`), kept for project history.

| Incident | Match | Confidence (that run) |
|---|---|---:|
| #1 Join bug | Yes | 0.90 |
| #2 Stale pipeline | Yes | 0.90 |
| #3 Schema change | Yes | 0.80 |
| #4 Duplicate rows | Partial | 0.85 |

**3/4 clean + 1 partial** at the time. Known #4 gap: model sometimes described same-`order_id` re-emit instead of distinct new ids; later evidence/`duplicate_transaction_groups` work and structured `claim_kind` improved consistency (see R4: 8/8 `duplicates` kind+artifact correct at 0.75).

### Reproduce (legacy harness)

```bash
$env:LLM_PROVIDER="groq"; python -m app.graph.evaluate
```

### Reproduce (current structured harness)

```bash
$env:LLM_PROVIDER="groq"; python -m tests.run_r4_campaign --per-incident 8
python -m tests.eval_r4_calibration --input eval_root_cause_results_r4_fresh.json
```
