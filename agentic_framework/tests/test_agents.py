"""Tests for ResearchAgent, SummaryAgent and the LLM wrapper."""

from __future__ import annotations

import asyncio
import random

import pytest

from agents import BoundaryViolationError, PermanentAgentError, ResearchAgent, SummaryAgent, TransientAgentError
from config import AgentConfig
from llm import LLMClient, LLMError, MockLLMClient, create_llm_client
from state import AgentResult, TaskState


def run(coro):
    return asyncio.run(coro)


def state_with(payload=None, artifacts=None) -> TaskState:
    state = TaskState(task_id="t-1", payload=payload or {})
    state.artifacts.update(artifacts or {})
    return state


def with_settings(cfg: AgentConfig, **settings) -> AgentConfig:
    return AgentConfig(
        name=cfg.name,
        class_name=cfg.class_name,
        role=cfg.role,
        rules=cfg.rules,
        boundaries=cfg.boundaries,
        settings={**cfg.settings, **settings},
    )


@pytest.fixture
def research_cfg(config) -> AgentConfig:
    return config.agents["research"]


@pytest.fixture
def summary_cfg(config) -> AgentConfig:
    return config.agents["summary"]


# ---------------------------------------------------------------- ResearchAgent


def test_research_agent_scrapes_and_attributes_sources(research_cfg):
    agent = ResearchAgent(research_cfg)
    urls = ["https://github.blog/post", "https://docs.github.com/actions"]

    result = run(agent.run(state_with({"topic": "ai", "urls": urls})))

    assert isinstance(result, AgentResult)
    assert result.handoff_to is None
    assert result.output["topic"] == "ai"
    assert [s["url"] for s in result.output["sources"]] == urls
    assert all(s["content"] for s in result.output["sources"])


def test_research_agent_simulation_is_deterministic(research_cfg):
    payload = {"urls": ["https://example.com/a"]}
    first = run(ResearchAgent(research_cfg).run(state_with(payload)))
    second = run(ResearchAgent(research_cfg).run(state_with(payload)))
    assert first.output == second.output


def test_research_agent_allows_subdomains(research_cfg):
    result = run(ResearchAgent(research_cfg).run(state_with({"urls": ["https://www.example.com/x"]})))
    assert result.output["sources"][0]["url"] == "https://www.example.com/x"


@pytest.mark.parametrize(
    "url",
    ["https://evil.com/x", "https://notexample.com/x", "https://example.com.evil.io/x"],
)
def test_research_agent_rejects_disallowed_domains(research_cfg, url):
    with pytest.raises(BoundaryViolationError):
        run(ResearchAgent(research_cfg).run(state_with({"urls": [url]})))


def test_research_agent_enforces_max_sources(research_cfg):
    urls = [f"https://example.com/{i}" for i in range(research_cfg.boundaries["max_sources"] + 1)]
    with pytest.raises(BoundaryViolationError, match="max_sources"):
        run(ResearchAgent(research_cfg).run(state_with({"urls": urls})))


@pytest.mark.parametrize("payload", [{}, {"urls": []}, {"urls": "https://example.com"}])
def test_research_agent_requires_urls(research_cfg, payload):
    with pytest.raises(PermanentAgentError, match="urls"):
        run(ResearchAgent(research_cfg).run(state_with(payload)))


@pytest.mark.parametrize("url", ["ftp://example.com/file", "not a url", 42])
def test_research_agent_rejects_invalid_urls(research_cfg, url):
    with pytest.raises(PermanentAgentError):
        run(ResearchAgent(research_cfg).run(state_with({"urls": [url]})))


def test_research_agent_truncates_content(research_cfg):
    async def fetcher(url):
        return "x" * 10_000

    result = run(ResearchAgent(research_cfg, fetcher=fetcher).run(state_with({"urls": ["https://example.com"]})))
    assert len(result.output["sources"][0]["content"]) == research_cfg.boundaries["max_content_chars"]


@pytest.mark.parametrize("exc", [ConnectionError("reset"), TimeoutError("slow")])
def test_research_agent_wraps_network_errors_as_transient(research_cfg, exc):
    async def fetcher(url):
        raise exc

    with pytest.raises(TransientAgentError):
        run(ResearchAgent(research_cfg, fetcher=fetcher).run(state_with({"urls": ["https://example.com"]})))


def test_research_agent_skips_empty_pages_and_fails_if_all_empty(research_cfg):
    async def fetcher(url):
        return "" if url.endswith("empty") else "real content."

    agent = ResearchAgent(research_cfg, fetcher=fetcher)
    result = run(agent.run(state_with({"urls": ["https://example.com/empty", "https://example.com/ok"]})))
    assert [s["url"] for s in result.output["sources"]] == ["https://example.com/ok"]

    with pytest.raises(PermanentAgentError, match="no usable content"):
        run(agent.run(state_with({"urls": ["https://example.com/empty"]})))


