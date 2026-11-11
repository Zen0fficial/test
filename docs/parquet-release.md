# Parquet implementation and verification status — November 10, 2026

Parquet is the canonical analytical store for the joint research workflow.
Native Qlib data/ML/RL, local task training, MLflow lineage and all three simulated
execution paths are integrated. SQLite retains transactional publication/task
state and native MLflow metadata; original documents and model weights remain
hash-addressed files. No MongoDB service is required by the replacement workflows.

This is a scoped local research release, not full upstream feature coverage.
`capabilities` keeps implementation, current evidence and remaining scope separate.
The final machine-readable verification bundle is
`output/joint/plan_completion_20261110/report.json`; private captures and executable
tests remain local. The [development plan](parquet-development-plan.md) remains
the backlog for the wider coverage release.

## Implemented paths

| Area | Implementation and verified boundary |
| --- | --- |
| Immutable storage | Exact decimal/time/ID schemas, observed revision histories, as-of selection, durable journal, replay cursors, schema migration, compaction, rollback and verified restoration |
| Native reader views | `storage resolve-bars` selects one source-verified version per bar at explicit knowledge/event cutoffs; raw histories and original archives remain available |
| Qlib data and RL | Native calendars, instruments, features, expressions/caches, Alpha158/360, bin parity and source-backed updates; six simple and two nested policies retain their existing budgets |
| Complete capture lineage | Appended snapshots retain all original archive identities and selected source ordinals; native input arrays and split boundaries are checked before training |
| Mongo-free workflows | Native normal/delayed training with fenced claims, monotonic leases, exponential retry, stale-result rejection, Parquet history and serialized MLflow reconciliation |
| Task restoration | Quiescent `tasks.sqlite` plus history backup/restore; original native MLflow runs/artifacts are verified at their existing locations, not silently copied or relocated |
| Daily ML comparison | Same selected model and predictions as the legacy path; 28 simulations audited independently for decisions, whole lots, exact prices, costs, cash and equity |
| Common native execution | Eighteen Qlib/Nautilus/LEAN comparisons consume the canonical minute snapshot; all 390 previous lifecycle cases remain byte-identical |
| Research catalogue | 29 of 32 installed Qlib learner classes in selected configurations; native MetaModelDS on 20 real stocks; six-model cross-sectional diagnostics on 16,800 scored observations |
| Online research | Native rolling tasks, actual scheduler interruption/restart, validation-only model promotion/rejection, alias repair and rollback |
| Nautilus indicators | All 37 concrete pinned indicator classes across 48 routes; native handler/raw parity, 13 independent formula oracles, warmup/session/reset behavior and Parquet outputs |
| Native recovery | 31 Nautilus and 30 LEAN interrupted-process cases reconstruct state from immutable genesis inputs; this is deterministic replay, not native full-memory checkpoint restoration |
| Additional data | Ten typed provider families, all 14 fixed-provider methods and 98 pinned REST routes inventoried; original untyped payloads remain available |

The daily model portfolio and minute native execution matrix are distinct tests.
Scheduled native commands exercise execution semantics; they are not represented
as native trading of the daily classifier. Previously examined holdouts establish
compatibility and accounting, not an untouched profitability result.

## Native order additions

The [earlier order matrix](native-order-coverage.md) remains a historical component
record. Its pending LEAN recovery/grouping and Nautilus activation entries are
superseded by these later verified components:

| Component | Cases | Semantics |
| --- | ---: | --- |
| Partial-parent Nautilus brackets | 15 | Bounded child exposure, partial fills, cancel/amend and independent accounts |
| Native type/rejection matrix | 50 | Market-to-limit, touched and trailing-limit orders, post-only and explicit unsupported-auction outcomes |
| Source-selective trailing policies | 36 plus 18 native offset checks | Actual received trades/BBO; joint midpoint/double-confirmation policies distinguished from native arithmetic |
| LEAN trailing/touched orders | 60 | Fixed/percentage offsets, native triggers and a faithful post-trigger partial-remainder latch |
| Nautilus activation and funded sell market-to-limit | 45 | Native activation gates, dormant cancellation and funded opposite-side execution |
| LEAN contingent groups | 55 | Native OCO/OUO, complete-parent OTO, held/active child amendments/cancellation and explicit native rejections |
| LEAN combo orders | 30 | Three native combo types, complete groups, amendment/cancellation, native Day and date-end GTD |
| LEAN immediate intents | 60 | Explicit joint IOC/FOK controller; native tickets execute guarded immediate children, not nonexistent native IOC/FOK classes |

Native behavior is preserved even when frameworks differ. LEAN OTO activates
children only after complete parent filling. Its pinned OCO cancellation path
also cancels the partially filled winner's remainder; remaining unprotected
inventory is reported. OUO supports proportional continuation. Native LEAN GTD
expires at date end and remains distinct from the joint exact-timestamp policy.

See [activation tests](native-activation-orders.md), [LEAN recovery](lean-recovery.md),
[contingent groups](lean-contingent.md), [combos and immediate intents](lean-combos-immediate.md),
and the corresponding component reports. The [remaining execution work](native-execution-remaining.md)
separates integration work from missing historical market inputs.
Assumed spreads, available quantity, lots and settlement are explicit in each
simulation. Native combo conformance does not establish actual historical
multi-instrument lot sizes or exchange queue fidelity.

## Implementation selection

