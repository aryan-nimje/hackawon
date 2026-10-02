"""Provider-agnostic LLM wrapper with structured output and mock mode."""

from __future__ import annotations

import json
import logging
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from config import get_settings

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMService:
    """Calls LLM providers or returns deterministic mock responses."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_mock(self) -> bool:
        return self.settings.effective_mock_mode

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 2048,
    ) -> str:
        if self.is_mock:
            return self._mock_text(user)
        return await self._call_provider(system, user, max_tokens=max_tokens)

    async def structured_output(
        self,
        system: str,
        user: str,
        schema: Type[T],
        *,
        max_retries: int = 2,
    ) -> T:
        if self.is_mock:
            return self._mock_structured(schema, user)

        last_error: Exception | None = None
        prompt_user = (
            f"{user}\n\nRespond with valid JSON matching this schema:\n"
            f"{json.dumps(schema.model_json_schema(), indent=2)}"
        )
        for attempt in range(max_retries + 1):
            raw = await self._call_provider(system, prompt_user)
            try:
                data = self._extract_json(raw)
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning("LLM structured output attempt %s failed: %s", attempt + 1, exc)
                prompt_user = (
                    f"{user}\n\nPrevious response was invalid: {exc}. "
                    f"Return ONLY valid JSON for schema {schema.__name__}."
                )
        raise ValueError(f"Failed to get valid structured output: {last_error}")

    async def _call_provider(self, system: str, user: str, *, max_tokens: int) -> str:
        provider = self.settings.llm_provider.lower()
        key = self.settings.llm_api_key
        model = self.settings.llm_model
        if not key:
            raise ValueError("LLM_API_KEY is required when not in mock mode")

        if provider == "anthropic":
            return await self._anthropic(system, user, key, model, max_tokens)
        if provider == "openai":
            return await self._openai(system, user, key, model, max_tokens)
        raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")

    async def _anthropic(
        self, system: str, user: str, key: str, model: str, max_tokens: int
    ) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    async def _openai(
        self, system: str, user: str, key: str, model: str, max_tokens: int
    ) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json(text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    @staticmethod
    def _mock_text(user: str) -> str:
        return "Mock LLM response for demo purposes."

    def _mock_structured(self, schema: Type[T], user: str) -> T:
        name = schema.__name__
        mocks: dict[str, Any] = {
            "DetectionOutput": {
                "incidents": [],
            },
        }
        if name in mocks:
            return schema.model_validate(mocks[name])
        raise ValueError(f"No mock data for schema {name}")


llm_service = LLMService()
