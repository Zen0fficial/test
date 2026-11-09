"""Command-line entry point for the joint data-only research framework."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .session import JointResearch, RuntimePaths


def main() -> None:
    """Dispatch explicit history, live, or model operations to one coordinator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("history")
    history.add_argument("--capture", type=Path, required=True)
    history.add_argument("--output", type=Path, required=True)
    history.add_argument("--allow-retrospective", action="store_true")
    history.add_argument("--selected-only", action="store_true")
    history.add_argument("--max-age-seconds", type=int, default=120)
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
    args = parser.parse_args()
    research = JointResearch(RuntimePaths.local(args.repo))
    if args.command == "history":
        report = research.history(args.capture, args.output, allow_retrospective=args.allow_retrospective,
                                  verify_all=not args.selected_only, max_age_seconds=args.max_age_seconds)
    elif args.command == "live":
        report = research.live(args.output, seconds=args.seconds, backend=args.backend,
                               disconnect_after=args.disconnect_after)
    else:
        report = research.model(args.output, response=args.response, start=args.start, end=args.end)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
