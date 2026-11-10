"""Run native lifecycle conformance in workers sharing the pinned Python runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .lifecycle import CASES, scenario
from .lifecycle_extended import CASES as EXTENDED, make
from .lifecycle_verify import audit
from .paper_common import capture
from .session import write_report


def cases(suite: str) -> tuple[str, ...]:
    """Choose a documented suite, rejecting accidental incomplete coverage requests."""
    if suite not in ("baseline", "extended", "all"):
        raise ValueError("Lifecycle suite must be baseline, extended or all")
    return CASES if suite == "baseline" else EXTENDED if suite == "extended" else CASES + EXTENDED


def worker(platform: str, response: Path, output: Path, source: Path, dotnet: Path, suite: str = "all") -> None:
    """Exercise selected lifecycle tasks over five held-out real sessions per engine."""
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    if platform == "qlib":
        from .paper_qlib import prepare_calendar
        from .lifecycle_qlib import run as execute

        prepare_calendar(data, output / "qlib_data")
    elif platform == "nautilus":
        from .lifecycle_nautilus import run as execute
    else:
        from .lifecycle_lean import run as execute
    results = {}
    for day in data.partitions["test"]:
        frame = data.minutes.loc[day]
        for case in cases(suite):
            task = make(frame, case) if case in EXTENDED else scenario(frame, case)
            key = day + "_" + case
            if platform == "qlib":
                native = execute(task, frame)
            elif platform == "nautilus":
                native = execute(task)
            else:
                native = execute(data, task, output / key, source, dotnet)
            write_report(output / f"{key}.json", {"task": task, "native": native})
            results[key] = audit(task, native, frame)
            print(platform, key, "passed", flush=True)
    write_report(output / "report.json", {"passed": True, "cases": results, "real_orders_enabled": False})


def run(response: Path, output: Path, repo: Path, python: Path, source: Path, dotnet: Path,
        suite: str = "all") -> dict[str, object]:
    """Build only converter additions, freeze inputs, and reject any missing lifecycle evidence."""
    from .lifecycle_lean import build

    selected = cases(suite)
    response, output = response.resolve(), output.resolve()
    if output == response.parent or response.parent in output.parents:
        raise ValueError("Lifecycle output must be outside the source capture")
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "provider_response.json"
    shutil.copyfile(response, frozen)
    write_report(output / "status.json", {"state": "running"})
    try:
        build(repo, source, dotnet, output)
        results = {}
        for platform in ("qlib", "nautilus", "lean"):
            with (output / f"{platform}.log").open("w", encoding="utf-8") as log:
                subprocess.run([str(python), "-m", "futu_bridge.joint.lifecycle_runner", "--platform", platform,
                                "--response", str(frozen), "--output", str(output / platform),
                                "--lean-source", str(source), "--dotnet", str(dotnet), "--suite", suite], cwd=repo,
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
            results[platform] = json.loads((output / platform / "report.json").read_text(encoding="utf-8"))
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != data.provenance["response_sha256"]:
            raise ValueError("Frozen lifecycle source changed")
        report = {"passed": True, "platforms": results, "data": data.provenance,
                  "scenarios": len(selected) * len(data.partitions["test"]) * 3, "suite": suite,
                  "providers_modified": False, "shared_python": str(python), "real_orders_enabled": False,
                  "activation": {"qlib": "same observation", "nautilus": "same observation",
                                 "lean": "next observation: native BacktestingBrokerage semantics"},
                  "limitations": ["100-share refreshed simulated depth, not observed historical depth",
                                  "Intraday cumulative parent fee convention; not a broker invoice reconstruction",
                                  "Native order state/accounting in Nautilus and LEAN; converter lifecycle in Qlib",
                                  "Nautilus stop-limit quote hook avoids native stale-trigger pricing",
                                  "Nautilus DAY maps to GTD at HK close; LEAN exact GTD is converter-owned",
                                  "GTC surviving this replay is not proof of restart or cross-day persistence",
                                  "No IOC/FOK, queue priority, network latency, auctions or cross-day orders"]}
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Internal worker interface used by the joint simulation API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("qlib", "nautilus", "lean"), required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lean-source", type=Path, required=True)
    parser.add_argument("--dotnet", type=Path, required=True)
    parser.add_argument("--suite", choices=("baseline", "extended", "all"), default="all")
    args = parser.parse_args()
    worker(args.platform, args.response, args.output, args.lean_source, args.dotnet, args.suite)


if __name__ == "__main__":
    main()
