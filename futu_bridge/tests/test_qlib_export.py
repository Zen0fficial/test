import csv
import json
import tempfile
import unittest
from pathlib import Path

from futu_bridge import MarketDataResponse
from futu_bridge.qlib_export import (
    QlibExportError,
    export_from_client,
    export_responses,
    qlib_frequency,
    qlib_instrument,
    qlib_row,
)


def response(symbol, bars, adjustment="none"):
    return MarketDataResponse(
        data={
            "symbol": symbol,
            "timeframe": "1d",
            "adjustment": adjustment,
            "session": "provider_default",
            "bars": bars,
            "count": len(bars),
            "complete_requested_range": True,
            "coverage_continuity_verified": False,
        },
        source={"provider": "fixture", "kind": "historical"},
        warnings=["fixture warning"],
    )


def daily_bar(date, close, volume=100):
    return {
        "date": date,
        "timestamp": date + "T20:00:00Z",
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": volume,
    }


class QlibExportTests(unittest.TestCase):
    def test_frequency_and_instrument_mapping(self):
        self.assertEqual(qlib_frequency("1d"), "day")
        self.assertEqual(qlib_frequency("1m"), "1min")
        self.assertEqual(qlib_instrument("US.AAPL"), "AAPL")
        self.assertEqual(qlib_instrument("HK.00700"), "HK_00700")
        self.assertEqual(qlib_instrument("FX.EUR/USD"), "FX_EUR_USD")

    def test_row_uses_daily_date_and_identity_factor(self):
        row = qlib_row(daily_bar("2026-10-23", 100.25), qlib_freq="day")
        self.assertEqual(row, {
            "date": "2026-10-23",
            "open": "99.25",
            "close": "100.25",
            "high": "101.25",
            "low": "98.25",
            "volume": "100",
            "factor": "1",
        })

    def test_intraday_row_normalizes_timestamp_to_utc_calendar_value(self):
        row = qlib_row({
            "timestamp": "2026-10-23T13:31:00-04:00",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 3,
        }, qlib_freq="1min")
        self.assertEqual(row["date"], "2026-10-23 17:31:00")

    def test_export_writes_one_file_per_instrument_and_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            result = export_responses(
                [("US.AAPL", response("US.AAPL", [daily_bar("2026-10-22", 99), daily_bar("2026-10-23", 100)]))],
                directory,
                timeframe="1d",
            )
            self.assertEqual(result.rows, 2)
            self.assertEqual(result.csv_paths, (Path(directory) / "AAPL.csv",))
            with result.csv_paths[0].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["date"], "2026-10-22")
            self.assertEqual(rows[1]["close"], "100")
            self.assertNotIn("symbol", rows[0])
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["instrument_map"], {"US.AAPL": "AAPL"})
            self.assertEqual(metadata["provenance"][0]["csv_file"], "AAPL.csv")

    def test_export_rejects_duplicate_calendar_values(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(QlibExportError):
                export_responses(
                    [("US.AAPL", response("US.AAPL", [daily_bar("2026-10-23", 100), daily_bar("2026-10-23", 101)]))],
                    directory,
                    timeframe="1d",
                )

    def test_export_refuses_stale_instrument_csvs(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "MSFT.csv"
            stale.write_text("preserve me", encoding="utf-8")
            with self.assertRaises(QlibExportError):
                export_responses(
                    [("US.AAPL", response("US.AAPL", [daily_bar("2026-10-23", 100)]))],
                    directory,
                    timeframe="1d",
                    overwrite=True,
                )
            self.assertEqual(stale.read_text(encoding="utf-8"), "preserve me")

    def test_adjusted_export_derives_factor_from_unadjusted_futu_request(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def bars(self, symbol, **kwargs):
                self.calls.append((symbol, kwargs["adjustment"]))
                if kwargs["adjustment"] == "forward":
                    return response(symbol, [daily_bar("2026-10-23", 110)], "forward")
                return response(symbol, [daily_bar("2026-10-23", 100)], "none")

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            result = export_from_client(client, ["US.AAPL"], directory, adjustment="forward")
            self.assertEqual(client.calls, [("US.AAPL", "forward"), ("US.AAPL", "none")])
            with result.csv_paths[0].open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["factor"], "1.1")
            self.assertEqual(row["volume"], "90.9090909090909")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertIn("matching unadjusted close", metadata["factor_semantics"])

    def test_adjusted_export_requires_matching_unadjusted_date(self):
        class FakeClient:
            def bars(self, symbol, **kwargs):
                if kwargs["adjustment"] == "forward":
                    return response(symbol, [daily_bar("2026-10-23", 110)], "forward")
                return response(symbol, [daily_bar("2026-10-22", 100)], "none")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(QlibExportError):
                export_from_client(FakeClient(), ["US.AAPL"], directory, adjustment="forward")


if __name__ == "__main__":
    unittest.main()
