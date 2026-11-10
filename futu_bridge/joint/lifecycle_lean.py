"""Offline LEAN lifecycle conversion reusing the fixed-provider native quote exporter."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from .lifecycle import validate
from .paper_lean import build as build_paper, export
from .qlib_rl_data import ExecutionData


def build(repo: Path, source: Path, dotnet: Path, output: Path) -> None:
    """Compile converter-owned order/fill adapters in the pinned LEAN checkout."""
    shutil.copyfile(repo / "futu_bridge/converters/lean_lifecycle.cs",
                    source / "Algorithm.CSharp/LocalHKLifecycleAlgorithm.cs")
    build_paper(repo, source, dotnet, output)


def run(data: ExecutionData, task: dict[str, object], output: Path,
        source: Path, dotnet: Path) -> dict[str, object]:
    """Run native tickets and accounting; use declared finite simulated quote capacity."""
    validate(task)
    output, source, dotnet = output.resolve(), source.resolve(), dotnet.resolve()
    path = export(data, output, source, [task["day"]])
    (output / "commands.json").write_text("[]", encoding="utf-8")
    task_path = output / "task.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["algorithm-type-name"] = "LocalHKLifecycleAlgorithm"
    config["parameters"]["task"] = str(task_path)
    if task.get("extended"):
        (output / "data/lifecycle-clock.csv").write_text(task["day"] + " 16:00:00\n", encoding="utf-8")
        config["parameters"]["lifecycle-clock"] = "true"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    with (output / "engine.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(dotnet), str(source / "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
                        "--config", str(path)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
    return json.loads((output / "native.json").read_text(encoding="utf-8"))
