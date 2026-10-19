"""Tests for the asynchronous orchestrator: queue, handoffs and retries."""

from __future__ import annotations

import asyncio
import json

import pytest

import orchestrator as orchestrator_module
from agents import BoundaryViolationError, PermanentAgentError, TransientAgentError
from config import ConfigError
from orchestrator import Orchestrator
from state import END_OF_PIPELINE, AgentResult, Task, TaskStatus
from tests.conftest import ScriptedAgent


def run(coro):
    return asyncio.run(coro)


def events(state, name):
    return [e for e in state.history if e["event"] == name]


@pytest.fixture
def make_orch(config, fake_sleep, zero_rng):
    def _make(agents=None, cfg=None):
        return Orchestrator(cfg or config, agents=agents, sleep=fake_sleep, rng=zero_rng)

    return _make


# ----------------------------------------------------------------- happy paths


def test_end_to_end_research_to_summary(make_orch):
    orch = make_orch()
    [state] = run(orch.run([Task(payload={"topic": "ai", "urls": ["https://github.blog/x"]}, task_id="t1")]))

    assert state.status is TaskStatus.SUCCEEDED
    assert state.error is None
    assert state.current_agent is None
    assert set(state.artifacts) == {"research", "summary"}
    assert state.artifacts["summary"]["sources"] == ["https://github.blog/x"]
    assert state.attempts == {"research": 1, "summary": 1}
    [handoff] = events(state, "handoff")
    assert (handoff["from_agent"], handoff["to_agent"]) == ("research", "summary")
    assert state.history[0]["event"] == "task_started"
    assert state.history[-1]["event"] == "task_succeeded"


def test_summary_agent_receives_research_artifact(make_orch):
    seen = {}

    async def summarise(state):
        seen.update(state.artifacts)
        return AgentResult(output="done")

    agents = {"research": ScriptedAgent("research", [AgentResult(output={"k": "v"})]),
              "summary": ScriptedAgent("summary", [summarise])}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert seen == {"research": {"k": "v"}}
    assert state.artifacts == {"research": {"k": "v"}, "summary": "done"}


def test_queue_processes_many_tasks_in_submission_order(make_config, fake_sleep, zero_rng):
    cfg = make_config(max_concurrency=3, queue_maxsize=1)  # tiny queue forces backpressure
    orch = Orchestrator(cfg, sleep=fake_sleep, rng=zero_rng)
    tasks = [Task(payload={"urls": [f"https://example.com/{i}"]}, task_id=f"t{i}") for i in range(10)]

    states = run(orch.run(tasks))

    assert [s.task_id for s in states] == [t.task_id for t in tasks]
    assert all(s.status is TaskStatus.SUCCEEDED for s in states)


def test_max_concurrency_is_respected(make_config, fake_sleep, zero_rng):
    active = peak = 0

    async def slow(state):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return AgentResult(output="ok", handoff_to=END_OF_PIPELINE)

    agents = {"research": ScriptedAgent("research", [slow]), "summary": ScriptedAgent("summary")}
    orch = Orchestrator(make_config(max_concurrency=2), agents=agents, sleep=fake_sleep, rng=zero_rng)
    run(orch.run([Task(payload={}) for _ in range(6)]))

    assert peak == 2


def test_empty_task_list(make_orch):
    assert run(make_orch().run([])) == []


# -------------------------------------------------------------------- handoffs


def test_agent_can_end_pipeline_early(make_orch):
    summary = ScriptedAgent("summary")
    agents = {"research": ScriptedAgent("research", [AgentResult("r", handoff_to=END_OF_PIPELINE)]),
              "summary": summary}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.SUCCEEDED
    assert summary.calls == 0
    assert events(state, "handoff") == []


