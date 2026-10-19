"""LLM wrapper used by agents that need language-model completions.

``LLMClient`` is the provider-agnostic interface. ``MockLLMClient`` is a
deterministic, offline implementation so the framework (and its tests) run
without network access or API keys. Plug in a real provider by subclassing
``LLMClient`` and registering it in :func:`create_llm_client`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """Raised by an LLM client when a completion cannot be produced."""


class LLMClient(ABC):
    def __init__(self, model: str, temperature: float = 0.0) -> None:
        self.model = model
        self.temperature = temperature

    @abstractmethod
    async def complete(self, system_prompt: str, prompt: str, *, max_words: int | None = None) -> str:
        """Return the model's completion for ``prompt``."""


class MockLLMClient(LLMClient):
    """Extractive "summariser" that returns the leading sentences of the prompt.

    It only looks at the text after the ``FINDINGS:`` marker (if present) so the
    instructions in the prompt are never echoed back as content.
    """

    _SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

    def __init__(self, model: str = "mock", temperature: float = 0.0) -> None:
        super().__init__(model, temperature)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system_prompt: str, prompt: str, *, max_words: int | None = None) -> str:
        self.calls.append({"system_prompt": system_prompt, "prompt": prompt, "max_words": max_words})
        _, _, findings = prompt.partition("FINDINGS:")
        text = " ".join((findings or prompt).split())
        if not text:
            return ""
        sentences = [s for s in self._SENTENCE_RE.split(text) if s]
        words: list[str] = []
        for sentence in sentences:
            words.extend(sentence.split())
            if max_words is not None and len(words) >= max_words:
                break
        if max_words is not None:
            words = words[:max_words]
        return " ".join(words)


def create_llm_client(settings: dict[str, Any] | None) -> LLMClient:
    """Build an LLM client from an agent's ``llm`` config block."""
    settings = settings or {}
    provider = settings.get("provider", "mock")
    model = str(settings.get("model", "mock"))
    temperature = float(settings.get("temperature", 0.0))
    if provider == "mock":
        return MockLLMClient(model=model, temperature=temperature)
    raise ValueError(f"unsupported LLM provider '{provider}'")
