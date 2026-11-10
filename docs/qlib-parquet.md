# Qlib on canonical Parquet snapshots

The converter keeps Qlib 0.9.7's expression operators, DataHandler processors,
DatasetH, supervised learners and native PPO/DQN algorithms unchanged. Its
calendar, instrument and feature storage classes read the actual verified Arrow
arrays in `SnapshotStore`. The original JSON response is retained for provenance;
it is not used as the analytical data source by the Parquet readers.

## Configure an isolated worker

```python
from pathlib import Path
from futu_bridge.joint.qlib_parquet import initialize

initialize(
    Path("output/my_store"),
    "committed-snapshot-identity",
    cache=Path("output/my_cache"),
    context={"processor_fit_interval": ["2020-01-02", "2024-01-01"]},
)

from qlib.data import D
prices = D.features(["HK_00700"], ["$close", "$close/Mean($close,5)-1"])
```

Initialize once at worker startup, outside an active Qlib recorder. Qlib has
process-global provider wrappers; initialize clears its native memory caches when
switching snapshots. Do not switch unrelated workflows concurrently within one
interpreter. Independent workers can safely pin different snapshots.

`ParquetCalendarStorage`, `ParquetInstrumentStorage` and `ParquetFeatureStorage`
implement the installed native storage interfaces. Configuration passes those
classes through Qlib's `LocalCalendarProvider`, `LocalInstrumentProvider` and
`LocalFeatureProvider`; no installed package is patched. Frequency substitution
is rejected. Raw minute labels remain source labels in this general data path.
Daily labels retain the provider's trading date.

The canonical decimal values remain exact. The Qlib view uses float32, matching
the existing bin exporter and native expression behavior. Missing VWAP remains
NaN because the provider's raw turnover scale is unverified. `factor=1` identifies
the explicitly unadjusted data; it is not a reconstructed corporate-action series.
Unknown fields and instruments follow the native missing-feature behavior.

Observed calendars and instrument spans do not establish future exchange sessions
or historical index membership. `future=True` emits a warning and returns the
observed calendar, preserving native historical rolling workflows without adding
fictional dates. Requests beyond available data return no observations or the
native out-of-range exception. An unknown market such as `hsi` is rejected.

## Update through publication

```python
from futu_bridge.joint.qlib_parquet_updates import publish_capture

update = publish_capture(
    Path("output/my_store"),
    "previous-snapshot-identity",
    Path("output/new-original-provider-response.json"),
)
new_identity = update["snapshot"]["identity"]
```

This converter-owned API merges complete original captures using
`symbol/timeframe/event_ns` identity. New observations extend observed calendars
and instrument lifetimes. Changed OHLCV, turnover or availability values replace
the current view only when the new capture was observed later. Older conflicting
corrections are rejected. Exact duplicate observations are no-ops. A later receipt
of unchanged prices still advances observed-at provenance in a new snapshot, so
an out-of-order older correction cannot overwrite information observed later.

Each update archives the original response before publishing an immutable
replacement through the coordinator's expected-head transaction. The returned
counts distinguish inserted, corrected and unchanged rows. A stale writer must
inspect and explicitly rebase onto the new head. Existing readers keep the old
snapshot and integer calendar indices. Older snapshots retain pre-correction
observations. This is a source-backed view updater, not a claim that a real price
correction occurred in the acceptance capture.

Direct native `clear`, `write`, calendar insertion and instrument mutation methods
remain read-only and raise `NotImplementedError`. They cannot coherently invent
source-backed market rows or historical constituent memberships. Use the explicit
publication API for supported updates. Arbitrary generic Qlib storage mutation
compatibility is not claimed.

## Cache behavior and legacy reproduction

`ParquetDatasetProvider` delegates evaluation to native Qlib and persists results
as Parquet. It retains pandas index and column metadata. Keys include the pinned
snapshot and content hash, ordered instrument universe and expressions, parsed
expression ASTs, query bounds, frequency, unadjusted-data mode, caller-declared
fit/availability context, converter/operator source hashes and numeric runtime
versions. Context affects cache identity; it does not fabricate missing historical
availability or apply a point-in-time filter by itself.

Native skip/use/refresh (`disk_cache=0/1/2`) semantics are supported. Requests with
arbitrary instrument processor objects bypass the persistent cache, so object
state cannot silently collide. The optional `ParquetExpressionProvider` provides
the same persistence contract for individual expressions. Normal initialization
uses native in-memory expressions plus the persistent dataset cache to avoid
writing hundreds of tiny expression files for Alpha360.

The per-process source view retains at most four snapshots and avoids rereading
Parquet for every field. Cache files publish through the transactional snapshot
coordinator. Competing workers may compute twice; one result is accepted and the
other must match exactly. Reads verify committed file hashes. Corrupt, partial
and stale entries are not treated as valid hits. No Redis service is required.

