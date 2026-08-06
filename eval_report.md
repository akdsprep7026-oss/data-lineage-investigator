# Data Lineage Investigator — Evaluation Report

**Generated:** 2026-08-06 04:55 UTC  
**LLM:** `groq:llama-3.3-70b-versatile`  
**Harness:** `python tests/run_eval.py` (Step 11)  
**Incidents evaluated:** 4 of 4

> **Note:** Ran against Groq (llama-3.3-70b-versatile) due to Gemini free-tier quota instability encountered during development.

## Summary

| Metric | Value |
|---|---|
| Overall accuracy | **3/4 clean matches + 1 partial** |
| Average investigation time | 5.3s |
| Average tokens / incident | 2,447 |
| Average cost / incident | n/a |
| Total tokens (all incidents) | 9,788 |
| Total cost (all incidents) | n/a |

## Per-incident results

| Incident | Match | Confidence | Retries | Evidence | Duration | Tokens | Cost |
|---|---|---:|---:|---:|---:|---:|---|
| #1 Revenue undercounting from a bad INNER JOIN | **Yes** | 0.90 | 0 | 9 | 12.0s | 3,025 | n/a |
| #2 Stale fct_daily_revenue from a failing Airflow job | **Yes** | 0.90 | 0 | 8 | 2.5s | 1,572 | n/a |
| #3 Breaking upstream schema change (renamed column) | **Yes** | 0.90 | 0 | 11 | 2.4s | 1,795 | n/a |
| #4 Revenue inflation from duplicated orders with new order_ids | **Partial** | 0.80 | 0 | 10 | 4.2s | 3,396 | n/a |

## Detail

### Incident #1 — Revenue undercounting from a bad INNER JOIN

**Status:** `resolved`  
**Match:** **Yes**  
**Confidence:** 0.90  
**Retry loops:** 0  
**Evidence entries:** 9  
**Duration:** 12.0s  
**Tokens:** 3,025  
**Cost:** n/a  
**Investigation id:** `dd8ae001-6366-4976-951b-c04e4497c10b`  
**Langfuse trace:** https://cloud.langfuse.com/project/cmsgjtqnj02i9ad0i6togyhzm/traces/ff3e8cadd8142d61740c2dac9107c4a5  

**Issue**

> Total revenue for 2024-01-20 looks lower than expected -- a handful of completed orders placed by recently-signed-up customers seem to be missing from the fct_daily_revenue dashboard metric entirely.

**Predicted root cause**

The INNER JOIN in stg_orders_cleaned.sql is silently dropping new customer orders without a matching record in raw_customers, causing undercounted revenue for days with new-customer orders.

**Ground truth**

sql_models/01_stg_orders_cleaned.sql joins raw_orders to raw_customers using an INNER JOIN. Orders placed by customers whose customer_id hasn't yet landed in raw_customers (e.g. a brand-new signup still propagating through the customer sync job) are silently dropped by the INNER JOIN instead of being kept (with a fallback/unknown region), undercounting revenue in fct_daily_revenue for any day with such orders.

### Incident #2 — Stale fct_daily_revenue from a failing Airflow job

**Status:** `resolved`  
**Match:** **Yes**  
**Confidence:** 0.90  
**Retry loops:** 0  
**Evidence entries:** 8  
**Duration:** 2.5s  
**Tokens:** 1,572  
**Cost:** n/a  
**Investigation id:** `454159f5-b20c-46b3-b26a-a8b281a3d3e4`  
**Langfuse trace:** https://cloud.langfuse.com/project/cmsgjtqnj02i9ad0i6togyhzm/traces/4c659e90ab3513aae96c6d3cad2168ef  

**Issue**

> The 'Total Revenue by Region' dashboard shows no data for the last two days (2024-01-29 and 2024-01-30), even though the business was clearly still taking orders on those days.

**Predicted root cause**

