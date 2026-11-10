# Qlib task workflows without MongoDB

The joint task adapter retains Qlib 0.9.7's native model training, dataset loading,
recorders, delayed training and result collectors. It replaces the Mongo-backed
task coordinator with a local SQLite control database and immutable Parquet task
history. All workers use `.venv-joint`; providers and installed packages remain
unchanged. No MongoDB server, Redis server or trading connection is constructed.

## Ownership and guarantees

`futu_bridge.joint.workflows` exports `TaskManager`, `TrainerRM`,
`DelayTrainerRM`, `run_task`, `LeaseLost` and `TaskFailure`.
Descriptive aliases `ParquetTaskManager`, `ParquetTrainerRM` and
`ParquetDelayTrainerRM` refer to the same implementations. `SQLiteTaskStore` is an
alias for the pool-scoped `TaskManager`, not a separate coordinator or database.

Each explicit task root contains:

- `tasks.sqlite`: task identity, pool, state, priority, lease, monotonic fencing
  token, attempts, accepted result reference and durable reconciliation outbox.
- `history/parquet/`: immutable transition snapshots, including serialized result
  payloads and their hashes. The event sequence preserves every accepted stage,
  failure, expired lease and administrative reset.
- `history/raw/`: content-addressed result bytes. These permit immediate durable
  commits before asynchronous Parquet publication. Native model weights,
  predictions and recorders remain in MLflow's artifact store.
- `history/control.sqlite`: the existing snapshot catalogue. Its schema remains
  independent of the task coordinator and MLflow database.

The task system executes **at least once**. Every claim increments a fencing
token. A heartbeat or result commit must match the current owner and token and
must arrive before lease expiry. A completed or replaced claim cannot commit a
second result. A proven dead process can be reclaimed immediately; a living but
unresponsive owner must reach its lease deadline. Training can happen more than once, while
only one result per stage is accepted. Unaccepted MLflow runs and orphan result
files are retained for diagnosis rather than silently promoted or deleted.

SQLite uses WAL, `synchronous=FULL`, short `BEGIN IMMEDIATE` transactions and a
bounded lock wait. Training and MLflow calls run outside catalogue transactions.
This is a **single-host** coordinator with multiple local processes. A network
share or a second-host worker is not a supported deployment and is not certified
by the local acceptance test.

