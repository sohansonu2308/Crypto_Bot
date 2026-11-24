"""OpenAI-compatible LLM client with structured Pydantic parsing and retry backoff."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from src.config import LLMConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_payload(raw_text: str) -> Any:
    """Extract and parse a JSON object/array from raw LLM output, stripping markdown fences."""
    if not raw_text or not raw_text.strip():
        raise ValueError("LLM returned an empty response.")

    cleaned = raw_text.strip()

    # 1. Try fenced code blocks first if present
    fence_match = _CODE_FENCE_RE.search(cleaned)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = candidate

    # 2. Try direct JSON parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. Extract outermost JSON object {...} or array [...]
    obj_start = cleaned.find("{")
    obj_end = cleaned.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        candidate = cleaned[obj_start : obj_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    arr_start = cleaned.find("[")
    arr_end = cleaned.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        candidate = cleaned[arr_start : arr_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract valid JSON from LLM response: {raw_text[:300]!r}")


class LLMClient:
    """Thin wrapper around the official OpenAI SDK targeting any OpenAI-compatible endpoint."""

    def __init__(self, config: LLMConfig, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
        else:
            if not config.api_key:
                raise ValueError("LLM_API_KEY is required to initialize LLMClient.")
            kwargs: dict[str, Any] = {
                "base_url": config.base_url,
                "api_key": config.api_key,
                "timeout": config.timeout_seconds,
                "max_retries": 0,  # Managed explicitly below with jittered exponential backoff
            }
            if config.extra_headers:
                kwargs["default_headers"] = config.extra_headers
            self.client = OpenAI(**kwargs)

    def complete_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        temperature: float | None = None,
    ) -> T:
        """Call Chat Completions and validate the response into `response_model`.

        Attempts `response_format={"type": "json_schema", ...}` first, and automatically falls back
        to prompt-guided JSON extraction if the provider rejects `json_schema`.
        """
        schema = response_model.model_json_schema()
        schema_str = json.dumps(schema, ensure_ascii=False)
        guided_system_prompt = (
            f"{system_prompt}\n\n"
            "CRITICAL OUTPUT FORMAT:\n"
            "Respond ONLY with a single valid JSON object matching this JSON Schema. "
            "Do not include commentary or markdown outside the JSON object.\n"
            f"JSON Schema:\n{schema_str}"
        )

        use_json_schema = True
        last_error: Exception | None = None
        temp = self.config.temperature if temperature is None else temperature

        for attempt in range(self.config.max_retries + 1):
            request_kwargs: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": guided_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temp,
            }
            if use_json_schema:
                request_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_model.__name__,
                        "schema": schema,
                        "strict": False,
                    },
                }

            try:
                response = self.client.chat.completions.create(**request_kwargs)
                content = response.choices[0].message.content or ""
                parsed = extract_json_payload(content)
                return response_model.model_validate(parsed)

            except BadRequestError as exc:
                # Many third-party OpenAI-compatible gateways reject response_format=json_schema.
                if use_json_schema:
                    logger.warning(
                        "Provider rejected response_format=json_schema (%s); falling back to JSON extraction.",
                        exc,
                    )
                    use_json_schema = False
                    last_error = exc
                    continue
                raise

            except (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                self._sleep_backoff(attempt, reason=type(exc).__name__)

            except APIStatusError as exc:
                status = getattr(exc, "status_code", 0)
                if status in (400, 422) and use_json_schema:
                    logger.warning(
                        "Provider returned HTTP %s with json_schema; falling back to JSON extraction.",
                        status,
                    )
                    use_json_schema = False
                    last_error = exc
                    continue
                if status == 429 or status >= 500:
                    last_error = exc
                    if attempt >= self.config.max_retries:
                        break
                    self._sleep_backoff(attempt, reason=f"HTTP {status}")
                else:
                    raise

            except (ValueError, ValidationError) as exc:
                last_error = exc
                if use_json_schema:
                    # Retry without json_schema in case the provider returned malformed wrapper output
                    use_json_schema = False
                if attempt >= self.config.max_retries:
                    break
                logger.warning(
                    "Structured output validation failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.config.max_retries + 1,
                    exc,
                )
                self._sleep_backoff(attempt, reason="ValidationError")

        raise RuntimeError(
            f"LLM call to model '{model}' failed after {self.config.max_retries + 1} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _sleep_backoff(attempt: int, reason: str) -> None:
        delay = min(30.0, (2.0**attempt) + random.uniform(0.25, 1.0))
        logger.warning("Retrying LLM request after %s in %.2fs...", reason, delay)
        time.sleep(delay)
