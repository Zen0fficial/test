"""Export read-only Futu bars into Qlib's CSV source format.

The exporter deliberately stops at Qlib's documented CSV boundary.  Qlib's
own ``scripts/dump_bin.py`` remains responsible for creating calendars,
instrument ranges, and binary feature files.  This module does not place
orders, infer futures rolls, or manufacture corporate-action factors.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .client import FutuClient, MarketDataResponse


TIMEFRAME_TO_QLIB_FREQ = {
    "1m": "1min",
    "3m": "3min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "60min",
    "1d": "day",
    "1Day": "day",
    "1w": "week",
    "1mo": "month",
}

CSV_FIELDS = ("date", "open", "close", "high", "low", "volume", "factor")
_UNSET = object()


class QlibExportError(ValueError):
    """Raised when a response cannot be represented safely in Qlib CSV."""


@dataclass(frozen=True)
class QlibExportResult:
    """Files and counts produced by one export operation."""

    csv_paths: tuple[Path, ...]
    metadata_path: Path
    symbols: tuple[str, ...]
    rows: int
    timeframe: str
    qlib_freq: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "csv_paths": [str(path) for path in self.csv_paths],
            "metadata_path": str(self.metadata_path),
            "symbols": list(self.symbols),
            "rows": self.rows,
            "timeframe": self.timeframe,
            "qlib_freq": self.qlib_freq,
        }


def qlib_frequency(timeframe: str) -> str:
    """Map a bridge timeframe to Qlib's calendar frequency name."""
    try:
        return TIMEFRAME_TO_QLIB_FREQ[timeframe]
    except KeyError as exc:
        raise QlibExportError(f"Unsupported Qlib export timeframe: {timeframe!r}") from exc


