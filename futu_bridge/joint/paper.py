"""Joint offline native execution conformance using a single frozen real capture."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from .paper_common import capture
from .paper_verify import verify
from .session import write_report


def worker(platform: str, response: Path, output: Path, variant: str, bps: int,
           source: Path, dotnet: Path) -> None:
    """Import one native engine per worker while sharing the exact Python environment."""
    data = capture(response)
    days = data.partitions["test"]
    if platform == "qlib":
        from .paper_qlib import run

        result = run(data, output, days, variant=variant, bps=bps)
    elif platform == "nautilus":
        from .paper_nautilus import run

        output.mkdir(parents=True)
        result = run(data, days, variant=variant, bps=bps)
    else:
        from .paper_lean import run

        result = run(data, output, source, dotnet, days, variant=variant, bps=bps)
    # Keep the raw evidence even when the independent verifier fails.
    write_report(output / "native.json", result)
    write_report(output / "verification.json", verify(data, result))


def run(response: Path, output: Path, repo: Path, python: Path, source: Path, dotnet: Path) -> dict[str, object]:
    """Build LEAN then independently verify eighteen native execution scenarios."""
    import subprocess
    from .paper_lean import build

    output, response = output.resolve(), response.resolve()
    if output == response.parent or response.parent in output.parents:
        raise ValueError("Use a separate output directory, outside the source capture")
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "provider_response.json"
    shutil.copyfile(response, frozen)
    write_report(output / "status.json", {"state": "running"})
    try:
        build(repo, source, dotnet, output)
        cases = {}
        for variant in ("split", "immediate"):
            for bps in (0, 5, 10):
                results = {}
                for platform in ("qlib", "nautilus", "lean"):
                    name = f"{platform}_{variant}_{bps}"
                    target = output / name
                    command = [str(python), "-m", "futu_bridge.joint.paper", "--platform", platform,
                               "--response", str(frozen), "--output", str(target), "--variant", variant,
                               "--bps", str(bps), "--lean-source", str(source), "--dotnet", str(dotnet)]
                    with (output / f"{name}.log").open("w", encoding="utf-8") as log:
                        subprocess.run(command, cwd=repo, stdout=log, stderr=subprocess.STDOUT,
                                       check=True, timeout=300)
                    results[platform] = json.loads((target / "verification.json").read_text(encoding="utf-8"))
                if len({json.dumps(r, sort_keys=True) for r in results.values()}) != 1:
                    raise ValueError("Native engines failed execution/account parity")
                cases[f"{variant}_{bps}"] = results
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != data.provenance["response_sha256"]:
            raise ValueError("Immutable input changed during native verification")
        report = {"passed": True, "cases": cases, "data": data.provenance, "test_days": data.partitions["test"],
                  "independent_verification": {"passed": True}, "shared_python": str(python),
                  "providers_modified": False, "real_orders_enabled": False,
                  "limitations": ["Derived quotes and LEAN trade marks from real opens, not observed tick/BBO data",
                                  "200-share quote depth and immediate settlement are simulation assumptions",
                                  "Market orders only; no queue, latency, auctions or corporate-action simulation",
                                  "HK.00700 only, price band (200, 500], 100-share lots",
                                  "Current IBKR Tier I cost scenario applied retrospectively"]}
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Internal worker CLI; the public joint command orchestrates all engines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("qlib", "nautilus", "lean"), required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", choices=("split", "immediate"), required=True)
    parser.add_argument("--bps", type=int, choices=(0, 5, 10), required=True)
    parser.add_argument("--lean-source", type=Path, required=True)
    parser.add_argument("--dotnet", type=Path, required=True)
    args = parser.parse_args()
    worker(args.platform, args.response, args.output, args.variant, args.bps, args.lean_source, args.dotnet)


if __name__ == "__main__":
    main()
