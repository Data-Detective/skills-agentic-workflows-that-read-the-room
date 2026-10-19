"""SummaryAgent: distils research findings with an LLM."""

from __future__ import annotations

from agents.base import BaseAgent, PermanentAgentError, TransientAgentError
from config import AgentConfig
from llm import LLMClient, LLMError, create_llm_client
from state import AgentResult, TaskState


class SummaryAgent(BaseAgent):
    """Turns the ResearchAgent's sources into a short, attributed brief.

    Reads ``state.artifacts[source_agent]`` (default ``"research"``) and calls
    the configured :class:`~llm.LLMClient`.
    """

    def __init__(self, config: AgentConfig, llm: LLMClient | None = None, source_agent: str = "research") -> None:
        super().__init__(config)
        self.llm = llm or create_llm_client(config.settings.get("llm"))
        self.source_agent = source_agent

    def build_prompt(self, topic: str, sources: list[dict]) -> str:
        max_chars = int(self.boundary("max_input_chars", 8000))
        body = "\n".join(f"{s['content']}" for s in sources)[:max_chars]
        return f"Summarise the findings below about '{topic}'.\nFINDINGS:\n{body}"

    async def run(self, state: TaskState) -> AgentResult:
        research = state.artifacts.get(self.source_agent)
        if not isinstance(research, dict) or not research.get("sources"):
            raise PermanentAgentError(f"no research findings from '{self.source_agent}' to summarise")

        sources = research["sources"]
        if not isinstance(sources, list) or not all(
            isinstance(s, dict) and isinstance(s.get("url"), str) and isinstance(s.get("content"), str)
            for s in sources
        ):
            raise PermanentAgentError("research sources must be a list of {'url': str, 'content': str} entries")
        max_words = int(self.boundary("max_summary_words", 100))
        prompt = self.build_prompt(research.get("topic", ""), sources)

        try:
            completion = await self.llm.complete(self.system_prompt(), prompt, max_words=max_words)
        except LLMError as exc:
            raise TransientAgentError(f"LLM call failed: {exc}") from exc

        if not isinstance(completion, str) or not completion.strip():
            raise TransientAgentError("LLM returned an empty completion")

        words = completion.split()
        summary = " ".join(words[:max_words])
        return AgentResult(
            output={
                "summary": summary,
                "word_count": min(len(words), max_words),
                "truncated": len(words) > max_words,
                "sources": [s["url"] for s in sources],
                "model": self.llm.model,
            }
        )