The 'build_fct_daily_revenue' pipeline job failed to run successfully since 2024-01-28T15:15:07Z, resulting in the 'fct_daily_revenue' table not being updated with the latest data, which in turn caused the 'Total Revenue by Region' dashboard to show no data for the last two days.

**Ground truth**

The build_fct_daily_revenue job has been failing for the last 2 days (connection timeouts to the warehouse) and has not successfully refreshed fct_daily_revenue since 2024-01-28T15:15:07Z. The upstream stg_orders_cleaned table is fully up to date; fct_daily_revenue is simply stale because the job that populates it stopped succeeding.

### Incident #3 — Breaking upstream schema change (renamed column)

**Status:** `resolved`  
**Match:** **Yes** — correctly identifies the missing column and failing job as root cause; doesn't explicitly name it as a rename, but the diagnosis is accurate and actionable.  
**Confidence:** 0.90  
**Retry loops:** 0  
**Evidence entries:** 11  
**Duration:** 2.4s  
**Tokens:** 1,795  
**Cost:** n/a  
**Investigation id:** `d4b53e8f-0251-4043-85dd-2c2b9d24a5f5`  
**Langfuse trace:** https://cloud.langfuse.com/project/cmsgjtqnj02i9ad0i6togyhzm/traces/f2f759cba42e46ddd53240fd31c6dbe4  

**Issue**

> The build_stg_orders_cleaned job is failing on every run with a database error, and stg_orders_cleaned / fct_daily_revenue haven't updated since the failure started.

**Predicted root cause**

The column 'created_at' in the 'raw_orders' table does not exist in the live database schema, causing the 'build_stg_orders_cleaned' job to fail.

**Ground truth**

raw_orders.created_at was renamed to raw_orders.order_created_at (an upstream source schema change) but sql_models/01_stg_orders_cleaned.sql still references the old column name (o.created_at), so every run fails with a 'no such column: o.created_at' database error. Because the job fails before committing, stg_orders_cleaned and fct_daily_revenue are frozen at their last successful state rather than being corrupted.

### Incident #4 — Revenue inflation from duplicated orders with new order_ids

**Status:** `needs_human_review`  
**Match:** **Partial** — correctly identifies the symptom (same transaction re-emitted under a new order_id), but the stated mechanism is wrong: it blames the window function's row-selection logic, when the actual gap is that dedup only operates on order_id at all, so a new order_id bypasses it entirely. Correctly landed in `needs_human_review` at 0.80 rather than falsely resolving.  
**Confidence:** 0.80  
**Retry loops:** 0  
**Evidence entries:** 10  
**Duration:** 4.2s  
**Tokens:** 3,396  
**Cost:** n/a  
**Investigation id:** `d102f344-d727-42c3-9cdb-9942699f5951`  
**Langfuse trace:** https://cloud.langfuse.com/project/cmsgjtqnj02i9ad0i6togyhzm/traces/345a7cc6ce0adf0fc3f24badd327352a  

**Issue**

> Total revenue for 2024-01-15 looks unusually high compared to neighboring days -- almost like some orders were counted twice.

**Predicted root cause**

The issue is caused by the fact that the stg_orders_cleaned table is not properly handling duplicate orders, specifically in cases where the same transaction is re-emitted under a new order_id. This could be due to the window function used in the stg_orders_cleaned.sql model not correctly identifying the most recent row per order_id.

**Ground truth**

On 2024-01-15, ~15 completed orders were re-emitted by the upstream order source with brand-new order_id values for what is actually the same underlying transaction (e.g. a checkout-retry / double-submit bug). sql_models/01_stg_orders_cleaned.sql only de-duplicates on order_id (ROW_NUMBER() PARTITION BY order_id), so these rows look like distinct legitimate orders and are not filtered out, inflating that day's revenue in fct_daily_revenue.

## How to reproduce

```bash
# Official report for this commit (Groq):
$env:LLM_PROVIDER="groq"; python tests/run_eval.py

# Default provider (Gemini), when quota allows:
python tests/run_eval.py
```
