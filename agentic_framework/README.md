# Agentic Workflow Framework

A small, dependency-light Python framework for running **multi-agent workflows**
on an `asyncio` task queue. An orchestrator moves each task through a chain of
specialised agents. It retries transient failures with exponential backoff, and
everything an agent may do is declared in `config.yaml`.

```
agentic_framework/
├── orchestrator.py          # Async engine: task queue, worker pool, handoffs, retries, CLI
├── config.yaml              # Agent roles, rules, boundaries, retry policy, pipeline
├── config.py                # YAML loader + strict validation → FrameworkConfig
├── state.py                 # Task, TaskState, AgentResult (the data that flows through the system)
├── llm.py                   # LLM wrapper: LLMClient interface + offline MockLLMClient
├── agents/
│   ├── base.py              # BaseAgent + error taxonomy (transient / permanent / boundary)
│   ├── research_agent.py    # ResearchAgent: simulated scraping within allowed domains
│   └── summary_agent.py     # SummaryAgent: distils research through the LLM wrapper
├── tests/                   # pytest suite (success paths + failure modes)
├── pytest.ini
└── requirements.txt
```

---

## Architecture

```
             ┌──────────────────────────── Orchestrator ─────────────────────────────┐
 Task ──► asyncio.Queue ──► worker-0..N ──► process_task(state)                       │
             │  (bounded,                     │                                       │
             │  backpressure)                 ▼                                       │
             │                 ┌── _run_with_retry(agent) ──┐                         │
             │                 │  wait_for(agent.run, timeout)                        │
             │                 │  Transient? → sleep(backoff) → retry                 │
             │                 │  Permanent? → fail task immediately                  │
             │                 └────────────┬───────────────┘                         │
             │                              ▼                                         │
             │               artifacts[agent] = result.output                         │
             │               next = result.handoff_to or next in pipeline             │
             └────────────────────────────────────────────────────────────────────────┘
                        │                                   │
                        ▼                                   ▼
               ┌────────────────┐   handoff        ┌────────────────┐
               │ ResearchAgent  │ ───────────────► │  SummaryAgent  │──► LLMClient
               │ (simulated     │  artifacts       │  (distils via  │    (MockLLMClient
               │  scraping)     │  ["research"]    │   LLM wrapper) │     by default)
               └────────────────┘                  └────────────────┘
```

### Components

| Component | Responsibility |
|---|---|
| **`Orchestrator`** (`orchestrator.py`) | Owns a bounded `asyncio.Queue` and a pool of `max_concurrency` workers. For each task it runs the pipeline, handles handoffs, applies the retry policy and agent timeout, and records every transition in `TaskState.history`. A worker survives errors in any single task. |
| **`ResearchAgent`** | Simulates scraping each URL in `payload["urls"]` (deterministic content, configurable latency and failure rate). It enforces `allowed_domains`, `max_sources` and `max_content_chars`. You can inject a `fetcher` coroutine to use real HTTP. |
| **`SummaryAgent`** | Reads `artifacts["research"]` and builds a prompt capped at `max_input_chars`. It calls the LLM wrapper, using the agent's role and rules as the system prompt, then limits the result to `max_summary_words`. |
| **`LLMClient`** (`llm.py`) | Provider-agnostic async interface. `MockLLMClient` is an offline, deterministic extractive summariser, so the framework runs without API keys. |
| **`config.py`** | Loads `config.yaml` with `yaml.safe_load` and validates it strictly. Invalid configuration raises `ConfigError` before any work starts. |

### Error taxonomy and retry semantics

| Error | Raised when | Retried? |
|---|---|---|
| `TransientAgentError` | Network blips, simulated fetch failures, LLM errors, empty LLM output | ✅ yes |
| `asyncio.TimeoutError` | An attempt exceeds `agent_timeout_seconds` (converted to transient) | ✅ yes |
| `PermanentAgentError` | Bad input, such as missing `urls`, invalid URLs or no research to summarise | ❌ no |
| `BoundaryViolationError` | A task asks an agent to act outside its configured boundaries | ❌ no |
| Any other exception | Bugs in agent code (wrapped as `PermanentAgentError`) | ❌ no |

Backoff after failed attempt *n* (1-based):

```
delay = min(max_delay_seconds, base_delay_seconds * backoff_factor ** (n - 1))
delay += delay * jitter_ratio * random()
```

With the default config the delays are 0.5 s and then 1.0 s, plus up to 10 % jitter. After `max_attempts` attempts the task is marked `failed`.

---

## Configuration (`config.yaml`)

```yaml
orchestrator:            # system-wide boundaries
  max_concurrency: 2     # parallel workers
  queue_maxsize: 100     # queue backpressure (0 = unbounded)
  agent_timeout_seconds: 10
  max_handoffs: 5        # loop guard for agent-to-agent handoffs
  retry: { max_attempts: 3, base_delay_seconds: 0.5, backoff_factor: 2.0,
           max_delay_seconds: 8.0, jitter_ratio: 0.1 }

pipeline: [research, summary]   # default handoff order

agents:
  research:
    class: ResearchAgent        # must be registered in agents.AGENT_REGISTRY
    role: ...                   # used in the agent's system prompt
    rules: [...]                # strict rules, also sent to the LLM
    boundaries: { allowed_domains: [...], max_sources: 3, max_content_chars: 2000 }
    simulation: { failure_rate: 0.0, latency_seconds: 0.05 }
  summary:
    class: SummaryAgent
    role: ...
    rules: [...]
    boundaries: { max_input_chars: 6000, max_summary_words: 60 }
    llm: { provider: mock, model: mock-distiller-1, temperature: 0.2 }
```

