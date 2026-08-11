"""Embedding function selection for the sandbox_data Chroma vector store.

Ingest (`python -m app.retrieval.ingest`) and query
(`app.retrieval.retriever` / the MCP retrieval server) must use the
*same* embedding model. Chroma persists the embedding function on the
collection; opening it with a different one raises a conflict (the
failure seen when an ONNX-built index is later queried with Gemini).

Selection (shared by ingest and query):

  EMBEDDING_PROVIDER=onnx|gemini
    - onnx   → Chroma's bundled ONNXMiniLM_L6_V2 (fully local/offline)
    - gemini → Google Gemini embeddings (needs a real GOOGLE_API_KEY)

  If EMBEDDING_PROVIDER is unset:
    - use gemini when GOOGLE_API_KEY is configured (non-placeholder)
    - otherwise use onnx

Querying an *existing* collection always prefers the embedding function
already persisted on that collection, so a leftover local index stays
queryable even if the process now has a Gemini key. Changing providers
for a fresh index requires re-running ingest (which rebuilds the
collection with the currently configured provider).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from chromadb.utils.embedding_functions import (
    ONNXMiniLM_L6_V2,
    GoogleGeminiEmbeddingFunction,
)
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
PROVIDERS = ("onnx", "gemini")

# Matches app/graph/llm.py: .env.example placeholders must not select Gemini.
PLACEHOLDER_PREFIX = "your-"

# Chroma's persisted embedding_function.name values.
CHROMA_NAME_TO_PROVIDER = {
    "onnx_mini_lm_l6_v2": "onnx",
    "google_gemini": "gemini",
}
PROVIDER_TO_CHROMA_NAME = {v: k for k, v in CHROMA_NAME_TO_PROVIDER.items()}

_embedding_functions: dict[str, Any] = {}


def _google_api_key_configured() -> bool:
    value = (os.getenv("GOOGLE_API_KEY") or "").strip()
    return bool(value) and not value.lower().startswith(PLACEHOLDER_PREFIX)


def resolve_embedding_provider() -> str:
    """Provider that new collections / ingest should use."""
    raw = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
    if raw in ("onnx", "local", "minilm"):
        return "onnx"
    if raw in ("gemini", "google"):
        if not _google_api_key_configured():
            logger.warning(
                "EMBEDDING_PROVIDER=gemini but GOOGLE_API_KEY is missing or "
                "still a placeholder; falling back to onnx."
            )
            return "onnx"
        return "gemini"
    if raw:
        logger.warning(
            "Unknown EMBEDDING_PROVIDER=%r; expected one of %s. "
            "Falling back to auto-detect.",
            raw,
            ", ".join(PROVIDERS),
        )

    if _google_api_key_configured():
        return "gemini"
    return "onnx"


def provider_from_chroma_name(name: str | None) -> Optional[str]:
    if not name:
        return None
    return CHROMA_NAME_TO_PROVIDER.get(name)


def get_embedding_function(provider: str | None = None):
    """Returns a process-wide singleton Chroma embedding function.

    Cached per provider so the (potentially heavyweight) embedding
    backend is only initialized once per process.
    """
    chosen = provider or resolve_embedding_provider()
    if chosen not in PROVIDERS:
        raise ValueError(f"Unknown embedding provider: {chosen!r}")

    cached = _embedding_functions.get(chosen)
    if cached is not None:
        return cached

    if chosen == "gemini":
        if not _google_api_key_configured():
            raise RuntimeError(
                "Gemini embeddings require a real GOOGLE_API_KEY "
                "(not a placeholder)."
            )
        fn = GoogleGeminiEmbeddingFunction(
            model_name=GEMINI_EMBEDDING_MODEL,
            api_key_env_var="GOOGLE_API_KEY",
        )
    else:
        fn = ONNXMiniLM_L6_V2()

    _embedding_functions[chosen] = fn
    return fn


def clear_embedding_function_cache() -> None:
    """Test helper: drop cached embedding function instances."""
    _embedding_functions.clear()
