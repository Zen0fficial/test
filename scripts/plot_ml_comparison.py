"""Render a saved real-data comparison without training or changing its selection."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


def render(root: Path) -> Path:
    """Plot primary-scenario equity and frictionless/costed ending values."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    if not report["completed"] or report["real_orders_enabled"]:
        raise ValueError("Require a completed artificial comparison")
    strategies = [("baseline", "Original baseline", "#4068a0"),
                  ("selected", "Selected model / cash", "#168575"),
                  ("always_long_intraday", "Always long intraday", "#b47742")]
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 5.8), gridspec_kw={"width_ratios": [1.7, 1]})
    fig.patch.set_facecolor("#fafbfc")
    fig.suptitle("HK.00700 | Retrospective artificial portfolio", x=.07, ha="left", fontsize=19, weight="bold")
    fig.text(.07, .89, "HKD100,000 · long-only · next open to same-session close · 333 sessions", fontsize=11)
    for index, (name, label, color) in enumerate(strategies):
        key = f"{name}_threshold0.5_slippage5"
        result = json.loads((root / f"simulation_{key}.json").read_text(encoding="utf-8"))
        dates = [date.fromisoformat(report["plan"]["splits"]["test"]["start"])]
        dates += [date.fromisoformat(point["date"]) for point in result["equity"]]
        values = [result["capital_hkd"], *[point["equity_hkd"] for point in result["equity"]]]
        axes[0].plot(dates, values, label=label, color=color, linewidth=2)
        gross = report["simulations"][f"{name}_frictionless"]["ending_equity_hkd"]
        for offset, amount, alpha in ((-.18, gross, .35), (.18, values[-1], 1)):
            axes[1].bar(index + offset, amount, width=.32, color=color, alpha=alpha)
            axes[1].text(index + offset, amount + 1700, f"{amount / 1000:.1f}k", ha="center", fontsize=9)
    axes[0].set_title("Equity after fees and slippage", loc="left", fontsize=12)
    axes[0].legend(loc="lower left", frameon=False, fontsize=9)
    axes[0].xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    axes[1].set_title("Ending equity: frictionless / costed", loc="left", fontsize=12)
    axes[1].set_xticks(range(3), ["Baseline", "Selected", "Always long\nintraday"])
    axes[1].set_ylim(0, 115000)
    for axis in axes:
        axis.set_axisbelow(True)
        axis.grid(axis="y", color="#dce2e8", linewidth=.7)
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:.0f}k"))
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_ylabel("HKD")
    fig.text(.07, .08, "Selected model made zero trades. Its outcome equals holding cash; this is not evidence of alpha.",
             fontsize=10, weight="bold")
    fig.text(.07, .04, "Costed: IBKR Tier I + HK market charges + 5 bps slippage per side. Previously examined test period.",
             fontsize=9, color="#4b5563")
    fig.subplots_adjust(left=.07, right=.98, bottom=.22, top=.8, wspace=.25)
    destination = root / "comparison.png"
    if destination.exists():
        raise ValueError("Refusing to overwrite an existing chart")
    fig.savefig(destination, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    return destination


def main() -> None:
    """Render a comparison's saved report and ledgers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", type=Path)
    args = parser.parse_args()
    print(render(args.comparison.resolve()))


if __name__ == "__main__":
    main()