Equivalent outputs were checked before timing. Eleven rotated trials after two
warmups selected fused NumPy for the common three-feature workload: median
1.71ms versus 3.96ms for its Decimal reference and 27.26ms for native Qlib
expressions, with maximum reference error below 3.7e-16. The chosen profile was
then exercised through the public `history` command. Native Qlib retains its
broader expression language and learning algorithms.

The Parquet layout comparison retained Zstd with the 128,000-row-group target
on the current 6,289-row capture. It produced 167,542 bytes versus 217,205 bytes
for Snappy at the same target. Small row groups increased size. The separately
verified finite-value processor avoids native parallel-dispatch overhead for
the measured small workload. These are warm-cache, small-capture results;
they do not establish cold-disk performance, broad hardware scaling or an
unqualified speed ranking of entire frameworks.

## Final verification

The November10 integration gate passed **480 Python tests**, the three unchanged
provider JavaScript tests, and the structural coding gate for **108 joint Python
modules**. The combined feature command passed for native Qlib, NautilusTrader,
LEAN and MLflow, including LEAN's optimizer, shared-Python algorithm and QuantBook
research adapters. The shared runtime verified all **211 pinned distributions**;
the **58 protected provider files** remain byte-identical to the baseline.

The release rechecked **390 lifecycle cases** with byte-identical results, **18
common native execution cases**, and **28 daily portfolio scenarios** with
9,324 decisions and 3,938 simulated fills independently audited. These are
overlapping verification suites, not counts to add into a single coverage score.
Both recovery matrices were rerun against the current data adapter: 31 Nautilus
and 30 LEAN killed-process cases passed. The local storage tests also kill actual
publishers during partial writes, before catalogue commit and after commit.

All six simple RL policies retain 24 iterations with 32 episodes each, and both
nested policies retain eight iterations with 16 episodes each. The final change
to the simple-RL verifier only rejects missing snapshot identities before access;
the saved training results were preserved and all eleven resulting ledgers were
independently re-audited with the current verifier. Historical source manifests
remain immutable. Failed and incomplete run folders were subsequently removed
from the active results tree at the user's request; they do not count as evidence.

A 60-second live capture durably recorded 473 received messages, including 464
fresh observations, across one forced reconnect with no local overflow. It does
not prove a full session or exchange-lossless delivery. Five newly received closed
Polymarket events also round-tripped 350 outcome records without backdating their
knowledge timestamps; this is metadata evidence, not independent chain settlement
verification.

The built wheel matches all 108 joint Python modules and 12 C# converter sources
and passes an isolated import/resource check. Tests and private captures are
excluded from the wheel; local MLflow artifact directories are ignored by Git.
The machine-readable release report links the logs, source-bound component audit,
capability inventory and wheel checksum. Test-case descriptions are in the linked
component documents; executable tests and raw captures remain local.

## November 11 repeat verification and MongoDB retirement

The repeat evidence is in `output/joint/full_retest_20261111/report.json`.
All offline checks passed: 484 Python regressions, three JavaScript tests,
the four-framework feature suite, 390 lifecycle scenarios, 61 interrupted-process
recovery cases, the model/indicator suites, Parquet restoration and packaging.
The six simple and two nested Qlib policies retained their original budgets;
allocation PPO also completed nine policies at 32,768 steps each. These counts
overlap and do not establish full upstream feature coverage or trading profitability.

The unused MongoDB execution option was retired during this repeat. The old
`qlib_features_tasks` entrypoint now runs the local replacement; `--mongo-uri`
is rejected. The affected feature and task entrypoints were retested after the
change. Parquet remains the analytical/history store, with SQLite coordinating
transactions and MLflow metadata. Qlib's `pymongo` import dependency remains in
the unchanged shared lock, without a MongoDB service.

The live checks ran after the Hong Kong close. Both converters connected but
admitted no fresh updates; their verifiers failed on absent admitted-data files.
The report therefore records offline success and **live freshness unverified**,
not an overall live-feed pass. An open-session repeat is still required.

## Remaining release gates

The first analytical storage/training release and its regressions are distinct
from closing the entire upstream catalogue. Remaining work includes:

- **Engineering:** general multi-asset strategy/event contracts, broader native
  portfolio/risk and execution-algorithm composition, partial-ratio combo policy,
  further aggregation/configuration combinations and additional typed public
  endpoint fields. Existing real panels can support more research integration;
  these items must not be called external blockers.
- **Scale and operation:** partitioned incremental publication, bounded-memory
  long-history replay, measured ingestion headroom, independent-disk restoration
  and a complete fresh session including lunch/reconnect. Current full-table
  publication is deliberately limited to measured local capture sizes.
- **Data:** historical publication times and constituents, dated corporate
  actions/FX/settlement, verified multi-instrument lot/tick metadata, HIST concepts,
  enhanced-index risk inputs, exchange queues and auction observations. Current
  metadata receipt does not reconstruct its historical availability.
- **Pinned optional dependencies:** CatBoost, XGBoost and Plotly are absent from
  the fixed shared environment. No substitute algorithm is presented as native
  coverage. Multi-host operation also requires a separate-host test and an
  appropriate coordinator contract.

Real brokerage routing and paid/cloud deployment remain outside the manual-trading
scope. `full_feature_coverage` remains false until required partial and unverified
families are actually closed.

The [operator guide](parquet-operations.md) documents ingestion, explicit source
selection, compatibility rollback, task operations and verification commands.
The [coding standard](joint-coding-standard.md) distinguishes the structural gate
from semantic static type checking and describes local test publishing policy.
