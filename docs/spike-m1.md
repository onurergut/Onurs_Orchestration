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
* §5.2's `tester_stage + security_stage` fan-out pattern (one parent,
  two parallel branches, one join) maps onto `@listen(and_(branch_a,
  branch_b))` for the join — see the next section for the runtime
  mechanics and what we actually observed.
* The `pyproject.toml` `crewai>=0.86` floor is fine, but the spec code
  samples are pinned to ~0.86. After M1 we should either tighten the floor
  (`crewai>=1.15`) or update the samples to match whichever version the
  CI environment actually resolves.

## Parallel stages findings

Task 10 verified the spec §5.2 fan-out pattern: a single `@start`
parent followed by two `@listen(parent)` branches and a downstream
`@listen(and_(branch_a, branch_b))` join. Two tests now live in
`tests/spike/test_flow_basics.py` (`test_parallel_join` and
`test_parallel_join_blocks_until_both_complete`); both pass and use
real `time.monotonic()` timestamps to prove the branches overlapped
rather than serialised.

### `and_(a, b)` is the real API, imported from `crewai.flow`

The brief wrote `from crewai.flow.flow import and_, listen, start`,
which is wrong for crewai 1.15.12 — `and_` and `or_` live on the
top-level `crewai.flow` package and originate in
`crewai.flow.dsl._conditions`. They are pure data builders: each
returns a `FlowCondition` dict of the form

```python
{"type": "AND_CONDITION", "conditions": [name_a, name_b]}
# or, with nesting:
{"type": "AND_CONDITION", "conditions": [{"type": "OR_CONDITION", ...}, name_c]}
```

so they compose: `@listen(and_(or_(x, y), z))` works and builds a
nested dict. The runtime walks this structure in
`_condition_satisfied` (top-level `crewai/flow/runtime/__init__.py`,
around lines 165–178), which delegates to the
`combine(_condition_satisfied(...) for ...)` helper. There is no
separate "AND condition" Python class — it is just a nested dict, so
any equality / hashing assumptions in downstream code should match
that.

### Branches really do run in parallel — but as threads, not processes

Listeners triggered by the same event are not run sequentially. The
runtime's `_dispatch_listener_completion` (lines ~3060–3104 in the
runtime `__init__.py`) finds every listener whose condition is
satisfied by the current trigger and, for those that are *not* an
OR-race group, schedules them via:

```python
tasks = [
    self._execute_single_listener(name, listener_result, ...)
    for name in listeners_triggered
]
await asyncio.gather(*tasks)
```

Each task calls `_execute_method`, which dispatches the body:

* async methods (`async def branch_a(...)`) are awaited inline on the
  event loop;
* sync methods (`def branch_a(...)`) are run via
  `await asyncio.to_thread(ctx.run, method, *args, **kwargs)` — i.e. on
  the default `ThreadPoolExecutor`.

So a fan-out from `init` to two sync branches produces two threads,
both blocked on the same `kickoff()` call. The spike's
`test_parallel_join` proves this by sleeping 50 ms in each branch and
asserting the joint wall-clock window is well under 100 ms — it ran in
~50 ms. **Concurrent I/O-bound sync work is fine. CPU-bound sync work
would contend on the GIL; if a branch needs to release the GIL, write
it as `async def` and `await` its awaits.** That is the contract for
spec §5.2's `tester_stage` and `security_stage`: as long as their work
is network-/LLM-bound (it will be — both stages call external APIs),
they will overlap.

Note: `Flow.kickoff()` is the synchronous wrapper around
`kickoff_async()` — it calls `asyncio.run(_run_flow())` (or, if a
loop is already running, runs `_run_flow` in a single-thread
`ThreadPoolExecutor`). So the parallelism is implemented in terms of
threads and an event loop, **not** separate processes. The default
thread pool is `min(32, os.cpu_count() + 4)`, which is plenty for the
spec's two-branch fan-out but is worth remembering if M1 grows the
fan-out to many branches.

### How `join` waits for both branches

`and_(branch_a, branch_b)` produces an AND condition dict. When
`branch_a` completes, the runtime calls `_condition_met` for `join`,
which appends `branch_a` to the listener's `seen` set; if the AND is
not yet satisfied (because `branch_b` has not fired), `_condition_met`
returns `False` and `join` is **not** scheduled. When `branch_b`
completes, `_condition_met` is called again, both triggers are in
`seen`, `_condition_satisfied` returns `True`, and `join` is finally
dispatched. The order in which the two branches complete is irrelevant
— the second one to fire is what unblocks the join. This was the
exact thing tested in `test_parallel_join_blocks_until_both_complete`:
`fast_branch` finished immediately, but the runtime held `join` back
until the deliberately-slowed `slow_branch` (50 ms sleep) also
completed.

### What happens if one branch raises?

