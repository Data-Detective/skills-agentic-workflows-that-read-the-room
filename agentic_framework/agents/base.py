"""Base agent class and the framework's error taxonomy."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from config import AgentConfig
from state import AgentResult, TaskState


class AgentError(Exception):
    """Base class for all agent failures."""

    retryable = False


class TransientAgentError(AgentError):
    """A temporary failure (network blip, rate limit, flaky LLM). Retried."""

    retryable = True


class PermanentAgentError(AgentError):
    """A failure that will not succeed on retry (bad input). Not retried."""


class BoundaryViolationError(PermanentAgentError):
    """The task asked an agent to act outside its configured boundaries."""


class BaseAgent(ABC):
    """Common behaviour for all agents.

    Subclasses implement :meth:`run`. They must treat ``state`` as read-only
    and communicate exclusively through the returned :class:`AgentResult`.
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def role(self) -> str:
        return self.config.role

    @property
    def rules(self) -> tuple[str, ...]:
        return self.config.rules

    def boundary(self, key: str, default: Any = None) -> Any:
        return self.config.boundaries.get(key, default)

    def system_prompt(self) -> str:
        rules = "\n".join(f"- {rule}" for rule in self.rules)
        return f"You are the '{self.name}' agent. Role: {self.role}\nRules:\n{rules}"

    @abstractmethod
    async def run(self, state: TaskState) -> AgentResult:
        """Execute the agent against the current task state."""
