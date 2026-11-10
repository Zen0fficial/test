# Operating the Parquet research store

The canonical analytical store contains immutable Parquet generations. SQLite
coordinates publication and local tasks; native MLflow owns its separate SQL
metadata. Futu and Polymarket acquisition code and the shared Python lock remain
unchanged. All execution commands below simulate trades locally.

Use PowerShell in `F:\test` and `.venv-joint\Scripts\python.exe`. No MongoDB or
Redis server is required for these adapters. Existing legacy bin datasets and
the old exporters remain available.

## Import and update

```powershell
$py = '.\.venv-joint\Scripts\python.exe'
& $py -m futu_bridge.joint storage --root output/joint/lake ingest `
  --dataset hk00700_daily --kind bars `
  --source output/joint/ml_comparison_20261109/comparison/provider_response.json
& $py -m futu_bridge.joint storage --root output/joint/lake list
& $py -m futu_bridge.joint storage --root output/joint/lake verify
```

The first import requires an absent head. Subsequent imports must explicitly pass
`--expected-head <identity>` from `list`. A stale expected head fails, including
when the input is a duplicate. Do not retry a publication by silently overwriting
the winner. Read the new snapshot and deliberately merge again.

Supported import kinds are `bars`, `events`, `probabilities`, `observations`
(captured SSE JSONL), and `decisions` (the existing three-stage equity filter).
Every original input is retained by content hash. Exact decimals and token/trade
IDs survive conversion. Unmapped provider fields remain in original JSON;
archival retention is not a claim that every field has an ML-ready schema.

Additional typed families use `provider-ingest`, for example:

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake provider-ingest `
  --dataset stock_news --family news `
  --source output/joint/provider_fields_capture_20261110/news_hk.json
```

Use `provider-ingest --help` for the ten family names. The [field inventory](provider-field-coverage.md)
lists actual filenames, supplied units, unknown fields, and the 98 pinned Polymarket
REST routes. A route inventory does not certify a successful live request to every route.
News/report links are metadata; unverified transcript text is not an issuer-verified statement.

Observation schema v2 uses canonical semantic JSON so whitespace changes during
journal replay do not create false revisions. Existing v1 datasets require an
explicit source-archive-verified migration into a new logical dataset:

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake migrate-observations `
  --snapshot <v1-snapshot-id> --dataset futu_observations_v2
& $py -m futu_bridge.joint storage --root output/joint/lake live `
  --dataset futu_observations_v2 --output output/joint/live_v2_new --seconds 300
```

Migration checks each original archive and independently regenerates normalized
fields before changing payload/version identities. Missing archives, modified
prices/times and inconsistent v1 version IDs fail. Old heads remain available.

Incremental histories retain observed versions. Identical delivery is idempotent;
conflicting values with the same identity and observation time fail. Independent
trade messages with equal price/size are preserved. Source sessions and sequences
remain explicit because cross-session duplicate trade identity is not guaranteed.

`storage.incremental.as_of(store, snapshot, knowledge_cutoff_ns,
event_cutoff_ns=...)` enforces knowledge and event-time cutoffs. Historical receipt
times remain unknown; historical bars/probabilities are not backdated to their
event time. A retrospective convention must be chosen explicitly by the research
adapter. Later event judgments and resolutions cannot silently enter earlier
features.

Native model readers need one resolved version per bar. Convert a revision
history imported by `storage ingest --kind bars` into a separate immutable view:

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake resolve-bars `
  --snapshot <history-snapshot-id> --dataset hk00700_daily_view `
  --knowledge-cutoff-ns <observed-UTC-nanoseconds>
```

An optional `--event-cutoff-ns` restricts market dates independently. The converter
verifies every selected row against untouched original archives and records the
history ID, both cutoffs and exact source ordinals. Queries before any original
receipt fail instead of backdating data. Use the resulting view ID with
`qlib_parquet.initialize`, `parquet_training --store/--snapshot`, or the explicit
canonical input arguments in `comparison`. The source history remains available.

For native Qlib, use `qlib_parquet_updates.publish_capture(root, pinned_snapshot,
response)` to create a resolved source-backed view. It accepts real catch-up data,
refreshes observation provenance and preserves old calendar indices in old
readers. Generic native in-place feature writes are intentionally unsupported.
See [Qlib storage contracts and tests](qlib-parquet.md).

## Capture live observations

Start the existing provider bridge if necessary; do not change its environment:

```powershell
.\.venv-qlib\Scripts\python.exe -m futu_bridge `
  --provider futu_bridge.native:create_provider --port 8765
```

