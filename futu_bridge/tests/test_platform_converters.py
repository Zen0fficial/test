"""Boundary regressions; fixtures here are synthetic, unlike the live audit."""

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from futu_bridge.converters.capture import load_capture, write_json
from futu_bridge.converters.lean import export
from futu_bridge.converters.validation import bar_end, bars, compare_rows, expected_rows, probabilities

TOKEN = "16040015440196279900485035793550429453516625694844857319147506590755961451627"


def fixture():
    manifest = {"schema_version": 1, "timeframe": "1m", "bar_timestamp": "end", "symbols": ["US.AAPL"],
                "start": "2024-11-04", "end": "2024-11-04", "outcomes": [
                    {"name": "probability_0", "token_id": TOKEN, "outcome": "Yes"}]}
    row = {"timestamp": "2024-11-04T14:31:00Z", "date": "2024-11-04", "open": "10.123456",
           "high": "11", "low": "9", "close": "10.8", "volume": "1234.0"}
    source = {"provider": "futu-desktop-session"}
    data = {"symbol": "US.AAPL", "timeframe": "1m", "adjustment": "none", "session": "regular",
            "complete_requested_range": True, "truncated": False, "bars": [row]}
    point = {"timestamp": 1730678400, "price": "0.123456789", "resolution_seconds": 60}
    responses = {"bars_0": {"data": data, "source": source},
                 "probability_0": {"data": {"data": [point], "pagination": {"has_more": False}},
                                   "source": {"provider": "polymarket", "request_params": {"token_id": TOKEN}}}}
    return manifest, responses


def save_capture(root, manifest, responses):
    root.mkdir()
    manifest = copy.deepcopy(manifest)
    manifest["records"] = []
    for name, response in responses.items():
        path = root / (name + ".json")
        write_json(path, response)
        manifest["records"].append({"name": name, "file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json(root / "manifest.json", manifest)


class ConverterTests(unittest.TestCase):
    def test_imports_do_not_import_engines_or_provider_native_code(self):
        code = "import sys; import futu_bridge.converters.nautilus, futu_bridge.converters.lean, futu_bridge.converters.qlib; assert not any(x in sys.modules for x in ('nautilus_trader', 'qlib', 'frida', 'futu_bridge.native'))"
        subprocess.run([sys.executable, "-c", code], check=True, timeout=20)

    def test_raw_capture_and_outcome_ids_remain_unchanged(self):
        manifest, responses = fixture()
        before = copy.deepcopy(responses)
        result = expected_rows(manifest, responses)
        self.assertEqual(result[1][1], TOKEN)
        self.assertEqual(responses, before)

    def test_history_delivered_after_bucket_closes(self):
        m, r = fixture()
        _, rows = next(probabilities(m, r))
        self.assertEqual(rows[0][1].isoformat(), "2024-11-04T00:01:00+00:00")

    def test_end_boundary_marker_is_not_duplicated(self):
        m, r = fixture()
        r["probability_0"]["data"]["data"].append({"timestamp": 1730764800, "price": "0.2", "resolution_seconds": 0})
        self.assertEqual(len(next(probabilities(m, r))[1]), 1)

    def test_unfinished_history_pagination_fails(self):
        m, r = fixture()
        r["probability_0"]["data"]["pagination"]["has_more"] = True
        with self.assertRaisesRegex(ValueError, "pagination"):
            list(probabilities(m, r))

    def test_wrong_token_identity_fails(self):
        m, r = fixture()
        r["probability_0"]["source"]["request_params"]["token_id"] = "123"
        with self.assertRaisesRegex(ValueError, "identity"):
            list(probabilities(m, r))

    def test_probability_range_and_duplicates_fail(self):
        for change in ("range", "duplicate"):
            with self.subTest(change=change):
                m, r = fixture()
                rows = r["probability_0"]["data"]["data"]
                if change == "range": rows[0]["price"] = "1.01"
                else: rows.append(copy.deepcopy(rows[0]))
                with self.assertRaises(ValueError): list(probabilities(m, r))

    def test_duplicate_bars_fail(self):
        m, r = fixture()
        r["bars_0"]["data"]["bars"] *= 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            list(bars(m, r))

    def test_adjusted_partial_and_sample_data_fail(self):
        for change in ("adjustment", "complete_requested_range", "provider"):
            with self.subTest(change=change):
                m, r = fixture()
                if change == "provider": r["bars_0"]["source"]["provider"] = "sample"
                else: r["bars_0"]["data"][change] = "forward" if change == "adjustment" else False
                with self.assertRaises(ValueError): list(bars(m, r))

    def test_nan_negative_volume_and_invalid_ohlc_fail(self):
        for field, value in (("close", "NaN"), ("volume", "-1"), ("high", "1")):
            with self.subTest(field=field):
                m, r = fixture()
                r["bars_0"]["data"]["bars"][0][field] = value
                with self.assertRaises(ValueError): list(bars(m, r))

    def test_timestamp_convention_is_explicit_and_timezone_required(self):
        row = {"timestamp": "2024-11-04T09:30:00-05:00"}
        self.assertEqual(bar_end(row, "start").isoformat(), "2024-11-04T14:31:00+00:00")
        with self.assertRaises(ValueError): bar_end(row, "guess")
        with self.assertRaises(ValueError): bar_end({"timestamp": "2024-11-04T09:30:00"}, "end")

    def test_decimal_comparison_detects_precision_loss_and_missing_rows(self):
        m, r = fixture()
        expected = expected_rows(m, r)
        self.assertTrue(compare_rows(expected, expected)["exact_decimal_match"])
        changed = copy.deepcopy(expected)
        changed[0][3] = "10.1235"
        with self.assertRaises(ValueError): compare_rows(changed, expected)
        with self.assertRaises(ValueError): compare_rows(expected[:-1], expected)

    def test_capture_hash_and_path_escape_fail(self):
        for change in ("hash", "path"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "capture"
                m, r = fixture()
                save_capture(root, m, r)
                if change == "hash": (root / "bars_0.json").write_text("{}", encoding="utf-8")
                else:
                    doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                    doc["records"][0]["file"] = "../outside.json"
                    write_json(root / "manifest.json", doc)
                with self.assertRaises(ValueError): load_capture(root)

    def test_lean_keeps_sub_tick_precision_and_converts_dst(self):
        for day, utc_end in (("2024-11-01", "13:31"), ("2024-11-04", "14:31")):
            with self.subTest(day=day), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                m, r = fixture()
                m["start"] = m["end"] = day
                r["bars_0"]["data"]["bars"][0]["timestamp"] = day + "T" + utc_end + ":00Z"
                r["bars_0"]["data"]["bars"][0]["date"] = day
                r["probability_0"]["data"]["data"][0]["timestamp"] = int(dt.datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())
                save_capture(root / "capture", m, r)
                for name in ("market-hours", "symbol-properties"):
                    (root / "source" / "Data" / name).mkdir(parents=True)
                export(root / "capture", root / "output", root / "source")
                archive = next((root / "output" / "data").rglob("*.zip"))
                with zipfile.ZipFile(archive) as z:
                    values = z.read(z.namelist()[0]).decode().strip().split(",")
                self.assertEqual(values[:2], ["34200000", "101234.560000"])


if __name__ == "__main__":
    unittest.main()
