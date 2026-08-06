"""Step 11 evaluation harness -- portfolio CV artifact.

Runs all 4 sandbox incident scenarios through the full investigation
graph end to end, resets sandbox data between each run, and writes a
clean markdown report to eval_report.md at the project root.

Per incident the report captures predicted root cause, ground truth,
confidence, retry loops, evidence count, wall-clock duration, and
tokens/cost when Langfuse has them. Match (and therefore overall
accuracy) is left as "[PENDING — review needed]" for a human to fill
in after reading predicted vs. ground truth -- the harness does not
auto-score.

Usage (from the project root):

    # Dev / harness check (does not consume Gemini quota):
    $env:LLM_PROVIDER="groq"; python tests/run_eval.py --output eval_report_harness_check.md

    # Official portfolio run (Gemini, once):
    Remove-Item Env:\\LLM_PROVIDER -ErrorAction SilentlyContinue
    python tests/run_eval.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from app.graph.llm import active_model_label  # noqa: E402
from app.graph.run_test import INCIDENTS  # noqa: E402
from app.graph.tracing import last_trace_id, last_trace_url, tracing_enabled  # noqa: E402
from app.graph.workflow import run_investigation  # noqa: E402
from app.retrieval.ingest import ingest  # noqa: E402
from app.sandbox_data.incidents import common  # noqa: E402

DEFAULT_REPORT_PATH = PROJECT_ROOT / "eval_report.md"
MATCH_PLACEHOLDER = "[PENDING — review needed]"
# Langfuse ingests asynchronously; a short wait after flush usually
# surfaces generation usage on the trace before we ask for it.
# Langfuse ingests asynchronously; freshly flushed traces often 404 for
# a few seconds before they become readable via the API.
LANGFUSE_FETCH_ATTEMPTS = 6
LANGFUSE_FETCH_WAIT_SECONDS = 3.0


def run_one(key: str) -> dict[str, Any]:
    """Apply one incident, run the full graph, return report fields."""
    module, json_path = INCIDENTS[key]
    # apply() resets to the clean baseline first, then injects this
    # incident -- so sandbox data is fresh for every scenario.
    module.apply()
    ingest()

    incident = json.loads((PROJECT_ROOT / json_path).read_text(encoding="utf-8"))
    issue_description = incident["issue_description"]

    started = time.perf_counter()
    final_state = run_investigation(issue_description)
    duration_seconds = time.perf_counter() - started

    top_hypothesis = final_state.get("top_hypothesis")
    predicted = final_state.get("final_root_cause") or (
        top_hypothesis["description"] if top_hypothesis else None
    )
    tokens, cost_usd = _fetch_trace_usage(last_trace_id())

    return {
        "incident": key,
        "title": incident["title"],
        "issue": issue_description,
        "predicted": predicted,
        "ground_truth": incident["ground_truth_root_cause"],
        "confidence": (
            float(top_hypothesis["confidence_score"]) if top_hypothesis else 0.0
        ),
        "retries": int(final_state.get("retry_count") or 0),
        "evidence_count": len(final_state.get("evidence") or []),
        "status": final_state.get("status"),
        "duration_seconds": duration_seconds,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "investigation_id": final_state.get("investigation_id"),
        "trace_url": last_trace_url(),
        "match": MATCH_PLACEHOLDER,
    }


def _fetch_trace_usage(
    trace_id: Optional[str],
) -> tuple[Optional[int], Optional[float]]:
    """Sum token usage (and cost, when priced) from a Langfuse trace.

    Returns (total_tokens, cost_usd). Either value may be None when
    tracing is off, the trace isn't ready yet, or Langfuse has no
    usage/cost for the model.
    """
    if not trace_id or not tracing_enabled():
        return None, None

    try:
        from langfuse import get_client
    except Exception:  # noqa: BLE001
        return None, None

    langfuse = get_client()
    last_error: Optional[BaseException] = None

    for attempt in range(LANGFUSE_FETCH_ATTEMPTS):
        try:
            # Give the exporter a moment after flush before the first
            # read, and between retries if generations aren't attached yet.
            time.sleep(LANGFUSE_FETCH_WAIT_SECONDS)
            trace = langfuse.api.trace.get(trace_id)
            generations = [
                obs
                for obs in (trace.observations or [])
                if getattr(obs, "type", None) == "GENERATION"
            ]
            if not generations and attempt + 1 < LANGFUSE_FETCH_ATTEMPTS:
                continue

            total_tokens = 0
            saw_tokens = False
            for obs in generations:
                usage = getattr(obs, "usage", None)
                details = getattr(obs, "usage_details", None) or {}
                count = None
                if usage is not None and getattr(usage, "total", None):
                    count = int(usage.total)
                elif details.get("total") is not None:
                    count = int(details["total"])
                if count is not None:
                    total_tokens += count
                    saw_tokens = True

            cost = getattr(trace, "total_cost", None)
            cost_usd = float(cost) if cost not in (None, 0, 0.0) else None
            # Some free-tier models report 0.0 cost even when tokens are
            # known -- keep cost as None so the report says "n/a" rather
            # than a misleading $0.00.
            return (total_tokens if saw_tokens else None), cost_usd
        except Exception as exc:  # noqa: BLE001 - report stays useful without usage
            last_error = exc
            continue

    if last_error is not None:
        print(f"  (Langfuse usage fetch failed: {last_error})")
    return None, None


def _fmt_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s"


def _fmt_tokens(tokens: Optional[int]) -> str:
    if tokens is None:
        return "n/a"
    return f"{tokens:,}"


def _fmt_cost(cost_usd: Optional[float]) -> str:
    if cost_usd is None:
        return "n/a"
    return f"${cost_usd:.6f}"


def _escape_md_cell(text: Optional[str]) -> str:
    """Keep predicted/ground-truth prose readable inside a markdown table."""
    if not text:
        return "(none)"
    return " ".join(text.split()).replace("|", "\\|")


def render_report(
    results: list[dict[str, Any]],
    *,
    llm_label: str,
    notes: Optional[list[str]] = None,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n = len(results)
    avg_duration = (
        sum(r["duration_seconds"] for r in results) / n if n else 0.0
    )
    token_values = [r["tokens"] for r in results if r["tokens"] is not None]
    cost_values = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    avg_tokens = (
        sum(token_values) / len(token_values) if token_values else None
    )
    avg_cost = sum(cost_values) / len(cost_values) if cost_values else None
    total_tokens = sum(token_values) if token_values else None
    total_cost = sum(cost_values) if cost_values else None

    lines: list[str] = [
        "# Data Lineage Investigator — Evaluation Report",
        "",
        f"**Generated:** {generated_at}  ",
        f"**LLM:** `{llm_label}`  ",
        f"**Harness:** `python tests/run_eval.py` (Step 11)  ",
        f"**Incidents evaluated:** {n} of 4",
        "",
    ]
    if notes:
        for note in notes:
            lines.append(f"> **Note:** {note}")
            lines.append("")
    lines.extend([
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Overall accuracy | {MATCH_PLACEHOLDER} |",
        f"| Average investigation time | {_fmt_seconds(avg_duration)} |",
        f"| Average tokens / incident | {_fmt_tokens(int(avg_tokens) if avg_tokens is not None else None)} |",
        f"| Average cost / incident | {_fmt_cost(avg_cost)} |",
        f"| Total tokens (all incidents) | {_fmt_tokens(total_tokens)} |",
        f"| Total cost (all incidents) | {_fmt_cost(total_cost)} |",
        "",
        "> **Accuracy / Match:** fill in by hand after reading each incident's "
        "predicted root cause against its ground truth. Do not treat a "
        "partial mechanism match as a full hit.",
        "",
        "## Per-incident results",
        "",
        "| Incident | Match | Confidence | Retries | Evidence | Duration | Tokens | Cost |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])

    for r in results:
        lines.append(
            f"| #{r['incident']} {r['title']} "
            f"| {r['match']} "
            f"| {r['confidence']:.2f} "
            f"| {r['retries']} "
            f"| {r['evidence_count']} "
            f"| {_fmt_seconds(r['duration_seconds'])} "
            f"| {_fmt_tokens(r['tokens'])} "
            f"| {_fmt_cost(r['cost_usd'])} |"
        )

    lines.extend(["", "## Detail", ""])

    for r in results:
        lines.extend(
            [
                f"### Incident #{r['incident']} — {r['title']}",
                "",
                f"**Status:** `{r['status']}`  ",
                f"**Match:** {r['match']}  ",
                f"**Confidence:** {r['confidence']:.2f}  ",
                f"**Retry loops:** {r['retries']}  ",
                f"**Evidence entries:** {r['evidence_count']}  ",
                f"**Duration:** {_fmt_seconds(r['duration_seconds'])}  ",
                f"**Tokens:** {_fmt_tokens(r['tokens'])}  ",
                f"**Cost:** {_fmt_cost(r['cost_usd'])}  ",
            ]
        )
        if r.get("investigation_id"):
            lines.append(f"**Investigation id:** `{r['investigation_id']}`  ")
        if r.get("trace_url"):
            lines.append(f"**Langfuse trace:** {r['trace_url']}  ")
        lines.extend(
            [
                "",
                "**Issue**",
                "",
                f"> {r['issue']}",
                "",
                "**Predicted root cause**",
                "",
                _escape_md_cell(r["predicted"]),
                "",
                "**Ground truth**",
                "",
                _escape_md_cell(r["ground_truth"]),
                "",
            ]
        )

    lines.extend(
        [
            "## How to reproduce",
            "",
            "```bash",
            "# Official Gemini run (default provider):",
            "python tests/run_eval.py",
            "",
            "# Dev / harness check against Groq:",
            '$env:LLM_PROVIDER="groq"; python tests/run_eval.py --output eval_report_harness_check.md',
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Step 11 end-to-end evaluation and write eval_report.md."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Markdown report path (default: {DEFAULT_REPORT_PATH.name})",
    )
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Optional note block quoted in the report header (repeatable).",
    )
    parser.add_argument(
        "incidents",
        nargs="*",
        default=list(INCIDENTS),
        help="Optional subset of incident keys (1 2 3 4). Default: all four.",
    )
    args = parser.parse_args(argv)

    selected = [str(key) for key in args.incidents]
    unknown = [key for key in selected if key not in INCIDENTS]
    if unknown:
        print(
            f"Unknown incident(s): {', '.join(unknown)}. "
            f"Choose from {', '.join(INCIDENTS)}."
        )
        return 1

    output_path = args.output
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    llm_label = active_model_label()
    print(f"LLM: {llm_label}")
    print(f"Tracing: {'on' if tracing_enabled() else 'off'}")
    print(f"Report: {output_path}\n")

    results: list[dict[str, Any]] = []
    failures: list[str] = []

    for key in selected:
        print(f"Running incident #{key}...")
        try:
            result = run_one(key)
        except Exception:
            # Stop the sweep on failure so a mid-run error is reported
            # exactly once rather than burning more provider quota.
            print(f"  FAILED:\n{traceback.format_exc()}")
            failures.append(key)
            break

        results.append(result)
        print(
            f"  status={result['status']} confidence={result['confidence']:.2f} "
            f"retries={result['retries']} evidence={result['evidence_count']} "
            f"duration={_fmt_seconds(result['duration_seconds'])} "
            f"tokens={_fmt_tokens(result['tokens'])} cost={_fmt_cost(result['cost_usd'])}"
        )

    report = render_report(results, llm_label=llm_label, notes=args.note or None)
    if failures:
        report += (
            f"\n## Incomplete run\n\n"
            f"Stopped after failure on incident(s): {', '.join(failures)}. "
            f"Remaining incidents were not executed.\n"
        )
    output_path.write_text(report, encoding="utf-8")
    print(f"\nWrote {output_path}")

    print("\nResetting the sandbox warehouse to its clean baseline...")
    common.reset_to_clean_baseline()
    ingest()
    print("Done.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
