"""Native LEAN Algorithm Framework, history, indicators and consolidation over fixed captures.

This adapter composes native universe/alpha/portfolio/risk/execution interfaces.
It never connects a brokerage and never installs or upgrades dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from time import perf_counter
from xml.sax.saxutils import escape

from .paper_common import capture
from .paper_lean import build as build_paper, export
from .qlib_rl_data import ExecutionData
from .session import write_report

PARAMETERS = ((10, 60, ".004"), (20, 60, ".004"), (30, 60, ".004"))


def python_environment(repo: Path) -> dict[str, str]:
    """Locate the shared interpreter DLL without selecting a different Python environment."""
    settings = dict(line.split(" = ", 1) for line in (repo / ".venv-joint/pyvenv.cfg").read_text().splitlines()
                    if " = " in line)
    base = Path(settings["home"])
    dll = base / "python312.dll"
    if not dll.is_file():
        raise ValueError("The fixed shared CPython 3.12 DLL is absent")
    return dict(os.environ, PYTHONNET_PYDLL=str(dll), PYTHONHOME=str(base))


def build(repo: Path, source: Path, dotnet: Path, output: Path) -> None:
    """Compile converter additions against the pinned LEAN source without upstream changes."""
    shutil.copyfile(repo / "futu_bridge/converters/lean_features.cs",
                    source / "Algorithm.CSharp/LocalHKFeaturesAlgorithm.cs")
    shutil.copyfile(repo / "futu_bridge/converters/lean_features_optimizer.cs",
                    source / "Algorithm.CSharp/LocalHKFeaturesOptimizer.cs")
    build_paper(repo, source, dotnet, output)


def build_host(repo: Path, source: Path, dotnet: Path, output: Path) -> Path:
    """Build a converter-owned host against existing DLLs without NuGet package dependencies."""
    output.mkdir(parents=True, exist_ok=False)
    references = list((source / "Launcher/bin/Release").glob("QuantConnect*.dll"))
    references += [source / "Launcher/bin/Release" / name for name in (
        "Python.Runtime.dll", "Newtonsoft.Json.dll", "NodaTime.dll")]
    if not all(path.is_file() for path in references):
        raise ValueError("The pinned native host dependencies are absent")
    items = "\n".join(f'<Reference Include="{escape(path.stem)}"><HintPath>{escape(str(path))}</HintPath>'
                      '<Private>false</Private></Reference>' for path in references)
    project = output / "JointLeanPythonHost.csproj"
    project.write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                       '<TargetFramework>net10.0</TargetFramework><AssemblyName>JointLeanPythonHost</AssemblyName>'
                       '<EnableDefaultCompileItems>false</EnableDefaultCompileItems><NuGetAudit>false</NuGetAudit>'
                       '</PropertyGroup><ItemGroup>' + items + '<Compile Include="' + escape(str(
                           repo / "futu_bridge/converters/lean_features_host.cs")) + '" /></ItemGroup></Project>',
                       encoding="utf-8")
    empty = output / "empty-package-source"
    empty.mkdir()
    with (output / "build.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), "restore", str(project), "--source", str(empty), "--nologo"],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        subprocess.run([str(dotnet), "build", str(project), "-c", "Release", "--no-restore", "--nologo"],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
    return output / "bin/Release/net10.0/JointLeanPythonHost.dll"


def execute(data: object, output: Path, source: Path, dotnet: Path,
            parameters: tuple[int, int, str], *, python_repo: Path | None = None,
            python_host: Path | None = None, optimizer_input: Path | None = None) -> dict[str, object]:
    """Run one native framework parameter configuration over every captured session."""
    days = sorted(set(str(t.date()) for t in data.minutes.index))
    config_path = export(data, output, source, days)
    clock = output / "data/feature-clock"
    clock.mkdir()
    for day in days:
        (clock / (day.replace("-", "") + ".csv")).write_text(day + " 16:00:00\n", encoding="utf-8")
    (output / "commands.json").write_text("[]", encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["algorithm-type-name"] = "LocalHKFeaturesAlgorithm"
    config["parameters"].update({"fast-period": str(parameters[0]), "slow-period": str(parameters[1]),
                                  "risk-limit": parameters[2]})
    if optimizer_input is not None:
        assembly = source / "Launcher/bin/Release/QuantConnect.Optimizer.dll"
        config["parameters"].update({"optimizer-input": str(optimizer_input),
                                      "optimizer-assembly": str(assembly),
                                      "validation-end": data.partitions["validation"][-1]})
    environment = os.environ.copy()
    if python_repo is not None:
        venv = python_repo / ".venv-joint"
        config.update({"algorithm-language": "Python", "algorithm-type-name": "LocalHKSharedPythonAlgorithm",
                       "algorithm-location": str(python_repo / "futu_bridge/joint/lean_features_python.py"),
                       "python-venv": str(venv),
                       "python-additional-paths": [str(python_repo / "futu_bridge/joint"),
                                                   str(source / "Launcher/bin/Release")]})
        environment = python_environment(python_repo)
    if python_host is not None:
        config["composer-dll-directory"] = str(source / "Launcher/bin/Release")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    started = perf_counter()
    command = [str(dotnet), str(source / "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
               "--config", str(config_path)]
    if python_host is not None:
        command = [str(dotnet), str(python_host), str(source / "Launcher/bin/Release"),
                   str(config_path), str(output / "host.json")]
    with (output / "engine.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=environment, check=True, timeout=300)
    result = json.loads((output / "native.json").read_text(encoding="utf-8"))
    if python_host is not None:
        host = json.loads((output / "host.json").read_text(encoding="utf-8"))
        if host != {"completed": True, "exit_code": 0, "python_finalized": True,
                    "engine_status": "Completed", "real_orders_enabled": False}:
            raise ValueError("Converter-owned host failed native engine/interpreter teardown")
        result["host"] = host
    result["wall_seconds"] = perf_counter() - started
    return result


def research(data: ExecutionData, config_path: Path, output: Path, repo: Path,
             source: Path, dotnet: Path, host: Path) -> dict[str, object]:
    """Run QuantBook History in its own native process and audit source and pandas values."""
    from .lean_features_verify import audit_research

    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("live-mode") is not False:
        raise ValueError("QuantBook verification must use the offline local data configuration")
    config.update({"object-store-root": str(output / "storage"),
                   "results-destination-folder": str(output / "results")})
    target = output / "config.json"
    write_report(target, config)
    report = output / "native.json"
    with (output / "engine.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), str(host), str(source / "Launcher/bin/Release"),
                        str(target), str(report), "research"], env=python_environment(repo),
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
    verified = audit_research(data.minutes, json.loads(report.read_text(encoding="utf-8")), repo)
    write_report(output / "verification.json", verified)
    return verified


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Freeze input, run native model composition and audit each candidate independently."""
    from .lean_features_verify import audit, audit_optimizer

    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    if output == response.parent or response.parent in output.parents:
        raise ValueError("LEAN feature output must be outside the source capture directory")
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "provider_response.json"
    shutil.copyfile(response, frozen)
    write_report(output / "status.json", {"state": "running"})
    source, dotnet = repo / "tmp/lean-src", repo / "tmp/dotnet/dotnet.exe"
    try:
        build(repo, source, dotnet, output)
        candidates, previous_scores, native_results = {}, [], {}
        optimizer_input = output / "optimizer-input.json"
        for fast, slow, risk in PARAMETERS:
            name = f"sma_{fast}_{slow}_risk_{risk}"
            optimizer_path = optimizer_input if len(previous_scores) == 2 else None
            if optimizer_path is not None:
                write_report(optimizer_path, previous_scores)
            native = execute(data, output / name, source, dotnet, (fast, slow, risk),
                             optimizer_input=optimizer_path)
            verified = audit(data.minutes, native)
            verified.update(wall_seconds=native["wall_seconds"])
            candidates[name] = verified
            native_results[name] = native
            validation_cash = verified["daily_cash"][data.partitions["validation"][-1]]
            previous_scores.append({"fast": fast, "validation_cash": validation_cash})
            write_report(output / name / "verification.json", verified)
        optimized = audit_optimizer(native["optimizer"], candidates, data.partitions["validation"][-1])
        host = build_host(repo, source, dotnet, output / "python-host")
        python_native = execute(data, output / "shared-python", source, dotnet, (20, 60, ".004"),
                                python_repo=repo, python_host=host)
        python_verified = audit(data.minutes, python_native)
        original = native_results["sma_20_60_risk_.004"]
        fields = ("trace", "features", "consolidated", "history", "universes", "clocks",
                  "cash", "holding", "daily_cash")
        if any(original[k] != python_native[k] for k in fields):
            raise ValueError("Native C# and shared-Python framework behavior differs")
        if Path(python_native["python_runtime"]["prefix"]).resolve() != (repo / ".venv-joint").resolve():
            raise ValueError("Native Python algorithm did not use the fixed shared environment")
        python_verified.update(native_event_parity=True, host=python_native["host"],
                               python_runtime=python_native["python_runtime"],
                               wall_seconds=python_native["wall_seconds"])
        write_report(output / "shared-python/verification.json", python_verified)
        research_verified = research(data, output / "shared-python/config.json", output / "research",
                                     repo, source, dotnet, host)
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != data.provenance["response_sha256"]:
            raise ValueError("Frozen provider capture changed")
        result = {"passed": True, "data": data.provenance, "candidates": candidates,
                  "native_optimizer": optimized, "shared_python_algorithm": python_verified,
                  "native_research": research_verified,
                  "parameter_selection": "Native grid maximizes pre-holdout validation cash; integration test only",
                  "implementation_selection": "Native framework composition; rolling sums for array mean batches",
                  "providers_modified": False, "real_orders_enabled": False,
                  "covered": ["scheduled_single_equity_universe", "native_insights", "native_insight_weighting",
                              "native_drawdown_risk", "native_immediate_execution", "native_tick_history",
                              "native_sma_ema", "native_tick_consolidation", "native_grid_optimizer",
                              "native_shared_python_algorithm", "native_quantbook_history"],
                  "limitations": ["One equity; no fundamental or multi-asset universe evidence",
                                  "Derived BBO and one-share source-open ticks; not historical tape/depth",
                                  "Custom HK calendar and immediate settlement remain scoped to the capture",
                                  "No native distributed optimizer or QuantConnect cloud/CLI deployment",
                                  "Python shutdown uses a converter-owned host; stock host fails in this runtime",
                                  "QuantBook covers local History and pandas; full research API remains unverified"]}
        write_report(output / "report.json", result)
        write_report(output / "status.json", {"state": "completed"})
        return result
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Provide a reproducible native LEAN feature verification entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo), indent=2))


if __name__ == "__main__":
    main()
