"""Embedding provider selection + ingest/retrieve compatibility."""

from __future__ import annotations

import pytest

from app.retrieval.embeddings import (
    PROVIDER_TO_CHROMA_NAME,
    clear_embedding_function_cache,
    get_embedding_function,
    provider_from_chroma_name,
    resolve_embedding_provider,
)
from app.retrieval.ingest import COLLECTION_NAME, get_chroma_client, get_collection, ingest
from app.retrieval.retriever import retrieve


@pytest.fixture(autouse=True)
def _reset_embedding_cache():
    clear_embedding_function_cache()
    yield
    clear_embedding_function_cache()


def test_resolve_embedding_provider_honours_explicit_onnx(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "real-looking-but-unused-key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "onnx")
    assert resolve_embedding_provider() == "onnx"


def test_resolve_embedding_provider_defaults_to_onnx_without_google_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    assert resolve_embedding_provider() == "onnx"


def test_resolve_embedding_provider_ignores_placeholder_google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "your-google-api-key-here")
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    assert resolve_embedding_provider() == "onnx"


def test_resolve_embedding_provider_gemini_requires_real_key(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "your-google-api-key-here")
    assert resolve_embedding_provider() == "onnx"


def test_get_embedding_function_onnx_name(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "onnx")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    fn = get_embedding_function()
    assert fn.name() == PROVIDER_TO_CHROMA_NAME["onnx"]


def test_ingest_and_retrieve_stay_compatible_when_google_key_appears_later(
    monkeypatch,
):
    """Index built with onnx must still be queryable if a Gemini key is set.

    This is the failure mode from local integration: collection persisted
    as onnx_mini_lm_l6_v2 while get_or_create_collection later passed
    google_gemini because GOOGLE_API_KEY was present.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "onnx")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    clear_embedding_function_cache()
    count = ingest(reset=True)
    assert count > 0

    client = get_chroma_client()
    collection = client.get_collection(COLLECTION_NAME)
    persisted = collection.configuration_json["embedding_function"]["name"]
    assert provider_from_chroma_name(persisted) == "onnx"

    # Simulate a later process that has a Gemini key / default auto-detect.
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-but-non-placeholder-key-for-test")
    clear_embedding_function_cache()

    # Opening for query must not raise an embedding-function conflict.
    opened = get_collection()
    opened_name = opened.configuration_json["embedding_function"]["name"]
    assert provider_from_chroma_name(opened_name) == "onnx"

    hits = retrieve("daily revenue dashboard", n_results=3)
    assert isinstance(hits, list)
    assert len(hits) > 0
