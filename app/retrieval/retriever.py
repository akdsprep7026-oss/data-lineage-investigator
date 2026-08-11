"""Similarity search over the sandbox_data Chroma collection built by
app.retrieval.ingest. This is the interface an agent should import to
pull relevant SQL models / pipeline jobs / dashboard widgets into its
context.
"""

from __future__ import annotations

from typing import Any, Optional

from app.retrieval.ingest import get_collection


def retrieve(
    query: str, filter_type: Optional[str] = None, n_results: int = 5
) -> list[dict[str, Any]]:
    """Runs a similarity search against the sandbox_data collection.

    Args:
        query: natural-language search text.
        filter_type: optional metadata "type" filter -- one of
            "sql_model", "pipeline_job", or "dashboard_widget". If None
            (default), searches across all document types.
        n_results: maximum number of results to return (default 5).

    Returns:
        A list of up to n_results dicts, ordered most-relevant first:
        {"document": str, "metadata": dict, "distance": float}.
        Lower distance means more similar.

    Opens the collection via get_collection(), which reuses the
    embedding function persisted at ingest time so query never conflicts
    with the index (e.g. ONNX-built index + Gemini key in the env).
    """
    collection = get_collection()

    where = {"type": filter_type} if filter_type else None
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where,
    )

    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    return [
        {"document": document, "metadata": metadata, "distance": distance}
        for document, metadata, distance in zip(documents, metadatas, distances)
    ]
