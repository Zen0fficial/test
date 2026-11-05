"""Aggregate actual engine checks and verify the old provider files are fixed."""

import argparse
from pathlib import Path
import subprocess
import urllib.request
import json

from .capture import load_capture, read_json, sha256, write_json
from .validation import expected_rows


def audit(capture_dir, run_dir, lean_source):
    root = Path(__file__).resolve().parents[2]
    manifest, responses = load_capture(capture_dir)
    expected = expected_rows(manifest, responses)
    directory = Path(run_dir).resolve()
    reports = {name: read_json(directory / name / "verification.json") for name in ("nautilus", "lean", "qlib")}
    capture_hash = sha256(Path(capture_dir) / "manifest.json")
    for name, report in reports.items():
        if report.get("capture_sha256") != capture_hash:
            raise ValueError("Platform reports refer to different captures: " + name)
    for name in ("nautilus", "lean"):
        report = reports[name]
        if (not report.get("exact_decimal_match") or report["rows"] != len(expected)
                or report["live_orders"] != 0 or report.get("submitted_orders") != 0):
            raise ValueError("Incomplete engine verification: " + name)
    if not reports["qlib"].get("passed"):
        raise ValueError("Qlib compatibility check failed")
    protected = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "HEAD", "futu_bridge"], cwd=root, text=True).splitlines()
    # Existing modules/tests/exporter are fixed; newly added converters are independent.
    protected = [path for path in protected if not path.startswith("futu_bridge/converters/") and path != "futu_bridge/tests/test_platform_converters.py"]
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", *protected], cwd=root, check=True, stdout=subprocess.PIPE)
    with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=15) as response:
        health = json.load(response)
    if health.get("provider_connected") is not True:
        raise ValueError("Local Futu provider is no longer connected")
    report = {"passed": True, "capture": str(Path(capture_dir).resolve()), "capture_sha256": sha256(Path(capture_dir) / "manifest.json"),
              "window": [manifest["start"], manifest["end"]], "symbols": manifest["symbols"],
              "polymarket_event": manifest["event_slug"], "outcomes": manifest["outcomes"],
              "platforms": reports, "providers_unchanged": True,
              "protected_file_sha256": {path: sha256(root / path) for path in protected},
              "converter_file_sha256": {str(path.relative_to(root)): sha256(path)
                                        for path in sorted((root / "futu_bridge/converters").iterdir())
                                        if path.suffix in (".py", ".cs")},
              "local_health_after": health,
              "lean_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=lean_source, text=True).strip(),
              "submitted_orders": 0, "live_orders": 0, "new_market_data_providers": 0,
              "limitations": ["Historical engine compatibility, not a live execution adapter or a latency benchmark",
                              "US equities, raw one-minute regular-session bars; no corporate-action return certification",
                              "Historical probabilities replay at bucket end; original receipt times are unknown",
                              "Current metadata and quotes are archived but are not backdated as trading signals",
                              "Qlib uses float32; its numerical error is recorded separately",
                              "Nautilus 1.231 is tested; the 2.x release-candidate API is not interchangeable"]}
    write_json(directory / "verification.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--lean-source", required=True)
    args = p.parse_args()
    report = audit(args.capture, args.run_dir, args.lean_source)
    print(json.dumps({key: report[key] for key in ("passed", "providers_unchanged", "window", "live_orders", "new_market_data_providers")}))


if __name__ == "__main__":
    main()
