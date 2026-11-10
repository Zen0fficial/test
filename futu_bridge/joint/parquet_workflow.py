"""One pinned Parquet dataset bundle through native research and simulated execution.

Daily model signals use the existing independently audited costed simulator.
Minute native-engine scenarios are execution conformance cases, not claims that
those engines trade the daily classifier. Native RL has a separate acceptance
runner using exactly the same canonical minute schema and split rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq

from .paper_verify import verify
from .qlib_parquet_execution import execution_from_snapshot
from .session import write_report
from .storage import SnapshotStore
from .storage.captures import ingest


def worker(platform: str, store: Path, snapshot: str, output: Path, repo: Path,
           variant: str, bps: int) -> None:
    """Run one offline native engine from Parquet arrays and independently audit every fill."""
    data = execution_from_snapshot(store, snapshot)
    days = data.partitions["test"]
    if platform == "qlib":
        from .paper_qlib import run

        result = run(data, output, days, variant=variant, bps=bps)
    elif platform == "nautilus":
        from .paper_nautilus import run

        output.mkdir(parents=True, exist_ok=False)
        result = run(data, days, variant=variant, bps=bps)
    elif platform == "lean":
        from .paper_lean import run

        result = run(data, output, repo / "tmp/lean-src", repo / "tmp/dotnet/dotnet.exe", days,
                     variant=variant, bps=bps)
    else:
        raise ValueError("Unsupported native offline platform")
    write_report(output / "native.json", result)
    write_report(output / "verification.json", verify(data, result))
    write_report(output / "dataset.json", data.provenance)


def run(response: Path, daily_response: Path, baseline: Path, output: Path, repo: Path) -> dict[str, object]:
    """Publish immutable inputs, run the unchanged model budgets, and compare eighteen native cases."""
    from .paper_lean import build

    output, repo = output.resolve(), repo.resolve()
    output.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(output / "store")
    minute = ingest(store, response, "hk00700_minute", "futu-bars")
    daily = ingest(store, daily_response, "hk00700_daily", "futu-bars")
    members = pa.Table.from_pylist([{"dataset": item.dataset, "snapshot": item.identity, "sha256": item.sha256}
                                   for item in (minute, daily)])
    bundle = store.publish("research_bundle", members, {"purpose": "HK.00700 research", "real_orders_enabled": False},
                           expected_head=None)
    env = {**os.environ, **{name: "1" for name in
                          ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
           "LOKY_MAX_CPU_COUNT": "2", "PYTHONHASHSEED": "0"}
    common = [sys.executable, "-m", "futu_bridge.joint.parquet_workflow", "--repo", str(repo),
              "--store", str(store.root)]
    model = output / "model"
    with (output / "model.log").open("w", encoding="utf-8") as log:
        subprocess.run([*common, "--worker", "model", "--snapshot", daily.identity, "--output", str(model),
                        "--response", str(daily_response.resolve()), "--baseline", str(baseline.resolve())],
                       cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
    model_report = json.loads((model / "report.json").read_text())
    if (not model_report.get("completed") or not model_report.get("model_reload_verified")
            or model_report.get("independent_verification", {}).get("passed") is not True):
        raise ValueError("Parquet model workflow did not complete verified artifact reload")
    build(repo, repo / "tmp/lean-src", repo / "tmp/dotnet/dotnet.exe", output)
    cases = {}
    for variant in ("split", "immediate"):
        for bps in (0, 5, 10):
            results = {}
            for platform in ("qlib", "nautilus", "lean"):
                name = f"{platform}_{variant}_{bps}"
                target = output / name
                with (output / (name + ".log")).open("w", encoding="utf-8") as log:
                    subprocess.run([*common, "--worker", platform, "--snapshot", minute.identity,
                                    "--output", str(target), "--variant", variant, "--bps", str(bps)],
                                   cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
                results[platform] = json.loads((target / "verification.json").read_text())
            if len({json.dumps(result, sort_keys=True) for result in results.values()}) != 1:
                raise ValueError("Native simulated account/fill parity failed")
            cases[f"{variant}_{bps}"] = results
    results = {}
    for name in ("features", "predictions"):
        published = store.publish(name, pq.read_table(model / f"{name}.parquet"),
                                  {"source_bundle": bundle.identity, "daily_snapshot": daily.identity,
                                   "real_orders_enabled": False}, expected_head=None)
        results[name] = published.identity
    report = {"passed": True, "bundle": bundle.identity, "minute_snapshot": minute.identity,
              "daily_snapshot": daily.identity, "result_snapshots": results, "native_cases": cases,
              "model_report": str(model / "report.json"), "selected_model": model_report["selected"],
              "independent_verification": {"passed": True,
                  "daily_model_simulations": model_report["independent_verification"],
                  "native_minute_cases": {name: {platform: value["passed"] for platform, value in engines.items()}
                                          for name, engines in cases.items()}}, "providers_modified": False,
              "runtime_lock_sha256": hashlib.sha256((repo / "requirements/joint-py312-windows.lock").read_bytes())
              .hexdigest(), "real_orders_enabled": False,
              "fees": {"schedule": "IBKR Pro SEHK Tier I", "reverified": "2026-11-10",
                       "historical_tariffs_reconstructed": False},
              "limitations": ["Previously examined holdout, not an untouched profitability test",
                              "Native execution cases use scheduled commands, not the daily ML signals",
                              "Daily ML signals use the independent costed simulated portfolio",
                              "No exchange queue, historical settlement or corporate-action evidence"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Internal workers and public canonical research workflow CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--daily-response", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--worker", choices=("model", "qlib", "nautilus", "lean"))
    parser.add_argument("--variant", choices=("split", "immediate"), default="split")
    parser.add_argument("--bps", type=int, choices=(0, 5, 10), default=5)
    args = parser.parse_args()
    if args.worker == "model":
        from .comparison import run as compare

        compare(args.response, args.baseline, args.output, args.repo / "tmp/qlib-v0.9.7",
                parquet=(args.store, args.snapshot))
    elif args.worker:
        worker(args.worker, args.store, args.snapshot, args.output, args.repo, args.variant, args.bps)
    else:
        if any(item is None for item in (args.response, args.daily_response, args.baseline)):
            parser.error("Provide minute/daily responses and the existing baseline directory")
        print(json.dumps(run(args.response, args.daily_response, args.baseline, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
