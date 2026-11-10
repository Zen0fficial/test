"""Independent checks of native Qlib execution journals and artificial cash.

Does not call the training reward, policy, simulator, or production fee helper.
Fees, chronology, whole lots and cash are recalculated directly from captured
prices and the documented current-cost scenario.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

import pandas as pd

from .qlib_rl_data import load_data
from .session import write_report


def verify_provenance(root: Path, reported: dict[str, object], expected: dict[str, object]) -> dict[str, object]:
    """Check legacy provenance exactly and independently validate any canonical snapshot extension."""
    if not isinstance(reported, dict):
        raise ValueError("Missing data provenance")
    if reported.get("analytical_source") != "parquet":
        if reported != expected:
            raise ValueError("Original capture provenance changed")
        return {"analytical_source": "legacy_json", "passed": True}
    from .storage import SnapshotStore
    from .qlib_parquet_lineage import execution_reference

    required = ("snapshot_root", "snapshot_id", "snapshot_sha256")
    if any(not isinstance(reported.get(name), str) or not reported[name] for name in required):
        raise ValueError("Pinned analytical provenance requires its store, snapshot identity and content hash")
    location = Path(reported["snapshot_root"])
    if not (location / "control.sqlite").is_file() or not (location / "parquet").is_dir():
        raise ValueError("Pinned analytical store is missing")
    store = SnapshotStore(location)
    snapshot = store.snapshot(reported["snapshot_id"])
    independent = execution_reference(store, snapshot.identity)
    digest = hashlib.sha256((root / "provider_response.json").read_bytes()).hexdigest()
    if reported != independent.provenance or digest != independent.provenance["response_sha256"]:
        raise ValueError("Pinned analytical provenance or original archive anchor differs")
    return {"passed": True, "analytical_source": "parquet", "snapshot_id": snapshot.identity,
            "snapshot_sha256": snapshot.sha256, "source_rows_verified": independent.provenance["raw_bars"],
            "original_source_captures": len(independent.provenance.get("capture_lineage", {}).get(
                "capture_sources", [digest]))}


def check_journal(result: dict[str, object], blocks: dict[str, pd.DataFrame], days: list[str]) -> dict[str, object]:
    """Rebuild each buy/sell cash movement from the source and audit native fills."""
    cash, fees, holding = Decimal(100000), Decimal(0), 0
    spread = Decimal(result["slippage_bps"]) / 10000
    actual_days, previous = [], ""
    native = {s["date"]: s["native"] for s in result["sessions"]}
    native_fills = {(day, fill["time"]): fill for day, session in native.items() for fill in session["fills"]}
    seen = set()
    for fill in result["ledger"]:
        time = fill["time"]
        day = time[:10]
        quantity = fill["shares"]
        if day not in days or time <= previous or type(quantity) is not int or quantity <= 0 or quantity % 100:
            raise ValueError("Invalid ledger chronology or lot size")
        previous = time
        if fill["side"] == "buy":
            reference = Decimal(str(blocks[day].loc[pd.Timestamp(time), "open"]))
            source = native_fills[(day, time)]
            if quantity != source["shares"] or abs(float(reference) - source["reference"]) > 1e-8:
                raise ValueError("Journal differs from native simulated fill")
            seen.add((day, time))
            price, sign = reference * (1 + spread), -1
            holding += quantity
        elif fill["side"] == "sell" and time == day + " 16:00:00" and quantity == holding == 200:
            reference = Decimal(str(blocks[day].close.iloc[-1]))
            price, sign = reference * (1 - spread), 1
            holding -= quantity
            actual_days.append(day)
        else:
            raise ValueError("Invalid closing sell or inventory")
        amount = price * quantity
        components = {"commission": max(Decimal(18), amount * Decimal(".0005")),
                      "exchange": amount * Decimal(".0000565"), "clearing": amount * Decimal(".000042"),
                      "sfc": amount * Decimal(".000027"), "frc": amount * Decimal(".0000015"),
                      "stamp": (amount * Decimal(".001")).quantize(Decimal(1), rounding=ROUND_CEILING)}
        fee = sum(components.values()).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        cash += sign * amount - fee
        fees += fee
        if (cash < 0 or holding not in (0, 100, 200) or Decimal(fill["price"]) != price
                or Decimal(fill["reference"]) != reference or Decimal(fill["notional"]) != amount
                or Decimal(fill["fees"]["total"]) != fee or Decimal(fill["cash_after"]) != cash
                or fill["holding_after"] != holding
                or any(Decimal(fill["fees"][key]) != value for key, value in components.items())):
            raise ValueError("Independent source/fee/cash verification failed")
    if (actual_days != days or seen != set(native_fills) or holding != 0
            or abs(float(cash) - result["ending_equity_hkd"]) > 1e-7
            or abs(float(fees) - result["fees_hkd"]) > 1e-7
            or result["round_trips"] != len(days) or result["fills"] != len(result["ledger"])
            or result["real_orders_enabled"] is not False):
        raise ValueError("Incomplete journal or incorrect summary")
    return {"passed": True, "fills_checked": len(result["ledger"]), "ending_equity_hkd": float(cash)}


def verify(root: Path) -> dict[str, object]:
    """Verify data provenance, frozen split/selection, all test journals and counts."""
    report = json.loads((root / "report.json").read_text())
    reported = report.get("data")
    if not isinstance(reported, dict):
        raise ValueError("Missing data provenance")
    if reported.get("analytical_source") == "parquet":
        from .storage import SnapshotStore
        from .qlib_parquet_lineage import execution_reference

        provenance = verify_provenance(root, reported, {})
        data = execution_reference(SnapshotStore(Path(reported["snapshot_root"])), reported["snapshot_id"])
    else:
        data = load_data(root / "provider_response.json")
        provenance = verify_provenance(root, reported, data.provenance)
    selection = json.loads((root / "selection.json").read_text())
    expected = max(sorted(selection["validation_scores"]), key=selection["validation_scores"].get)
    if (report["partitions"] != data.partitions or selection["selected"] != expected
            or report["selected"] != expected):
        raise ValueError("Provenance, split, or validation-only selection mismatch")
    checks = {}
    for path in [*sorted(root.glob("*_test.json")), *sorted(root.glob("selected_slippage*.json"))]:
        checks[path.stem] = check_journal(json.loads(path.read_text()), data.blocks, data.partitions["test"])
    if len(checks) != 11:
        raise ValueError("Expected eight candidate and three selected cost-scenario journals")
    result = {"passed": True, "checks": checks, "native_execution_backend": "Qlib simple simulator",
              "response_sha256": hashlib.sha256((root / "provider_response.json").read_bytes()).hexdigest(),
              "dataset_verification": provenance,
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "real_orders_enabled": False, "scope": "Independent prices, fees, lots and artificial cash"}
    write_report(root / "verification.json", result)
    return result


def main() -> None:
    """Audit a completed native Qlib execution experiment without retraining."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    print(json.dumps(verify(parser.parse_args().run), indent=2))


if __name__ == "__main__":
    main()