Schema version 3 uses system-wide `time.monotonic_ns()` for lease deadlines and
retry eligibility. Wall time is retained only for human-readable audit dates;
equal-priority tasks use database insertion order. Forward/backward system-clock
adjustments cannot expire a healthy claim or reorder queued work. The pinned
Windows Python uses `GetTickCount64()` for its monotonic clock. Python documents
that this clock is shared across processes and unaffected by system-clock updates.
See the [Python monotonic clock contract](https://docs.python.org/3.12/library/time.html#time.monotonic).

Claims are bound to the worker PID and the exact 64-bit process-creation FILETIME
returned by the documented
[GetProcessTimes API](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes).
The adapter checks process exit through a zero-time
[WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
query and closes every handle. It does not estimate boot identity from wall time
minus uptime. Following a reboot, absent owners or reused PIDs with a different
creation identity are recovered without waiting for an obsolete pre-reboot
monotonic deadline. Unknown/access-denied process state fails closed. These
process-incarnation checks are specifically verified for the fixed Windows runtime.

Opening a legacy task database adds the new columns transactionally. Existing
running wall-clock claims are requeued in their original stage with an incremented
fence and a `lease_clock_migration` event. Definitions, saved intermediate results
and all history remain intact; an old worker cannot commit through its old fence.
Previously generated evidence directories remain unchanged by verification.

## Native behavior and intentional differences

The compatible lifecycle is:

```text
waiting -> running -> done
waiting -> running -> part_done -> running -> done
                    |
                    +-> waiting/part_done after a failed attempt
                    +-> failed after exhausted retries
```

`create_task` deduplicates definitions atomically, preserves submission order and
returns an empty list without inserting for `dry_run=True`, matching native Qlib.
Identity hashing ignores dictionary insertion order and Python object memoization;
it retains scalar and container types. Opaque Python objects use their trusted
local serialization, so reproducible JSON-like configurations are preferred.
Larger priority values run first, with stable creation-order ties.

`safe_fetch_task` restores the original stage when its caller raises an exception
or `KeyboardInterrupt`. `run_task` additionally retries ordinary callback failures
up to the stored attempt policy, defaulting to three failures per stage. Successful
intermediate stages reset that failure budget. `retries=False` selects native-style
exception propagation with rollback. Terminal `failed` or `cancelled` tasks make
`wait` raise `TaskFailure`; waits are bounded instead of hanging indefinitely.

Failed callbacks use a stored per-task exponential backoff: the default delays are
0.1, 0.2, 0.4 seconds and so on, capped at 30 seconds, subject to the attempt limit.
`retry_backoff_seconds` and `max_retry_backoff_seconds` configure that policy when
tasks are created; the maximum accepted cap is one hour. Workers continue polling
delayed waiting tasks and do not confuse them with completion. Eligibility uses
the monotonic clock and is bound to the process incarnation that scheduled the
delay. If that process has exited, the delay is discarded conservatively so a
reboot cannot strand a task; retry counts and fencing remain intact. Native
`safe_fetch_task` rollback stays immediate and does not consume a retry attempt.

The normal trainer calls unmodified `qlib.model.trainer.task_train`. The delayed
trainer calls unmodified `begin_task_train` and `end_task_train`. Both preserve
recorder order, `_id in TaskManager` tags and `train_status` begin/end tags.
`worker()` drains available tasks and waits for other running claims to complete
or expire; an idle timeout bounds that wait. `force_release=True` uses Qlib's
existing subprocess helper with its current configuration.

Supported query operations are equality, dotted fields, `$and`, `$or`, `$in`,
`$nin`, `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte` and `$exists`. Unsupported
operators fail explicitly. This interface does not expose Mongo collection
objects, BSON-specific return values, arbitrary Mongo query syntax or native
`get_mongodb()`. `insert_task_def` and `insert_task` return an object with
`inserted_id`; decoded Python definitions/results are used. `replace_task` checks
the version and invalidates old claims. `remove` hides a task while retaining its
history and allows a later new task with the same definition.

Pickle is retained for faithful Qlib recorder, tuple, NumPy-array and Python-object
round trips. Task roots and artifacts must be trusted local files; this is not an
untrusted task submission service. The pinned environment may still contain
`pymongo` as a Qlib dependency; the replacement requires no Mongo service or client
construction and does not mutate the common lock.

## Recorder outbox and Parquet history

An accepted result and its outbox entry commit in one SQLite transaction after
the result bytes have been finalized. `reconcile()` applies accepted-result and
lineage tags to native MLflow recorders, then publishes an immutable history
snapshot. It retains failed outbox entries for retry. Older accepted stages that
have been superseded cannot overwrite the current task's lineage on a normal
reconciliation pass. SQLite accepted-result references remain authoritative;
MLflow tags are asynchronously reconciled metadata.

Reconciliation takes a per-pool Windows byte-range lock **before** reading the
outbox and holds it across tag updates and history publication. Competing local
processes wait at most 30 seconds by default (`reconcile(lock_timeout=...)` can
set a different finite bound). A process crash releases the OS lock automatically.
The persistent empty lock file is not evidence of an active owner. No SQLite
transaction is held across MLflow calls. This prevents an earlier delayed stage
from writing stale tags after a newer reconciler has already completed, while
allowing task claims and result commits to continue normally.

The lineage tags include the dataset snapshot ID and checksum, original source
hash, feature-file hash, workflow-code hash and common-lock hash when present in
the task's `joint_task` metadata. Native `RecorderCollector` can consume accepted
recorders directly and continues to perform native artifact loading, grouping and
processing. It does not need MongoDB.

History publication compares the committed snapshot's source sequence before
rewriting data. If a worker dies after snapshot publication but before updating
its export watermark, the next call verifies and adopts that snapshot. Current
local histories use complete Parquet snapshots; this deliberately favors simple
auditable recovery over a claim of incremental-file performance for millions of
tasks. No destructive history garbage collection runs automatically.

## Usage

```python
from pathlib import Path
from futu_bridge.joint.workflows import DelayTrainerRM, TaskManager, TrainerRM

root = Path("output/joint/tasks")
trainer = TrainerRM(experiment_name="research", task_pool="daily", root=root)
records = trainer.end_train(trainer.train(task_definitions))

delayed = DelayTrainerRM(experiment_name="research", task_pool="rolling", root=root)
prepared_records = delayed.train(task_definitions)
trained_records = delayed.end_train(prepared_records)

manager = TaskManager("daily", root)
states = manager.task_stat()
reconciliation = manager.reconcile()
```

Qlib and MLflow must first be initialized for the intended local data and artifact
root. Create the experiment once before starting multiple workers. Set an explicit
`task_pool` when different workflows share an experiment. `lease_seconds`,
`max_attempts` and `wait_timeout` are configurable trainer parameters; choose a
lease longer than expected local storage pauses. The heartbeat renews every third
of the lease interval. A worker whose renewal fails cannot publish its result.

For separate workers, initialize the same Qlib data/tracking configuration in each
shared-environment process and call the same trainer's `worker()`. The coordinator
may set `skip_run_task=True` to collect results in original submission order.

## Quiescent task backup and restoration

The operator path backs up `tasks.sqlite` and the complete immutable `history/`
store. It locks every existing pool's reconciliation path, reserves SQLite writes
with `BEGIN IMMEDIATE`, checks that the pool set did not change, and rejects any
running/owned task. Workers must finish or be explicitly stopped and recovered
before backup. A concurrent write transaction causes a bounded lock failure.
An incomplete failed destination is retained for diagnosis; choose a new path
when retrying. Backup does not silently cancel training or accept stale claims.

```powershell
& .venv-joint/Scripts/python.exe -m futu_bridge.joint.workflows `
  --root <existing-task-root> --destination <new-backup-root> backup
& .venv-joint/Scripts/python.exe -m futu_bridge.joint.workflows `
  --root <new-backup-root> verify-backup
& .venv-joint/Scripts/python.exe -m futu_bridge.joint.workflows `
  --root <new-backup-root> --destination <new-restored-task-root> restore
```

`backup_manifest.json` records exact file hashes, logical task-table fingerprints,
accepted/intermediate result references, native recorder identities, original
MLflow URIs and artifact file hashes. Verification is read-only; it does not
initialize or migrate the backed-up databases. Restoration rejects corruption,
missing recorders or changed/missing required original artifact bytes before
publishing a usable restored root. There are no active claims to revive because
backup requires quiescence. Native recorded IDs, results and history stay equal.

This is deliberately **not a portable MLflow or whole-system backup**. Native
MLflow databases and artifact directories remain separately owned, and must
still exist at their original absolute locations. The backup does not rewrite
URI fields inside trusted recorder pickle objects, migrate external tracking
databases, or copy provider captures outside the task history. Quiescent backup
and relocation of those external dependencies is a separate operator operation.
Keep backup roots immutable; once a restored root is opened for normal task
work, its live database may change and its original backup-file hashes no longer
describe current state. Retain the untouched backup as the restoration source.

## Real-data acceptance and local cases

Run in a new output directory:

```powershell
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'
.venv-joint/Scripts/python.exe -m futu_bridge.joint.workflows.acceptance `
  --response output/joint/model_20261109/model/provider_response.json `
  --output output/joint/parquet_tasks_20261110
```

The acceptance runner imports 1,683 real HK.00700 daily observations into the
canonical store and derives causal return/price-volume features from those
Parquet arrays. It trains six native ridge configurations with two workers and
eight with four workers. Every worker must hold a claim before the first training
completion, and every worker must produce a real accepted model. Independent
sklearn predictions, exact checkpoint reload and native `RecorderCollector`
outputs are checked on the held-out rows. These are conformance tests using the
existing historical split, not new evidence of predictive profitability.

It also runs native delayed preparation/fitting, one transient failure, permanent
failure exhaustion, and kills actual owned worker processes at four checkpoints:
after claim; inside the model-fit callback after native numerical fitting but
before callback return; after native model artifacts exist but before accepted
result commit; and after the accepted commit but before outbox reconciliation.
Expired heartbeats and result commits must be rejected. Dead workers must be
reclaimed before their 120-second lease deadline. The last checkpoint must
reconcile the original recorder without retraining. Mongo client construction is
patched to raise throughout native task/training execution in each acceptance
worker, making accidental Mongo use fail.

Twenty-nine ignored local task tests additionally exercise dictionary-order identity,
dry-run, priorities and query semantics, safe-fetch rollback, typed intermediate
results, heartbeats, automatic orphan recovery, cancellation fencing, terminal
waits, hash corruption, export interruption, outbox retry and remove/recreate
behavior. The added cases cover forward/backward wall-clock jumps, FIFO order,
monotonic reset with a dead owner, PID reuse, unknown process access, legacy schema
migration, exact Windows creation identity, exponential backoff/caps and eligibility
before/after a deadline. Two additional process cases serialize competing old/new
stage tags on the same real local MLflow run and kill a held-lock owner to verify
bounded wait and OS release. Reboot cases inject changed process state and reset timer
values; they do not reboot the user's PC. Test source and private captures remain
outside published artifacts.

The final schema-3 acceptance with serialized reconciliation passed in
[`parquet_tasks_reconcile_verified_20261110/report.json`](../output/joint/parquet_tasks_reconcile_verified_20261110/report.json).
It produced 21 distinct accepted native model recorders, nine published task-history
snapshots and no pending outbox errors. All accepted recorders carry matching
snapshot/source/feature/code/lock lineage; interrupted uncommitted recorder IDs
are identifiable and are not tagged as accepted. Proven dead workers were reclaimed
in less than 0.1 seconds while their original 120-second deadlines were still in the
future; the transient retry occurred 172 milliseconds after its first attempt,
exceeding the configured 100-millisecond backoff. Windows monotonic-clock resolution
limits precision for the shortest recovery measurement. The earlier successful
`parquet_tasks_verified_20261110` report certifies the initial wall-clock implementation
only and remains preserved. The earlier
`parquet_tasks_20261110` attempt is retained as failed evidence: its worker-PID
oracle incorrectly compared a Windows virtual-environment redirector PID with
the actual Python worker PID. The corrected run verifies actual child identities
and terminates the owned child process tree during fault injection.

Five further backup tests verify the accepted real-training store (22 tasks,
21 native recorders), exact task/result/history restoration, running-claim and
concurrent-writer rejection, corrupted backup rejection and missing native
artifact rejection. Actual CLI backup/restore/reverification reports are
`output/joint/tasks_backup_final_20261110.json`, `tasks_restore_final_20261110.json`
and `tasks_backup_restore_verify_20261110.json`. The additive backup module and
CLI were tested after the final 21-model run; execution-core source hashes remain
unchanged. The earlier backup probe is retained as failed evidence: verification
initially used a store constructor that rewrote the catalog header, invalidating
its byte hash. Final verification opens existing SQLite catalogs read-only.

This release does not claim multi-host coordination, exactly-once training,
arbitrary Mongo API compatibility or transactional atomicity across SQLite and
MLflow. Read the produced `report.json` and source hashes for the exercised
implementation and inspect any retained orphan runs before manual cleanup.
