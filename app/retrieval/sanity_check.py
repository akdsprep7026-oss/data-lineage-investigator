"""Quick sanity-check script for the Step 4 retrieval layer.

(Re)ingests the current sandbox_data into Chroma and runs a query
against it, printing the top matches with their metadata so you can
eyeball whether retrieval surfaces the actually-relevant SQL
model/pipeline job/dashboard widget instead of a random one.

Usage:
    python -m app.retrieval.sanity_check
    python -m app.retrieval.sanity_check "some other query"
    python -m app.retrieval.sanity_check "duplicate orders" --type sql_model
"""

from __future__ import annotations

import sys

from app.retrieval.ingest import ingest
from app.retrieval.retriever import retrieve

DEFAULT_QUERY = "revenue calculation region"


def main() -> None:
    args = sys.argv[1:]
    filter_type = None
    if "--type" in args:
        idx = args.index("--type")
        filter_type = args[idx + 1]
        del args[idx : idx + 2]
    query = " ".join(args) or DEFAULT_QUERY

    print("Ingesting sandbox_data into Chroma...")
    count = ingest()
    print(f"  -> indexed {count} documents\n")

    print(f'Query: "{query}"' + (f" (filter_type={filter_type})" if filter_type else ""))
    print("-" * 70)

    hits = retrieve(query, filter_type=filter_type)
    if not hits:
        print("No results.")
        return

    for rank, hit in enumerate(hits, start=1):
        meta = hit["metadata"]
        preview = " ".join(hit["document"].split())[:100]
        print(f"{rank}. type={meta.get('type')} distance={hit['distance']:.4f}")
        print(f"   metadata: {meta}")
        print(f"   preview:  {preview}...")
        print()


if __name__ == "__main__":
    main()