def test_agent_can_hand_off_to_agent_outside_pipeline(make_orch):
    reviewer = ScriptedAgent("reviewer")
    summary = ScriptedAgent("summary")
    agents = {"research": ScriptedAgent("research", [AgentResult("r", handoff_to="reviewer")]),
              "summary": summary, "reviewer": reviewer}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.SUCCEEDED
    assert reviewer.calls == 1
    assert summary.calls == 0  # reviewer is not in the pipeline, so the chain ends there


def test_handoff_to_unknown_agent_fails(make_orch):
    agents = {"research": ScriptedAgent("research", [AgentResult("r", handoff_to="ghost")]),
              "summary": ScriptedAgent("summary")}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.FAILED
    assert "ghost" in state.error


def test_handoff_loop_is_capped_by_max_handoffs(make_config, fake_sleep, zero_rng):
    agents = {"research": ScriptedAgent("research", [AgentResult("r", handoff_to="summary")]),
              "summary": ScriptedAgent("summary", [AgentResult("s", handoff_to="research")])}
    orch = Orchestrator(make_config(max_handoffs=3), agents=agents, sleep=fake_sleep, rng=zero_rng)
    [state] = run(orch.run([Task(payload={})]))

    assert state.status is TaskStatus.FAILED
    assert "max_handoffs" in state.error
    assert len(events(state, "handoff")) == 3


# --------------------------------------------------------------------- retries


def test_transient_failure_is_retried_with_exponential_backoff(make_orch, fake_sleep, config):
    research = ScriptedAgent("research", [
        TransientAgentError("blip 1"),
        TransientAgentError("blip 2"),
        AgentResult("r"),
    ])
    agents = {"research": research, "summary": ScriptedAgent("summary")}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.SUCCEEDED
    assert state.attempts == {"research": 3, "summary": 1}
    base, factor = config.retry.base_delay_seconds, config.retry.backoff_factor
    assert fake_sleep.delays == [base, base * factor]
    assert [e["error"] for e in events(state, "retry_scheduled")] == ["blip 1", "blip 2"]


def test_backoff_includes_jitter(config, fake_sleep):
    class HalfRandom:
        def random(self):
            return 0.5

    agents = {"research": ScriptedAgent("research", [TransientAgentError("x"), AgentResult("r")]),
              "summary": ScriptedAgent("summary")}
    run(Orchestrator(config, agents=agents, sleep=fake_sleep, rng=HalfRandom()).run([Task(payload={})]))

    base = config.retry.base_delay_seconds
    assert fake_sleep.delays == [pytest.approx(base * (1 + 0.5 * config.retry.jitter_ratio))]


def test_backoff_is_capped_at_max_delay(make_config, fake_sleep, zero_rng):
    cfg = make_config(retry={"max_attempts": 6, "base_delay_seconds": 1, "backoff_factor": 3, "max_delay_seconds": 5})
    agents = {"research": ScriptedAgent("research", [TransientAgentError("x")]), "summary": ScriptedAgent("summary")}
    run(Orchestrator(cfg, agents=agents, sleep=fake_sleep, rng=zero_rng).run([Task(payload={})]))

    assert fake_sleep.delays == [1, 3, 5, 5, 5]


def test_retries_exhausted_marks_task_failed(make_orch, fake_sleep, config):
    summary = ScriptedAgent("summary")
    agents = {"research": ScriptedAgent("research", [TransientAgentError("down")]), "summary": summary}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    max_attempts = config.retry.max_attempts
    assert state.status is TaskStatus.FAILED
    assert state.error == "research: down"
    assert state.current_agent == "research"
    assert state.attempts == {"research": max_attempts}
    assert len(fake_sleep.delays) == max_attempts - 1
    assert summary.calls == 0
    assert "research" not in state.artifacts
    assert events(state, "task_failed")[0]["error_type"] == "TransientAgentError"


@pytest.mark.parametrize(
    "error",
    [PermanentAgentError("bad input"), BoundaryViolationError("off limits"), ValueError("bug")],
)
def test_non_retryable_errors_fail_fast(make_orch, fake_sleep, error):
    agents = {"research": ScriptedAgent("research", [error]), "summary": ScriptedAgent("summary")}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.FAILED
    assert state.attempts == {"research": 1}
    assert fake_sleep.delays == []
    assert str(error) in state.error


