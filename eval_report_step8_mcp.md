# Step 8 re-verification -- after the MCP refactor (Groq)

Step 8 moved the two tools an agent uses to look at the world behind MCP
servers: the sandbox warehouse (`app/mcp_servers/postgres_server.py`) and the
retrieval index (`app/mcp_servers/retrieval_server.py`). `data_quality_node`
and `lineage_agent_node` now reach them through MCP client calls
(`app/mcp_servers/client.py`) instead of importing Python functions. That's an
architecture change, so the investigation results should be **unchanged**.

Run against `LLM_PROVIDER=groq` (`llama-3.3-70b-versatile`), deliberately, so
this compares like with like against
[`eval_report_groq.md`](eval_report_groq.md) -- the Step 7 baseline, also Groq.
The Gemini reference run is still outstanding and still blocked by the stuck
free-tier quota; it isn't needed for this check, which is about whether the
refactor changed behaviour, not about reaching reference quality.

The harness was run **twice**, because a single LLM-mediated run can't tell
"the refactor changed something" apart from "the model worded it differently
this time". Two runs bracket the noise.

## Step 7 baseline vs. Step 8, per incident

| Incident | Step 7 (baseline) | Step 8 run 1 | Step 8 run 2 | Same? |
|---|---|---|---|---|
| #1 Join bug | **Yes**, 0.90 | **Yes**, 0.90 | **Yes**, 0.90 | Yes |
| #2 Stale pipeline | **Yes**, 0.90 | **Yes**, 0.90 | **Yes**, 0.90 | Yes |
| #3 Schema change | **Yes**, 0.80 | **Yes**, 0.80 | **Yes**, 0.90 | Yes |
| #4 Duplicate rows | **Partial**, 0.85 | **Partial**, 0.80 | **Partial**, 0.80 | Yes |
| **Total** | **3 clean + 1 partial** | **3 clean + 1 partial** | **3 clean + 1 partial** | **Yes** |

Match/confidence are compared because they're what the Step 7 report recorded.
Graded by the same standard it used: does the predicted root cause name the
same mechanism as the ground truth.

Both Step 8 runs reproduce the Step 7 headline exactly, and clear the "at
least 3/4" bar.

### The differences that did show up, and why they aren't the refactor

- **Incident #3 moved within the Step 8 runs themselves**: run 1 reached the
  retry cap and ended `needs_human_review` at 0.80, run 2 confirmed on the
  first pass and ended `resolved` at 0.90 with `data_quality` never scheduled.
  Two runs of identical code, one release apart from each other by nothing at
  all. That's the size of Groq's run-to-run envelope on this incident, and
  Step 7's 0.80 sits inside it.
- **Incident #4's wording got terser** ("duplicate orders ... not being
  correctly de-duplicated") than Step 7's ("keeps the most recent row per
  `order_id`, without considering order status"). Both are the same *partial*:
  right that `stg_orders_cleaned`'s de-duplication is insufficient, silent on
  the actual mechanism (same transaction re-emitted under a **new** `order_id`).
  This is precisely the known gap carried forward from Step 7, unchanged.
- **Confidence drifted by 0.05 on #4** (0.85 -> 0.80). Same side of the 0.8
  auto-resolve threshold, same disposition.

Note the evidence *sources* per incident also matched the baseline's routing
expectations: #2 pulled `etl_agent` (job metadata) with no `sql_analysis`, #3
pulled `schema_agent`, #1 and #4 pulled `sql_analysis` + `data_quality`. The
keyword router is upstream of the refactor and behaved identically.

## The part that isn't subject to LLM noise

An end-to-end eval through a language model is the weakest possible instrument
for proving "nothing changed", so the actual equivalence is pinned down by
tests instead, in `tests/test_mcp_servers.py`:

- **`run_basic_checks` over MCP == the pre-Step-8 aggregate SQL.** The old
  implementation is kept verbatim in the test as the reference, and the two are
  compared field for field across all four warehouse tables -- including
  `fct_daily_revenue`, which has no id column at all, and `raw_customers`,
  where the id column is `customer_id` rather than `order_id`.
- **The same holds under the incidents that make the check matter.** Under
  incident 4 (duplicate transactions under fresh `order_id`s) and incident 3
  (`created_at` renamed to `order_created_at` while the server is already
  running), the MCP-backed report still matches the direct-SQL one exactly.
  Incident 3 also confirms the server reflects the schema per call rather than
  caching what it saw at startup.
- **`retrieve` over MCP == the in-process Step 4 retriever**: same documents,
  same metadata, same order, for both the unfiltered and the
  `filter_type="sql_model"` search.
- **The published tool contract is asserted**, over a real MCP session with the
  servers running as subprocesses: tool names, required vs. optional
  parameters, and a non-empty documented schema for every tool.

Those are deterministic, run offline with no API quota, and will keep holding
the line in CI. The full suite is 89 tests (64 pre-existing, all still
passing, plus 25 new).

## What changed in the code

1. **`app/mcp_servers/postgres_server.py`** -- MCP server over the sandbox
   warehouse, exposing `get_schema`, `check_row_count` and
   `query_table(table_name, filters)`. Read-only by construction: SELECT only,
   table and filter-column names validated against the live schema before
   interpolation, filter values always bound parameters.
2. **`app/mcp_servers/retrieval_server.py`** -- MCP server exposing the Step 4
   `retrieve(query, filter_type, n_results)` as a tool, adding no logic of its
   own.
3. **`app/mcp_servers/client.py`** -- the sync/async bridge. LangGraph nodes are
   synchronous and the MCP SDK is async, so this owns one background event-loop
   thread and one long-lived stdio session per server, and exposes `call_tool`
   as an ordinary blocking call.
4. **`app/graph/data_quality.py`** -- fetches through the three MCP tools and
   aggregates in Python; the checks themselves are untouched. The aggregation
   stayed on the client side on purpose, so the server keeps offering the three
   small general-purpose tools the spec asks for rather than growing a bespoke
   `count_the_duplicates_for_me` tool useful to exactly one caller.
5. **`app/graph/nodes.py`** -- `lineage_agent_node` and `data_quality_node` now
   make MCP tool calls. `validation_node` still reads the source artifacts
   directly, on purpose: its whole value is being independent of the path the
   specialists took, and routing it through the same tool layer would erode
   that.

## Reproducing

```bash
# The deterministic part (no network, no quota):
python -m pytest tests/test_mcp_servers.py -q

# The end-to-end evaluation this report compares:
$env:LLM_PROVIDER="groq"; python -m app.graph.evaluate

# Either server can also be driven by any MCP client, not just this codebase:
python -m app.mcp_servers.postgres_server
python -m app.mcp_servers.retrieval_server
```
