# M1 Spike: CrewAI Flow API Discovery

Task 8 of Milestone 1. Goal: prove a minimal 3-stage Flow runs end-to-end against
the actually-installed `crewai` and document the real API surface so M1
implementation work doesn't keep rediscovering it.

## Installed version

```
crewai                   1.15.12
```

`pyproject.toml` pins `crewai>=0.86`, so any version ≥ 0.86 is acceptable — and
1.15.12 satisfies it. The spec was written against an older API (~0.86), so the
code samples in §5.2 / the task brief **do not match the installed library**.
This spike adapts the brief's pattern to the real API and records the
differences below.

## Where things live

| Symbol                          | Import path                              | Notes                                                                                                       |
| ------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `Flow`                          | `crewai.flow`                            | NOT `crewai.flow.flow`. The brief had the wrong submodule.                                                  |
| `@start`                        | `crewai.flow`                            | From `crewai.flow.dsl._start`. Marks an entry-point method.                                                 |
| `@listen`                       | `crewai.flow`                            | From `crewai.flow.dsl._listen`. Triggers when the referenced method returns.                                |
| `@persist`                      | `crewai.flow`                            | From `crewai.flow.persistence.decorators`. Pure metadata stamper — records config; engine drives the save.  |
| `FlowPersistence` (interface)   | `crewai.flow.persistence.base`           | `save_state`, `load_state`, `init_db`, …                                                                     |
| `SQLiteFlowPersistence`         | `crewai.flow.persistence.sqlite`         | Built-in fallback. `__init__(db_path: str | None = None, **kwargs)`.                                       |
| `CheckpointConfig`              | `crewai.state.checkpoint_config`         | `(*, location: str = './.checkpoints', on_events: list[str] = ...)`.                                        |
| `Flow.kickoff`                  | instance method                          | **Synchronous.** Returns `Any | StreamSession[Any]` (the last method's value, or a stream session).      |
| `Flow.kickoff_async` / `akickoff` | instance method                        | Async twin. Same signature, returns `Any | AsyncStreamSession[Any]`.                                         |

## Flow class signature (relevant fields)

```python
class Flow(...):
    initial_state: BaseModel | dict | None = None
    name: str | None = None
    tracing: bool | None = None
    stream: bool = False
    memory: ... = None
    input_provider: ... = None
    persistence: FlowPersistence | None = None   # constructor injection
    checkpoint: CheckpointConfig | bool | None = None   # short-circuit for default config
    max_method_calls: int = 100
```

So both `persistence=` (FlowPersistence instance) and `checkpoint=` are constructor
parameters on the Flow itself. They do not need to come from decorators at the
class level — the `@persist` decorator is the *other* way to wire it (records
config on the class/method for the engine to pick up).

## `@start` / `@listen` actual signatures

```python
def start(condition: FlowTrigger | None = None) -> FlowMethodDecorator
def listen(condition: FlowTrigger) -> FlowMethodDecorator
```

`condition` can be:

* a method reference (e.g. `@listen(plan)`)
* a string route label
* the result of `or_(*methods)` or `and_(*methods)`

The brief's pattern of `@start()` and `@listen(prev_method)` works as-is once
imports are corrected.

## `@persist` — what it really is

`crewai.flow.persistence.decorators.persist`:

```python
def persist(
    persistence: FlowPersistence | None = None,
    verbose: bool = False,
) -> Callable[[type | Callable[..., T]], type | Callable[..., T]]:
    """Decorator to persist flow state.

    Applied at the class level → persists all flow methods.
    Applied at the method level → persists only that method.

    It is a pure metadata stamper: it records the persistence configuration
    on the class or method, and the Flow engine saves state after each
    persisted method completes.
    """
```

So `@persist` doesn't perform I/O itself — it stamps metadata and the Flow
runtime drives the persistence. If `persistence` is not supplied, the engine
uses `default_flow_persistence()` (a registered factory if present, else the
built-in SQLite fallback).

Two equivalent ways to enable persistence on a flow:

1. Decorator:
   ```python
   @persist
   class MyFlow(Flow[MyState]): ...
   ```
2. Constructor:
   ```python
   MyFlow(persistence=SQLiteFlowPersistence(db_path="./state.db"))
   ```

Spec §5.2's "`@persist` + `checkpoint=True`" is **the decorator path with the
Flow's `checkpoint` field left as `True` for default behaviour**. Detailed
wiring will be exercised in Task 9.

## `checkpoint=True` — what it actually means

`Flow.checkpoint` is a constructor field that accepts `CheckpointConfig | bool | None`:

* `None` → no checkpointing (default).
* `True` → use a default `CheckpointConfig` (location `./.checkpoints`, all events).
* `CheckpointConfig(location=..., on_events=[...])` → explicit config.

`CheckpointConfig` fields (verified):

* `location: str = './.checkpoints'` — directory for JSON provider, file path
  for SQLite provider.
* `on_events: list[str]` — subset of the ~80 lifecycle event literals
  (`flow_started`, `method_execution_started`, `method_execution_finished`,
  `flow_finished`, etc.). When empty, all relevant events are checkpointed.

Spec §5.2's "`@persist` + `checkpoint=True`" maps cleanly onto:

```python
@persist
class MyFlow(Flow[MyState]):
    ...
```

with the Flow instantiated as `MyFlow(checkpoint=True)` (or just default — the
brief does not yet require a specific checkpoint location). The actual
checkpoint side-effects will be measured in Task 9.

## Deviations from spec §5.2 / brief code

| Brief code                              | Installed reality                                | Action taken                              |
| --------------------------------------- | ------------------------------------------------ | ----------------------------------------- |
| `from crewai.flow.flow import Flow, listen, start` | `from crewai.flow import Flow, listen, start` | Updated imports.                          |
| `async def test_...: ... await flow.kickoff()` | `kickoff()` is sync; `kickoff_async()` is async. | Test is sync; calls `flow.kickoff()`.     |
| `stages_done: list[str] = []`           | Works, but uses a class-level default that Pydantic v2 copies per instance. | Used `Field(default_factory=list)` defensively. |
| `@persist` + `checkpoint=True`          | `@persist` is a metadata stamper; `checkpoint=` is a Flow constructor arg. | Deferred to Task 9; not used in Task 8 test. |

## Verification

```
$ .venv/bin/pytest tests/spike/test_flow_basics.py -v
tests/spike/test_flow_basics.py::test_spike_flow_runs_three_stages PASSED
tests/spike/test_flow_basics.py::test_spike_flow_state_is_isolated_per_instance PASSED
2 passed
```

`ruff check tests/spike/` — clean.

## Implications for spec §5.2

* §5.2's "`@persist` + `checkpoint=True`" should be interpreted as **decorator
  form + Flow constructor arg form**, not both decorators. Concretely, the
  production flow will likely be:

  ```python
  @persist(verbose=False)
  class OrchestrationFlow(Flow[OrchestrationState]):
      ...
  ```

  instantiated with `OrchestrationFlow(checkpoint=True)` (or with an explicit
  `CheckpointConfig(location=...)` pointing at Postgres-backed storage when
  we wire it up — the built-in `SQLiteFlowPersistence` is fine for the spike
  but not for production).

* §5.2's "`async def` test with `await flow.kickoff()`" pattern needs to use
  `await flow.kickoff_async()` (or be plain sync with `flow.kickoff()`).
* The `pyproject.toml` `crewai>=0.86` floor is fine, but the spec code
  samples are pinned to ~0.86. After M1 we should either tighten the floor
  (`crewai>=1.15`) or update the samples to match whichever version the
  CI environment actually resolves.
