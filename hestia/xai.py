"""Shared xAI HTTP transport; domain prompts and response validation stay local."""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class XaiTransport:
    """Apply one connection, authentication, and status policy to xAI requests."""

    def __init__(self, settings: Settings):
        self._base_url = settings.xai_base_url
        self._api_key = settings.xai_api_key
        self._model = settings.xai_model

    def post(self, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        with httpx.Client(base_url=self._base_url, timeout=timeout) as client:
            response = client.post(
                path,
                headers={"Authorization": f"Bearer {self._api_key}"},
                **kwargs,
            )
        response.raise_for_status()
        return response

    def chat_content(
        self,
        *,
        messages: list[dict],
        temperature: float,
        timeout: float = 60,
    ) -> str:
        response = self.post(
            "/chat/completions",
            timeout=timeout,
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
            },
        )
        return response.json()["choices"][0]["message"]["content"]
