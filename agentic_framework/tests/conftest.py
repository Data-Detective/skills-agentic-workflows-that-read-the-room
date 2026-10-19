"""Shared fixtures and test doubles."""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from agents.base import BaseAgent
from config import AgentConfig, FrameworkConfig, parse_config
from state import AgentResult, TaskState

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture
def raw_config() -> dict[str, Any]:
    """The real ``config.yaml`` as a mutable dict, with simulated latency removed."""
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["agents"]["research"]["simulation"]["latency_seconds"] = 0
    return copy.deepcopy(raw)


@pytest.fixture
def make_config(raw_config) -> Callable[..., FrameworkConfig]:
    """Build a validated config, applying ``orchestrator``/``retry`` overrides."""

    def _make(retry: dict | None = None, **orchestrator: Any) -> FrameworkConfig:
        raw = copy.deepcopy(raw_config)
        raw["orchestrator"].update(orchestrator)
        raw["orchestrator"]["retry"].update(retry or {})
        return parse_config(raw)

    return _make


@pytest.fixture
def config(make_config) -> FrameworkConfig:
    return make_config()


class RecordingSleep:
    """Drop-in replacement for ``asyncio.sleep`` that records delays instantly."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def fake_sleep() -> RecordingSleep:
    return RecordingSleep()


class ZeroRandom(random.Random):
    """RNG that always returns 0.0 so backoff jitter is deterministic."""

    def random(self) -> float:  # noqa: D401 - matches random.Random API
        return 0.0


@pytest.fixture
def zero_rng() -> ZeroRandom:
    return ZeroRandom()


def agent_config(name: str, **boundaries: Any) -> AgentConfig:
    return AgentConfig(name=name, class_name="Scripted", role=f"{name} role", rules=("be good",), boundaries=boundaries)


class ScriptedAgent(BaseAgent):
    """Agent whose behaviour on each call is taken from ``outcomes``.

    Each outcome may be an exception (raised), an :class:`AgentResult`
    (returned), or a coroutine function taking the state. The last outcome is
    repeated once the script is exhausted.
    """

    def __init__(self, name: str, outcomes: list[Any] | None = None) -> None:
        super().__init__(agent_config(name))
        self.outcomes = outcomes or [AgentResult(output=f"{name}-output")]
        self.calls = 0

    async def run(self, state: TaskState) -> AgentResult:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(state)
        return outcome
