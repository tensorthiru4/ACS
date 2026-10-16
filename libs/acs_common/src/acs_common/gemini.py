"""Gemini Flash (Google AI Studio free tier) with schema-constrained JSON output.

* The API key comes from `GOOGLE_API_KEY`, injected from Secret Manager by Cloud Run.
* Transient overloads (500/503/504) are retried by the SDK. 429 is left out on
  purpose: retrying a quota error only burns more of the quota.
* `generate_json` asks for `application/json` constrained by a JSON Schema and
  parses the reply. Callers must still validate it (pydantic / jsonschema): the
  model's output is untrusted input like any other.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class ModelError(RuntimeError):
    """The model call failed or returned unusable output."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def genai_client() -> Any:
    from google import genai
    from google.genai import types

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    return genai.Client(
        http_options=types.HttpOptions(
            timeout=120_000,
            retry_options=types.HttpRetryOptions(
                attempts=4, initial_delay=2.0, max_delay=16.0, http_status_codes=[500, 503, 504]
            ),
        )
    )


def _raise_model_error(exc: Exception) -> None:
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        transient = exc.code in (429, 500, 502, 503, 504)
        logger.warning("model call failed: %s %s", exc.code, exc.status)
        raise ModelError(f"model call failed ({exc.code})", transient=transient) from exc
    raise ModelError("model call failed", transient=True) from exc


async def generate_json(
    client: Any,
    model: str,
    *,
    system: str,
    parts: list[Any],
    schema: dict[str, Any],
    max_output_tokens: int = 16384,
) -> dict[str, Any]:
    """One schema-constrained generation. `parts` are genai Parts or strings.

    Gemini 3.x Flash "thinks" before answering and those tokens count against
    `max_output_tokens`, so the budget is generous; a reply cut off at the limit is
    reported as such rather than as malformed JSON.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
    )
    try:
        response = await client.aio.models.generate_content(
            model=model, contents=parts, config=config
        )
    except Exception as exc:
        _raise_model_error(exc)
        raise  # unreachable; keeps type checkers happy
    text = response.text or ""
    try:
        value = json.loads(text)
    except ValueError as exc:
        candidates = getattr(response, "candidates", None) or []
        finish = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        if "MAX_TOKENS" in finish:
            raise ModelError("model reply was cut off at the output-token limit") from exc
        raise ModelError("model returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ModelError("model returned a non-object JSON value")
    return value


async def embed(
    client: Any, model: str, texts: list[str], *, task_type: str, dimensions: int
) -> list[list[float]]:
    """Batch text embeddings (one request for all texts)."""
    from google.genai import types

    if not texts:
        return []
    try:
        response = await client.aio.models.embed_content(
            model=model,
            contents=texts,
            config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=dimensions),
        )
    except Exception as exc:
        _raise_model_error(exc)
        raise
    vectors = [list(e.values or []) for e in (response.embeddings or [])]
    if len(vectors) != len(texts) or any(len(v) != dimensions for v in vectors):
        raise ModelError("embedding response has the wrong shape")
    return vectors
