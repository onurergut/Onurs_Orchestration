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
| `@persist` + `checkpoint=True`          | `@persist()` is a metadata stamper; `checkpoint=` is a Flow constructor arg. The decorator **must** be written `@persist()` (with parens), not `@persist` — see §"Persist + checkpoint findings" below. | Deferred to Task 9; not used in Task 8 test. |

## Verification

```
$ .venv/bin/pytest tests/spike/test_flow_basics.py -v
tests/spike/test_flow_basics.py::test_spike_flow_runs_three_stages PASSED
tests/spike/test_flow_basics.py::test_spike_flow_state_is_isolated_per_instance PASSED
tests/spike/test_flow_basics.py::test_persist_round_trip PASSED
tests/spike/test_flow_basics.py::test_checkpoint_config_writes_json_artifacts PASSED
4 passed
```

`ruff check tests/spike/` — clean.

## Persist + checkpoint findings

Task 9 added two tests that wire `@persist()` and `CheckpointConfig`
together on the same Flow class. The brief assumed they "just work" as
adjacent decorators; the installed library (crewai 1.15.12) requires
specific syntax and has surprising semantics around `restore_from_state_id`.

### `@persist()` decorator — call it with parens

`persist(persistence=None, verbose=False)` is a decorator **factory**. The
brief's illustrative `@persist` (no parens) does not work; Python binds the
underlying function as the `persistence=` positional arg and `persist()`
returns its inner `decorator` closure in place of the original method. The
runtime then tries to call that closure with `(self, *args)` and silently
produces empty state.

Reproduction (after Task 9's first cut): with `@persist` (no parens) on the
`init` method, `flow.state.stages_done` stayed `[]` after kickoff and the
listener chain broke at `plan`. Switching every occurrence to `@persist()`
fixed it. Symptom surface: `StartMethod._meth` is a `persist.<locals>.decorator`
function instead of the original method.

```python
# CORRECT
@start()
@persist()
def init(self): ...

# WRONG — replaces init with persist's inner decorator closure
@start()
@persist
def init(self): ...
```

### State must have an `id` field

The persistence decorator rejects any state model without an `id`
attribute (see `crewai/flow/persistence/decorators.py` — the id check
raises `ValueError("Flow state must have an 'id' field for
persistence")`). The library's own `crewai.flow.runtime.FlowState`
provides a `uuid4()` default; we roll our own here so the test doesn't
import a private path.

```python
class SpikeState(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # ...
```

### Default persistence backend is SQLite at a user-data path

`default_flow_persistence()` (called when neither `@persist` nor
`persistence=` is supplied on the instance) returns
`SQLiteFlowPersistence(db_path="<user data dir>/flow_states.db")` — on
macOS that's `~/Library/Application Support/Onurs_Orchestration/flow_states.db`.
For tests we override via:

```python
SQLiteFlowPersistence(db_path=str(tmp_path / "states.db"))
```

`init_db` is called automatically in the `@model_validator` and creates the
`flow_states` and `pending_feedback` tables. `save_state` inserts one row
per completed method (no upsert); `load_state(uuid)` returns the latest
row's JSON.

### `@persist()` is method-scoped; class-level `@persist` exists too

`@persist()` applied to a **method** stamps `__flow_persistence_config__`
on the `FlowMethod` wrapper. The runtime's `_persist_method_completion`
reads `method_definition.persist` first, falling back to
`self._definition.persist` (only set when `@persist` decorates the
**class**). To snapshot after every completed method, either decorate the
class (`@persist() class MyFlow(...)`) or apply `@persist()` to every
method. Mixing is fine: with method-level `@persist()` on `init/plan/execute`,
the SQLite history accumulates one row per stage.

### `checkpoint=True` writes nothing for a Flow — override `on_events`

`Flow.checkpoint=True` is coerced (in `_coerce_checkpoint`) to
`CheckpointConfig(on_events=["task_completed"])`. `task_completed` is a
Crew event that Flow never fires, so the default-on-True config writes
zero JSON files for an entire Flow run. The right knob is:

```python
CheckpointConfig(location=str(ckpt_dir), on_events=["*"])
# or, for tighter control:
CheckpointConfig(
    location=str(ckpt_dir),
    on_events=["flow_started", "method_execution_finished", "flow_finished"],
)
```

### Checkpoint artifact location and shape

With `JsonProvider` (default) and `on_events=["*"]`, each matched event
produces one file under `<location>/main/{ts}_{uuid8}_p-{parent_id}.json`.
The `main/` subdirectory is the default `branch`; the timestamp + 8-char
uuid make up the id; the parent id encodes lineage in the filename (no
parsing needed). Each file's body is the full `RuntimeState` dump — a
`crewai_version`, `parent_id`, `branch`, the `entities` array (each
entity is `model_dump(mode="json")`-serialized, with public flow fields
plus `checkpoint_completed_methods`, `checkpoint_method_outputs`,
`checkpoint_method_counts`, `checkpoint_state`), and the `event_record`.

`SqliteProvider` would use `<location>` as a database file path instead
(the validator appends `.db` if no suffix). For production we'll likely
want a custom Postgres-backed provider; the `FlowPersistence` interface
is just `init_db`, `save_state`, `load_state` and the pending-feedback
triad.

### `restore_from_state_id` forks, not resumes

`Flow.kickoff(restore_from_state_id=uuid)`:
1. Calls `persistence.load_state(uuid)`.
2. Hydrates `self._state` from the snapshot via `_restore_state`.
3. Mints a **fresh** `state.id` (unless `inputs={"id": ...}` is also
   pinned, in which case that wins — and that pins the new flow to the
   source's persistence key, usually not what you want).
4. Runs the whole flow again from the start.

Observable consequence in the test: after restore, the new flow's
`stages_done` contains the **restored three entries followed by the
re-executed three entries** (`["init", "plan", "execute", "init", "plan",
"execute"]`). The source UUID's snapshot is preserved untouched — the
fork writes under the new id. This is *fork* semantics, not *resume*.
There is no built-in idempotency-on-restore story at the method level; the
caller has to detect re-execution. (That's the gap Task 11 will look at.)

`Flow.from_checkpoint` / `Flow.fork` exist alongside, operating on the
`CheckpointConfig` line (different persistence layer — the JSON files,
not the SQLite `@persist` DB). They use `_restored_from_checkpoint` and
`_is_execution_resuming` flags internally and are the right tool when you
want *resume* (skip already-completed methods) rather than *fork*.

### Things that did **not** pan out the way the brief suggested

The brief's test_persist_round_trip sketch expected `flow.state.flow_uuid`
and constructor args `persist=True, checkpoint=True` — neither exists.
`flow.state.flow_uuid` is not a thing in this library (it's `flow.state.id`),
and constructor kwargs are `persistence=` (a `FlowPersistence` instance) and
`checkpoint=` (a `CheckpointConfig | bool | None`).

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
