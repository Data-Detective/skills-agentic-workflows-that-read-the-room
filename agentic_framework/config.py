"""Load and validate the framework configuration (``config.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    backoff_factor: float = 2.0
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.1

    def delay_for(self, attempt: int, rand: float = 0.0) -> float:
        """Return the backoff delay after the given (1-based) failed attempt.

        ``rand`` must be in ``[0, 1)`` and scales the jitter component.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * self.backoff_factor ** (attempt - 1),
        )
        return delay + delay * self.jitter_ratio * rand


@dataclass(frozen=True)
class AgentConfig:
    name: str
    class_name: str
    role: str
    rules: tuple[str, ...]
    boundaries: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrameworkConfig:
    max_concurrency: int
    queue_maxsize: int
    agent_timeout_seconds: float
    max_handoffs: int
    retry: RetryPolicy
    pipeline: tuple[str, ...]
    agents: dict[str, AgentConfig]


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f"missing required key '{key}' in {where}")
    return mapping[key]


def _positive_number(value: Any, where: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{where} must be {'>= 0' if allow_zero else '> 0'}, got {value!r}")
    return value


def _positive_int(value: Any, where: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    return int(_positive_number(value, where, allow_zero=allow_zero))


def parse_config(raw: Any) -> FrameworkConfig:
    """Validate a raw (already YAML-decoded) mapping and build a config object."""
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    orch = _require(raw, "orchestrator", "root")
    if not isinstance(orch, dict):
        raise ConfigError("'orchestrator' must be a mapping")
    retry_raw = orch.get("retry", {})
    if not isinstance(retry_raw, dict):
        raise ConfigError("orchestrator.retry must be a mapping")

    retry = RetryPolicy(
        max_attempts=_positive_int(retry_raw.get("max_attempts", 3), "retry.max_attempts"),
        base_delay_seconds=_positive_number(
            retry_raw.get("base_delay_seconds", 0.5), "retry.base_delay_seconds", allow_zero=True
        ),
        backoff_factor=_positive_number(retry_raw.get("backoff_factor", 2.0), "retry.backoff_factor"),
        max_delay_seconds=_positive_number(
            retry_raw.get("max_delay_seconds", 8.0), "retry.max_delay_seconds", allow_zero=True
        ),
        jitter_ratio=_positive_number(retry_raw.get("jitter_ratio", 0.1), "retry.jitter_ratio", allow_zero=True),
    )
    if retry.backoff_factor < 1:
        raise ConfigError("retry.backoff_factor must be >= 1")
    if retry.max_delay_seconds < retry.base_delay_seconds:
        raise ConfigError("retry.max_delay_seconds must be >= retry.base_delay_seconds")

    agents_raw = _require(raw, "agents", "root")
    if not isinstance(agents_raw, dict) or not agents_raw:
        raise ConfigError("'agents' must be a non-empty mapping")

    agents: dict[str, AgentConfig] = {}
    for name, spec in agents_raw.items():
        where = f"agents.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a mapping")
        rules = _require(spec, "rules", where)
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise ConfigError(f"{where}.rules must be a list of strings")
        boundaries = spec.get("boundaries", {})
        if not isinstance(boundaries, dict):
            raise ConfigError(f"{where}.boundaries must be a mapping")
        role = _require(spec, "role", where)
        if not isinstance(role, str) or not role.strip():
            raise ConfigError(f"{where}.role must be a non-empty string")
        class_name = _require(spec, "class", where)
        if not isinstance(class_name, str) or not class_name.strip():
            raise ConfigError(f"{where}.class must be a non-empty string")
        settings = {k: v for k, v in spec.items() if k not in {"class", "role", "rules", "boundaries"}}
        agents[name] = AgentConfig(
            name=name,
            class_name=class_name.strip(),
            role=role.strip(),
            rules=tuple(rules),
            boundaries=dict(boundaries),
            settings=settings,
        )

    pipeline = _require(raw, "pipeline", "root")
    if not isinstance(pipeline, list) or not pipeline:
        raise ConfigError("'pipeline' must be a non-empty list of agent names")
    for step in pipeline:
        if step not in agents:
            raise ConfigError(f"pipeline references undefined agent '{step}'")
    if len(set(pipeline)) != len(pipeline):
        raise ConfigError("pipeline must not contain duplicate agents")

    return FrameworkConfig(
        max_concurrency=_positive_int(orch.get("max_concurrency", 1), "orchestrator.max_concurrency"),
        queue_maxsize=_positive_int(orch.get("queue_maxsize", 0), "orchestrator.queue_maxsize", allow_zero=True),
        agent_timeout_seconds=_positive_number(
            orch.get("agent_timeout_seconds", 30), "orchestrator.agent_timeout_seconds"
        ),
        max_handoffs=_positive_int(orch.get("max_handoffs", 10), "orchestrator.max_handoffs"),
        retry=retry,
        pipeline=tuple(pipeline),
        agents=agents,
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> FrameworkConfig:
    """Read ``path`` and return a validated :class:`FrameworkConfig`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file '{path}': {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in '{path}': {exc}") from exc
    return parse_config(raw)
