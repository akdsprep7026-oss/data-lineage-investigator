"""Embedding function selection for the sandbox_data Chroma vector store.

Mirrors the DATABASE_URL fallback pattern in app/db/base.py: if a
GOOGLE_API_KEY is configured (see .env), real Gemini embeddings are used
-- this is what production/staging should do. Otherwise we transparently
fall back to Chroma's bundled ONNXMiniLM_L6_V2 model, a fully local
embedding model that implements the same functionality as
sentence-transformers' "all-MiniLM-L6-v2" without requiring torch or a
separate sentence-transformers install (chromadb, onnxruntime and
tokenizers -- its only dependencies -- are already installed). This
keeps retrieval usable out of the box on a fresh checkout with no API
key configured.
"""

from __future__ import annotations

import os

from chromadb.utils.embedding_functions import (
    ONNXMiniLM_L6_V2,
    GoogleGeminiEmbeddingFunction,
)
from dotenv import load_dotenv

load_dotenv()

GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

_embedding_function = None


def get_embedding_function():
    """Returns a process-wide singleton Chroma embedding function.

    Uses Gemini if GOOGLE_API_KEY is set, otherwise falls back to the
    local ONNX MiniLM model. Cached so the (potentially heavyweight,
    e.g. loading the ONNX model into memory) embedding backend is only
    initialized once per process.
    """
    global _embedding_function
    if _embedding_function is not None:
        return _embedding_function

    if os.getenv("GOOGLE_API_KEY"):
        _embedding_function = GoogleGeminiEmbeddingFunction(
            model_name=GEMINI_EMBEDDING_MODEL,
            api_key_env_var="GOOGLE_API_KEY",
        )
    else:
        _embedding_function = ONNXMiniLM_L6_V2()
    return _embedding_function
