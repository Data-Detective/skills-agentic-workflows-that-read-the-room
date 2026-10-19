"""Specialised agents and the registry used to build them from config."""

from agents.base import (
    AgentError,
    BaseAgent,
    BoundaryViolationError,
    PermanentAgentError,
    TransientAgentError,
)
from agents.research_agent import ResearchAgent
from agents.summary_agent import SummaryAgent

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "ResearchAgent": ResearchAgent,
    "SummaryAgent": SummaryAgent,
}

__all__ = [
    "AGENT_REGISTRY",
    "AgentError",
    "BaseAgent",
    "BoundaryViolationError",
    "PermanentAgentError",
    "ResearchAgent",
    "SummaryAgent",
    "TransientAgentError",
]
