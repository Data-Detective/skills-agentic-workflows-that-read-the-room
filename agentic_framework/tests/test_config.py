"""Tests for configuration loading and validation."""

from __future__ import annotations

import copy

import pytest

from config import ConfigError, RetryPolicy, load_config, parse_config
from orchestrator import build_agents
from tests.conftest import CONFIG_PATH


def test_default_config_loads_roles_rules_and_boundaries():
    cfg = load_config(CONFIG_PATH)

    assert cfg.pipeline == ("research", "summary")
    assert set(cfg.agents) == {"research", "summary"}
    for agent in cfg.agents.values():
        assert agent.role
        assert agent.rules
    assert "github.blog" in cfg.agents["research"].boundaries["allowed_domains"]
    assert cfg.agents["summary"].boundaries["max_summary_words"] > 0
    assert cfg.agents["summary"].settings["llm"]["provider"] == "mock"


def test_build_agents_instantiates_registered_classes(config):
    agents = build_agents(config)
    assert type(agents["research"]).__name__ == "ResearchAgent"
    assert type(agents["summary"]).__name__ == "SummaryAgent"


def test_retry_policy_backoff_is_exponential_and_capped():
    policy = RetryPolicy(base_delay_seconds=0.5, backoff_factor=2.0, max_delay_seconds=3.0, jitter_ratio=0.1)
    assert [policy.delay_for(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert policy.delay_for(1, rand=0.5) == pytest.approx(0.525)
    with pytest.raises(ValueError):
        policy.delay_for(0)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.pop("agents"), "agents"),
        (lambda r: r.pop("orchestrator"), "orchestrator"),
        (lambda r: r.update(orchestrator="nope"), "orchestrator"),
        (lambda r: r.update(pipeline=[]), "pipeline"),
        (lambda r: r.update(pipeline=["research", "ghost"]), "ghost"),
        (lambda r: r["orchestrator"]["retry"].update(max_attempts=0), "max_attempts"),
        (lambda r: r["orchestrator"]["retry"].update(backoff_factor=0.5), "backoff_factor"),
        (lambda r: r["orchestrator"]["retry"].update(max_delay_seconds=0.1), "max_delay_seconds"),
        (lambda r: r.update(pipeline=["research", "summary", "research"]), "duplicate"),
        (lambda r: r["orchestrator"].update(max_concurrency="many"), "max_concurrency"),
        (lambda r: r["orchestrator"].update(agent_timeout_seconds=True), "agent_timeout_seconds"),
        (lambda r: r["agents"]["research"].pop("rules"), "rules"),
        (lambda r: r["agents"]["research"].update(rules="not-a-list"), "rules"),
        (lambda r: r["agents"]["summary"].update(role="  "), "role"),
        (lambda r: r["agents"]["summary"].update(boundaries=["x"]), "boundaries"),
        (lambda r: r["agents"]["summary"].update({"class": None}), "class"),
        (lambda r: r["agents"]["summary"].update({"class": ""}), "class"),
    ],
)
def test_invalid_config_is_rejected(raw_config, mutate, message):
    raw = copy.deepcopy(raw_config)
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_non_mapping_root_is_rejected():
    with pytest.raises(ConfigError):
        parse_config(["not", "a", "mapping"])


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_malformed_yaml(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("orchestrator: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_unknown_agent_class_is_rejected(raw_config):
    raw_config["agents"]["research"]["class"] = "DoesNotExist"
    with pytest.raises(ConfigError, match="DoesNotExist"):
        build_agents(parse_config(raw_config))
