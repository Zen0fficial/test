"""Joint data research and local artificial portfolios; real orders stay disabled."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .session import JointResearch, RuntimePaths


def main() -> None:
    """Dispatch explicit data, model, or simulated comparison operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    storage = commands.add_parser("storage")
    storage.add_argument("--root", type=Path, default=Path("output/joint/lake"))
    storage.add_argument("arguments", nargs=argparse.REMAINDER)
    parquet = commands.add_parser("research-parquet")
    parquet.add_argument("--response", type=Path, required=True)
    parquet.add_argument("--daily-response", type=Path, required=True)
    parquet.add_argument("--baseline", type=Path, required=True)
    parquet.add_argument("--output", type=Path, required=True)
    history = commands.add_parser("history")
    history.add_argument("--capture", type=Path, required=True)
    history.add_argument("--output", type=Path, required=True)
    history.add_argument("--allow-retrospective", action="store_true")
    history.add_argument("--selected-only", action="store_true")
    history.add_argument("--max-age-seconds", type=int, default=120)
    history.add_argument("--performance-profile", type=Path)
    live = commands.add_parser("live")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--seconds", type=int, default=0)
    live.add_argument("--disconnect-after", type=int, default=0)
    live.add_argument("--backend", choices=("auto", "nautilus", "lean"), default="auto")
    model = commands.add_parser("model")
    model.add_argument("--output", type=Path, required=True)
    model.add_argument("--response", type=Path, required=True)
    model.add_argument("--start", required=True)
    model.add_argument("--end", required=True)
    comparison = commands.add_parser("compare-models")
    comparison.add_argument("--output", type=Path, required=True)
    comparison.add_argument("--response", type=Path, required=True)
    comparison.add_argument("--baseline-dir", type=Path, required=True)
    comparison.add_argument("--storage", choices=("parquet", "legacy_bin"), default="parquet")
    rl = commands.add_parser("train-rl")
    rl.add_argument("--output", type=Path, required=True)
    rl.add_argument("--response", type=Path, required=True)
    rl.add_argument("--baseline-dir", type=Path, required=True)
    rl.add_argument("--steps", type=int, default=32768)
    native_rl = commands.add_parser("train-qlib-rl")
    native_rl.add_argument("--output", type=Path, required=True)
    native_rl.add_argument("--response", type=Path, required=True)
    native_rl.add_argument("--iterations", type=int, default=24)
    native_rl.add_argument("--episodes", type=int, default=32)
    native_rl.add_argument("--storage", choices=("parquet", "legacy_json"), default="parquet")
    nested = commands.add_parser("train-qlib-nested")
    nested.add_argument("--output", type=Path, required=True)
    nested.add_argument("--response", type=Path, required=True)
    nested.add_argument("--iterations", type=int, default=8)
    nested.add_argument("--episodes", type=int, default=16)
    nested.add_argument("--storage", choices=("parquet", "legacy_json"), default="parquet")
    paper = commands.add_parser("simulate-native")
    paper.add_argument("--output", type=Path, required=True)
    paper.add_argument("--response", type=Path, required=True)
    lifecycle = commands.add_parser("simulate-lifecycle")
    lifecycle.add_argument("--output", type=Path, required=True)
    lifecycle.add_argument("--response", type=Path, required=True)
    lifecycle.add_argument("--suite", choices=("baseline", "extended", "all"), default="all")
    features = commands.add_parser("verify-features")
    features.add_argument("--response", type=Path, required=True)
    features.add_argument("--daily-response", type=Path, required=True)
    features.add_argument("--output", type=Path, required=True)
    features.add_argument("--platforms", nargs="+", choices=("qlib", "nautilus", "lean", "mlflow"),
                          default=["qlib", "nautilus", "lean", "mlflow"])
    performance = commands.add_parser("benchmark-features")
    performance.add_argument("--capture", type=Path, required=True)
    performance.add_argument("--storage", type=Path, required=True)
    performance.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "storage":
        from .storage.__main__ import main as storage_main

        storage_main(["--root", str(args.root), *args.arguments])
        return
    if args.command == "capabilities":
        from .capabilities import report as capability_report

        print(json.dumps(capability_report(args.repo.resolve()), indent=2))
        return
    research = JointResearch(RuntimePaths.local(args.repo))
    if args.command == "research-parquet":
        report = research.research_parquet(args.output, response=args.response, daily_response=args.daily_response,
                                           baseline=args.baseline)
    elif args.command == "history":
        report = research.history(args.capture, args.output, allow_retrospective=args.allow_retrospective,
                                  verify_all=not args.selected_only, max_age_seconds=args.max_age_seconds,
                                  performance_profile=args.performance_profile)
    elif args.command == "live":
        report = research.live(args.output, seconds=args.seconds, backend=args.backend,
                               disconnect_after=args.disconnect_after)
    elif args.command == "model":
        report = research.model(args.output, response=args.response, start=args.start, end=args.end)
    elif args.command == "compare-models":
        report = research.compare_models(args.output, response=args.response, baseline_dir=args.baseline_dir,
                                          storage=args.storage)
    elif args.command == "train-rl":
        report = research.train_rl(args.output, response=args.response,
                                   baseline_dir=args.baseline_dir, steps=args.steps)
    elif args.command == "train-qlib-rl":
        report = research.train_qlib_rl(args.output, response=args.response,
                                        iterations=args.iterations, episodes=args.episodes, storage=args.storage)
    elif args.command == "train-qlib-nested":
        report = research.train_qlib_nested(args.output, response=args.response,
                                           iterations=args.iterations, episodes=args.episodes, storage=args.storage)
    elif args.command == "simulate-native":
        report = research.simulate_native(args.output, response=args.response)
    elif args.command == "verify-features":
        report = research.verify_features(args.output, response=args.response, daily_response=args.daily_response,
                                          platforms=tuple(args.platforms))
    elif args.command == "benchmark-features":
        report = research.benchmark_features(args.capture, args.storage, args.output)
    else:
        report = research.simulate_lifecycle(args.output, response=args.response, suite=args.suite)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