`export_bin(root, snapshot, output)` publishes a new, versioned native bin cache
from Parquet with a source-snapshot/file-hash manifest. It never overwrites an
existing output or changes the original exporter. The files preserve native
float32 features and absolute calendar offsets. Bin remains a selectable
compatibility path, while original market decimals remain in Parquet.

## Native RL from Parquet

`execution_from_snapshot(root, identity)` produces the existing `ExecutionData`
contract directly from Parquet. It retains the original converter's admission
rules: 330 continuous minutes per HK session, exclusion of the source 09:30
auction label, subtraction of one minute from admitted bar-end labels, lunch
break, eleven 30-minute blocks and chronological train/validation/test splits.
The conversion compares exactly against the original JSON reader, including
train-only feature normalization and index metadata.

Both `qlib_rl.run(..., data=execution_data)` and
`qlib_nested_rl.run(..., data=execution_data)` use this contract without modifying
their learning budgets, observations, actions, rewards, termination, fees or
checkpoint logic. The optional argument leaves existing callers unchanged.
Original-capture identity must match the input provenance. Native RL MLflow runs
record snapshot identity, snapshot content hash and source-response identity.

The public `parquet_training` entrypoint independently checks the canonical
arrays and session partitions against original archived source rows before any
training starts. Both simple and nested execution use this guard. The default
budgets remain 24 × 32 for each of six simple policies and 8 × 16 for each of two
nested policies. `--store ROOT --snapshot ID` accepts a pinned canonical union;
`--response PATH` first creates a new canonical store. `legacy_json` remains an
explicit rollback mode.

## Multiple captures and source partitions

`publish_capture` records the complete original source manifest when appending
or revising data. `qlib_parquet_lineage` reconstructs the selected union directly
from untouched, hash-checked archives and verifies every stored field, including
source-row ordinals, observation times and unknown historical availability.
The latest observed row wins. Equal-time conflicting receipts fail closed.
The final capture hash is an archive anchor, not a claim that its response alone
contains the whole union. All contributing archives and selections appear in
the provenance of a multi-source execution dataset.

The optional `source_rows=[...]` update argument publishes selected original
row ordinals without editing a response. Half-open source selection intervals
are recorded in `capture_selections`. This is also used to test partial-range
catch-up by partitioning existing real captures; these tests are explicitly
structural partitions, not claims of newly observed provider deliveries. Old
pinned snapshots remain unchanged. A shortened snapshot carrying a whole-source
manifest is rejected as incomplete, rather than silently accepted as a subset.

`qlib_parquet_lineage_verify` verifies a 3,972 + 2,317 row minute partition and
a two-capture 1,000 + 685 row daily union. It runs native simple/nested training
at the full default budgets and the unchanged daily comparison. The legacy
session converter consumes an in-memory reconstruction for independent audit;
no synthetic provider response is written or presented as an original capture.

Daily model comparison can also select `--store ROOT --snapshot ID`. Its new
independent verifier audits all 28 fee/slippage/control scenarios against saved
probability thresholds and exact next-session source prices. It reconstructs
whole-lot sizing, every fee component, cash, closing inventory, drawdown and
summary metrics without calling the production decision mapper, simulator or
fee helper. Frictionless scenario journals are saved as well as their summaries.

## Acceptance and test cases

Run with the existing shared environment and one numerical thread per library:

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.qlib_parquet_verify `
  --response output/joint/model_20261109/model/provider_response.json `
  --output output/joint/new_qlib_parquet_verification --train-rl
```

The acceptance compares seven primitive feature fields, calendar/instrument and
expression results, all Alpha158/360 raw/processed frames, DatasetH boundaries,
native Ridge predictions and model reloads. It checks exact values, not rounded
summaries. Canonical Parquet-derived feature bin files must match the legacy
exporter's bytes.

The real data comprises 1,683 daily bars and 6,289 source minute bars, of which
6,270 continuous minutes across 19 sessions feed execution. Full RL verification
trains six simple-simulator policies at 24 iterations × 32 episodes and two nested
policies at 8 × 16, retains native checkpoint reload/account audits and verifies
all eight MLflow runs reference their Parquet input snapshot. Source hashes must
remain unchanged throughout acceptance.

Local tests cover half-open boundaries, NaN versus absent fields, immutable
copies, unknown frequencies/universes, observed-only calendars, exact HK session
conversion, normalization, MultiIndex cache round trips, snapshot/context key
invalidation, corruption rejection, competing publishers, source-backed catch-up,
stale writers, mixed-frequency rejection and canonical bin export. A separate
genuine later capture verifies the addition of two daily bars and refreshed
observation provenance for the preceding 1,683 unchanged bars. Tests and
private market captures remain local and excluded from the wheel.

Current point-in-time fundamentals, historical constituent membership and future
calendar coverage remain constrained by missing authoritative input data. These
adapters do not relabel current or retrospectively revised information as data
known at an earlier decision time.