def qlib_instrument(symbol: str) -> str:
    """Map Futu symbols to stable Qlib-safe instrument codes.

    US symbols keep the familiar ticker (``US.AAPL`` -> ``AAPL``). Other
    markets retain a market prefix to prevent collisions (``HK.00700`` ->
    ``HK_00700``). Dots and other filename-hostile characters become ``_``.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise QlibExportError("A nonempty Futu symbol is required.")
    value = symbol.strip()
    prefix, separator, code = value.partition(".")
    if separator:
        value = code if prefix.upper() == "US" else prefix.upper() + "_" + code
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
    if not value or value in (".", ".."):
        raise QlibExportError(f"Futu symbol {symbol!r} cannot be mapped to a Qlib instrument code.")
    if value in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                 *("LPT" + str(i) for i in range(1, 10))}:
        value += "_"
    return value


def _number(value: Any, field: str, *, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise QlibExportError(f"Bar field {field!r} is missing.")
        return None
    if isinstance(value, bool):
        raise QlibExportError(f"Bar field {field!r} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QlibExportError(f"Bar field {field!r} is not numeric.") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise QlibExportError(f"Bar field {field!r} is not finite.")
    return format(number, ".15g")


def _date_value(row: Mapping[str, Any], qlib_freq: str) -> str:
    """Return a timezone-free Qlib calendar value in UTC for intraday data."""
    if qlib_freq == "day":
        value = row.get("date")
        if not isinstance(value, str) or len(value) < 10:
            raise QlibExportError("Daily bars require a YYYY-MM-DD date field.")
        try:
            return dt.date.fromisoformat(value[:10]).isoformat()
        except ValueError as exc:
            raise QlibExportError(f"Invalid daily bar date: {value!r}") from exc

    value = row.get("timestamp")
    if not isinstance(value, str) or not value:
        raise QlibExportError("Intraday bars require a UTC timestamp field.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QlibExportError(f"Invalid bar timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QlibExportError("Intraday bar timestamps must include a timezone.")
    parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def qlib_row(row: Mapping[str, Any], *, qlib_freq: str,
             factor: float = 1.0, volume: Any = _UNSET) -> dict[str, str]:
    """Convert one bridge bar into the documented Qlib CSV fields.

    The caller supplies ``factor``. For adjusted exports the public helper
    derives it from paired adjusted/unadjusted Futu closes and converts volume
    to Qlib's adjusted-volume convention using raw volume divided by factor.
    """
    if (not isinstance(factor, (int, float)) or isinstance(factor, bool) or
            not math.isfinite(float(factor)) or factor <= 0):
        raise QlibExportError("factor must be a positive number.")
    values = {
        "date": _date_value(row, qlib_freq),
        "open": _number(row.get("open"), "open", required=True),
        "close": _number(row.get("close"), "close", required=True),
        "high": _number(row.get("high"), "high", required=True),
        "low": _number(row.get("low"), "low", required=True),
        "volume": _number(row.get("volume") if volume is _UNSET else volume, "volume"),
        "factor": format(float(factor), ".15g"),
    }
    if values["volume"] is None:
        values["volume"] = ""
    return values  # type: ignore[return-value]


def _response_parts(response: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], list[str]]:
    if isinstance(response, MarketDataResponse):
        return response.data, response.source, list(response.warnings)
    if isinstance(response, Mapping):
        data = response.get("data")
        if isinstance(data, Mapping):
            return data, response.get("source", {}), list(response.get("warnings", []))
        return response, {}, []
    raise QlibExportError("Expected a MarketDataResponse or response mapping.")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def export_responses(responses: Iterable[tuple[str, Any]], output_dir: Path | str,
                     *, timeframe: str, adjustment: str = "none",
                     session: str = "provider_default", factors: Optional[Mapping[str, Mapping[str, float]]] = None,
                     adjusted_volumes: Optional[Mapping[str, Mapping[str, Optional[float]]]] = None,
                     metadata_filename: str = "futu_qlib.metadata.json",
                     overwrite: bool = False) -> QlibExportResult:
    """Write Qlib CSV and provenance metadata from already-fetched responses."""
    qlib_freq = qlib_frequency(timeframe)
    if adjustment not in ("none", "forward", "backward"):
        raise QlibExportError("adjustment must be none, forward, or backward.")
    if (not isinstance(metadata_filename, str) or not metadata_filename or
            Path(metadata_filename).name != metadata_filename or not metadata_filename.endswith(".json")):
        raise QlibExportError("metadata_filename must be a simple .json filename.")
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    metadata_path = destination / metadata_filename

    rows_by_instrument: dict[str, list[dict[str, str]]] = {}
    symbol_files: dict[str, str] = {}
    provenance: list[dict[str, Any]] = []
    symbol_set: list[str] = []
    for requested_symbol, response in responses:
        data, source, warnings = _response_parts(response)
        returned_symbol = data.get("symbol", requested_symbol)
        if not isinstance(returned_symbol, str) or not returned_symbol:
            raise QlibExportError(f"Response for {requested_symbol!r} lacks a symbol.")
        bars = data.get("bars")
        if not isinstance(bars, list):
            raise QlibExportError(f"Response for {returned_symbol!r} lacks a bars list.")
        if not bars:
            raise QlibExportError(f"Futu returned no bars for {returned_symbol!r}; no Qlib instrument was written.")
        response_adjustment = data.get("adjustment")
        if response_adjustment is not None and response_adjustment != adjustment:
            raise QlibExportError(f"Futu response adjustment {response_adjustment!r} does not match requested {adjustment!r}.")
        response_session = data.get("session")
        if response_session is not None and response_session != session:
            raise QlibExportError(f"Futu response session {response_session!r} does not match requested {session!r}.")
        if returned_symbol not in symbol_set:
            symbol_set.append(returned_symbol)
        instrument = qlib_instrument(returned_symbol)
        existing_symbol = symbol_files.get(instrument)
        if existing_symbol is not None and existing_symbol != returned_symbol:
            raise QlibExportError(f"Futu symbols {existing_symbol!r} and {returned_symbol!r} map to the same Qlib code {instrument!r}.")
        if instrument in rows_by_instrument:
            raise QlibExportError(f"Multiple responses were supplied for {returned_symbol!r}; merge and deduplicate them before export.")
        symbol_files[instrument] = returned_symbol
        factor_rows = (factors or {}).get(returned_symbol, {})
        volume_rows = (adjusted_volumes or {}).get(returned_symbol, {})
        converted = []
        for bar in bars:
            date_value = _date_value(bar, qlib_freq)
            if adjustment != "none" and date_value not in factor_rows:
                raise QlibExportError(f"No adjustment factor found for {returned_symbol} on {date_value}.")
            if adjustment != "none" and date_value not in volume_rows:
                raise QlibExportError(f"No adjusted-volume value found for {returned_symbol} on {date_value}.")
            factor = factor_rows.get(date_value, 1.0)
            volume = volume_rows[date_value] if adjustment != "none" else _UNSET
            converted.append(qlib_row(bar, qlib_freq=qlib_freq, factor=factor, volume=volume))
        converted.sort(key=lambda item: item["date"])
        dates = [row["date"] for row in converted]
        if len(dates) != len(set(dates)):
            raise QlibExportError(f"Duplicate Qlib calendar values found for {returned_symbol}; resolve them before export.")
        rows_by_instrument.setdefault(instrument, []).extend(converted)
        provenance.append({
            "requested_symbol": requested_symbol,
            "returned_symbol": returned_symbol,
            "qlib_instrument": instrument,
            "csv_file": instrument + ".csv",
            "count": len(converted),
            "source": dict(source),
            "warnings": warnings,
            "response_metadata": {
                key: data.get(key) for key in (
                    "timeframe", "adjustment", "session", "complete_requested_range",
                    "completion_reason", "coverage_continuity_verified",
                    "requested_range_extends_into_future", "truncated",
                ) if key in data
            },
        })

    rows_count = sum(len(rows) for rows in rows_by_instrument.values())
    if not rows_count:
        raise QlibExportError("No bars were returned; refusing to create an empty Qlib dataset.")
    csv_paths = tuple(destination / (instrument + ".csv") for instrument in sorted(rows_by_instrument))
    targets = (*csv_paths, metadata_path)
    expected_csvs = {path.resolve() for path in csv_paths}
    stale_csvs = sorted(str(path) for path in destination.glob("*.csv") if path.resolve() not in expected_csvs)
    if stale_csvs:
        raise QlibExportError("Output directory contains other instrument CSVs that Qlib would also ingest; use a clean directory: " + ", ".join(stale_csvs))
    if not overwrite:
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            raise FileExistsError("Export would overwrite existing files; pass overwrite=True to replace: " + ", ".join(existing))

    for instrument in sorted(rows_by_instrument):
        csv_path = destination / (instrument + ".csv")
        temporary = csv_path.with_name(csv_path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows_by_instrument[instrument])
        os.replace(temporary, csv_path)

    metadata = {
        "format": "qlib_csv_source",
        "schema": list(CSV_FIELDS),
        "symbols": symbol_set,
        "instrument_map": {symbol: qlib_instrument(symbol) for symbol in symbol_set},
        "rows": rows_count,
        "timeframe": timeframe,
        "qlib_freq": qlib_freq,
        "adjustment_requested": adjustment,
        "session_requested": session,
        "factor_semantics": (
            "Futu adjusted close divided by matching unadjusted close for each date"
            if adjustment != "none" else "identity factor 1.0 for unadjusted prices"
        ),
        "volume_semantics": (
            "matching raw Futu volume divided by the adjustment factor, following Qlib's adjusted-volume convention"
            if adjustment != "none" else "Futu unadjusted volume"
        ),
        "source_provider": "futu-desktop-bridge",
        "read_only": True,
        "provenance": provenance,
        "warnings": [
            "Qlib calendars and binary features must be generated with Qlib's own dump_bin.py.",
            "Futu provider metadata does not certify continuity of every trading date.",
            "Futu continuous-futures roll methodology and historical universe membership are not inferred here.",
        ],
    }
    _write_json(metadata_path, metadata)
    return QlibExportResult(csv_paths, metadata_path, tuple(symbol_set), rows_count, timeframe, qlib_freq)


def export_from_client(client: Any, symbols: Sequence[str], output_dir: Path | str, *,
                       timeframe: str = "1d", start: Optional[str] = None,
                       end: Optional[str] = None, adjustment: str = "forward",
                       session: str = "provider_default", metadata_filename: str = "futu_qlib.metadata.json",
                       overwrite: bool = False) -> QlibExportResult:
    """Fetch bars through a client and export them without any trading calls."""
    if isinstance(symbols, str) or not symbols:
        raise QlibExportError("symbols must be a nonempty sequence.")
    responses = []
    factors: dict[str, dict[str, float]] = {}
    adjusted_volumes: dict[str, dict[str, Optional[float]]] = {}
    for symbol in symbols:
        response = client.bars(symbol, timeframe=timeframe, start=start, end=end,
                               adjustment=adjustment, session=session)
        responses.append((symbol, response))
        if adjustment != "none":
            raw_response = client.bars(symbol, timeframe=timeframe, start=start, end=end,
                                       adjustment="none", session=session)
            adjusted_data, _, _ = _response_parts(response)
            raw_data, _, _ = _response_parts(raw_response)
            adjusted_symbol = adjusted_data.get("symbol", symbol)
            raw_symbol = raw_data.get("symbol", symbol)
            if adjusted_symbol != raw_symbol:
                raise QlibExportError(f"Adjusted and unadjusted requests resolved to different symbols for {symbol!r}.")
            if raw_data.get("adjustment", "none") != "none" or raw_data.get("session", session) != session:
                raise QlibExportError(f"Unadjusted factor request for {symbol!r} returned unexpected request metadata.")
            qlib_freq = qlib_frequency(timeframe)
            raw_by_date = {_date_value(row, qlib_freq): row for row in raw_data.get("bars", [])}
            factor_by_date = {}
            for row in adjusted_data.get("bars", []):
                date_value = _date_value(row, qlib_freq)
                raw_row = raw_by_date.get(date_value)
                if raw_row is None:
                    raise QlibExportError(f"Raw Futu bars do not cover adjusted bar {adjusted_symbol} on {date_value}.")
                adjusted_close = row.get("close")
                raw_close = raw_row.get("close")
                if (not isinstance(adjusted_close, (int, float)) or isinstance(adjusted_close, bool) or
                        not isinstance(raw_close, (int, float)) or isinstance(raw_close, bool) or
                        not math.isfinite(float(adjusted_close)) or not math.isfinite(float(raw_close)) or raw_close == 0):
                    raise QlibExportError(f"Cannot derive a positive adjustment factor for {adjusted_symbol} on {date_value}.")
                factor_value = float(adjusted_close) / float(raw_close)
                if not math.isfinite(factor_value) or factor_value <= 0:
                    raise QlibExportError(f"Cannot derive a positive adjustment factor for {adjusted_symbol} on {date_value}.")
                factor_by_date[date_value] = factor_value
                raw_volume = raw_row.get("volume")
                if raw_volume is None:
                    adjusted_volumes.setdefault(adjusted_symbol, {})[date_value] = None
                elif (not isinstance(raw_volume, (int, float)) or isinstance(raw_volume, bool) or
                      not math.isfinite(float(raw_volume)) or raw_volume < 0):
                    raise QlibExportError(f"Cannot derive adjusted volume for {adjusted_symbol} on {date_value}.")
                else:
                    adjusted_volumes.setdefault(adjusted_symbol, {})[date_value] = float(raw_volume) / factor_value
            factors[adjusted_symbol] = factor_by_date
    return export_responses(responses, output_dir, timeframe=timeframe,
                            adjustment=adjustment, session=session,
                            factors=factors, adjusted_volumes=adjusted_volumes,
                            metadata_filename=metadata_filename,
                            overwrite=overwrite)


def export_live(symbols: Sequence[str], output_dir: Path | str, **kwargs: Any) -> QlibExportResult:
    """Open the signed-in desktop read-only and export requested bars."""
    with FutuClient.desktop() as client:
        return export_from_client(client, symbols, output_dir, **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export read-only Futu bars to Qlib CSV source format.")
    parser.add_argument("--symbol", action="append", required=True, dest="symbols",
                        help="Provider symbol such as US.AAPL; repeat for multiple instruments.")
    parser.add_argument("--output-dir", default="output/qlib_source", help="Destination directory.")
    parser.add_argument("--metadata-filename", default="futu_qlib.metadata.json",
                        help="Provenance JSON filename inside output-dir.")
    parser.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_TO_QLIB_FREQ), help="Futu candle timeframe.")
    parser.add_argument("--start", help="Inclusive YYYY-MM-DD or timezone-aware timestamp.")
    parser.add_argument("--end", help="Inclusive YYYY-MM-DD or timezone-aware timestamp.")
    parser.add_argument("--adjustment", default="forward", choices=("none", "forward", "backward"))
    parser.add_argument("--session", default="provider_default",
                        choices=("provider_default", "regular", "premarket", "postmarket", "extended", "overnight", "all"))
    parser.add_argument("--overwrite", action="store_true", help="Replace matching export files if they already exist.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    result = export_live(args.symbols, args.output_dir, timeframe=args.timeframe,
                         start=args.start, end=args.end, adjustment=args.adjustment,
                         session=args.session, metadata_filename=args.metadata_filename,
                         overwrite=args.overwrite)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
