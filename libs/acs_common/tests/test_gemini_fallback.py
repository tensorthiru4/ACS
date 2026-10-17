from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors

from acs_common import gemini

QUOTA_BODY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "13023s"}],
    }
}


class FakeClient:
    """Stands in for genai.Client: Gemini ("m") raises `error`; other models answer."""

    def __init__(self, error: Exception, fallback_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error = error
        self.fallback_error = fallback_error
        self.aio = self
        self.models = self

    async def generate_content(self, *, model: str, contents: object, config: object) -> object:
        self.calls.append((model, config))
        if model == "m":
            raise self.error
        if self.fallback_error:
            raise self.fallback_error
        return SimpleNamespace(text='{"ok": true}', candidates=[])

    def models_called(self) -> list[str]:
        return [model for model, _ in self.calls]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(gemini, "_down_until", {})
    monkeypatch.delenv("ACS_FALLBACK_MODEL", raising=False)


async def test_quota_error_falls_back_to_gemma_then_skips_gemini():
    client = FakeClient(errors.ClientError(429, QUOTA_BODY))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    first = await gemini.generate_json(client, "m", system="sys", parts=["hi"], schema=schema)
    second = await gemini.generate_json(client, "m", system="sys", parts=["hi"], schema=schema)

    assert first == second == {"ok": True}
    # The second request went straight to Gemma during the cool-down.
    assert client.models_called() == ["m", "gemma-4-31b-it", "gemma-4-31b-it"]
    config = client.calls[1][1]
    assert config.response_json_schema == schema
    assert config.system_instruction == "sys"


async def test_fallback_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("ACS_FALLBACK_MODEL", "gemma-4-26b-a4b-it")
    client = FakeClient(errors.ServerError(503, {"error": {"code": 503}}))
    await gemini.generate_json(client, "m", system="s", parts=["x"], schema={})
    assert client.models_called() == ["m", "gemma-4-26b-a4b-it"]


async def test_empty_fallback_model_turns_it_off(monkeypatch):
    monkeypatch.setenv("ACS_FALLBACK_MODEL", "")
    client = FakeClient(errors.ClientError(429, QUOTA_BODY))
    with pytest.raises(gemini.ModelError) as info:
        await gemini.generate_json(client, "m", system="s", parts=["x"], schema={})
    assert info.value.transient
    assert client.models_called() == ["m"]


async def test_non_text_parts_do_not_fall_back():
    from google.genai import types

    client = FakeClient(errors.ClientError(429, QUOTA_BODY))
    pdf = types.Part.from_bytes(data=b"%PDF-1.7", mime_type="application/pdf")
    with pytest.raises(gemini.ModelError) as info:
        await gemini.generate_json(client, "m", system="s", parts=[pdf, "extract"], schema={})
    assert info.value.transient
    assert client.models_called() == ["m"]


async def test_bad_request_is_not_retried_on_gemma():
    client = FakeClient(errors.ClientError(400, {"error": {"code": 400}}))
    with pytest.raises(gemini.ModelError) as info:
        await gemini.generate_json(client, "m", system="s", parts=["x"], schema={})
    assert not info.value.transient
    assert client.models_called() == ["m"]


async def test_both_down_surfaces_the_gemini_error():
    client = FakeClient(
        errors.ClientError(429, QUOTA_BODY), errors.ClientError(400, {"error": {"code": 400}})
    )
    with pytest.raises(gemini.ModelError) as info:
        await gemini.generate_json(client, "m", system="s", parts=["x"], schema={})
    assert info.value.transient
    assert str(info.value) == "model call failed (429)"


async def test_gemini_3_thinks_at_low_level_and_gemma_uses_its_default():
    client = FakeClient(errors.ClientError(429, QUOTA_BODY))
    await gemini.generate_json(client, "gemini-3.8-flash", system="s", parts=["x"], schema={})
    await gemini.generate_json(client, "gemma-4-31b-it", system="s", parts=["x"], schema={})
    gemini_config, gemma_config = (config for _, config in client.calls)
    assert gemini_config.thinking_config.thinking_level == "LOW"
    assert gemma_config.thinking_config is None


def test_cooldown_uses_retry_delay_capped():
    assert gemini.retry_delay_s(QUOTA_BODY) == 13023.0
    assert gemini.retry_delay_s({"error": {}}) is None
