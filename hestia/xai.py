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

    def post(
        self,
        path: str,
        *,
        timeout: float,
        max_response_bytes: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        started = time.monotonic()
        response = None
        try:
            with httpx.Client(base_url=self._base_url, timeout=timeout) as client:
                request_kwargs = {
                    "headers": {"Authorization": f"Bearer {self._api_key}"},
                    **kwargs,
                }
                if max_response_bytes is None:
                    response = client.post(path, **request_kwargs)
                    response.raise_for_status()
                else:
                    response = self._bounded_post(
                        client,
                        path,
                        max_response_bytes=max_response_bytes,
                        **request_kwargs,
                    )
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

    @staticmethod
    def _bounded_post(
        client: httpx.Client,
        path: str,
        *,
        max_response_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        with client.stream("POST", path, **kwargs) as streamed:
            streamed.raise_for_status()
            content_length = streamed.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise ValueError("xai response has an invalid Content-Length") from exc
                if declared_length > max_response_bytes:
                    raise ValueError("xai response exceeds the transport size limit")

            body = bytearray()
            for chunk in streamed.iter_bytes():
                if len(body) + len(chunk) > max_response_bytes:
                    raise ValueError("xai response exceeds the transport size limit")
                body.extend(chunk)
            decoded_headers = [
                (name, value)
                for name, value in streamed.headers.multi_items()
                if name.lower()
                not in {"content-encoding", "content-length", "transfer-encoding"}
            ]
            return httpx.Response(
                streamed.status_code,
                headers=decoded_headers,
                content=bytes(body),
                request=streamed.request,
                extensions=streamed.extensions,
            )

    def chat_content(
        self,
        *,
        messages: list[dict],
        temperature: float,
        timeout: float = 60,
        max_response_bytes: int | None = None,
    ) -> str:
        response = self.post(
            "/chat/completions",
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
            },
        )
        return response.json()["choices"][0]["message"]["content"]
