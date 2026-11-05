"""Compatibility consumer using the existing, unchanged Qlib CSV exporter."""

import argparse
import copy
import csv
from pathlib import Path
import subprocess
import sys

from ..qlib_export import export_responses, qlib_instrument
from .capture import load_capture, sha256, write_json
from .validation import FIELDS, bar_end, bars, number, probabilities


def run(capture_dir, output, qlib_source):
    # Qlib stays entirely optional; this import is executed only in its process.
    import numpy as np
    import qlib
    from qlib.config import REG_US
    from qlib.data import D

    manifest, responses = load_capture(capture_dir)
    equity_inputs, probability_inputs = list(bars(manifest, responses)), list(probabilities(manifest, responses))
    dump_script = Path(qlib_source).resolve() / "scripts" / "dump_bin.py"
    if not dump_script.is_file():
        raise ValueError("--qlib-source must contain the matching Qlib scripts/dump_bin.py")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source_views = []
    for symbol, response in equity_inputs:
        view = copy.deepcopy(response)
        for row in view["data"]["bars"]:
            row["timestamp"] = bar_end(row, manifest["bar_timestamp"]).isoformat()
        source_views.append((symbol, view))
    export_responses(source_views, root / "equity_csv", timeframe="1m", session="regular")
    probability_dir = root / "probability_csv"
    probability_dir.mkdir()
    mapping = {}
    for index, (outcome, rows) in enumerate(probability_inputs):
        code = f"PM_{index}"
        mapping[code] = outcome
        with (probability_dir / (code + ".csv")).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "probability"])
            for row, available in rows:
                writer.writerow([available.strftime("%Y-%m-%d %H:%M:%S"), str(number(row["price"]))])
    for kind, fields in (("equity", "open,high,low,close,volume,factor"), ("probability", "probability")):
        command = [sys.executable, str(dump_script), "dump_all", "--data_path", str(root / (kind + "_csv")),
                   "--qlib_dir", str(root / (kind + "_bin")), "--freq", "1min", "--max_workers", "1",
                   "--include_fields", fields]
        with (root / (kind + "_dump.log")).open("w", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    report = {"platform": "Qlib", "version": qlib.__version__, "capture_sha256": sha256(Path(capture_dir) / "manifest.json"),
              "storage": "Qlib float32; checked against independently cast source values",
              "bar_rows": 0, "probability_rows": 0, "max_abs_error": {}, "outcome_mapping": mapping}
    qlib.init(provider_uri=str(root / "equity_bin"), region=REG_US, expression_cache=None, dataset_cache=None)
    for symbol, response in equity_inputs:
        frame = D.features([qlib_instrument(symbol)], ["$" + f for f in FIELDS], freq="1min").dropna(how="all")
        rows = response["data"]["bars"]
        times = [bar_end(row, manifest["bar_timestamp"]).strftime("%Y-%m-%d %H:%M:%S") for row in rows]
        actual_times = [x.strftime("%Y-%m-%d %H:%M:%S") for x in frame.index.get_level_values("datetime")]
        if actual_times != times:
            raise ValueError("Qlib changed the equity calendar or row count")
        expected = np.array([[float(number(row[f])) for f in FIELDS] for row in rows], dtype=np.float64)
        np.testing.assert_array_equal(frame.to_numpy(), expected.astype(np.float32))
        report["bar_rows"] += len(frame)
        report["max_abs_error"][symbol] = dict(zip(FIELDS, np.max(np.abs(frame.to_numpy().astype(np.float64) - expected), axis=0).tolist()))
    qlib.init(provider_uri=str(root / "probability_bin"), region=REG_US, expression_cache=None, dataset_cache=None)
    for index, (outcome, rows) in enumerate(probability_inputs):
        frame = D.features([f"PM_{index}"], ["$probability"], freq="1min").dropna(how="all")
        times = [available.strftime("%Y-%m-%d %H:%M:%S") for _, available in rows]
        actual_times = [x.strftime("%Y-%m-%d %H:%M:%S") for x in frame.index.get_level_values("datetime")]
        if actual_times != times:
            raise ValueError("Qlib changed the probability calendar or row count")
        expected = np.array([float(number(row["price"])) for row, _ in rows], dtype=np.float64)
        np.testing.assert_array_equal(frame.iloc[:, 0].to_numpy(), expected.astype(np.float32))
        report["probability_rows"] += len(frame)
        report["max_abs_error"][outcome["token_id"]] = float(np.max(np.abs(frame.iloc[:, 0].to_numpy().astype(np.float64) - expected)))
    report["passed"] = True
    write_json(root / "verification.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--qlib-source", required=True)
    args = p.parse_args()
    result = run(args.capture, args.output, args.qlib_source)
    print({key: result[key] for key in ("platform", "version", "bar_rows", "probability_rows", "passed")})


if __name__ == "__main__":
    main()