Validation rejects missing sections, empty or non-string rules, a pipeline that references undefined agents, non-positive or non-numeric limits, `backoff_factor < 1` and unregistered agent classes.

---

## Running it

Requirements: Python 3.10+.

```bash
cd agentic_framework
python -m pip install -r requirements.txt

# Run the two built-in demo tasks and print their final state as JSON
python orchestrator.py

# Run one ad-hoc task
python orchestrator.py --topic "agentic workflows" \
  --url https://github.blog/ai-and-ml/agents --url https://docs.github.com/actions

# Use another config file / more verbose logs
python orchestrator.py --config ./config.yaml --log-level DEBUG
```

Exit codes: `0` means every task succeeded, `1` means at least one task failed, and `2` means the configuration is invalid.

To see retries in action, set `agents.research.simulation.failure_rate` to `0.5` and run with `--log-level INFO`.

### Programmatic use

```python
import asyncio
from orchestrator import Orchestrator
from state import Task

orch = Orchestrator.from_config_file("config.yaml")
states = asyncio.run(orch.run([
    Task(payload={"topic": "agents", "urls": ["https://example.com/post"]}),
]))
print(states[0].status, states[0].artifacts["summary"]["summary"])
```

### Running the tests

```bash
cd agentic_framework
python -m pytest            # or, from the repo root: python -m pytest agentic_framework
```

The tests need no network access and no pytest plugins. Async code runs with `asyncio.run`, and backoff sleeps are replaced by a recording fake, so the suite finishes in under a second.

The suite covers:

- **Success paths:** end-to-end research → summary, artifact handoff, ordered results across many concurrent tasks, `max_concurrency`, queue backpressure, early exit and off-pipeline handoffs, and the CLI.
- **Failure modes:** domain and source-count boundary violations, invalid or missing URLs, network errors, empty pages, simulated flakiness, LLM errors and empty or overlong completions, timeouts, exhausted retries, fail-fast on permanent errors, unexpected exceptions, unknown handoff targets, handoff loops, invalid return values, isolation of a failing task, invalid configs and bad CLI config.

---

## How state flows through the system

One `TaskState` object is created per submitted `Task` and travels the whole pipeline. **Only the orchestrator writes to it.** Agents read it and return an `AgentResult`. This keeps every change explicit and auditable.

```
Task(payload)                        TaskState
     │                               ├─ task_id, payload       (immutable input)
     ▼                               ├─ status                 pending → running → succeeded | failed
 TaskState.from_task ──► queue       ├─ current_agent          agent currently executing / that failed
                           │         ├─ artifacts{agent: out}  each agent's output, keyed by agent name
                           ▼         ├─ attempts{agent: n}     attempts per agent (incl. retries)
                       worker        ├─ history[]              timestamped event log
                           │         └─ error                  "<agent>: <message>" on failure
                           ▼
  1. status=running, history += task_started
  2. agent = pipeline[0]
  3. loop:
       attempts[agent] += 1, history += agent_attempt
       result = agent.run(state)        (agent reads payload + earlier artifacts)
         ├─ transient error → history += retry_scheduled(delay) → sleep → retry
         ├─ permanent error / retries exhausted
         │     → history += agent_failed, task_failed; status=failed; error set; STOP
         └─ success → history += agent_succeeded
       artifacts[agent] = result.output
       next = END? stop : result.handoff_to or next agent in pipeline
       history += handoff(from_agent, to_agent)   (bounded by max_handoffs)
  4. status=succeeded, current_agent=None, history += task_succeeded
```

Concretely, for the default pipeline:

1. **Input:** `payload = {"topic": "...", "urls": [...]}`
2. **ResearchAgent** reads `payload` and writes
   `artifacts["research"] = {"topic": ..., "sources": [{"url", "content"}, ...]}`
3. **Handoff** `research → summary` is recorded in `history`.
4. **SummaryAgent** reads `artifacts["research"]` and writes
   `artifacts["summary"] = {"summary", "word_count", "truncated", "sources", "model"}`
5. **Output:** `orchestrator.run()` returns the final `TaskState`s in submission order. `TaskState.to_dict()` serialises them, as the CLI does.

If a task fails, the artifacts from earlier steps stay on the state, so you can inspect what was produced before the failure.

---

## Extending

- **New agent:** subclass `agents.base.BaseAgent`, implement `async def run(self, state) -> AgentResult`, add it to `AGENT_REGISTRY` in `agents/__init__.py`, and declare it under `agents:` in `config.yaml`. Add it to `pipeline` or reach it with `handoff_to`.
- **Real LLM:** subclass `llm.LLMClient`, implement `complete()` (raise `LLMError` on provider failures so they are retried), and register the provider in `create_llm_client`.
- **Real scraping:** pass `ResearchAgent(config, fetcher=my_async_fetch)`. Raise `ConnectionError`, `TimeoutError` or `TransientAgentError` for failures that are worth retrying.
