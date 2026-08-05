"""CLI to apply/reset sandbox incident scenarios.

Exactly one incident is ever active at a time: `apply` always resets to
the clean baseline first, then injects that one scenario's bug.

Usage:
    python -m app.sandbox_data.incidents.manage reset
    python -m app.sandbox_data.incidents.manage apply <1|2|3|4>
    python -m app.sandbox_data.incidents.manage status
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.sandbox_data.incidents import (
    common,
    incident_01_join_bug,
    incident_02_stale_pipeline,
    incident_03_schema_change,
    incident_04_duplicate_rows,
)
from app.sandbox_data.models import get_engine

INCIDENTS = {
    "1": incident_01_join_bug,
    "2": incident_02_stale_pipeline,
    "3": incident_03_schema_change,
    "4": incident_04_duplicate_rows,
}


def print_status() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        counts = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            for table in (
                "raw_customers",
                "raw_orders",
                "stg_orders_cleaned",
                "fct_daily_revenue",
            )
        }
    print("Current row counts:")
    for table, count in counts.items():
        print(f"  {table}: {count}")


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 1

    command = argv[0]

    if command == "reset":
        common.reset_to_clean_baseline()
        print("Reset to clean baseline.")
        print_status()
        return 0

    if command == "apply":
        if len(argv) < 2 or argv[1] not in INCIDENTS:
            print(f"Usage: apply <{'|'.join(INCIDENTS)}>")
            return 1
        INCIDENTS[argv[1]].apply()
        print_status()
        return 0

    if command == "status":
        print_status()
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
