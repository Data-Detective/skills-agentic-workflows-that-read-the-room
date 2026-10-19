"""ResearchAgent: collects (simulated) source material for a task."""

from __future__ import annotations

import asyncio
import hashlib
import random
from typing import Awaitable, Callable
from urllib.parse import urlparse

from agents.base import BaseAgent, BoundaryViolationError, PermanentAgentError, TransientAgentError
from config import AgentConfig
from state import AgentResult, TaskState

Fetcher = Callable[[str], Awaitable[str]]


class ResearchAgent(BaseAgent):
    """Fetches the URLs in ``payload["urls"]`` and returns attributed snippets.

    Scraping is simulated by default (no network access). Inject a custom
    ``fetcher`` coroutine to plug in a real HTTP client or a test double.
    """

    def __init__(
        self,
        config: AgentConfig,
        fetcher: Fetcher | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(config)
        self._rng = rng or random.Random()
        self._fetcher = fetcher or self._simulated_fetch

    async def _simulated_fetch(self, url: str) -> str:
        sim = self.config.settings.get("simulation", {}) or {}
        await asyncio.sleep(float(sim.get("latency_seconds", 0.0)))
        if self._rng.random() < float(sim.get("failure_rate", 0.0)):
            raise TransientAgentError(f"simulated network error while fetching {url}")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        host = urlparse(url).hostname
        path = urlparse(url).path or "/"
        return (
            f"Page {path} on {host} discusses agentic workflows. "
            f"It explains how autonomous agents hand work to each other through an orchestrator. "
            f"Reference id {digest}. "
            f"Retries with exponential backoff keep transient failures from derailing a run."
        )

    def _check_url(self, url: object) -> str:
        if not isinstance(url, str):
            raise PermanentAgentError(f"url must be a string, got {url!r}")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PermanentAgentError(f"invalid URL '{url}'")
        host = parsed.hostname.lower()
        allowed = [d.lower() for d in self.boundary("allowed_domains", [])]
        if not any(host == d or host.endswith("." + d) for d in allowed):
            raise BoundaryViolationError(f"domain '{host}' is not in allowed_domains")
        return url

    async def _fetch_one(self, url: str) -> str:
        try:
            return await self._fetcher(url)
        except TransientAgentError:
            raise
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
            raise TransientAgentError(f"fetch failed for {url}: {exc}") from exc

    async def run(self, state: TaskState) -> AgentResult:
        urls = state.payload.get("urls")
        if not isinstance(urls, list) or not urls:
            raise PermanentAgentError("payload must include a non-empty 'urls' list")

        max_sources = int(self.boundary("max_sources", len(urls)))
        if len(urls) > max_sources:
            raise BoundaryViolationError(f"{len(urls)} URLs requested but max_sources is {max_sources}")

        checked = [self._check_url(u) for u in urls]
        max_chars = int(self.boundary("max_content_chars", 10_000))
        pages = await asyncio.gather(*(self._fetch_one(u) for u in checked))

        sources = [
            {"url": url, "content": page.strip()[:max_chars]}
            for url, page in zip(checked, pages)
            if isinstance(page, str) and page.strip()
        ]
        if not sources:
            raise PermanentAgentError("no usable content was retrieved from any source")

        return AgentResult(output={"topic": state.payload.get("topic", ""), "sources": sources})
