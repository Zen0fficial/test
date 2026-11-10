# Durable local Qlib scheduling and model selection

`futu_bridge.joint.online_parquet` schedules native Qlib Ridge tasks against an
immutable Parquet snapshot. It uses native `RollingGen`, `DatasetH`, expressions,
`task_train`, MLflow recorders and the MLflow model registry. MongoDB is not
required. Local providers and the shared Python environment remain unchanged.

The scheduler consumes already published snapshots; capture remains a separate
operation. Each accepted task binds snapshot ID/hash, converter configuration,
source fingerprint and observed decision date. Polling the same version again,
including after a process restart, produces the same durable task identity.
Publishing additional observed dates creates a new task. No future calendar is
invented and no broker submission path is present.

## Usage

Use the same task root for all processes and an existing canonical store:

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.online_parquet poll `
  --root output/joint/online --store output/joint/data/store `
  --dataset hk00700_daily --output output/joint/online/latest_poll.json
.venv-joint/Scripts/python.exe -m futu_bridge.joint.online_parquet worker `
  --root output/joint/online
```

`--polls N --interval S` enables bounded repeated polling. The command does not
install a background service. The worker uses the joint task manager's durable
claims, leases, bounded retry and result fencing; it does not acknowledge an
expired worker's completion. See [task workflows](qlib-parquet-tasks.md).

The default configuration uses three causal return/price/volume expressions,
native Ridge, feature-only missing-value replacement and one-day label purges.
Changes to the definition create different task identities. These defaults are
a scheduling acceptance model, not an optimized trading strategy.

## Promotion, rollback and recovery

`Registry(root).register(task_id)` accepts a completed durable task with a
`FINISHED` native MLflow recorder and matching snapshot lineage. It creates a
native model version pointing to the Qlib model artifact. This is a generic
native Qlib artifact, not a claimed MLflow serving flavor.

`Registry(root).decide(candidate_id, reference_id)` compares candidate and
incumbent on the reference task's identical observed validation window. It
rejects training overlap, invalid lineage and nonfinite predictions. Promotion
requires strictly lower validation MSE. The current feature pipeline has no
learned normalization, so both models receive the same causal feature definition.

An explicit `rollback=True` permits rollback only to a previously accepted
champion. Every accepted decision is durable before publishing the native
MLflow `champion` alias. SQLite stores the small transactional decision/outbox
state; Parquet stores market data and task-history snapshots. `reconcile()`
replays the newest desired alias and repairs interrupted publication or external
alias drift. It cannot choose an older desired decision during a concurrent
promotion. Polling invokes reconciliation when registry state exists.

## Evidence and limits

`online_parquet_verify` kills a scheduler process after its first durable poll,
starts a fresh process, and verifies one accepted task for that snapshot. It then
publishes the real HK.00700 capture with 1,685 daily bars over the earlier 1,683,
trains both native models, checks identical-window validation, promotion,
rejection of regression, explicit rollback and recovery from alias drift.

The validation harness deliberately bootstraps the weaker validated candidate
in its isolated registry to exercise promotion. It is a functional test, not a
claim that chronological retraining always improves a model. Existing captures
are retrospective and do not establish historical publication-time availability.
No trades, simulated or real, are emitted by this scheduler. Portfolio execution
remains an explicit downstream simulated workflow.
