"""Task state shared by the orchestrator and every agent.

A single :class:`TaskState` object travels through the whole pipeline. Agents
read ``payload`` and earlier ``artifacts`` and return an ``AgentResult``; only
the orchestrator mutates the state, which keeps the data flow auditable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Task:
    """A unit of work submitted to the orchestrator."""

    payload: dict[str, Any]
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class AgentResult:
    """What an agent returns after a successful run.

    ``handoff_to`` overrides the default pipeline order. Use ``None`` (the
    default) to continue with the next agent in the pipeline, or
    :data:`END_OF_PIPELINE` to finish the task early.
    """

    output: Any
    handoff_to: str | None = None


END_OF_PIPELINE = "__end__"


@dataclass
class TaskState:
    task_id: str
    payload: dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    current_agent: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_task(cls, task: Task) -> "TaskState":
        return cls(task_id=task.task_id, payload=dict(task.payload))

    def record(self, event: str, **details: Any) -> None:
        self.history.append({"ts": time.time(), "event": event, **details})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data
