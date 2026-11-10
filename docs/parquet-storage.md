# Parquet snapshot storage and baseline validation

Implementation history: 2026-11-10. This page records the initial P0/P1 foundation.
Subsequent implementation adds incremental versions, a durable capture journal,
replay cursors, predicate scans, compaction, observed filter decisions, native Qlib
Parquet ML/RL and MongoDB-free local tasks. See the current
[operator guide](parquet-operations.md), [Qlib adapters](qlib-parquet.md) and
[task contracts](qlib-parquet-tasks.md). Providers, legacy exports and the shared
dependency lock remain unchanged; `compare-models` now defaults to Parquet with
an explicit `--storage legacy_bin` rollback path.

## Available storage behavior

`futu_bridge.joint.storage.SnapshotStore` publishes complete Arrow tables into
immutable Parquet files. SQLite stores only snapshot metadata, lineage and the
current pointer for each dataset. Original capture bytes are archived by SHA-256.
Neither MongoDB nor a separate SQL server is needed for this storage component.

The first importers support complete, unadjusted Futu minute/daily captures and
observed Polymarket event metadata. Futu prices and volume use Decimal128 with
nine fractional places; values that cannot fit are rejected rather than rounded.
Event text and complete JSON metadata remain available alongside the raw capture.

Source bar timestamps are preserved. Historical availability is explicitly null,
and the acquisition timestamp is stored separately. The minute importer keeps all
6,289 original bars; the existing RL converter independently selects its 6,270
continuous-session observations. This store does not silently apply that filter
or change daily midnight labels into invented publication times.

Each publication closes and verifies the Parquet file, flushes it, finalizes its
unique filename, then compares the expected head and updates the catalogue in a
short SQLite transaction. A stale publisher fails with `SnapshotConflict`.
Readers use a snapshot ID and therefore retain their original data after a newer
head is published. Data/schema hashes and row counts are checked on reads.

A failed publication may leave a staged or finalized orphan file. `inspect()`
reports these files without deleting them; they cannot be read as committed
snapshots. Compaction, garbage collection and incremental row merging are not
implemented yet. The current reader performs full-file hash checks and the writer
verifies full-table round trips; these are correctness-first defaults, not a
claimed optimal analytical read path.

## Import existing captures

Choose a new storage root or a new logical dataset name. The first publication
expects no existing dataset head; subsequent publications require its exact ID.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.storage.captures `
  --root output/joint/my_parquet_store `
  --source output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --dataset hk_00700_minute --kind futu-bars

.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.storage.captures `
  --root output/joint/my_parquet_store `
  --source output/platform_compatibility/capture_20261102_03/event.json `
  --dataset polymarket_events --kind polymarket-events
```

Publication prints the committed snapshot ID and provenance. To create a new
complete version of an existing dataset, provide `--expected-head <snapshot-id>`.
This does not append rows or automatically deduplicate overlapping captures.
Schema changes require an explicit `allow_schema_change=True` through the Python
API; old versions remain independently readable.

```python
from pathlib import Path
from futu_bridge.joint.storage import SnapshotStore

store = SnapshotStore(Path("output/joint/my_parquet_store"))
snapshot_id = store.head("hk_00700_minute")
if snapshot_id is None:
    raise RuntimeError("Import the dataset first")
table = store.read(snapshot_id, columns=["event_ns", "open", "close"])
print(store.inspect())
store.backup(Path("output/joint/my_parquet_backup"))
```

Backup uses SQLite's online backup API, then copies and verifies referenced
immutable files and original captures. Restore by opening the new directory with
`SnapshotStore`. A same-disk backup tests restoration but does not protect against
physical disk failure. Sudden power-loss guarantees still depend on the filesystem
and hardware; interruption tests are not a hardware durability certification.

## Baseline fixes and verification

The LEAN native optimizer now receives its objective in the native `Statistics`
result structure, with an explicit export of native parameter IDs and values.
Independent verification checks every candidate and the validation-only winner.
The QuantBook probe uses History and pandas directly, avoiding an uninitialized
transaction manager. It verifies 62 native and pandas observations at the exact
source timestamps, including the native tick interval's two endpoints.
See [LEAN feature coverage](lean-feature-coverage.md).

The combined runner places frozen inputs and worker outputs in separate sibling
directories. Numerical worker counts are bounded without changing model
configurations or training iterations. Its published evidence index contains
source fingerprints and report hashes. The capability report distinguishes
current-source verification, historical scoped evidence, altered files and
source changes; an unavailable task manager is not certified by an unrelated
passing Qlib model report.

The 390-scenario regression passed on the original HK.00700 capture. All 130
journals per engine match the earlier baseline exactly. The shared runtime check
passed all 211 distribution pins. Local tests passed: 288 Python tests, including
15 storage cases, plus three native JavaScript tests. These counts describe this
milestone rather than a claim to full upstream feature coverage.

Storage cases include two competing Windows processes with one accepted writer,
stale-head rejection, an interrupted file write, failure after finalization,
tampering, snapshot pinning, schema migration, distinct NaN/null masks, and backup
restoration. Market-data inputs are real captures; fault injection changes test
conditions and is not presented as new market data. All trading remains simulated.

Local evidence:

- `output/joint/parquet_baseline_20261110/runtime.json`
- `output/joint/parquet_baseline_20261110/tests_final.log`
- `output/joint/lifecycle_p0_20261110/report.json`
- `output/joint/feature_coverage_parquet_foundation_20261110/report.json`
- `output/joint/parquet_store_20261110/verification.json`
- `output/joint/parquet_storage_tests_20261110.log`
- `output/joint/parquet_baseline_20261110/report.json` combines the milestone checks,
  current source manifest, provider invariance and wheel-content verification.

## Subsequent work and remaining limits

Incremental histories, as-of selection, durable journals and replay, time/column
predicates, compaction, raw archive audits and rollback are now implemented.
The source-backed Qlib adapters and caches have exact native feature/model parity,
and native simple/nested RL runs consume Parquet arrays. The task workflow uses a
separate local SQLite coordinator with fenced leases and Parquet history.

The [operator guide](parquet-operations.md) describes current commands. Current
small captures use full-table immutable publication; automatic garbage deletion,
multi-host tasks and full-session/lunch capture certification remain unavailable.
Field retention in raw JSON is distinct from complete typed conversion of every
provider API. Framework-wide feature coverage still requires the explicit native
and research cases in the development plan.