def test_agent_timeout_is_treated_as_transient(make_config, fake_sleep, zero_rng):
    async def hang(state):
        await asyncio.sleep(10)

    cfg = make_config(agent_timeout_seconds=0.01, retry={"max_attempts": 2})
    agents = {"research": ScriptedAgent("research", [hang, AgentResult("r")]), "summary": ScriptedAgent("summary")}
    [state] = run(Orchestrator(cfg, agents=agents, sleep=fake_sleep, rng=zero_rng).run([Task(payload={})]))

    assert state.status is TaskStatus.SUCCEEDED
    assert state.attempts["research"] == 2
    assert "timed out" in events(state, "retry_scheduled")[0]["error"]


def test_invalid_agent_return_value_fails(make_orch):
    agents = {"research": ScriptedAgent("research", [AgentResult("r")]),
              "summary": ScriptedAgent("summary", ["not an AgentResult"])}
    [state] = run(make_orch(agents).run([Task(payload={})]))

    assert state.status is TaskStatus.FAILED
    assert "expected AgentResult" in state.error
    assert state.artifacts == {"research": "r"}  # earlier work is preserved for debugging


def test_one_failing_task_does_not_affect_others(make_orch):
    tasks = [
        Task(payload={"urls": ["https://example.com/ok"]}, task_id="ok"),
        Task(payload={"urls": ["https://evil.com/"]}, task_id="bad"),
        Task(payload={}, task_id="empty"),
        Task(payload={"urls": ["https://github.blog/ok"]}, task_id="ok2"),
    ]
    states = {s.task_id: s for s in run(make_orch().run(tasks))}

    assert states["ok"].status is TaskStatus.SUCCEEDED
    assert states["ok2"].status is TaskStatus.SUCCEEDED
    assert states["bad"].status is TaskStatus.FAILED and "allowed_domains" in states["bad"].error
    assert states["empty"].status is TaskStatus.FAILED and "urls" in states["empty"].error


def test_worker_survives_unexpected_orchestrator_errors(make_orch, monkeypatch):
    orch = make_orch()
    original = orch.process_task

    async def flaky_process(state):
        if state.task_id == "boom":
            raise RuntimeError("kaboom")
        return await original(state)

    monkeypatch.setattr(orch, "process_task", flaky_process)
    states = run(orch.run([Task(payload={"urls": ["https://example.com"]}, task_id=i) for i in ("boom", "fine")]))

    assert states[0].status is TaskStatus.FAILED and "kaboom" in states[0].error
    assert states[1].status is TaskStatus.SUCCEEDED


def test_missing_pipeline_agent_instance_is_rejected(config):
    with pytest.raises(ConfigError, match="summary"):
        Orchestrator(config, agents={"research": ScriptedAgent("research")})


# ------------------------------------------------------------------------- CLI


def test_cli_runs_demo_tasks(capsys, raw_config, tmp_path):
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")

    code = run(orchestrator_module.main(["--config", str(cfg_path), "--log-level", "ERROR"]))

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(output) == 2
    assert all(item["status"] == "succeeded" for item in output)


def test_cli_ad_hoc_task_failure_exit_code(capsys, raw_config, tmp_path):
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")

    code = run(orchestrator_module.main(
        ["--config", str(cfg_path), "--url", "https://evil.com", "--topic", "x", "--log-level", "ERROR"]
    ))

    [item] = json.loads(capsys.readouterr().out)
    assert code == 1
    assert item["status"] == "failed"
    assert item["payload"] == {"topic": "x", "urls": ["https://evil.com"]}


def test_cli_bad_config_exit_code(capsys, tmp_path):
    code = run(orchestrator_module.main(["--config", str(tmp_path / "nope.yaml")]))
    assert code == 2
    assert "configuration error" in capsys.readouterr().err