In another shell, capture a bounded interval:

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake live `
  --output output/joint/live_capture_new --seconds 300 --disconnect-after 90
```

The consumer owns its socket and bounded queue. The journal's OS lock permits one
writer and is released on process death. Group commits default to one second or
10,000 messages. `durable_sequence` advances only after file fsync and the SQLite
watermark commit. `uncommitted_messages` describes the current loss window.
This is not a guarantee against every disk/controller/power failure.

Recovery verifies the hash chain, admits complete unacknowledged records and
quarantines an incomplete final fragment. A damaged acknowledged prefix fails
closed. Journal replay publishes Parquet before its cursor; if a process dies
between those steps, repeated replay is idempotent. Disk/permission failures stop
acknowledgement. Queue overflow is a failed capture, not a dropped-message success.
Every replay revalidates the acknowledged hash chain, even if the journal was
already open. Cursors bind to their journal prefix hash and committed snapshot
ancestry. Replacing a journal with different valid contents at the same path
fails closed; use a new capture path. Rollback atomically invalidates affected
replay cursors, so subsequent replay reprocesses the complete journal idempotently.

Reconnection records an unknown gap. The provider cannot supply lossless exchange
replay. Initial snapshots are retained but do not count as fresh market activity.
A short daytime capture does not certify a full session or lunch boundary.

## Research, model comparison and rollback

`compare-models` now defaults to Parquet for the Qlib analytical input. Add
`--storage legacy_bin` to reproduce the legacy bin path. Model settings, train/
validation/test boundaries and simulated cost rules are unchanged.

The joint `train-qlib-rl` and `train-qlib-nested` commands also default to a
canonical Parquet input; `--storage legacy_json` selects their original JSON
reader. Existing native learning budgets remain 24 iterations × 32 episodes for
the simple suite and 8 × 16 for the nested suite. To use an already pinned minute
snapshot directly:

```powershell
& $py -m futu_bridge.joint.parquet_training --algorithm nested `
  --store output/joint/lake --snapshot <minute-snapshot-id> `
  --output output/joint/nested_training_new
```

Resolve revision histories into one row per instrument/time before training;
ambiguous repeated bar identities are rejected. The raw archive supplies only
provenance when reading a pinned snapshot; market arrays come from Parquet.

```powershell
& $py -m futu_bridge.joint research-parquet `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --daily-response output/joint/ml_comparison_20261109/comparison/provider_response.json `
  --baseline output/joint/model_20261109/model `
  --output output/joint/parquet_research_new
```

This publishes a pinned daily/minute bundle, runs the existing model comparison
and independent costed artificial portfolio, and checks eighteen native Qlib,
Nautilus and LEAN execution cases against one minute snapshot. The native cases
use scheduled conformance commands; they are distinct from the daily classifier's
signals. Features and predictions are published back to Parquet. MLflow records
dataset, code and lock identities with its model artifacts.

The fee scenario was rechecked against IBKR's official schedules on 2026-11-10:
SEHK Tier I commission is 0.05%, minimum HKD 18, plus exchange/clearing/regulatory
charges and stamp duty. Current costs are applied retrospectively; historical
tariffs, financing and settlement are not reconstructed.
[Commission schedule](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php),
[SEHK third-party charges](https://www.interactivebrokers.com/en/accounts/fees/SEHK-StkFees.php).

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake compact `
  --snapshot <current-id> --compression zstd --row-group-size 128000
& $py -m futu_bridge.joint storage --root output/joint/lake rollback `
  --dataset hk00700_daily --snapshot <old-id> --expected-head <current-id>
```

Compaction verifies unchanged rows and publishes a new generation. Rollback
changes only the head pointer. Pinned readers remain stable in both cases. All
committed generations are retained. `cleanup-candidates` only lists old staged or
unreferenced files: age alone does not prove a slow publisher is dead, so automatic
deletion is disabled.

