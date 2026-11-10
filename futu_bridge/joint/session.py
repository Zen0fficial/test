"""One research API coordinating engines in workers with shared Python dependencies."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .alignment import align
from .contracts import Dataset
from .features import reference_features
from .selection import Capability, Selection, select


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Shared Python by default; explicit paths retain legacy/runtime portability."""

    repo: Path
    qlib_python: Path
    nautilus_python: Path
    dotnet: Path
    qlib_source: Path
    lean_source: Path

    @classmethod
    def local(cls, repo: Path) -> RuntimePaths:
        """Use one pinned Python for Qlib, Nautilus, and MLflow; LEAN retains .NET."""
        root = repo.resolve()
        return cls(root, root / ".venv-joint/Scripts/python.exe",
                   root / ".venv-joint/Scripts/python.exe", root / "tmp/dotnet/dotnet.exe",
                   root / "tmp/qlib-v0.9.7", root / "tmp/lean-src")

    def available(self) -> set[str]:
        """Discover filesystem prerequisites; process failures still fail closed."""
        result = {"reference"}
        if self.qlib_python.is_file() and (self.qlib_source / "scripts/dump_bin.py").is_file():
            result.add("qlib")
        if self.nautilus_python.is_file():
            result.add("nautilus")
            result.add("parquet")
        if self.dotnet.is_file() and self.qlib_python.is_file() and (
            self.lean_source / "Launcher/QuantConnect.Lean.Launcher.csproj"
        ).is_file():
            result.add("lean")
        return result


def read_report(path: Path) -> dict[str, object]:
    """Require a JSON object, rejecting malformed backend completion artifacts."""
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected report object: {path}")
    return value


