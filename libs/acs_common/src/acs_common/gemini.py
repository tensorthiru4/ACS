"""Gemini Flash (Google AI Studio free tier) with schema-constrained JSON output.

* The API key comes from `GOOGLE_API_KEY`, injected from Secret Manager by Cloud Run.
* Transient overloads (500/503/504) are retried by the SDK. 429 is left out on
  purpose: retrying a quota error only burns more of the quota.
* `generate_json` asks for `application/json` constrained by a JSON Schema and
  parses the reply. Callers must still validate it (pydantic / jsonschema): the
  model's output is untrusted input like any other.
* When Gemini is out of quota or overloaded, text-only requests fall back to Gemma
  (`ACS_FALLBACK_MODEL`, default `gemma-4-31b-it`; empty turns it off). Gemma runs on
  the same AI Studio key with a separate free-tier quota. After Gemini fails it is
  skipped for a cool-down (its `retryDelay` for a quota error, capped) so each request
  does not first wait on a model that is known to be down.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_MODEL = "gemma-4-31b-it"
MAX_COOLDOWN_S = 600.0
OVERLOAD_COOLDOWN_S = 60.0

_down_until: dict[str, float] = {}


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


def _model_error(exc: Exception) -> ModelError:
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        transient = exc.code in (429, 500, 502, 503, 504)
        logger.warning("model call failed: %s %s", exc.code, exc.status)
        return ModelError(f"model call failed ({exc.code})", transient=transient)
    return ModelError("model call failed", transient=True)


def _raise_model_error(exc: Exception) -> None:
    raise _model_error(exc) from exc


def fallback_model() -> str | None:
    return os.environ.get("ACS_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip() or None


def retry_delay_s(details: Any) -> float | None:
    """`retryDelay` ("13023s") from a Gemini 429 body, if present."""
    error = details.get("error", details) if isinstance(details, dict) else {}
    for item in error.get("details", []) if isinstance(error, dict) else []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(delay or ""))
        if match:
            return float(match.group(1))
    return None


def _mark_down(model: str, exc: Exception) -> None:
    from google.genai import errors

    delay = None
    if isinstance(exc, errors.APIError) and exc.code == 429:
        delay = retry_delay_s(exc.details)
    _down_until[model] = time.monotonic() + min(delay or OVERLOAD_COOLDOWN_S, MAX_COOLDOWN_S)


def _is_down(model: str) -> bool:
    return time.monotonic() < _down_until.get(model, 0.0)


def _text_only(parts: list[Any]) -> bool:
    """Gemma is not sent the PDFs and audio the order desk sends Gemini."""
    return all(
        isinstance(part, str)
        or bool(getattr(part, "text", None) and not getattr(part, "inline_data", None))
        for part in parts
    )


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
    fallback = fallback_model() if _text_only(parts) else None
    args = (client, system, parts, schema, max_output_tokens)
    if fallback and _is_down(model):
        try:
            return await _generate(fallback, *args)
        except ModelError:
            raise
        except Exception as exc:
            raise _model_error(exc) from exc
    try:
        return await _generate(model, *args)
    except ModelError:
        raise
    except Exception as exc:
        error = _model_error(exc)
        if fallback is None or not error.transient:
            raise error from exc
        _mark_down(model, exc)
    logger.warning("%s unavailable; falling back to %s", model, fallback)
    try:
        return await _generate(fallback, *args)
    except ModelError:
        raise
    except Exception as exc:
        logger.warning("fallback model failed too: %s", type(exc).__name__)
        raise error from exc


async def _generate(
    model: str,
    client: Any,
    system: str,
    parts: list[Any],
    schema: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    """One call to `model`. API errors propagate as-is; bad output is a ModelError."""
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        # Schema-constrained extraction needs little reasoning. At the default level Gemini 3
        # can think for minutes and spend the whole output budget before it answers.
        thinking_config=(
            types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
            if model.startswith("gemini-3")
            else None
        ),
    )
    response = await client.aio.models.generate_content(model=model, contents=parts, config=config)
    text = response.text or ""
    try:
        value = json.loads(text)
    except ValueError as exc:
        candidates = getattr(response, "candidates", None) or []
        finish = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        usage = getattr(response, "usage_metadata", None)
        logger.warning(
            "%s returned unusable output: finish=%s thoughts=%s output=%s chars=%d",
            model,
            finish or "?",
            getattr(usage, "thoughts_token_count", None),
            getattr(usage, "candidates_token_count", None),
            len(text),
        )
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
