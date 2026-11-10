# Joint framework capability audit

> Historical snapshot before the native QlibRL integration on the same day.
> The missing Tianshou/native trainer gap below has since been resolved for
> native PPO/DQN with Qlib's simple execution simulator. See
> [the tested milestone and remaining limitations](native-qlib-rl.md), and run
> `python -m futu_bridge.joint capabilities` for the current feature inventory.
> The subsequent [native execution milestone](native-execution.md) also closes
> the scoped nested HK simulator and native paper-execution gaps. Other pending
> families below remain a backlog, not claims of implemented functionality.
> [Advanced offline lifecycle coverage](advanced-order-lifecycle.md) subsequently
> adds stop-limits, amendments and GTC/DAY/GTD; full feature coverage remains false.

Audit date: **2026-11-09**. Workspace: `F:\test`. Repository HEAD:
`cd6cbcb03dfcb59eb8d8931e8676dfd0bea96f59` (main), plus the current uncommitted
shared-runtime, supervised-comparison, artificial-simulation, and RL changes.

## Finding

**The joint framework does not currently provide the full capabilities of Qlib,
NautilusTrader, and LEAN.** It provides a tested subset of data conversion,
native data replay, live data observation, feature calculation, selected model
training, experiment tracking, and custom artificial portfolio accounting.

The original implementation narrowed the requested composition of frameworks
into a small research workflow. That is a substantive scope gap. Installing a
framework, finding its source files, or successfully importing a class does not
establish that its features work through the joint layer with our providers.

**Qlib's native reinforcement-learning pipeline is neither integrated nor
currently importable.** The new PPO implementation uses Stable-Baselines3 and
Gymnasium. It is an additional implementation, not QlibRL integration.

This audit paused feature work. It inspected code, installed distributions,
upstream documentation, builds, saved real-market experiments, and tests. It
ran import probes and existing regressions, without new training, dependency
installation, or market-data capture. The previously launched final PPO run had
already completed when its process was checked; no RL training process remained.

## Scope and evidence standard

This is a feature-family audit of the **pinned local versions**, with selected
executable probes and recorded integration evidence. It is not a claim to have
executed every upstream class, example, or optional plugin. No defensible
percentage of complete upstream coverage can be assigned without a versioned,
enumerated capability inventory and acceptance tests for every item.

| Status | Meaning |
|---|---|
| Tested, scoped | Exercised through our integration, within the stated data and behavior limits |
| Partial | Some of the family works; substantial upstream functionality is not exposed or verified |
| Unintegrated | Native code/API exists upstream or locally, but no supported joint workflow demonstrates it |
| Blocked | A concrete local dependency/import failure was reproduced |
| Intentional boundary | Excluded by the current user requirement, particularly real brokerage orders |

Source-file inventory and reproducible import results are recorded in
[`capability_audit_20261109.json`](../output/joint/capability_audit_20261109.json).
The inventory is evidence of code presence, not a feature-count denominator.
It includes Qlib models, strategies and RL sources, Nautilus indicator sources,
and LEAN Algorithm Framework sources. Compiled modules are not exhaustively
represented by a source-file listing.

## Installed and built environment

| Component | Local state | What this establishes |
|---|---|---|
| Python | CPython 3.12.16, `.venv-joint` | Qlib, Nautilus, MLflow, supervised ML and custom PPO share one dependency environment |
| Qlib | `pyqlib==0.9.7` | Installed; storage, expressions, DatasetH/DataHandlerLP, LightGBM and recorder paths exercised |
| NautilusTrader | `1.231.0` | Installed; native replay and live data-client paths exercised |
| MLflow | `3.18.0` | Installed; local SQLite runs and artifact round trips exercised |
| LEAN | Source commit `49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8`, local .NET build | C# custom-data observer algorithms run; no general LEAN Python integration demonstrated |
| Shared numerical stack | NumPy 1.26.4, pandas 2.3.3, PyArrow 25.0.1 | Pinned combination passes exercised native-import and workflow checks |
| Supervised ML | scikit-learn 1.9.1, LightGBM 4.7.0 | Selected logistic and Qlib LightGBM workflows run |
| Added RL stack | Torch 2.15.0, Gymnasium 1.4.0, Stable-Baselines3 2.9.2 | Custom PPO runs, not QlibRL |
| Legacy Gym | 0.26.2 | Still present for upstream dependencies; emits its maintenance warning |
| Missing optional packages | Tianshou, CatBoost, XGBoost, Ray, Optuna, Python `lean` and `pythonnet` distributions | Optional capabilities cannot be assumed available; concrete Qlib failures are detailed below |