def write_report(path: Path, value: object) -> None:
    """Publish JSON atomically; a crash cannot leave a partial success report."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class JointResearch:
    """Compose Qlib, Nautilus, LEAN and shared causal data policies.

    Each invocation uses a new output folder. Child output is logged and every
    exit status checked. Missing runtime fallback occurs only before work;
    malformed data, conversion mismatches and crashes never silently downgrade.
    """

    def __init__(self, runtimes: RuntimePaths) -> None:
        """Bind paths without importing frameworks or starting providers."""
        self.runtimes = runtimes

    def research_parquet(self, output: Path, *, response: Path, daily_response: Path,
                         baseline: Path) -> dict[str, object]:
        """Run a canonical snapshot bundle through models and three native simulated engines."""
        from .parquet_workflow import run

        return run(response, daily_response, baseline, output, self.runtimes.repo)

    def _execute(self, command: list[str], root: Path, name: str, timeout: int | None = 300) -> float:
        """Run without a shell, with bounded waits and an operation-specific log."""
        started = time.perf_counter()
        with (root / f"{name}.log").open("w", encoding="utf-8") as log:
            subprocess.run(command, cwd=self.runtimes.repo, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)
        return time.perf_counter() - started

    def _lean_build(self, root: Path, live: bool = False) -> None:
        """Compile only converter-owned additions into the separate LEAN checkout."""
        source, destination = ("lean_live.cs", "LocalFutuLive.cs") if live else (
            "lean_algorithm.cs", "LocalProviderCompatibilityAlgorithm.cs")
        shutil.copyfile(self.runtimes.repo / "futu_bridge/converters" / source,
                        self.runtimes.lean_source / "Algorithm.CSharp" / destination)
        self._execute([str(self.runtimes.dotnet), "build",
                       str(self.runtimes.lean_source / "Launcher/QuantConnect.Lean.Launcher.csproj"),
                       "-c", "Release", "--no-restore", "--nologo", "-v", "quiet"], root, "lean-build")

    def _replay(self, backend: str, capture: Path, root: Path) -> float:
        """Run one real native engine and its independent value/timestamp verifier."""
        target = root / backend
        if backend == "nautilus":
            return self._execute([str(self.runtimes.nautilus_python), "-m", "futu_bridge.converters.nautilus",
                                  "--capture", str(capture), "--output", str(target)], root, backend)
        self._lean_build(root)
        base = [str(self.runtimes.qlib_python), "-m", "futu_bridge.converters.lean"]
        args = ["--capture", str(capture), "--output", str(target)]
        self._execute([*base, "export", *args, "--lean-source", str(self.runtimes.lean_source)], root, "lean-export")
        elapsed = self._execute([str(self.runtimes.dotnet), str(self.runtimes.lean_source /
                                "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"),
                                "--config", str(target / "config.json")], root, "lean-engine")
        self._execute([*base, "verify", *args], root, "lean-verify")
        return elapsed

    def history(
        self, capture: Path, output: Path, *, allow_retrospective: bool = False,
        verify_all: bool = True, max_age_seconds: int = 120, replay_backend: str = "auto",
        performance_profile: Path | None = None,
    ) -> dict[str, object]:
        """Create joint features and cross-provider observations from one capture.

        Explicit retrospective consent is mandatory because original data
        receipt timestamps are unknown. verify_all requires all three engines;
        False permits the documented missing-runtime fallback policy.
        """
        if not allow_retrospective:
            raise ValueError("Historical availability is unverified; set allow_retrospective=True explicitly")
        if type(max_age_seconds) is not int or max_age_seconds < 0:
            raise ValueError("Staleness limit must be a nonnegative integer")
        available = self.runtimes.available()
        selected = select(Capability.REPLAY, available, replay_backend)
        feature_backend = select(Capability.FEATURES, available)
        if verify_all and not {"nautilus", "lean", "qlib"} <= available:
            raise RuntimeError("verify_all requires Nautilus, LEAN and Qlib runtimes")
        capture = capture.resolve()
        root = output.resolve()
        if root == capture or capture in root.parents:
            raise ValueError("Output must not be inside the source capture")
        Dataset.from_capture(capture)
        root.mkdir(parents=True, exist_ok=False)
        write_report(root / "status.json", {"state": "running"})
        try:
            snapshot = root / "input"
            snapshot.mkdir()
            shutil.copyfile(capture / "manifest.json", snapshot / "manifest.json")
            records = read_report(capture / "manifest.json").get("records")
            if not isinstance(records, list):
                raise ValueError("Invalid capture record list")
            for record in records:
                source = (capture / record["file"]).resolve()
                if source.parent != capture:
                    raise ValueError("Capture record escapes the source directory")
                shutil.copyfile(source, snapshot / source.name)
            dataset = Dataset.from_capture(snapshot)
            if performance_profile is not None:
                from .performance import choose

                measured = choose(performance_profile, dataset)
                feature_backend = Selection(measured, "Verified equivalent-workload benchmark selection", False)
            started = time.perf_counter()
            reference = reference_features(dataset.bars)
            reference_seconds = time.perf_counter() - started
            write_report(root / "reference_features.json", {"rows": reference})
            joined = align(dataset.bars, dataset.probabilities, max_age_ns=max_age_seconds * 1_000_000_000)
            write_report(root / "joined.json", [
                {"symbol": row.bar.symbol, "available_ns": row.bar.available_ns, "token": row.token,
                 "probability": str(row.probability.value) if row.probability else None,
                 "probability_available_ns": row.probability.available_ns if row.probability else None,
                 "age_ns": row.age_ns} for row in joined
            ])
            backends = ("nautilus", "lean") if verify_all else (selected.backend,)
            durations: dict[str, float] = {}
            reports: dict[str, object] = {}
            for backend in backends:
                durations[backend] = self._replay(backend, snapshot, root)
                report = read_report(root / backend / "verification.json")
                if (report.get("capture_sha256") != dataset.capture_sha256
                        or report.get("submitted_orders") != 0 or report.get("live_orders") != 0
                        or report.get("exact_decimal_match") is not True
                        or report.get("bars") != len(dataset.bars)
                        or report.get("probabilities") != len(dataset.probabilities)):
                    raise ValueError(f"Incomplete/mismatched {backend} report")
                reports[backend] = report
            if feature_backend.backend == "qlib" or (verify_all and "qlib" in available):
                durations["qlib_storage"] = self._execute([
                    str(self.runtimes.qlib_python), "-m", "futu_bridge.converters.qlib", "--capture", str(snapshot),
                    "--output", str(root / "qlib"), "--qlib-source", str(self.runtimes.qlib_source)], root, "qlib")
                qlib_report = read_report(root / "qlib/verification.json")
                if (qlib_report.get("passed") is not True
                        or qlib_report.get("capture_sha256") != dataset.capture_sha256
                        or qlib_report.get("bar_rows") != len(dataset.bars)
                        or qlib_report.get("probability_rows") != len(dataset.probabilities)):
                    raise ValueError("Incomplete/mismatched Qlib report")
                reports["qlib"] = qlib_report
                self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.feature_worker",
                               "--capture", str(snapshot), "--storage", str(root / "qlib/equity_bin"),
                               "--output", str(root / "features.json"), "--backend", feature_backend.backend],
                              root, "selected-features")
            elif feature_backend.backend == "numpy":
                self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.feature_worker",
                               "--capture", str(snapshot), "--storage", str(root / "unused"),
                               "--output", str(root / "features.json"), "--backend", "numpy"],
                              root, "selected-features")
            else:
                write_report(root / "features.json", {"rows": reference, "max_absolute_error": 0.0})
            feature_report = read_report(root / "features.json")
            feature_rows = feature_report.get("rows")
            if not isinstance(feature_rows, list) or len(feature_rows) != len(dataset.bars):
                raise ValueError("Incomplete feature result")
            feature_map = {(str(row["symbol"]), int(row["available_ns"])): row for row in feature_rows}
            if len(feature_map) != len(dataset.bars):
                raise ValueError("Duplicate feature identities")
            outcomes: dict[tuple[str, int], dict[str, object]] = {}
            for row in joined:
                outcomes.setdefault((row.bar.symbol, row.bar.available_ns), {})[row.token] = {
                    "value": str(row.probability.value) if row.probability else None,
                    "available_ns": row.probability.available_ns if row.probability else None,
                    "age_ns": row.age_ns,
                }
            write_report(root / "research_rows.json", [
                {"symbol": bar.symbol, "available_ns": bar.available_ns,
                 "ohlcv": {name: str(getattr(bar, name)) for name in ("open", "high", "low", "close", "volume")},
                 "features": feature_map[(bar.symbol, bar.available_ns)],
                 "outcomes": outcomes.get((bar.symbol, bar.available_ns), {})}
                for bar in dataset.bars
            ])
            report = {
                "passed": True, "mode": "historical-data-only", "orders_enabled": False,
                "capture_sha256": dataset.capture_sha256, "availability": dataset.availability,
                "selection": {"replay": asdict(selected), "features": asdict(feature_backend)},
                "backends": reports, "bars": len(dataset.bars), "probabilities": len(dataset.probabilities),
                "joined_rows": len(joined), "missing_or_stale_rows": sum(r.probability is None for r in joined),
                "max_age_seconds": max_age_seconds, "reference_feature_seconds": reference_seconds,
                "feature_max_absolute_error": feature_report.get("max_absolute_error"),
                "backend_wall_seconds": durations,
                "timing_note": "Single-run wall times include different conversion/startup work; not a speed ranking",
                "limitations": ["Historical bucket-end availability is assumed, not observed receipt time",
                                "Qlib stores float32; exact source values remain in the immutable capture",
                                "No trading, execution, P&L, or automatic order capability"],
            }
            write_report(root / "report.json", report)
            write_report(root / "status.json", {"state": "complete"})
            return report
        except Exception as error:
            write_report(root / "status.json", {
                "state": "failed", "error_type": type(error).__name__, "detail": str(error),
            })
            raise

    def live(
        self, output: Path, *, seconds: int = 0, backend: str = "auto", disconnect_after: int = 0,
    ) -> dict[str, object]:
        """Run a data-only HK.00700 observer; STOP file controls continuous mode.

        The fixed converter handles stream retry; a framework crash never
        automatically changes backend or falsely claims lossless recovery.
        """
        if type(seconds) is not int or seconds < 0:
            raise ValueError("seconds must be a nonnegative integer")
        if type(disconnect_after) is not int or disconnect_after < 0:
            raise ValueError("disconnect_after must be a nonnegative integer")
        if seconds and disconnect_after and seconds < disconnect_after + 20:
            raise ValueError("Allow at least 20 seconds after the requested disconnect")
        chosen = select(Capability.LIVE, self.runtimes.available(), backend)
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        write_report(root / "selection.json", asdict(chosen))
        target = root / chosen.backend
        timeout = seconds + 120 if seconds else None
        if chosen.backend == "nautilus":
            command = [str(self.runtimes.nautilus_python), "-m", "futu_bridge.converters.nautilus_live",
                       "--symbol", "00700", "--output", str(target), "--seconds", str(seconds),
                       "--disconnect-after", str(disconnect_after)]
        else:
            self._lean_build(root, live=True)
            self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.converters.lean_live",
                           "--output", str(target), "--lean-source", str(self.runtimes.lean_source),
                           "--seconds", str(seconds), "--disconnect-after", str(disconnect_after)], root, "configure")
            command = [str(self.runtimes.dotnet), str(self.runtimes.lean_source /
                       "Launcher/bin/Release/QuantConnect.Lean.Launcher.dll"), "--config", str(target / "config.json")]
        self._execute(command, root, "live", timeout)
        report = read_report(target / "run.json")
        if report.get("live_orders") != 0:
            raise ValueError("Live observer must report zero orders")
        if seconds:
            verify = [str(self.runtimes.qlib_python), "-m", "futu_bridge.converters.live_verify",
                      "--output", str(target)]
            if disconnect_after:
                verify.append("--require-reconnect")
            self._execute(verify, root, "verify")
            report = read_report(target / "verification.json")
        result: dict[str, object] = {
            "completed": True, "mode": "live-data-only", "fresh_activity_verified": bool(seconds),
            "selection": asdict(chosen), "result": report, "orders_enabled": False,
        }
        write_report(root / "report.json", result)
        return result

    def model(self, output: Path, *, response: Path, start: str, end: str) -> dict[str, object]:
        """Train the existing leakage-tested baseline on a saved real daily response."""
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.ml.baseline",
                       "--input-response", str(response.resolve()), "--start", start, "--end", end,
                       "--output", str(root / "model")], root, "model")
        report = read_report(root / "model/report.json")
        if report.get("completed") is not True or report.get("orders_enabled") is not False:
            raise ValueError("Incomplete data-only model report")
        return report

    def compare_models(self, output: Path, *, response: Path, baseline_dir: Path,
                       storage: str = "parquet") -> dict[str, object]:
        """Compare research models with local artificial trades; never enable real orders."""
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.comparison",
                       "--response", str(response.resolve()), "--baseline-dir", str(baseline_dir.resolve()),
                       "--output", str(root / "comparison"), "--qlib-source", str(self.runtimes.qlib_source),
                       "--storage", storage],
                      root, "comparison", timeout=600)
        report = read_report(root / "comparison/report.json")
        if report.get("completed") is not True or report.get("real_orders_enabled") is not False:
            raise ValueError("Comparison did not complete with real orders disabled")
        return report

    def train_rl(self, output: Path, *, response: Path, baseline_dir: Path,
                 steps: int = 32768) -> dict[str, object]:
        """Train shared-runtime PPO policies and evaluate only artificial portfolios."""
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.rl",
                       "--response", str(response.resolve()), "--baseline-dir", str(baseline_dir.resolve()),
                       "--output", str(root / "rl"), "--qlib-source", str(self.runtimes.qlib_source),
                       "--steps", str(steps)], root, "rl", timeout=3600)
        report = read_report(root / "rl/report.json")
        if report.get("completed") is not True or report.get("real_orders_enabled") is not False:
            raise ValueError("RL did not complete with real orders disabled")
        self._execute([str(self.runtimes.nautilus_python), "-m", "futu_bridge.joint.rl_verify",
                       "--run", str(root / "rl")], root, "rl-verify", timeout=300)
        verification = read_report(root / "rl/verification.json")
        if verification.get("passed") is not True or verification.get("real_orders_enabled") is not False:
            raise ValueError("Independent RL verification failed")
        report = {**report, "independent_verification": verification}
        write_report(root / "report.json", report)
        return report

    def train_qlib_rl(self, output: Path, *, response: Path,
                      iterations: int = 24, episodes: int = 32, storage: str = "parquet") -> dict[str, object]:
        """Train native Qlib PPO/DQN and independently audit simulated execution."""
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.parquet_training",
                       "--algorithm", "simple", "--storage", storage,
                       "--response", str(response.resolve()), "--output", str(root / "qlib_rl"),
                       "--iterations", str(iterations), "--episodes", str(episodes)],
                      root, "qlib-rl", timeout=3600)
        report = read_report(root / "qlib_rl/report.json")
        if report.get("completed") is not True or report.get("real_orders_enabled") is not False:
            raise ValueError("Native Qlib RL did not complete with real orders disabled")
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.qlib_rl_verify",
                       "--run", str(root / "qlib_rl")], root, "qlib-rl-verify", timeout=180)
        verification = read_report(root / "qlib_rl/verification.json")
        if verification.get("passed") is not True or verification.get("real_orders_enabled") is not False:
            raise ValueError("Native Qlib RL independent verification failed")
        report = {**report, "independent_verification": verification}
        write_report(root / "report.json", report)
        return report

    def train_qlib_nested(self, output: Path, *, response: Path,
                          iterations: int = 8, episodes: int = 16, storage: str = "parquet") -> dict[str, object]:
        """Train through Qlib's full nested HK exchange with native simulated accounts."""
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.parquet_training",
                       "--algorithm", "nested", "--storage", storage,
                       "--response", str(response.resolve()), "--output", str(root / "nested"),
                       "--iterations", str(iterations), "--episodes", str(episodes)], root, "nested", timeout=3600)
        report = read_report(root / "nested/report.json")
        if (report.get("passed") is not True or report.get("real_orders_enabled") is not False
                or report.get("independent_verification", {}).get("passed") is not True):
            raise ValueError("Native nested training or execution verification failed")
        write_report(root / "report.json", report)
        return report

    def simulate_native(self, output: Path, *, response: Path) -> dict[str, object]:
        """Compare Qlib, Nautilus and LEAN native simulated fills and accounting."""
        from .paper import run

        if self.runtimes.qlib_python.resolve() != self.runtimes.nautilus_python.resolve():
            raise ValueError("Native execution comparison requires one shared Python environment")
        return run(response, output, self.runtimes.repo, self.runtimes.qlib_python,
                   self.runtimes.lean_source, self.runtimes.dotnet)

    def simulate_lifecycle(self, output: Path, *, response: Path, suite: str = "all") -> dict[str, object]:
        """Test simulated order lifecycles, including stop-limits, amendments and expiry."""
        from .lifecycle_runner import run

        if self.runtimes.qlib_python.resolve() != self.runtimes.nautilus_python.resolve():
            raise ValueError("Lifecycle conformance requires one shared Python environment")
        return run(response, output, self.runtimes.repo, self.runtimes.qlib_python,
                   self.runtimes.lean_source, self.runtimes.dotnet, suite)

    def verify_features(self, output: Path, *, response: Path, daily_response: Path,
                        platforms: tuple[str, ...] = ("qlib", "nautilus", "lean", "mlflow"),
                        mongo_uri: str | None = None) -> dict[str, object]:
        """Run native feature/model/portfolio/registry acceptance against fixed real captures."""
        from .coverage_runner import run

        if self.runtimes.qlib_python.resolve() != self.runtimes.nautilus_python.resolve():
            raise ValueError("Feature verification requires one shared Python environment")
        return run(response, daily_response, output, self.runtimes.repo, self.runtimes.qlib_python, platforms,
                   mongo_uri=mongo_uri)

    def benchmark_features(self, capture: Path, storage: Path, output: Path) -> dict[str, object]:
        """Select equivalent feature implementations using repeated correctness-qualified timings."""
        output = output.resolve()
        log_root = output.parent
        log_root.mkdir(parents=True, exist_ok=True)
        self._execute([str(self.runtimes.qlib_python), "-m", "futu_bridge.joint.performance",
                       "--capture", str(capture.resolve()), "--storage", str(storage.resolve()),
                       "--output", str(output)], log_root, output.name + "-benchmark", timeout=300)
        return read_report(output / "report.json")