## Backup and restoration

```powershell
& $py -m futu_bridge.joint storage --root output/joint/lake backup `
  --destination output/joint/lake_backup_new
& $py -m futu_bridge.joint storage --root output/joint/lake_backup_new verify
```

Backup uses SQLite's online backup API and copies/hash-checks referenced immutable
Parquet and original archives. The destination is immediately usable as another
store root. It must be new and outside the source root. A second directory on the
same disk tests restoration; it does not protect against disk failure.

Capture journals and native MLflow databases/artifact directories outside the
store require their own consistent backup. Do not copy a live SQLite main file
without its journal or an online backup. Preserve MLflow artifact URI locations
when restoring its native database; the store backup does not rewrite them.

Inspect an existing local task root without deserializing task/model payloads:

```powershell
& $py -m futu_bridge.joint.workflows --root <task-root> inspect
& $py -m futu_bridge.joint.workflows --root <task-root> --pool <pool-name> export
& $py -m futu_bridge.joint.workflows --root <task-root> --pool <pool-name> reconcile
```

Inspection does not claim tasks or repair leases. `export` publishes immutable
task history; `reconcile` uses trusted local result artifacts to repair native
MLflow tags. Monotonic lease values are durations on this host, not UTC dates.

Task roots also support `backup`, `restore` and `verify-backup`. Backup requires
no running claims, locks recorder reconciliation, reserves SQLite writes and
copies the exact task database plus immutable history. Restoration verifies
the original native MLflow runs and artifact hashes at their existing locations;
this is not a portable backup of the separately owned MLflow service. See the
[task backup commands and tests](qlib-parquet-tasks.md).

## Acceptance commands and limitations

```powershell
& $py -m futu_bridge.joint.storage.acceptance --output output/joint/storage_check_new
& $py -m futu_bridge.joint.qlib_parquet_verify `
  --response output/joint/model_20261109/model/provider_response.json `
  --output output/joint/qlib_parquet_check_new --train-rl
& $py -m futu_bridge.joint.workflows.acceptance `
  --response output/joint/model_20261109/model/provider_response.json `
  --output output/joint/task_check_new
```

Use new output directories. Run heavy model suites and performance benchmarks
sequentially with bounded numerical threads. The RL command retains the existing
six simple-policy and two nested-policy configurations and training budgets.

The 2026-11-10 comparison used 11 rotated trials after two warmups. On the current
1,560-bar common-feature capture, medians were 1.71 ms for fused NumPy, 3.96 ms
for the Decimal reference, and 27.26 ms for native Qlib expressions. The NumPy
result matched the reference to 3.7e-16. These timings measure the same three
gap-aware features, not the complete frameworks. Native Qlib remains responsible
for its wider expression language and models. The processor comparison selected
`FiniteByDate` on 64 real Alpha158 rows; its large advantage includes native
parallel-dispatch overhead and must not be extrapolated to every workload.

Zstd with the incumbent 128,000-row-group target retained selection on 6,289
minute rows, producing a 167,542-byte file versus 217,205 bytes for Snappy at the
same target. These captures are smaller than a row group; sharding them further
increased size. All layout candidates had exact value/schema parity. These are
warm-cache measurements, not OS-cold I/O claims. Selection profiles bind source,
input, lock, runtime, thread settings and machine; stale profiles are rejected.

See [task contracts](qlib-parquet-tasks.md), [native order coverage](native-order-coverage.md)
and the [development plan](parquet-development-plan.md). Separate-host workers,
historical point-in-time fundamentals/risk inputs, optional model dependencies,
lossless order queues and all native APIs are not implied by these acceptance
commands. The capability report keeps implementation scope separate from current
source-bound evidence.

Read the current capability report with:

```powershell
& $py -m futu_bridge.joint capabilities
```

The [November10 release status](parquet-release.md) records the final 480-test
gate, all four native feature suites and the exact remaining scope. Its local
verification bundle is `output/joint/plan_completion_20261110/report.json`.
Changing source files invalidates published evidence until the affected paths
are reverified. A historical passing run is retained but does not certify a new
implementation automatically.
