# Native Qlib reinforcement learning

This page documents the original simple-simulator workflow. The separate
[full nested HK simulator](native-execution.md) is now implemented and tested,
including native PPO/DQN learning, exchange execution and account accounting.

The joint framework now integrates Qlib 0.9.7's **native PPO and DQN execution
workflow**, in the same `.venv-joint` used by NautilusTrader and MLflow.
Futu and Polymarket provider implementations are unchanged. This is separate
from the existing Gymnasium/Stable-Baselines3 allocation experiment.

## What is native, and what is adapted

| Component | Implementation |
|---|---|
| Training lifecycle | `qlib.rl.trainer.Trainer.fit/test` |
| Collection and learning | Native `TrainingVessel.train/validate/test` and Tianshou 0.4.10 |
| Algorithms | `qlib.rl.order_execution.policy.PPO` and `DQN` |
| Network | Qlib `Recurrent`, GRU, hidden/output dimensions 32/32 |
| Observations | Qlib `FullHistoryStateInterpreter`, including current/future masking |
| Actions | Qlib `CategoricalActionInterpreter`, fractions 0, 0.5 and 1 of a 200-share parent |
| Environment | Qlib `EnvWrapper` |
| Execution transitions | Qlib `SingleAssetOrderExecutionSimple`; only data loading is overridden |
| Reward | Native `PAPenaltyReward` with concentration penalty zero, minus child-order fees and adverse slippage |
| Data adapter | Validated Futu capture to HK session blocks and training-only normalized features |
| Seed iterator | Synchronous shuffled training epochs; single-pass validation/test |
| Artificial account | Independent Decimal cash ledger for native buy fills and prescribed closing sales |
| Experiment records | MLflow SQLite, metrics, weights, trainer checkpoints, artifact byte verification |

