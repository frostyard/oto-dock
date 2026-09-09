"""Ollama Discover reads the server's NATIVE listing (``/api/tags``), which
lives at the server root — the platform stores the OpenAI-compatible base
(``…/v1``) because Codex's provider and the chat adapter need it, so the
native root must drop that suffix (a ``/v1/api/tags`` request is a 404 and
Discover found no models on a correctly configured Ollama, 2026-09-08)."""

import pytest

from core.layers.providers.openai_compat_adapter import OllamaAdapter


@pytest.mark.parametrize("stored, root", [
    ("http://192.168.1.8:8080/v1", "http://192.168.1.8:8080"),
    ("http://192.168.1.8:8080/v1/", "http://192.168.1.8:8080"),
    ("http://host:11434/V1", "http://host:11434"),
    ("http://host:11434", "http://host:11434"),
    ("http://host:11434/", "http://host:11434"),
    ("  http://host:11434/v1  ", "http://host:11434"),
    ("", "http://localhost:11434"),
    (None, "http://localhost:11434"),
    # A reverse-proxied Ollama under a prefix keeps its prefix.
    ("https://ai.example.com/ollama/v1", "https://ai.example.com/ollama"),
])
def test_native_api_base_drops_the_openai_suffix(stored, root):
    assert OllamaAdapter.native_api_base(stored) == root


@pytest.mark.asyncio
async def test_discover_requests_the_root_api_tags(monkeypatch):
    import httpx

    seen: list[str] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen3.6-35b-a3b"}, {"name": "gemma4:9b"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    models = await OllamaAdapter().list_available_models("", "http://192.168.1.8:8080/v1")
    assert seen == ["http://192.168.1.8:8080/api/tags"]
    assert [m["model_id"] for m in models] == ["gemma4:9b", "qwen3.6-35b-a3b"]
