# Shared Qlib, NautilusTrader, and MLflow runtime

The joint framework now uses **one Windows x64 CPython 3.12 environment**,
`.venv-joint`. Qlib, NautilusTrader, MLflow, and the ML code load the same
installed dependencies. Worker processes remain separate to contain engine
state; they invoke the same Python executable. LEAN remains a .NET application.
The running Futu provider and its original environment are unchanged.

## Tested dependency set

| Component | Version |
|---|---|
| CPython | 3.12.16 |
| Qlib | 0.9.7 |
| NautilusTrader | 1.231.0 |
| MLflow | 3.18.0 |
| NumPy | 1.26.4 |
| pandas | 2.3.3 |
| PyArrow | 25.0.1 |
| scikit-learn | 1.9.1 |
| LightGBM | 4.7.0 |
| Torch | 2.15.0 |
| Gym / Gymnasium | 0.26.2 / 1.4.0 |
| Stable-Baselines3 | 2.9.2 |
| Tianshou | 0.4.10+joint.1 (metadata-only compatibility wheel) |
| TensorBoard / protobuf | 2.21.0 / 7.36.2 |

[joint.in](../requirements/joint.in) records the deliberate direct constraints.
[joint-py312-windows.lock](../requirements/joint-py312-windows.lock) pins all
211 resolved dependency distributions. It is a Windows/Python 3.12 version
lock, not a cross-platform or artifact-hash lock. Install the complete set;
independent upgrades defeat the shared compatibility contract.

Python 3.12 has binary wheels for both selected frameworks. The initial shared
installation exposed an undeclared runtime conflict: PyArrow 26.0.1 imports
failed with NumPy 1.26.4 even though `pip check` succeeded. PyArrow 25.0.1 passes
actual native engine imports and replay with this NumPy version. Neither
framework needed source edits or binary rebuilds.

Native QlibRL additionally needs Tianshou 0.4.10. Its upstream distribution
pins protobuf 3.19 for an old documentation-build issue, conflicting with the
modern shared environment. `scripts/build_tianshou_compat.py` verifies the
original wheel's fixed SHA-256 and creates a deterministic local wheel that
removes only this obsolete direct dependency and adds a local version suffix.
All upstream Python and license bytes remain identical. TensorBoard retains
its own protobuf requirement. The setup script builds this wheel before
installing the lock with `--find-links tmp/joint-wheels`.

The native PPO/DQN real-data tests, TensorBoard event write/read, MLflow artifact
round trips, both framework import orders and `pip check` pass together. The
single-worker seed sampler uses a documented synchronous adapter to avoid
Qlib's Windows DataQueue destructor polling a closed handle; learning and
simulation code remain upstream. See [native RL details](native-qlib-rl.md).

Qlib's default file experiment store also conflicted with modern MLflow's
legacy-backend policy. The comparison now explicitly creates an **absolute
local SQLite database** and a local artifact directory. Qlib's native `R.start`
records one finished run per LightGBM model. The joint layer verifies metrics,
downloads each artifact through MLflow, and compares its SHA-256 with the
original. It does not enable the legacy file-store exception or inherit a
remote tracking URI. This follows MLflow's supported
[database-backed storage](https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/).

## Setup and verification

On a new machine, install Windows x64 Python 3.12, then run:

```powershell
.\scripts\setup_joint_runtime.ps1 -PythonExe 'C:\path\to\Python312\python.exe'
```

On this prepared machine `.venv-joint` already exists. Verify it without
reinstalling packages:

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.runtime_check `
  --output output/joint/new-runtime-check.json
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.quality
```

The checker enforces the version lock, verifies imports originate inside the
same environment, imports Qlib's compiled rolling library and Nautilus's
historical/live engines, checks multiple import orders, and runs `pip check`.
Real market-data tests are additional requirements, not replaced by these
import checks. `RuntimePaths.local()` and `run_live_platforms.ps1` now select
the shared interpreter. Explicit `RuntimePaths` remain available for legacy
reproduction. The older environments are retained, not overwritten or removed.

## Real-data verification on November 9, 2026

- Qlib, Nautilus, and LEAN replayed the same 1,560 real AAPL/MSFT minute bars
  and 5,760 real Polymarket outcome observations. Native values/timestamps
  matched exactly; Qlib passed its declared float32 tolerances.
- The existing HK.00700 daily Qlib binary dataset remains readable with the
  unchanged exporter and reader. Dates without original coverage remain null.
- Both live HK.00700 adapters passed 60-second runs with forced disconnection
  after 15 seconds: Nautilus delivered 21 fresh trades and 93 BBO updates;
  LEAN delivered 21 fresh trades and 92 BBO updates. Both reconnected and
  reported zero live orders.
- The [ML comparison](ml-comparison.md) completed in the shared environment,
  reproducing the selected model and costed portfolio results. Both Qlib
  experiment runs finished in SQLite, with artifact byte checks passing.
- The native RL milestone passed 183 Python regressions and three JavaScript
  regressions. Its structural gate covered 23 joint modules. These include twelve
  new native RL tests, including differential simulator behavior, future-data
  masking, independent accounting, exact checkpoint continuation and unchanged
  upstream algorithm bytes.

The updated runtime evidence is `output/joint/native_qlib_runtime_20261109.json`.
The full native training/evaluation report is
`output/joint/qlib_native_final_20261109/report.json`. The chronological experiment
has six native runs, 4,608 training episodes, and 11 independently checked test
and cost-scenario journals. This establishes the documented simple-simulator
workflow. The later [native execution milestone](native-execution.md) adds
the full nested HK exchange and native Nautilus/LEAN simulations using the same
211-package lock; no environment or dependency change was needed.

Local evidence: `output/joint/shared_runtime_final_20261109.json`,
`shared_history_final_20261109/report.json`,
`shared_live_20261109/{nautilus,lean}/verification.json`, and
`shared_ml_final_20261109/comparison/report.json` under `output/joint`.
`shared_validation_20261109.json` records the independent ledger, baseline,
cross-environment, legacy-dataset, and unchanged-provider audit. Simulation
summary differences across Python versions were at most `1.07e-14`, confined
to Sharpe calculations; cash and fee results matched.
The initial failed Arrow run is retained separately. These checks establish
compatibility for the pinned versions and exercised workflows; future upgrades
must pass the same checks before replacing the lock.
