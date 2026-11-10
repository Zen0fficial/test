"""Validate the shared Windows Python research environment without placing orders."""

from __future__ import annotations

import argparse
import importlib
import platform
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from .session import write_report

CORE = {"qlib": ("pyqlib", "0.9.7"), "nautilus_trader": ("nautilus_trader", "1.231.0"),
        "mlflow": ("mlflow", "3.18.0"), "numpy": ("numpy", "1.26.4"), "pandas": ("pandas", "2.3.3"),
        "pyarrow": ("pyarrow", "25.0.1"), "gymnasium": ("gymnasium", "1.4.0"),
        "stable_baselines3": ("stable-baselines3", "2.9.2"), "torch": ("torch", "2.15.0"),
        "tianshou": ("tianshou", "0.4.10+joint.1")}


def verify(repo: Path) -> dict[str, object]:
    """Check one interpreter, exact resolved pins, native imports, and both import orders."""
    if (sys.version_info[:2] != (3, 12) or sys.platform != "win32"
            or platform.python_implementation() != "CPython" or platform.architecture()[0] != "64bit"):
        raise ValueError("The shared lock targets Windows x64 CPython 3.12")
    pins = (repo / "requirements/joint-py312-windows.lock").read_text(encoding="utf-8")
    count = 0
    for line in pins.splitlines():
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==")
        if version(name) != expected:
            raise ValueError(f"Shared dependency drift: {name}")
        count += 1
    modules: dict[str, object] = {}
    prefix = Path(sys.prefix).resolve()
    for name, (distribution, expected) in CORE.items():
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        if version(distribution) != expected or not origin.is_relative_to(prefix):
            raise ValueError(f"Package does not belong to the shared environment: {name}")
        modules[name] = {"version": expected, "file": str(origin)}
    importlib.import_module("qlib.data._libs.rolling")
    importlib.import_module("nautilus_trader.core.nautilus_pyo3")
    importlib.import_module("nautilus_trader.backtest.engine")
    importlib.import_module("nautilus_trader.live.node")
    for name in ("qlib.rl.trainer", "qlib.rl.order_execution.policy",
                 "qlib.rl.order_execution.simulator_simple", "qlib.rl.order_execution.simulator_qlib"):
        importlib.import_module(name)
    from torch.utils.tensorboard import SummaryWriter
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

    with tempfile.TemporaryDirectory(prefix="joint-tensorboard-") as folder:
        with SummaryWriter(folder) as writer:
            writer.add_scalar("native_rl/compatibility", 1.0, 0)
        events = EventAccumulator(folder).Reload().Scalars("native_rl/compatibility")
        if len(events) != 1 or events[0].value != 1:
            raise ValueError("TensorBoard/protobuf round trip failed")
    dependency_check = subprocess.run([sys.executable, "-m", "pip", "check"], check=True,
                                      capture_output=True, text=True, timeout=120)
    for order in ("import qlib.rl.trainer, nautilus_trader, mlflow, torch, stable_baselines3",
                  "import stable_baselines3, torch, mlflow, nautilus_trader, qlib.rl.trainer"):
        subprocess.run([sys.executable, "-c", order], check=True, capture_output=True, text=True, timeout=120)
    return {"passed": True, "python": platform.python_version(), "executable": sys.executable,
            "prefix": str(prefix), "modules": modules, "pinned_distributions_checked": count,
            "pip_check": dependency_check.stdout.strip(), "both_import_orders_passed": True,
            "native_extensions_imported": True, "qlib_rl_imports_passed": True,
            "tensorboard_protobuf_roundtrip": True, "real_orders_enabled": False}


def main() -> None:
    """Publish shared-runtime evidence only after every compatibility check passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Choose a new runtime verification report path")
    report = verify(args.repo.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.output, report)
    print(f"Shared runtime verified: {report['pinned_distributions_checked']} pinned distributions")


if __name__ == "__main__":
    main()
