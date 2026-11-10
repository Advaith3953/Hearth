"""Talking to the local language model. Only a model on this computer is ever contacted."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ModelError(Exception):
    pass


class Model(Protocol):
    def chat(self, messages: list[dict[str, str]]) -> str: ...


def check_local_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "http" or (u.hostname or "") not in LOOPBACK:
        raise ValueError("Hearth only talks to a model running on this computer (http://127.0.0.1:...), "
                         f"not '{url}'")
    return url.rstrip("/")


class OllamaModel:
    """Chat with a model served by Ollama on localhost. Proxies are bypassed on purpose."""

    def __init__(self, name: str, url: str = "http://127.0.0.1:11434", timeout: float = 300.0,
                 num_ctx: int = 8192) -> None:
        self.name = name
        self.url = check_local_url(url)
        self.timeout = timeout
        self.num_ctx = num_ctx
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, path: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise ModelError(f"the model server answered {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ModelError("cannot reach Ollama on this computer. Is it running? (start it, then try again)") from e
        if not isinstance(data, dict):
            raise ModelError("unexpected answer from the model server")
        return data

    def installed(self) -> list[str]:
        data = self._call("/api/tags", timeout=5)
        return [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]

    def _body(self, messages: list[dict[str, str]], temperature: float, repeat_penalty: float) -> dict[str, Any]:
        return {"model": self.name, "messages": messages, "stream": False, "format": "json",
                "options": {"temperature": temperature, "num_ctx": self.num_ctx, "repeat_penalty": repeat_penalty}}

    def chat(self, messages: list[dict[str, str]]) -> str:
        try:
            data = self._call("/api/chat", self._body(messages, 0.1, 1.0))
        except ModelError as e:
            if "repeat" not in str(e):
                raise
            # Ollama aborts a reply when the model loops on the same token; one retry with some randomness usually fixes it
            data = self._call("/api/chat", self._body(messages, 0.7, 1.15))
        msg = data.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return str(msg["content"])
        raise ModelError(str(data.get("error") or "the model returned no message (is the model installed?)"))


class ScriptedModel:
    """A stand-in model for tests and the eval self-test: replies come from a list or a function."""

    def __init__(self, replies: list[str] | Callable[[list[dict[str, str]]], str]) -> None:
        self.replies = replies
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls.append([dict(m) for m in messages])
        if callable(self.replies):
            return self.replies(messages)
        if not self.replies:
            raise ModelError("scripted model ran out of replies")
        return self.replies.pop(0)