The lock pins 202 distributions. It is specific to Windows/Python 3.12 and
contains version pins, not artifact hashes. Worker processes remain separate
but invoke the same interpreter. LEAN retains its .NET runtime. The running
provider and older Python environments remain separate and unchanged.

Two compatibility fixes are proven for the exercised paths: pinning PyArrow 25
avoids the observed Arrow 26/NumPy 1 incompatibility; explicit SQLite experiment
stores avoid Qlib's default file-store conflict with this MLflow installation.
Neither fix proves that all optional upstream modules work. In particular,
QlibRL currently fails before training can begin. Missing Python `pythonnet`
does not prove the absence of LEAN's .NET Python.Runtime assembly; LEAN Python
algorithms remain an unverified integration path.

## Joint API and implementation selection

The public `JointResearch` operations are `history`, `live`, `model`,
`compare_models`, and `train_rl`. The CLI exposes the same five operations.
Runtime checks and the coding gate are separate utilities.

The capability selector contains only four entries:

| Capability | Fixed preference |
|---|---|
| Historical replay | Nautilus, then LEAN |
| Live observation | LEAN, then Nautilus |
| Shared minute features | Qlib, then the independent reference implementation |
| Storage | Qlib |

These are explicit policies, not a general implementation discovery or
optimization system. Availability checks inspect prerequisite files. Fallback
occurs before work begins when prerequisites are unavailable; runtime failures
propagate rather than silently switching implementations. There is no
function-by-function upstream registry, general native-API passthrough,
cross-framework strategy interface, or automated selection across all upstream
implementations. Supervised validation selects among a small fixed candidate
set; it does not search the whole Qlib model catalogue.

## Qlib capability matrix

