"""The Ollama client.

**Why the native ``/api/chat`` endpoint and not ``/v1/chat/completions``.**

``qwen3.5:4b-mlx`` is a reasoning model. Asked through the OpenAI-compatible
endpoint it puts its chain of thought in a non-standard ``reasoning`` field
and leaves ``content`` empty until the thinking finishes -- measured on this
machine at 2026-09-15: a 300-token budget was entirely consumed by reasoning,
``finish_reason`` came back ``length``, and the answer was an empty string.

Ollama's native endpoint takes ``think: false``, which the OpenAI-compatible
one does not expose. With it, the same prompt returned ``SELECT 1;`` in a few
tokens. That is roughly a tenfold difference in tokens per item, which is the
difference between an overnight pass finishing and not.

``think`` is therefore a **config axis, not a constant**: whether reasoning
helps text-to-SQL accuracy enough to pay for its tokens is an open question
and one of the things this harness exists to measure. The default is off
because the cheap arm should be the baseline, not the special case.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULT_BASE_URL", "ModelError", "OllamaChat", "OllamaEmbedder"]

DEFAULT_BASE_URL = "http://localhost:11434"


class ModelError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return parsed
    except urllib.error.URLError as e:
        raise ModelError(f"{url}: {e}") from e
    except json.JSONDecodeError as e:
        raise ModelError(f"{url}: response was not JSON: {e}") from e


@dataclass(frozen=True)
class ChatResponse:
    """One completion, with the accounting kept beside it.

    ``reasoning`` is stored even when thinking is off (it is then empty), so a
    run's records have one shape regardless of the arm that produced them.
    """

    content: str
    reasoning: str
    usage: dict[str, Any]


class OllamaChat:
    """Chat completions against a local Ollama model."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.0,
        think: bool = False,
        num_predict: int = 512,
        timeout_s: float = 300.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.think = think
        self.num_predict = num_predict
        self.timeout_s = timeout_s

    @property
    def config(self) -> dict[str, Any]:
        """The generation settings, for storing with every run.

        A score measured at ``think=False`` and one measured at ``think=True``
        are not the same measurement, so the flag travels with the number.
        """
        return {
            "model": self.model,
            "temperature": self.temperature,
            "think": self.think,
            "num_predict": self.num_predict,
        }

    def complete(self, system: str, user: str) -> ChatResponse:
        """One turn. Returns content, reasoning and token accounting."""
        data = _post(
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "think": self.think,
                "stream": False,
                "options": {"temperature": self.temperature, "num_predict": self.num_predict},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            self.timeout_s,
        )
        if "error" in data:
            raise ModelError(str(data["error"]))
        message = data.get("message") or {}
        return ChatResponse(
            content=str(message.get("content") or ""),
            reasoning=str(message.get("thinking") or ""),
            usage={
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
                "eval_duration_ns": data.get("eval_duration"),
                "done_reason": data.get("done_reason"),
            },
        )


class OllamaEmbedder:
    """Embeddings for the retrieval indexes.

    A different model from the generator on purpose: ``qwen3-embedding:0.6b``
    is 639 MB, so generator and embedder together sit at roughly 4.7 GB and
    both stay resident on a 16 GB machine. Reloading one per query would
    dominate the wall clock of an overnight pass.
    """

    def __init__(
        self,
        model: str = "qwen3-embedding:0.6b",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        """The embedding width, discovered once by embedding a probe string.

        Discovered rather than hardcoded because the vector table's schema
        depends on it, and a mismatch between a table built at one width and
        vectors written at another fails deep inside sqlite-vec with an error
        that says nothing about the cause.
        """
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings."""
        if not texts:
            return []
        data = _post(
            f"{self.base_url}/api/embed",
            {"model": self.model, "input": texts},
            self.timeout_s,
        )
        if "error" in data:
            raise ModelError(str(data["error"]))
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ModelError(f"expected {len(texts)} embeddings, got {type(vectors).__name__}")
        return [[float(x) for x in v] for v in vectors]