def test_research_agent_simulated_failure_rate(research_cfg):
    cfg = with_settings(research_cfg, simulation={"failure_rate": 1.0, "latency_seconds": 0})
    with pytest.raises(TransientAgentError, match="simulated"):
        run(ResearchAgent(cfg, rng=random.Random(0)).run(state_with({"urls": ["https://example.com"]})))


# ----------------------------------------------------------------- SummaryAgent


RESEARCH = {
    "topic": "agents",
    "sources": [
        {"url": "https://example.com/a", "content": "Agents collaborate. They hand off work."},
        {"url": "https://example.com/b", "content": "Orchestrators retry failures with backoff."},
    ],
}


def test_summary_agent_distils_research_with_llm(summary_cfg):
    llm = MockLLMClient(model="test-model")
    agent = SummaryAgent(summary_cfg, llm=llm)

    result = run(agent.run(state_with(artifacts={"research": RESEARCH})))

    out = result.output
    assert out["summary"].startswith("Agents collaborate.")
    assert out["sources"] == ["https://example.com/a", "https://example.com/b"]
    assert out["model"] == "test-model"
    assert out["truncated"] is False
    # The agent's role and rules are sent to the LLM as the system prompt.
    call = llm.calls[0]
    assert summary_cfg.role in call["system_prompt"]
    assert all(rule in call["system_prompt"] for rule in summary_cfg.rules)
    assert call["max_words"] == summary_cfg.boundaries["max_summary_words"]
    assert "Orchestrators retry" in call["prompt"]


def test_summary_agent_uses_configured_llm_by_default(summary_cfg):
    agent = SummaryAgent(summary_cfg)
    assert isinstance(agent.llm, MockLLMClient)
    assert agent.llm.model == summary_cfg.settings["llm"]["model"]


def test_summary_agent_truncates_input_to_boundary(summary_cfg):
    llm = MockLLMClient()
    research = {"topic": "t", "sources": [{"url": "https://example.com", "content": "word " * 5000}]}
    run(SummaryAgent(summary_cfg, llm=llm).run(state_with(artifacts={"research": research})))
    findings = llm.calls[0]["prompt"].split("FINDINGS:\n", 1)[1]
    assert len(findings) <= summary_cfg.boundaries["max_input_chars"]


class StubLLM(LLMClient):
    def __init__(self, reply=None, error=None):
        super().__init__(model="stub")
        self.reply, self.error = reply, error

    async def complete(self, system_prompt, prompt, *, max_words=None):
        if self.error:
            raise self.error
        return self.reply


def test_summary_agent_enforces_word_limit_on_verbose_llm(summary_cfg):
    limit = summary_cfg.boundaries["max_summary_words"]
    agent = SummaryAgent(summary_cfg, llm=StubLLM(reply="word " * (limit * 3)))
    out = run(agent.run(state_with(artifacts={"research": RESEARCH}))).output
    assert len(out["summary"].split()) == limit
    assert out["word_count"] == limit
    assert out["truncated"] is True


@pytest.mark.parametrize("artifacts", [{}, {"research": {}}, {"research": {"sources": []}}, {"research": "oops"}])
def test_summary_agent_requires_research(summary_cfg, artifacts):
    with pytest.raises(PermanentAgentError, match="no research findings"):
        run(SummaryAgent(summary_cfg, llm=StubLLM(reply="x")).run(state_with(artifacts=artifacts)))


@pytest.mark.parametrize(
    "sources",
    ["not-a-list", [{"url": "https://example.com"}], [{"content": "x"}], ["string-entry"]],
)
def test_summary_agent_rejects_malformed_sources(summary_cfg, sources):
    research = {"topic": "t", "sources": sources}
    with pytest.raises(PermanentAgentError, match="research sources"):
        run(SummaryAgent(summary_cfg, llm=StubLLM(reply="x")).run(state_with(artifacts={"research": research})))


def test_summary_agent_llm_error_is_transient(summary_cfg):
    agent = SummaryAgent(summary_cfg, llm=StubLLM(error=LLMError("rate limited")))
    with pytest.raises(TransientAgentError, match="rate limited"):
        run(agent.run(state_with(artifacts={"research": RESEARCH})))


@pytest.mark.parametrize("reply", ["", "   ", None])
def test_summary_agent_empty_completion_is_transient(summary_cfg, reply):
    agent = SummaryAgent(summary_cfg, llm=StubLLM(reply=reply))
    with pytest.raises(TransientAgentError, match="empty"):
        run(agent.run(state_with(artifacts={"research": RESEARCH})))


# ------------------------------------------------------------------ LLM wrapper


def test_mock_llm_respects_max_words_and_ignores_instructions():
    llm = MockLLMClient()
    reply = run(llm.complete("sys", "Do X.\nFINDINGS:\nOne two three. Four five six.", max_words=4))
    assert reply == "One two three. Four"


def test_create_llm_client_rejects_unknown_provider():
    assert isinstance(create_llm_client(None), MockLLMClient)
    with pytest.raises(ValueError, match="unsupported"):
        create_llm_client({"provider": "skynet"})
