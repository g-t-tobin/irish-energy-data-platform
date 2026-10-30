# ADR 0001: Shared run history (DynamoDB) and Step Functions execution

Status: accepted for design and local implementation (plan Phase 6). Nothing is
deployed, and pipeline execution stays disabled. Hosting and Lambda-versus-Fargate
packaging still need your confirmation before Phase 7.

## Decisions

**Run history store.** A single DynamoDB table (on-demand, point-in-time recovery,
deletion protection, retained on stack removal), behind the `RunHistory`
interface in `src/run_store.py`. SQLite remains the default for local
development and tests. `RUN_HISTORY_BACKEND=dynamodb` with `RUN_HISTORY_TABLE`
(and optional `RUN_HISTORY_REGION`, default `eu-west-1`) selects DynamoDB. The
layout lives in `src/run_history_schema.py`.

| Item | Key | Purpose |
|---|---|---|
| run | `pk=RUN#<id>`, `sk=RUN`, `gsi1pk=RUNS`, `gsi1sk=<created>#<id>` | one record per run; the index lists newest first |
| lock | `pk=LOCK#pipeline` | at most one active run; holds `run_id` and `lease_expires_at` |
| idempotency | `pk=IDEM#<sha256(requester\|key)>` | maps a repeated request to its run; DynamoDB TTL removes it after 24 h |

The listing index uses one partition key. That is fine for tens of runs per day;
revisit before any high-volume use.

**Concurrency.** `create_run` is one `TransactWriteItems`: put the lock (only if
absent or its lease has expired), put the run, and (if a key was sent) put the
idempotency item. Two API tasks cannot both start a run.

**Lease, not a permanent flag.** The lock lease is 45 minutes, longer than the
30-minute state-machine timeout. Every progress update extends it. A terminal
update deletes the lock only if it still belongs to that run. If the release is
missed, the lease expiry frees the lock, so a crashed run cannot block forever.

**Duplicates and retries.** `Idempotency-Key` (8-64 letters, digits, `-`, `_`) is
scoped to the requesting user. A repeat returns the original run and starts
nothing, even while that run is active. The Step Functions execution name is the
run ID, so a repeated `StartExecution` is a no-op.

**Guarded transitions.** Late progress updates cannot revive a finished run. A
terminal status is accepted over an active or interrupted run (the real outcome
wins over a recovery guess), never over another terminal state.

**Interrupted runs.** `recover_interrupted` (called at API startup) marks active
runs interrupted only if the lock lease has expired, so restarting one API task
never disturbs a healthy run on another. `reconcile_active_runs` asks Step
Functions (`DescribeExecution`) for the real outcome of runs still marked active.
Chosen approach: run it at startup and from a scheduled job (Phase 7), not on every
read, which keeps the read path free of extra permissions and latency. It is
tested but not wired in.

**Step Functions.** `build_state_machine_definition` (`infrastructure/run_control/spec.py`)
describes: mark running, then Bronze, Silver, Gold and Glue registration as Lambda
tasks (retry on transient Lambda errors, per-task timeout, `Catch` on every task),
progress and terminal updates written to DynamoDB with the same conditions as the
store, lock release in the final step, and an overall timeout. Failures after Gold
exists are `partial_failure`; error text written by the workflow is fixed and
generic. `StepFunctionsExecutor` records the run, then starts the execution with
only `run_id` and `days_back` as input. It is not constructed by the API.

**Packaging (proposed, needs confirmation).** One Lambda container image (pandas
and pyarrow exceed the zip limits) with a thin handler per stage that calls the
existing functions. A Fargate task is the alternative if a stage outgrows Lambda.

## IAM (design only; nothing created)

`spec.py` builds least-privilege policies: the API role reads only the Gold prefix,
uses the one table and its listing index, starts one state machine, and describes
only its executions; the state-machine role can update/delete items in the table
and invoke the listed stage functions; the stage role reads and writes the lake
prefixes and uses one Glue table. Deviation from the production document: the API
role also needs `dynamodb:DeleteItem` (lock release when a start fails), and the
state machine, not the stage functions, writes progress to DynamoDB. The developer
profile is not changed.

## Migration and rollback

- **Migration:** local SQLite history is development data and is not imported.
  Existing SQLite files gain the `requested_by` and `idempotency_key` columns
  automatically. Cutover is a configuration change (`RUN_HISTORY_BACKEND`,
  `RUN_HISTORY_TABLE`) once the table exists and the permissions are confirmed.
- **Rollback:** set `RUN_HISTORY_BACKEND=sqlite` (or unset it) and restart. The
  DynamoDB table is retained and untouched. No code path creates runs while
  execution is disabled, so rolling back loses nothing that matters now.
- **Stuck lock:** wait for the lease, or delete the single item
  `LOCK#pipeline`/`LOCK` by hand after confirming no execution is running.
- **Recovery of the table:** point-in-time recovery; deletion protection on.

## Limitations

- Not deployed and not tested against real DynamoDB or Step Functions; tests use an
  in-memory double that evaluates only the expressions the store emits.
- Run history is not paginated beyond 200 items; recovery looks at the 50 newest runs.
- The state machine and policies are data only until Phase 7.
- Idempotency keys expire after 24 hours.
