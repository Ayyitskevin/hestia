"""Shared xAI HTTP transport; domain prompts and response validation stay local."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("hestia.xai")


class XaiTransport:
    """Apply one connection, authentication, and status policy to xAI requests."""

    def __init__(self, settings: Settings):
        self._base_url = settings.xai_base_url
        self._api_key = settings.xai_api_key
        self._model = settings.xai_model

    def post(self, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        response = None
        try:
            with httpx.Client(base_url=self._base_url, timeout=timeout) as client:
                response = client.post(
                    path,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    **kwargs,
                )
            response.raise_for_status()
        except Exception:
            log.warning(
                "xai request failed",
                extra={
                    "action": "xai.request",
                    "path": path,
                    "status": getattr(response, "status_code", "error"),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise
        log.info(
            "xai request completed",
            extra={
                "action": "xai.request",
                "path": path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
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
