"""Unified native feature verification using fixed data and one Python environment."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

from .session import write_report
from .evidence import publish, source_manifest

PLATFORMS = ("qlib", "nautilus", "lean", "mlflow")


def worker(platform: str, response: Path, output: Path, repo: Path,
           mongo_uri: str | None = None) -> dict[str, object]:
    """Dispatch only explicitly integrated native verification implementations."""
    if platform not in PLATFORMS:
        raise ValueError("Unknown joint feature platform")
    module = importlib.import_module(f"futu_bridge.joint.{platform}_features")
    result = module.run(response, output, repo)
    if platform == "nautilus":
        from .nautilus_features_contingent import run as contingents

        result["contingent_orders"] = contingents(response, output / "contingents", repo)
    if platform == "qlib" and mongo_uri is not None:
        from .qlib_features_tasks import run as tasks

        result["distributed_tasks"] = tasks(response, output / "mongo_tasks", repo, mongo_uri=mongo_uri)
    write_report(output / "report.json", result)
    return result


def run(response: Path, daily_response: Path, output: Path, repo: Path, python: Path,
        platforms: tuple[str, ...] = PLATFORMS, *, mongo_uri: str | None = None) -> dict[str, object]:
    """Freeze real inputs and fail on any native implementation or independent audit failure."""
    if not platforms or len(set(platforms)) != len(platforms) or set(platforms) - set(PLATFORMS):
        raise ValueError("Feature platforms must be a nonempty unique supported selection")
    if mongo_uri is not None:
        uri = urlparse(mongo_uri)
        if uri.scheme != "mongodb" or uri.hostname not in ("127.0.0.1", "localhost") or uri.username:
            raise ValueError("Feature verification uses an isolated local MongoDB instance only")
    response, daily_response, output = response.resolve(), daily_response.resolve(), output.resolve()
    for path in (response, daily_response):
        if output == path.parent or path.parent in output.parents:
            raise ValueError("Feature output must be outside source captures")
    output.mkdir(parents=True, exist_ok=False)
    implementation = source_manifest(repo)
    inputs = output / "inputs"
    inputs.mkdir()
    sources = {}
    for name, path in (("minute", response), ("daily", daily_response)):
        frozen = inputs / f"{name}_provider_response.json"
        shutil.copyfile(path, frozen)
        sources[name] = {"path": str(frozen), "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest()}
    write_report(output / "status.json", {"state": "running"})
    reports = {}
    try:
        for platform in platforms:
            source = sources["daily" if platform == "qlib" else "minute"]["path"]
            extras = ["--mongo-uri", mongo_uri] if mongo_uri is not None and platform == "qlib" else []
            with (output / f"{platform}.log").open("w", encoding="utf-8") as log:
                subprocess.run([str(python), "-m", "futu_bridge.joint.coverage_runner", "--platform", platform,
                                "--response", source, "--output", str(output / platform),
                                "--repo", str(repo), *extras],
                               cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8", MLFLOW_DISABLE_AGENT_HINT="1",
                                        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                                        NUMEXPR_NUM_THREADS="1", LOKY_MAX_CPU_COUNT="2"))
            report = json.loads((output / platform / "report.json").read_text(encoding="utf-8"))
            if report.get("passed") is not True or report.get("real_orders_enabled") is not False:
                raise ValueError(f"Incomplete or unsafe native feature verification: {platform}")
            reports[platform] = report
        for source in sources.values():
            if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("Native feature verification mutated a frozen provider response")
        result = {"passed": True, "platforms": reports, "sources": sources, "shared_python": str(python),
                  "implementation_sources": implementation,
                  "providers_modified": False, "real_orders_enabled": False, "full_feature_coverage": False,
                  "scope": "Behaviorally verified native feature families; see each report for precise limits"}
        write_report(output / "report.json", result)
        publish(repo, {platform: output / platform / "report.json" for platform in reports}, implementation)
        write_report(output / "status.json", {"state": "completed"})
        return result
    except Exception:
        write_report(output / "status.json", {"state": "failed", "completed_platforms": list(reports)})
        raise


def main() -> None:
    """Run one feature worker; the public joint API coordinates multi-platform verification."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mongo-uri")
    args = parser.parse_args()
    result = worker(args.platform, args.response, args.output, args.repo, args.mongo_uri)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