This was not exhaustively tested, but the dispatch path is:
`_execute_single_listener` catches exceptions in
`_execute_method`'s try/except and propagates them into the
`asyncio.gather(..., return_exceptions=False)` (default) at line 3104,
which re-raises the first exception. Concretely:

* a branch that raises will fail the whole `kickoff()` call (the
  exception propagates up through `kickoff_async` and out of
  `kickoff`);
* `join` will *not* be scheduled, because the failed branch's
  `_completed_methods` entry is never added (it lives in the try
  block, after the method returns successfully — see
  `_execute_method` line 2868);
* the failed branch's exception is raised to the caller of
  `kickoff()`, so the orchestrator / API layer will need a try/except
  around `kickoff()` (or to use `kickoff_async()` and handle
  exceptions there).

This is worth a follow-up test in M2 once the orchestrator code paths
are wired up — the spike did not want to assert on a behaviour that
might reasonably change between minor versions.

### State sharing between parallel branches — caveat

All branches share `self.state` on the same `Flow` instance. Pydantic
`BaseModel` is not thread-safe for arbitrary attribute mutation. In
this spike we are safe because:

* each branch writes its own distinct fields (`a_done`, `b_started_at`,
  etc.);
* `join` runs **after both branches finish** (in `_execute_single_listener`'s
  outer `await`), so it never races with a branch's writes.

If M1 needs cross-branch shared state (e.g. one branch accumulates
tokens spent, the other reads them), put a `threading.Lock` on the
state and acquire it in every read/write, or use a thread-safe
primitive (`queue.Queue`, `itertools.count`, `multiprocessing.Value`).
The default `BaseModel` attribute write is atomic-enough at the
attribute level for small scalars but not for compound updates
(`self.state.counter += 1` is a read-modify-write race even on a
single int). For the production fan-out, **avoid shared mutable
state in branches** and let `join` aggregate branch-local results
instead.

### Other minor observations

* The condition dict from `and_`/`or_` uses string keys `"and"` / `"or"`
  at one layer of coercion (`_to_definition_condition` in
  `_conditions.py`), and the runtime's `_condition_branches` looks
  for `"and"` / `"or"` keys. If you build a condition by hand rather
  than via `and_(...)` / `or_(...)`, mirror those keys exactly.
* `@listen(and_(branch_a, branch_b))` does **not** receive the
  upstream return value as a useful payload — by the time `join`
  fires, both `branch_a` and `branch_b` have produced their results
  but the runtime passes only a synthesised trigger (typically the
  triggering event's payload, which may be `None`). If `join` needs
  the branches' outputs, have each branch stash its result on
  `self.state` and read from there.
* `@router` paths are orthogonal to this — a router picks exactly
  one branch based on a Python predicate, and the unselected branches
  do not run. The AND-join pattern is for *every* branch must run.

## Idempotency contract

Task 11 spike result. Spec §5 requires that every external side-effect
(a Jira/Linear/GitHub ticket create, an LLM API call that bills, a
push to a remote worktree, etc.) be safe to retry. The mechanism is a
deterministic `idempotency_key` and a Postgres-backed
`external_receipts` table (see migration `001_initial_schema`) — every
adapter call consults that table before executing, and on retry/resume
short-circuits if a successful receipt already exists.

The contract is verified end-to-end in
`tests/spike/test_idempotency.py` against the real Postgres test DB,
using a `FakeTaskSourceAdapter` that mirrors what the M2
`TaskSourceAdapter` / `CsvAdapter` pair will look like. The M2 adapters
themselves don't exist yet, so the spike exercises the *pattern* with
a small in-test fake rather than importing code that's out of scope.

### Reference shape — `FakeTaskSourceAdapter`

```python
async def create_external_task(self, run_id: UUID, external_id: str,
                               operation: str = "create") -> dict:
    key = f"{self.name}:{external_id}:{operation}"
    async with session_scope() as session:
        existing = (await session.execute(
            select(ExternalReceipt).where(
                ExternalReceipt.idempotency_key == key
            )
        )).scalar_one_or_none()

        if existing is not None and existing.status == "succeeded":
            cached = dict(existing.response_payload or {})
            cached["reused"] = True
            return cached

        # Fresh path: execute + persist in the same transaction.
        self.calls.append(f"{external_id}:{operation}")
        payload = {"external_id": external_id, "operation": operation, ...}
        session.add(ExternalReceipt(
            run_id=run_id,
            idempotency_key=key,
            adapter_type=self.name,
            operation=operation,
            external_id=external_id,
            request_payload={"external_id": external_id},
            response_payload=payload,
            status="succeeded",
        ))
        return payload
```

### The six rules

Every adapter call MUST follow all six:

1. **Compute `idempotency_key` deterministically from `(adapter_type,
   external_id, operation)`** — and, in production, `project_id` and
   `stage`. The key MUST be a pure function of those inputs; nothing
   from the external system's response (no `external_ref` returned by
   the API) may enter the key, or a retry after a partial failure
   would compute a *different* key and double-execute. See
   `FakeTaskSourceAdapter.create_external_task` (`tests/spike/test_idempotency.py`).