The installed source and the [Qlib 0.9.7 documentation](https://qlib.readthedocs.io/en/stable/)
provide the capability-family reference. Local integration findings come from
our code and probes.

| Feature family | Status | Local evidence and gap |
|---|---|---|
| Local data export and binary storage | Tested, scoped | Original CSV/bin exporter retained; real minute and HK daily datasets readable; float32 tolerances explicit |
| Data access and expression engine | Partial | Native `D.features` and selected expressions used; no general joint expression/configuration interface |
| DataHandlerLP and DatasetH | Tested, scoped | Used in the daily LightGBM comparison; not a general configurable dataset service |
| Alpha158 / Alpha360 and processor catalogue | Unintegrated | Both handlers import successfully; neither feature set is trained or validated through our joint workflow |
| Stock pools, dynamic universes, point-in-time fundamentals | Unintegrated | No joint universe lifecycle or validated point-in-time corporate/fundamental dataset |
| Expression/dataset caches and data services | Unintegrated | Tested comparison disables these caches; no service/cache lifecycle integration |
| LightGBM forecasting | Tested, scoped | Two native Qlib LGBModel candidates with 7/15 leaves; saved models and tracking verified |
| Broader neural/model catalogue | Partial / blocked | LSTM imports after Torch installation but is not trained; CatBoost and XGBoost imports fail because their packages are absent; other architectures unverified |
| Native strategy and portfolio backtests | Unintegrated | TopkDropout, weighting/enhanced-indexing and native exchange/account behavior are not connected to joint simulations |
| Nested decisions and order execution | Unintegrated | No native multi-frequency/nested-executor workflow with the fixed providers |
| Native RL trainer and policy families | Blocked | Trainer and order-execution policy imports fail on missing Tianshou; no QlibRL training/backtest evidence |
| Native RL state/action/reward/simulator interfaces | Unintegrated | No local-provider adapter for QlibRL's single-asset order-execution task or its interpreters/training vessel |
| Workflow configuration and recorder | Partial | Qlib recorder is used for two LGB runs; general `qrun` task/configuration workflow is not exposed |
| Task scheduling and distributed workflows | Unintegrated | Task-manager module imports, but no configured/tested task infrastructure |
| Rolling/online learning and model updates | Unintegrated | No native Qlib online manager/rolling workflow; the new custom PPO folds do not satisfy this |
| Meta-learning / data selection / tuning | Unintegrated | Source families exist; no joint task or verified local-data run |
| Native analysis/report catalogue | Partial | Joint metrics and plots exist; native signal/portfolio analysis catalogue is not exposed or systematically verified |

### Native RL failure versus the added PPO implementation

These commands were executed in the shared environment:

```python
import qlib.rl.trainer
import qlib.rl.order_execution.policy
```

Both fail with `ModuleNotFoundError: No module named 'tianshou'`.
Installed Qlib metadata declares its RL extra as `tianshou<=0.4.10`, `torch`,
and `numpy<2.0.0`. Adding Tianshou alone is not yet a proven compatibility fix:
the whole native stack still needs imports, environment tests, training,
serialization, and local-data evaluation in the shared environment.

QlibRL uses Tianshou/Gym and provides native simulator, interpreter, reward,
policy, training-vessel and trainer abstractions, including a single-asset
order-execution workflow. These are distinct from our Gymnasium allocation
environment and Stable-Baselines3 PPO implementation.
See the [QlibRL framework documentation](https://qlib.readthedocs.io/en/stable/component/rl/framework.html).

## NautilusTrader capability matrix

The pinned installed modules establish local code presence. The current
[upstream backtesting overview](https://nautilustrader.io/docs/latest/concepts/backtesting/)
describes the broader architecture; its latest APIs must not be assumed to
match pinned 1.231.0. This audit does not claim coverage of newer upstream APIs.

| Feature family | Status | Local evidence and gap |
|---|---|---|
| Historical native engine and callbacks | Tested, scoped | Real bars and custom Polymarket observations replay exactly; observer submits zero orders |
| Live data client and reconnect | Tested, scoped | HK.00700 native trade/BBO callbacks verified, including forced reconnection |
| Full order-book data and matching | Unintegrated | Raw depth may remain in the source journal; no general native depth/delta conversion and replay contract |
| Instrument and venue modelling | Partial | Narrow equity/custom-data mappings; replay precision is not proof of exchange tick-rule fidelity for every asset |
| Parquet catalogue / partitioned persistence | Unintegrated | `ParquetDataCatalog` imports; no supported joint catalogue-backed workflow |
| Indicators and aggregation | Unintegrated | Native catalogue is not exposed as a configurable joint feature backend |
| Strategy / Actor / execution-algorithm composition | Partial | Observer objects exist; no general shared strategy or execution-algorithm adapter |
| Native simulated orders and fills | Unintegrated | ExecutionEngine imports; no native strategy order/fill/P&L acceptance run with our providers |
| Fill, latency, fee and market-impact models | Unintegrated | Native model families are not used by the custom Decimal ledger |
| Portfolio, positions, accounting and risk engine | Unintegrated | No joint strategy run through these native capabilities |
| BacktestNode, configuration and batch runs | Unintegrated | Fixed replay runner only; no general experiment/batch facade |
| Native performance analysis and reports | Partial | Exact replay verification exists; native strategy performance catalogue is not connected |
| Native venue/broker adapters | Intentional boundary / unintegrated | Shipped adapters are not replacements for the fixed providers; optional dependencies/configurations unverified |
| Real brokerage execution | Intentional boundary | Disabled, consistent with manual trading |

## LEAN capability matrix

The local source checkout contains the Algorithm Framework families described
in the [official framework overview](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview).
The tested local route is a C# custom-data observer compiled into LEAN.

| Feature family | Status | Local evidence and gap |
|---|---|---|
| Local .NET engine and C# algorithm | Tested, scoped | Native historical and live observer algorithms run |
| Historical local/custom data | Tested, scoped | Exact bar and Polymarket observation replay through converter-owned readers |
| Live local data queue | Tested, scoped | HK.00700 trade/BBO observation and reconnect verification |
| History, calendars, consolidation and indicators | Partial | Narrow input/calendar mappings; no general joint History/consolidator/indicator interface |
| Universe Selection | Unintegrated | No joint dynamic universe model |
| Alpha and Insight lifecycle | Unintegrated | No conversion of trained joint signals into a tested native Alpha/Insight workflow |
| Portfolio Construction and optimizers | Unintegrated | No tested native target allocation pipeline |
| Risk Management | Unintegrated | No native risk-model workflow through the joint API |
| Execution models and native paper orders | Unintegrated | Observer algorithms submit zero orders; custom ledger is separate |
| Brokerage/security/fill/fee/slippage models | Unintegrated | IBKR costs are implemented in our Python ledger, not a verified LEAN fee model |
| Corporate actions, settlement, FX and buying power | Unintegrated | Not validated through local converters and native portfolio accounting |
| Asset-class coverage | Partial | Tested equities and custom probabilities only; options/futures/FX/crypto workflows not covered |
| Research/QuantBook and Python algorithms | Unintegrated / unverified | Source exists; no configured and tested shared-Python LEAN research/algorithm path |
| Optimization and parameter sweeps | Unintegrated | Native source present; no joint job/optimizer integration |
| CLI and deployment tooling | Unintegrated | Local .NET launcher path works; Python LEAN CLI distribution absent |
| Real brokerage execution | Intentional boundary | Disabled, consistent with manual trading |

QuantConnect-hosted services and licensed datasets are not automatically
included by building open-source LEAN. Their absence is not evidence that the
local engine build failed. The fixed-provider requirement still governs data
access for this project.

## MLflow and custom joint capabilities

MLflow is an independent package. The local integration uses tracking, not its
entire model-lifecycle feature set. Upstream distinguishes artifact logging
from registered model versions and aliases; see the
[MLflow Model Registry documentation](https://mlflow.org/docs/latest/ml/model-registry/).

| Feature family | Status | Local evidence and gap |
|---|---|---|
| Run parameters and metrics | Tested, scoped | Two finished Qlib LGB runs and nine finished PPO runs in their experiment stores |
| Model artifact storage and retrieval | Tested, scoped | Byte/hash checks and model reload checks recorded |
| Common tracking service/UI | Unintegrated | SQLite and artifacts are local to output directories; no central service deployed |
| Registered model versions and aliases | Unintegrated | Artifact logging is not a registered-model workflow |
| Model promotion, serving and automated deployment | Unintegrated | No joint lifecycle pipeline |
| Unified dataset lineage and generic evaluation | Partial | Explicit capture/code hashes and custom metrics; no general cross-framework lifecycle catalogue |
| Full autologging and other MLflow feature families | Unintegrated | No claim of complete package coverage |
| Fixed-provider composition and causal alignment | Tested, scoped | Immutable captures, provenance, timestamps, stale handling and gap-aware features |
| General provider-field coverage | Partial | News, options, earnings, full depth and other provider outputs are not all mapped into every framework |
| Supervised comparison | Tested, scoped | Frozen baseline, limited candidate set, validation selection and saved artifacts |
| Artificial trading | Tested, scoped | Custom single-stock Decimal ledger, costs and slippage; not native Nautilus/LEAN execution |
| PPO training | Tested, scoped | Custom Gymnasium environment and SB3 PPO; not QlibRL |
| General strategy/model interchange | Unintegrated | No universal model/strategy lifecycle spanning all three frameworks |
| Manual-trading signal service | Partial | Research prediction artifacts/helpers exist; no proven continuous model-to-dashboard signal/monitoring lifecycle |

## Data and simulation boundaries

1. The generic historical converter validates **US equity symbols** and the
   tested minute-bar session convention, plus Polymarket custom observations.
   Live HK.00700 and the separate HK daily model/RL verifier are additional
   specific paths; they do not establish universal HK historical conversion.
2. Shared minute features cover three formulas. The HK daily comparison uses
   the original 11 features plus four native Qlib expressions. It does not
   automatically incorporate all Qlib features or Polymarket signals.
3. Historical Polymarket availability uses an explicit retrospective bucket-end
   assumption. This is not certified historical publication-time data.
4. A reconnect test does not establish exchange gap recovery or lossless
   streaming. Native full-depth reconstruction remains unintegrated.
5. The artificial portfolio is HK.00700, HKD 100,000, long-only, no leverage,
   100-share lots, previous completed-bar signals, next-open entry and same-day
   close exit. It uses assumed fills and 0/5/10 bps per-side slippage scenarios.
6. The implemented IBKR Tier-I fee schedule is charged in the Decimal ledger.
   This audit verifies implementation evidence, not current tariff publication
   or a time-varying historical fee schedule. Turnover beyond the implemented
   tier is rejected. Unsettled proceeds are assumed reusable.
7. There is no native queue/partial-fill simulation, persistent overnight
   inventory, dividends/splits ledger, settled-cash account, margin model,
   multi-currency financing, or general multi-asset portfolio integration.
8. Qlib's `REG_US` configuration is used with an exported local HK calendar for
   feature computation. That is not a complete native HK market simulation.
9. The structural gate forbids several order-method names throughout joint
   Python modules. This is broader than the manual-trading requirement and can
   obstruct legitimate offline native backtests. It should eventually separate
   offline simulation from live routing, rather than treating both as forbidden.

## What the real-market evidence demonstrates

These are completed artifacts inspected during the audit. The audit did not
restart the captures or training.

| Evidence | Observed result | Limit |
|---|---|---|
| Shared runtime | 202 pinned distributions checked; native imports, import orders and `pip check` pass | Does not test every optional module; QlibRL fails the additional audit probes |
| Historical three-framework replay | 1,560 real AAPL/MSFT minute bars plus 5,760 Polymarket outcome observations | Native exact values/timestamps; Qlib float32 tolerances; data-only observer runs |
| Nautilus live HK.00700 | 21 fresh trades, 93 BBO updates; 19 fresh trades after reconnect | 60-second run; zero live orders |
| LEAN live HK.00700 | 21 fresh trades, 92 BBO updates; 18 fresh trades after reconnect | 60-second run; zero live orders |
| Supervised workflow after RL dependencies | Completed; models reload and two MLflow runs verified | Small predefined model family, single-stock task |
| Custom PPO | Three folds x three seeds x 32,768 steps = 294,912 steps; nine completed MLflow runs | Custom SB3 task, not native QlibRL |
| Independent RL evidence | 1,685 HK daily bars replayed natively; nine scenario ledgers and 766 artificial fills checked | Native data replay plus independent accounting, not native order execution |

The PPO task chooses cash, one lot, or the maximum affordable whole lots.
Its 15 market features plus equity/drawdown state are normalized using training
data. Positions close every session, making it largely a contextual allocation
task rather than multi-day inventory management or execution scheduling.

On the same 333 retrospective evaluation sessions, carrying cash continuously
between folds, the saved primary 5-bps results are:

| Strategy | Ending HKD | Net return | Round trips |
|---|---:|---:|---:|
| Cash | 100,000.00 | 0.00% | 0 |
| Validation-gated PPO | 97,487.01 | -2.51% | 5 |
| Ungated PPO ensemble | 94,787.705 | -5.21% | 30 |
| Original baseline policy | 78,387.645 | -21.61% | 61 |

The primary PPO gate selected the ensemble for the first test fold and cash
for the remaining two. These results show functioning training and accounting;
they do not show profitable signals or outperformance of cash. The evaluation
period had already been examined, so it is not a fresh untouched holdout.

## Verification, documentation and release status

- **171 Python tests passed** during this audit (`unittest discover`). These
  cover repository behavior, not upstream feature completeness.
- **3 JavaScript tests passed** for the native bridge hook/build behavior.
- The **17-module structural coding gate passed**. It checks syntax,
  documentation, annotations and selected policies. It is not a semantic type
  checker, full lint suite, coverage measurement, or upstream test-suite run.
- The nine import probes distinguish importable-but-unintegrated modules from
  concrete dependency failures. Import success alone is deliberately not marked
  as functional integration.
- There is no checked-in GitHub Actions workflow for this acceptance suite.
  Recent framework/simulation/RL test files remain ignored locally under the
  earlier instruction not to publish them. A clean clone therefore lacks those
  new regression tests.
- The most recent local wheel,
  `tmp/shared-joint-wheel/futu_desktop_bridge-0.4.0-py3-none-any.whl`, does **not**
  contain `futu_bridge/joint/rl.py`. It predates the RL changes and does not
  represent the current working tree.
- The current runtime/model/simulation/RL changes are **uncommitted and
  unpushed**. GitHub HEAD is not evidence of those local capabilities.
- `docs/shared-runtime.md` still lists 194 dependencies and older test/module
  counts. The actual lock has 202, and this audit passed 171 Python tests with
  17 joint modules. The package description and selector still say data-only.
- There is no dedicated, completed RL user guide. Existing documentation must
  not be interpreted as full upstream coverage.
- Targeted Git comparisons show no changes to the original provider,
  Polymarket HTTP/stream modules, Qlib exporter or baseline from repository HEAD.
  The unrelated user HTML document was not edited.

## Work required to meet the full-feature expectation

These are follow-up requirements, not changes made by this audit.

1. **Define a versioned capability catalogue with acceptance criteria.** Preserve
   access to native APIs and configuration while offering common adapters for
   genuinely interchangeable operations. A five-command facade cannot by itself
   represent the full native frameworks. Record dependencies, data requirements,
   platform constraints, implementation choice, and test evidence per capability.
2. **Integrate native QlibRL in the shared environment.** Resolve and verify its
   Tianshou/Gym/Torch stack, adapt local data to its native environment/task
   interfaces, train and reload native policies, and compare on controlled data.
   Keep the existing SB3 implementation distinctly identified as another backend.
3. **Connect native offline strategy and portfolio engines.** Exercise Nautilus
   simulated orders/fills/accounting and LEAN Alpha/Portfolio/Risk/Execution
   models, with equivalent fees, data and decision timing. Preserve the explicit
   prohibition on real brokerage routing.
4. **Expose Qlib's remaining research families.** Include feature handlers,
   model catalogue with optional dependencies, portfolio/nested backtests,
   rolling/online workflows, analysis, tuning and meta-learning. Mark every
   unsupported data prerequisite honestly rather than silently approximating it.
5. **Expand converters without changing providers.** Support required asset,
   calendar, universe, adjustment, depth and publication-time semantics from
   data the fixed providers actually supply. Missing source data must remain an
   explicit limitation, not be fabricated by a converter.
6. **Complete lifecycle and reproducibility.** Add model registry/versioning and
   explicit promotion where needed, unify experiment discovery, validate a clean
   environment/build, update documentation, and resolve the tension between
   published reproducibility and locally ignored acceptance tests.
7. **Benchmark overlapping implementations by capability.** Compare correctness,
   available features, reproducibility and performance under equivalent inputs;
   do not label an implementation universally optimal from one replay timing.

Full feature access should refer to the pinned **open-source** capabilities.
It does not remove upstream data requirements, install optional dependencies by
implication, provide paid hosted services, or authorize live orders. Existing
native APIs should remain usable when a common abstraction cannot preserve
their semantics.

## Local evidence index

- [Audit import probes and source inventory](../output/joint/capability_audit_20261109.json)
- [Audit Python regression log](../tmp/capability-audit-tests.log)
- [Shared runtime verification](../output/joint/shared_runtime_rl_20261109.json)
- [Historical replay report](../output/joint/shared_history_final_20261109/report.json)
- [Nautilus live verification](../output/joint/shared_live_20261109/nautilus/verification.json)
- [LEAN live verification](../output/joint/shared_live_20261109/lean/verification.json)
- [Supervised compatibility after RL dependencies](../output/joint/rl_supervised_compat_20261109/comparison/report.json)
- [Final custom RL report](../output/joint/rl_final_20261109/rl/report.json)
- [Native replay and independent RL ledger audit](../output/joint/rl_final_20261109/rl/verification.json)
- [Earlier shared-environment and simulation audit](../output/joint/shared_validation_20261109.json)

The `output/` and `tmp/` artifacts are local and ignored by Git. Their links are
available in this workspace; they will not become public merely by publishing
this Markdown report.
