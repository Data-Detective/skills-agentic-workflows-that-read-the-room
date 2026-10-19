"""Asynchronous orchestrator for the agentic workflow framework.

Responsibilities:
* own the task queue and a pool of concurrent workers,
* route each task through the agent pipeline (multi-agent handoffs),
* retry transient agent failures with exponential backoff,
* record every state transition on the task's ``history``.

Run ``python orchestrator.py --help`` for CLI usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from typing import Awaitable, Callable, Iterable

from agents import AGENT_REGISTRY, AgentError, BaseAgent, PermanentAgentError, TransientAgentError
from config import DEFAULT_CONFIG_PATH, ConfigError, FrameworkConfig, load_config
from state import END_OF_PIPELINE, AgentResult, Task, TaskState, TaskStatus

logger = logging.getLogger("orchestrator")

Sleep = Callable[[float], Awaitable[None]]


def build_agents(config: FrameworkConfig) -> dict[str, BaseAgent]:
    """Instantiate every agent declared in the config via ``AGENT_REGISTRY``."""
    agents: dict[str, BaseAgent] = {}
    for name, agent_cfg in config.agents.items():
        cls = AGENT_REGISTRY.get(agent_cfg.class_name)
        if cls is None:
            raise ConfigError(f"agents.{name}.class '{agent_cfg.class_name}' is not a registered agent class")
        agents[name] = cls(agent_cfg)
    return agents


class Orchestrator:
    def __init__(
        self,
        config: FrameworkConfig,
        agents: dict[str, BaseAgent] | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.agents = agents if agents is not None else build_agents(config)
        missing = [name for name in config.pipeline if name not in self.agents]
        if missing:
            raise ConfigError(f"no agent instance provided for pipeline step(s): {', '.join(missing)}")
        self._sleep = sleep
        self._rng = rng or random.Random()

    @classmethod
    def from_config_file(cls, path=DEFAULT_CONFIG_PATH, **kwargs) -> "Orchestrator":
        return cls(load_config(path), **kwargs)

    # ------------------------------------------------------------------ queue

    async def run(self, tasks: Iterable[Task]) -> list[TaskState]:
        """Process ``tasks`` through the queue and return their final states.

        States are returned in submission order regardless of completion order.
        """
        queue: asyncio.Queue[TaskState] = asyncio.Queue(maxsize=self.config.queue_maxsize)
        states = [TaskState.from_task(task) for task in tasks]
        workers = [
            asyncio.create_task(self._worker(queue, worker_id), name=f"worker-{worker_id}")
            for worker_id in range(self.config.max_concurrency)
        ]
        try:
            for state in states:
                await queue.put(state)  # blocks when the queue is full (backpressure)
                logger.debug("queued task %s", state.task_id)
            await queue.join()
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return states

    async def _worker(self, queue: "asyncio.Queue[TaskState]", worker_id: int) -> None:
        while True:
            state = await queue.get()
            try:
                logger.info("worker-%d picked up task %s", worker_id, state.task_id)
                await self.process_task(state)
            except Exception as exc:  # last-resort guard: one bad task must not kill the worker
                logger.exception("unexpected error while processing task %s", state.task_id)
                state.status = TaskStatus.FAILED
                state.error = f"orchestrator error: {exc!r}"
                state.record("task_failed", error=state.error)
            finally:
                queue.task_done()

    # --------------------------------------------------------------- pipeline

    async def process_task(self, state: TaskState) -> TaskState:
        """Drive a single task through the agent pipeline until it ends."""
        state.status = TaskStatus.RUNNING
        state.record("task_started")
        agent_name: str | None = self.config.pipeline[0]
        handoffs = 0

        while agent_name is not None:
            state.current_agent = agent_name
            try:
                result = await self._run_with_retry(agent_name, self.agents[agent_name], state)
            except AgentError as exc:
                return self._fail(state, agent_name, exc, source=agent_name)
            state.artifacts[agent_name] = result.output

            # Routing errors are attributed to the orchestrator, not the agent that succeeded.
            try:
                next_agent = self._next_agent(agent_name, result)
                if next_agent is not None:
                    handoffs += 1
                    if handoffs > self.config.max_handoffs:
                        raise PermanentAgentError(f"exceeded max_handoffs ({self.config.max_handoffs})")
            except AgentError as exc:
                return self._fail(state, agent_name, exc, source="routing")
            if next_agent is not None:
                state.record("handoff", from_agent=agent_name, to_agent=next_agent)
                logger.info("task %s: handoff %s -> %s", state.task_id, agent_name, next_agent)
            agent_name = next_agent

        state.current_agent = None
        state.status = TaskStatus.SUCCEEDED
        state.record("task_succeeded")
        logger.info("task %s succeeded", state.task_id)
        return state

    @staticmethod
    def _fail(state: TaskState, agent_name: str, exc: AgentError, *, source: str) -> TaskState:
        state.status = TaskStatus.FAILED
        state.error = f"{source}: {exc}"
        state.record("task_failed", agent=agent_name, source=source, error=str(exc), error_type=type(exc).__name__)
        logger.warning("task %s failed (%s): %s", state.task_id, source, exc)
        return state

    def _next_agent(self, current: str, result: AgentResult) -> str | None:
        if result.handoff_to == END_OF_PIPELINE:
            return None
        if result.handoff_to is not None:
            if result.handoff_to not in self.agents:
                raise PermanentAgentError(f"'{current}' tried to hand off to unknown agent '{result.handoff_to}'")
            return result.handoff_to
        pipeline = self.config.pipeline
        if current not in pipeline:
            return None
        index = pipeline.index(current) + 1
        return pipeline[index] if index < len(pipeline) else None

    # ------------------------------------------------------------------ retry

    async def _run_with_retry(self, name: str, agent: BaseAgent, state: TaskState) -> AgentResult:
        policy = self.config.retry
        timeout = self.config.agent_timeout_seconds

        for attempt in range(1, policy.max_attempts + 1):
            state.attempts[name] = state.attempts.get(name, 0) + 1
            state.record("agent_attempt", agent=name, attempt=attempt)
            try:
                result = await asyncio.wait_for(agent.run(state), timeout=timeout)
                if not isinstance(result, AgentResult):
                    raise PermanentAgentError(f"agent returned {type(result).__name__}, expected AgentResult")
                state.record("agent_succeeded", agent=name, attempt=attempt)
                return result
            except asyncio.TimeoutError:
                error: AgentError = TransientAgentError(f"timed out after {timeout}s")
            except AgentError as exc:
                error = exc
            except Exception as exc:  # unknown bugs are not retried
                error = PermanentAgentError(f"unexpected {type(exc).__name__}: {exc}")

            if not error.retryable or attempt >= policy.max_attempts:
                state.record("agent_failed", agent=name, attempt=attempt, error=str(error))
                raise error

            delay = policy.delay_for(attempt, self._rng.random())
            state.record("retry_scheduled", agent=name, attempt=attempt, delay=round(delay, 4), error=str(error))
            logger.info(
                "task %s: %s attempt %d failed (%s); retrying in %.2fs",
                state.task_id, name, attempt, error, delay,
            )
            await self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------- CLI

def demo_tasks() -> list[Task]:
    return [
        Task(payload={"topic": "agentic workflows", "urls": ["https://github.blog/ai-and-ml/agents"]}),
        Task(
            payload={
                "topic": "orchestration patterns",
                "urls": ["https://example.com/orchestration", "https://docs.github.com/actions"],
            }
        ),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the agentic workflow orchestrator.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.yaml")
    parser.add_argument("--topic", help="topic for a single ad-hoc task")
    parser.add_argument("--url", action="append", dest="urls", help="source URL (repeatable)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    if args.topic and not args.urls:
        parser.error("--topic requires at least one --url")
    return args


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        orchestrator = Orchestrator.from_config_file(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.urls:
        tasks = [Task(payload={"topic": args.topic or "", "urls": args.urls})]
    else:
        tasks = demo_tasks()

    states = await orchestrator.run(tasks)
    print(json.dumps([s.to_dict() for s in states], indent=2, default=str))
    return 0 if all(s.status is TaskStatus.SUCCEEDED for s in states) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