2. **Before executing, query `external_receipts` for an existing row
   with that key.** The lookup MUST happen in the same DB session that
   will write the new row, so the dedup check and the insert see a
   consistent snapshot. `SELECT … WHERE idempotency_key = :key` is
   the only required predicate; `external_receipts.idempotency_key` is
   `UNIQUE` (migration `001_initial_schema`), so a duplicate insert
   would also fail with `IntegrityError` as a backstop.

3. **If found with status `succeeded`, return the cached
   `response_payload` — do NOT re-execute.** The adapter MUST record
   that the path was the dedup short-circuit (the test uses
   `cached["reused"] = True`) so callers and audit logs can tell the
   two paths apart. Do NOT mutate the receipt on a hit — leave the
   existing row alone. The test
   `test_second_call_with_same_key_skips_execution` pins both
   behaviours.

4. **If found with status `pending`, raise or wait.** A `pending`
   row means another in-flight attempt owns the operation; calling
   again would race. The adapter either raises a typed error (e.g.
   `ReceiptInFlight`) that the orchestrator can convert into a retry
   later, or blocks on the row until the in-flight attempt completes
   and the row transitions to `succeeded` or `failed`. Do NOT execute
   speculatively. (Not exercised in the spike — the test set covers
   `succeeded` and `failed`, since `pending` requires concurrent
   in-flight semantics that are awkward to simulate in a single-process
   test.)

5. **If not found (or found with status `failed`), insert the row and
   execute the side-effect.** A `failed` row from a prior attempt MUST
   NOT block a retry — overwrite it (or, alternatively, mark the old
   row `failed` and insert a new one with a bumped attempt counter).
   The test `test_failed_receipt_allows_retry` pins the over-write
   semantics. After execution, set `status` to `succeeded` (or
   `failed` with an error payload), `response_payload` to the
   adapter's return value, and `completed_at` to `now()`.

6. **Receipt writes happen in the same DB transaction as the
   side-effect's success path.** The pattern above uses one
   `session_scope()` for the dedup check and the insert; the
   `session_scope()` helper in `orchestration/storage/db.py` commits
   on exit and rolls back on exception, so a crash between "executed"
   and "wrote receipt" leaves no row — which is fine, because the
   retry will see no row and re-execute (per rule 5). The corollary
   for adapters that need an external-ID returned from the API before
   they can write the receipt: write a `pending` row first (within
   the same transaction), perform the side-effect, then UPDATE the
   row to `succeeded`/`failed` with the response payload. Never
   commit a side-effect's result without first having a matching
   receipt row in `external_receipts`.

### What the spike verified

`tests/spike/test_idempotency.py` exercises four cases against the
real `external_receipts` table:

| Test | Validates |
| --- | --- |
| `test_first_call_executes_and_persists_receipt` | Rule 2 + 5 + 6: no prior row → adapter executes once, receipt persisted with `status="succeeded"` and full `response_payload`. |
| `test_second_call_with_same_key_skips_execution` | Rule 3: prior `succeeded` row → adapter does NOT re-execute (`self.calls` unchanged), does NOT insert a new row (still exactly one), returns cached payload with `reused=True`. |
| `test_different_external_id_means_independent_execution` | Rule 1 (and the corollary that the key really does encode `external_id`): `T1` and `T2` produce two separate receipts under two separate keys. |
| `test_failed_receipt_allows_retry` | Rule 5 corollary: a prior `failed` row is overwritten on the next attempt, not treated as a hit. |

All four pass against `postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration_test`.
The autouse `_isolate_state` fixture in `tests/conftest.py`
TRUNCATEs every table before each test, so each test starts on a
clean schema.

### What M2 must add to close the contract

* Real `orchestration.adapters.task_source.base.TaskSourceAdapter`
  and `.csv.CsvAdapter` modules that import this contract and
  implement the six rules above (the spike's `FakeTaskSourceAdapter`
  is a reference shape, not the production one).
* An `idempotency_key` builder helper that takes the canonical
  inputs `(project_id, task_id, stage, operation, attempt)` and
  returns the deterministic key. Keep it a single pure function —
  every adapter MUST go through it so the key format cannot drift.
* A typed exception (`ReceiptInFlight` or similar) for the `pending`
  path (rule 4) so the orchestrator can decide between raise-and-retry
  and block-on-row.
* An audit-log entry on every successful side-effect so the dedup
  short-circuit is visible in `audit_log` (rule 3 corollary).