The [upstream QlibRL design](https://qlib.readthedocs.io/en/stable/component/rl/framework.html)
supports these simulator/interpreter/reward adapters. The algorithms are not
reimplemented in the joint layer. The full nested-executor simulator
`SingleAssetOrderExecution` is **not** interchangeable with this path: its
installed adapter contains `REG_CN` index calculations that need a separate,
tested HK adaptation. That adaptation is now available through
`train-qlib-nested`; it does not silently replace this simple-simulator path.

## Shared-runtime compatibility

Upstream Tianshou 0.4.10 requires protobuf 3.19, which conflicts with modern
packages in the shared environment. Its
[setup.py](https://raw.githubusercontent.com/thu-ml/tianshou/v0.4.10/setup.py)
identifies a Sphinx compatibility issue at that pin. Its wheel contains no
direct protobuf imports.

`scripts/build_tianshou_compat.py` produces `tianshou==0.4.10+joint.1` from the
original wheel with SHA-256
`4c52df8552a3c32e6d17996c5d89de3383a00d43648b6d0ffcbe52181a001b74`.
It removes only that obsolete direct dependency, changes distribution version
metadata and regenerates wheel records. Every upstream Python file and license
is byte-identical. The upstream module's `__version__` consequently still reads
`0.4.10`; distribution metadata identifies the compatibility build. TensorBoard
keeps its own protobuf requirement, and event write/read is tested with the
shared protobuf 7.36.2. The local version satisfies Qlib's `<=0.4.10` constraint.

The runtime lock now has 211 pinned distributions. Existing package versions
were not downgraded. The nine additions support native QlibRL. Fresh setup
builds the compatibility wheel before resolving the lock:

```powershell
.\scripts\setup_joint_runtime.ps1 -PythonExe 'C:\path\to\Python312\python.exe'
```

For an existing shared environment, installation is explicit, never performed
by a data provider or training job:

```powershell
.\.venv-joint\Scripts\python.exe scripts/build_tianshou_compat.py
.\.venv-joint\Scripts\python.exe -m pip install --find-links tmp/joint-wheels -r requirements/joint-py312-windows.lock
```

Qlib's multiprocessing `DataQueue` destructor also attempts to poll closed
handles on Windows. `SynchronousVessel` replaces only seed delivery for the
single dummy worker. It preserves shuffled complete training epochs and visits
each validation/test order exactly once. Differential tests verify simulator
transitions against the original class, and sampling tests verify membership
and repeatability. No upstream installed source files are patched.

## Capture and run

The input is a saved response from the unchanged local `/v1/bars` endpoint:

```powershell
New-Item -ItemType Directory -Force output/native-rl-input | Out-Null
Invoke-WebRequest -Uri 'http://127.0.0.1:8765/v1/bars?symbol=HK.00700&timeframe=1m&start=2026-10-12&end=2026-11-06&adjustment=none' -OutFile output/native-rl-input/response.json

.\.venv-joint\Scripts\python.exe -m futu_bridge.joint train-qlib-rl `
  --response output/native-rl-input/response.json `
  --output output/joint/native-qlib-new-run `
  --iterations 24 --episodes 32
```

Choose a new output directory each time. The Python equivalent is:

```python
from pathlib import Path
from futu_bridge.joint import JointResearch, RuntimePaths

research = JointResearch(RuntimePaths.local(Path.cwd()))
report = research.train_qlib_rl(
    Path("output/joint/native-qlib-new-run"),
    response=Path("output/native-rl-input/response.json"),
    iterations=24,
    episodes=32,
)
```

This runs both native algorithms with seeds 11, 29 and 47. Each policy receives
24 collections of 32 episodes. PPO uses five update passes per collection;
DQN uses its native update and target network. Actual interaction counts are
recorded; episodes can terminate early after the order fills. There is no
silent fallback to SB3 if native training fails.

The original `train-rl` command continues to run the separate daily SB3
allocation task with its existing arguments and artifacts.

## Market data and causality

The November 9 capture contains 6,289 real, unadjusted HK.00700 minute records
across 19 sessions from October 12 to November 6, 2026. The converter checks
chronology, duplicates, OHLCV constraints and capture completion.

Observed HK labels run 09:31–12:00 and 13:01–16:00, plus a separate 09:30
record. The converter explicitly treats the continuous-session labels as bar
ends and subtracts one minute for simulation start times. It excludes the
separate 09:30 records and records those exclusions. It verifies all 330
continuous minutes per session; incomplete sessions fail rather than being
filled with invented prices. The timestamp convention is an explicit
conversion assumption, not a claim that the provider now verifies sessions.

The resulting 6,270 continuous minute bars become 209 blocks of 30 observed
minutes. Lunch is not filled. Each block's execution reference is its first
observed minute's open. Features use block OHLCV with previous-session price
normalization and training-only scaling. Native Qlib masks the current and
future block features before a policy decision. Unit tests perturb future
features and verify that earlier observations remain unchanged.

The first day supplies previous-day context. Remaining sessions split in time:

| Partition | Sessions | Dates |
|---|---:|---|
| Training | 10 | October 13–27 |
| Validation | 3 | October 28–30 |
| Test | 5 | November 2–6 |

The test period was used during integration debugging. These are retrospective
engineering checks, not a new untouched performance holdout.

## Simulated execution and costs

The task is to execute a **prescribed 200-share buy order** across eleven
intraday opportunities. Actions request zero, 100 or the remaining shares.
Qlib's native simple simulator forces completion at the final opportunity.
It does not model exchange queues or partial fills here. Each nonzero step is
treated as a separate child order and receives its own commission minimum.

All 200 shares are sold at the captured session closing price in the artificial
cash ledger. That closing sale is prescribed, not an RL decision. HKD 100,000
cash carries across evaluation days, with no borrowing, no shorting, and
100-share lots. Cash and inventory constraints are checked on every fill.
The existing IBKR Tier-I fee function is applied, plus 5 bps adverse slippage
per side. Selected-policy scenarios also report 0 and 10 bps. This is a
current-cost scenario, not reconstruction of historical tariffs.

Validation chooses between all six learned policies, immediate execution, and
a two-lot schedule. Selection maximizes mean net buy-execution advantage
relative to the arithmetic mean of the eleven grid opens. That reference is a
diagnostic price benchmark, not an executable fractional-share TWAP strategy.
The selection is saved before held-out results are evaluated.

The tested validation choice was **immediate execution**, not an RL policy.
On the five test sessions at 5 bps:

| Implementation | Ending HKD | Net return |
|---|---:|---:|
| Validation-selected immediate execution | 103,830.09 | +3.83% |
| Fixed two-lot schedule | 101,622.60 | +1.62% |
| Native PPO, each of three seeds | 98,979.63 | -1.02% |
| Native DQN, seed 11 | 98,979.63 | -1.02% |
| Native DQN, seeds 29 / 47 | 101,881.89 | +1.88% |

Native training completed, but it did not beat the immediate-execution control
on this small sample. This execution task does not replace or establish an
improvement to the daily direction-prediction baseline or SB3 allocation task.

## Checkpoints, evidence and tests

Each native model saves pure policy weights and a trainer checkpoint. The
vessel checkpoint additionally preserves optimizer state, DQN update count,
sampler state and Python/NumPy/Torch RNG state, which upstream's minimal vessel
checkpoint omits. A fresh identically configured vessel can resume through
native `Trainer.fit(vessel, ckpt_path=...)`, with `max_iters` greater than the
checkpoint's completed iteration count. Saved trainer checkpoints have their
stop flag cleared for continuation. Only trusted local checkpoints should be
loaded; full trainer state uses native pickle-based serialization.

Tests compare uninterrupted training against save/reload/continuation for both
PPO and DQN and require bit-identical policy tensors for the tested CPU,
single-worker configuration. This is not a distributed/GPU determinism claim.

The full experiment records six finished MLflow runs, 4,608 native training
episodes and 11,220 interactions. Policy parameters change, all reload logits
match, and MLflow retrieves byte-identical weights and trainer artifacts.
An independent verifier recalculates captured prices, fee components, lot
sizes, inventory and cash for eleven evaluation/scenario journals containing
125 artificial fills. It does not call the production fee or simulation code.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.qlib_rl_verify --run output/joint/qlib_native_final_20261109/qlib_rl
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.runtime_check --output output/joint/new-native-runtime-check.json
.\.venv-joint\Scripts\python.exe -m unittest discover -s futu_bridge/tests
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.quality
```

Local tests remain excluded from published updates under the existing project
preference. The test specification covers malformed input, HK calendar
conversion, native simulator parity for buys and sells, causal masking,
training-only scaling, deterministic sampling, checkpoint continuation,
independent accounting/tampering, slippage, compatibility-wheel integrity and
honest capability verification. The existing SB3 and supervised tests remain.

Main evidence is under `output/joint/qlib_native_final_20261109/`, with runtime
evidence in `output/joint/native_qlib_runtime_20261109.json` and compatibility
replay in `output/joint/native_qlib_history_regression_20261109/report.json`.
These local artifacts are ignored by Git.

## Remaining feature coverage

`python -m futu_bridge.joint capabilities` lists the feature families with
implementation state and available local verification kept separate. It does
not convert the existence of installed classes into claims of tested support.

The subsequent [native execution milestone](native-execution.md) adds the
scoped full Qlib nested HK path and native Nautilus/LEAN market-order accounts.
Next priorities include broader order lifecycles, LEAN's modular strategy and
portfolio pipeline, broader Qlib handlers/models/online workflows, and MLflow
model registry/lifecycle features. General asset classes, full provider-field
conversion, point-in-time datasets and general cross-framework strategy contracts
remain explicit gaps. The common providers and shared Python requirement
continue to apply to all of them. Real brokerage routing remains disabled.
