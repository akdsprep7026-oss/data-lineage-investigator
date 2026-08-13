"""R4 incremental LLM-only campaign runner (eval only).

Runs benchmark investigations one at a time, records claim_source, and
STOPS when the direct LLM path is exhausted (consecutive fallbacks), so
fallback runs are never treated as LLM calibration data.

Usage:

    $env:LLM_PROVIDER=\"groq\"
    python -m tests.run_r4_campaign --per-incident 8 --max-fallback-streak 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from tests.eval_root_cause import (  # noqa: E402
    CLAIM_SOURCE_LLM,
    load_benchmarks,
    run_one_benchmark,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-incident", type=int, default=8)
    parser.add_argument(
        "--max-fallback-streak",
        type=int,
        default=2,
        help="Stop after this many consecutive non-LLM root-cause outcomes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "eval_root_cause_results_r4.json",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=2.0,
        help="Pause between runs to reduce burst throttling",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing DIRECT LLM rows in --output and fill remaining slots",
    )
    args = parser.parse_args(argv)

    benchmarks = load_benchmarks()
    all_results: list[dict[str, Any]] = []
    llm_count = 0
    completed_llm_by_incident: dict[str, int] = {item.key: 0 for item in benchmarks}

    if args.resume and args.output.exists():
        prior = json.loads(args.output.read_text(encoding="utf-8"))
        for row in prior.get("runs") or []:
            source = (row.get("score") or {}).get("claim_source")
            if source != CLAIM_SOURCE_LLM:
                continue
            all_results.append(row)
            llm_count += 1
            key = str(row.get("benchmark_key"))
            if key in completed_llm_by_incident:
                completed_llm_by_incident[key] += 1
        print(
            f"Resumed {llm_count} prior DIRECT LLM run(s) from {args.output}; "
            f"prior fallback rows are not carried forward."
        )

    fallback_streak = 0
    stopped_reason = "completed"

    for benchmark in benchmarks:
        already = completed_llm_by_incident.get(benchmark.key, 0)
        for run_idx in range(already + 1, args.per_incident + 1):
            print(
                f"R4 incident #{benchmark.key} run {run_idx}/{args.per_incident} "
                f"(llm_so_far={llm_count})..."
            )
            result = run_one_benchmark(benchmark)
            result["run"] = run_idx
            source = (result.get("score") or {}).get("claim_source")
            result["score"]["campaign"] = "r4"
            all_results.append(result)
            score = result["score"]
            print(
                f"  source={source} status={score.get('status')} "
                f"kind={score.get('structured_claim_kind')} "
                f"conf={score.get('confidence')} "
                f"e2e={score.get('end_to_end_correct')}"
            )

            # Persist after every run so a stop still leaves a usable file.
            payload = {
                "campaign": "r4",
                "benchmarks": [asdict(item) for item in benchmarks],
                "runs": all_results,
                "stopped_reason": None,
                "llm_count": llm_count,
            }
            if source == CLAIM_SOURCE_LLM:
                llm_count += 1
                completed_llm_by_incident[benchmark.key] = (
                    completed_llm_by_incident.get(benchmark.key, 0) + 1
                )
                fallback_streak = 0
                payload["llm_count"] = llm_count
            else:
                fallback_streak += 1
                print(
                    f"  non-LLM source={source}; "
                    f"fallback_streak={fallback_streak}/{args.max_fallback_streak}"
                )
                if fallback_streak >= args.max_fallback_streak:
                    stopped_reason = (
                        f"stopped_after_{fallback_streak}_consecutive_non_llm"
                    )
                    payload["stopped_reason"] = stopped_reason
                    payload["llm_count"] = llm_count
                    args.output.write_text(
                        json.dumps(payload, indent=2, default=str),
                        encoding="utf-8",
                    )
                    print(
                        f"\nSTOP: LLM path unavailable "
                        f"(llm_count={llm_count}). Wrote {args.output}"
                    )
                    return 2

            payload["stopped_reason"] = stopped_reason if stopped_reason != "completed" else None
            payload["llm_count"] = llm_count
            args.output.write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

    payload = {
        "campaign": "r4",
        "benchmarks": [asdict(item) for item in benchmarks],
        "runs": all_results,
        "stopped_reason": "completed",
        "llm_count": llm_count,
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nCompleted. llm_count={llm_count}. Wrote {args.output}")
    return 0 if llm_count >= 30 else 3


if __name__ == "__main__":
    raise SystemExit(main())
