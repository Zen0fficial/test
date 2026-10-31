"""Version-gated market-data access through the running Futu desktop session.

This adapter calls a small allowlist of market-data getters and chart requests.
Its history observer only reads replies to its own requests. It does not change
quote permissions, authenticate, place orders, or read credentials.
"""

import ctypes
from contextlib import closing
from ctypes import wintypes
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Dict, List, Optional

from .provider import CAPABILITIES, EventStream, MarketDataProvider, ProviderError, ProviderEvent, ProviderResult
from .wire import parse_bars, parse_quote_snapshots
from .feed_wire import parse_book, parse_trades
from .option_wire import parse_option_chain, parse_option_expiries
from .push_native import NativePushMixin
from .calendar_native import NativeCalendarMixin, FTQUOTE_SHA256
from .short_native import NativeShortVolumeMixin


SUPPORTED_VERSION = "16.33.17808"
QOTBASE_SHA256 = "8b2ed71ee723d7e8ffabba8df4e8e8aaa1f09ce5ec2a3b16586f4109a2a0246e"
COREBIZ_SHA256 = "1d48dc2712b4de4034c68316f7747775660720b0be90914aaa5b06c499450beb"
FTNEWS_SHA256 = "9cdc686720c4e98bb42fcf08349f231be6179014b8cddd54c2f4df88cb964775"
PRICE_SCALE = 1_000_000_000
FIELDS = ("stock_id", "code", "futu_symbol", "eng_name", "cs_market", "market_code",
          "instrument_type_v2", "sub_instrument_type_v2", "exchange", "listed_exchange",
          "currency_code", "delisting_flag", "no_subscription", "price_accuracy")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _frida():
    try:
        return importlib.import_module("frida")
    except ImportError:
        vendor = Path(__file__).resolve().parent.parent / "coverage_audit" / "vendor"
        if (vendor / "frida" / "__init__.py").is_file():
            sys.path.insert(0, str(vendor))
            return importlib.import_module("frida")
        raise ProviderError("dependency_missing", "Install the package's native extra: pip install -e '.[native]'", 503)


def _process_path(pid: int) -> Path:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ProviderError("desktop_process_unavailable", "Cannot inspect the Futu process path.", 503)
    try:
        size = wintypes.DWORD(32768)
        value = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
            raise ProviderError("desktop_process_unavailable", "Cannot read the Futu process path.", 503)
        return Path(value.value)
    finally:
        kernel.CloseHandle(handle)


def canonical_symbol(record: Dict) -> str:
    code = record.get("code") or record.get("futu_symbol") or str(record["stock_id"])
    market = record.get("cs_market")
    exchange = record.get("exchange")
    if market in (1, 6):
        prefix = "HK"
    elif market == 2 or (market == 38 and exchange in ("CME", "CBOT", "COMEX", "NYMEX")):
        prefix = "US"
    elif market == 11 or exchange == "FX":
        prefix = "FX"
    elif market == 12 or exchange == "BD":
        prefix = "BD"
    else:
        return "ID." + str(record["stock_id"])
    return prefix + "." + code


class SymbolCatalog:
    """Public security master only; mode=ro includes the live database's WAL."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def search(self, query: str, limit: int = 25) -> Dict:
        """Bounded read-only search by native code, public name, or ID prefix."""
        if not isinstance(query, str) or len(query) > 128 or any(ord(char) < 32 for char in query):
            raise ProviderError("invalid_query", "Catalog query must be at most 128 characters without controls.", 400)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProviderError("invalid_query", "Catalog result limit must be between 1 and 100.", 400)
        if not self.path.is_file():
            raise ProviderError("catalog_unavailable", "The public security catalog was not found.", 503)
        query = query.strip()
        prefix, separator, suffix = query.partition(".")
        prefix = prefix.upper() if separator else ""
        qualified = prefix in ("US", "HK", "FX", "BD", "ID")
        term = suffix if qualified else query
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "(code LIKE ? ESCAPE '\\' OR futu_symbol LIKE ? ESCAPE '\\' OR eng_name LIKE ? ESCAPE '\\')"
        args = [pattern, pattern, pattern]
        if qualified and prefix == "ID":
            if term and not term.isdigit():
                raise ProviderError("invalid_query", "An ID. search must contain digits.", 400)
            where, args = "CAST(stock_id AS TEXT) LIKE ?", [term + "%"]
        elif qualified and prefix == "US":
            where += " AND (cs_market = 2 OR (cs_market = 38 AND exchange IN ('CME','CBOT','COMEX','NYMEX')))"
        elif qualified and prefix == "HK":
            where += " AND cs_market IN (1,6)"
        elif qualified and prefix == "FX":
            where += " AND (cs_market = 11 OR exchange = 'FX')"
        elif qualified and prefix == "BD":
            where += " AND (cs_market = 12 OR exchange = 'BD')"
        sql = ("SELECT " + ",".join(FIELDS) + " FROM t_stock WHERE " + where +
               " ORDER BY CASE WHEN code = ? COLLATE NOCASE OR futu_symbol = ? COLLATE NOCASE THEN 0 ELSE 1 END, code,stock_id LIMIT ?")
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row
                rows = connection.execute(sql, args + [term, term, limit + 1]).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError("catalog_read_failed", "Could not search the public security catalog.", 503,
                                {"sqlite_error": type(exc).__name__}) from exc
        instruments = []
        for row in rows[:limit]:
            record = dict(row)
            record["stock_id"] = str(record["stock_id"])
            record["canonical_symbol"] = canonical_symbol(record)
            record["symbol"] = "ID." + record["stock_id"]
            instruments.append(record)
        return {"query": query, "limit": limit, "count": len(instruments),
                "has_more": len(rows) > limit, "instruments": instruments, "catalog_only": True}

    def resolve(self, symbol: str) -> Dict:
        if not self.path.is_file():
            raise ProviderError("catalog_unavailable", "The Futu public security catalog was not found.", 503)
        prefix, separator, code = symbol.partition(".")
        prefix = prefix.upper() if separator else ""
        code = code if separator else symbol
        where, args = "(code = ? COLLATE NOCASE OR futu_symbol = ? COLLATE NOCASE)", [code, code]
        if prefix == "ID":
            if not code.isdigit():
                raise ProviderError("invalid_symbol", "ID. identifiers must contain digits.", 400)
            where, args = "stock_id = ?", [int(code)]
        elif prefix == "US":
            where += " AND (cs_market = 2 OR (cs_market = 38 AND exchange IN ('CME','CBOT','COMEX','NYMEX')))"
        elif prefix == "HK":
            where += " AND cs_market IN (1,6)"
        elif prefix == "FX":
            where += " AND (cs_market = 11 OR exchange = 'FX')"
        elif prefix == "BD":
            where += " AND (cs_market = 12 OR exchange = 'BD')"
        elif prefix:
            raise ProviderError("symbol_market_unsupported", "Use a catalog-qualified US, HK, FX, BD or ID symbol.", 422)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row
                rows = connection.execute("SELECT " + ",".join(FIELDS) + " FROM t_stock WHERE " + where + " LIMIT 3", args).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError("catalog_read_failed", "Could not read the Futu public security catalog.", 503,
                                {"sqlite_error": type(exc).__name__}) from exc
        if not rows:
            raise ProviderError("symbol_not_found", "Symbol is absent from the local public security catalog.", 404,
                                {"symbol": symbol})
        if len(rows) != 1:
            raise ProviderError("ambiguous_symbol", "Symbol matches several instruments; use a qualified symbol or ID.stock_id.", 409,
                                {"symbol": symbol, "candidates": [
                                    {"symbol": "ID." + str(row["stock_id"]),
                                     "canonical_symbol": canonical_symbol(dict(row)), "name": row["eng_name"]}
                                    for row in rows]})
        record = dict(rows[0])
        record["stock_id"] = str(record["stock_id"])
        record["canonical_symbol"] = canonical_symbol(record)
        record["symbol"] = "ID." + record["stock_id"] if prefix == "ID" else record["canonical_symbol"]
        return record


def normalize_quote(record: Dict, native: Dict, observed_at: str) -> Dict:
    raw = dict(native.get("raw", {}))
    def price(name):
        value = raw.get(name)
        return int(value) / PRICE_SCALE if value is not None else None
    stamp = raw.get("ExchangeTime")
    market_time = None
    if stamp is not None and int(stamp) > 0:
        market_time = dt.datetime.fromtimestamp(int(stamp) / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"symbol": record["symbol"], "stock_id": str(record["stock_id"]),
            "name": record.get("eng_name"), "last": price("PriceNominal"),
            "last_close": price("PriceLastClose"), "exchange_timestamp": market_time,
            "exchange_timestamp_ms": int(stamp) if stamp is not None else None,
            "observed_at": observed_at, "raw": raw, "price_scale": PRICE_SCALE,
            "currency_code": record.get("currency_code"), "exchange": record.get("exchange"),
            "instrument_type_v2": record.get("instrument_type_v2"),
            "delisting_flag": record.get("delisting_flag"), "status": "available"}


def normalize_book(record: Dict, native: Dict, depth: int) -> Dict:
    def levels(items):
        result = []
        for item in items:
            precision = item.get("volume_precision")
            try:
                precision = int(precision) if precision is not None else None
            except (ValueError, TypeError):
                precision = None
            valid_precision = precision is not None and 0 <= precision <= 12
            raw_volume = item.get("volume_raw")
            volume = int(raw_volume) / (10 ** precision) if raw_volume is not None and valid_precision else None
            result.append({"level": item["level"], "price": int(item["price_raw"]) / PRICE_SCALE,
                           "volume": volume, "volume_precision": precision,
                           "order_count": int(item["order_count"]) if item.get("order_count") is not None else None,
                           "price_raw": item["price_raw"], "volume_raw": raw_volume})
        return result
    return {"symbol": record["symbol"], "stock_id": str(record["stock_id"]),
            "bids": levels(native.get("bids", [])), "asks": levels(native.get("asks", [])),
            "book_kind": native.get("book_kind"), "requested_depth": depth,
            "observed_at": utcnow(), "exchange_timestamp": None,
            "raw_times": native.get("raw_times", {}), "native_book": native, "price_scale": PRICE_SCALE}


def normalize_trades(record: Dict, native: Dict) -> Dict:
    rows = []
    for raw in native.get("rows", []):
        if raw.get("price_raw") is None or raw.get("exchange_data_time_ms") is None:
            continue
        size = size_decimal = None
        try:
            value = raw.get("normalized_volume_decimal") if "normalized_volume_decimal" in raw else (raw.get("hp_volume") or raw.get("volume_raw"))
            amount = Decimal(value or "NaN")
            if amount.is_finite() and amount >= 0:
                size_decimal = str(amount)
                size = float(amount)
        except (InvalidOperation, ValueError, TypeError):
            pass
        timestamp = dt.datetime.fromtimestamp(int(raw["exchange_data_time_ms"]) / 1000,
                                             dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows.append({"symbol": record["symbol"], "trade_id": raw.get("tick_key"),
                     "timestamp": timestamp, "exchange_timestamp_ms": int(raw["exchange_data_time_ms"]),
                     "price": int(raw["price_raw"]) / PRICE_SCALE, "size": size,
                     "size_decimal": size_decimal, "exchange": raw.get("exchange_name"),
                     "tick_direction": {0:"invalid", 1:"buy", 2:"sell", 3:"neutral"}.get(raw.get("type_raw"), "unknown"),
                     "trade_type": raw.get("trade_type"), "condition_code": raw.get("cond_code"),
                     "raw": raw})
    rows.sort(key=lambda row: (row["exchange_timestamp_ms"], int(row["trade_id"]) if row.get("trade_id") else 0))
    return {"symbol": record["symbol"], "stock_id": str(record["stock_id"]), "trades": rows,
            "count": len(rows), "total_cached": native.get("total_cached"),
            "order": "exchange_timestamp_then_native_id_ascending",
            "complete_history": False, "period": native.get("period_name"),
            "price_scale": PRICE_SCALE, "observed_at": utcnow(),
            "skipped_cache_indices": native.get("skipped_indices", [])}


class NativeProvider(NativePushMixin, NativeCalendarMixin, NativeShortVolumeMixin, MarketDataProvider):
    name = "futu-desktop-session"

    def __init__(self, pid: Optional[int] = None, catalog_path: Optional[Path] = None,
                 poll_interval: float = 1.0, stream_mode: str = "push", push_queue_size: int = 1024):
        if sys.platform != "win32":
            raise ProviderError("platform_unsupported", "The validated desktop adapter requires Windows x64.", 503)
        if not 0.25 <= poll_interval <= 60:
            raise ValueError("poll_interval must be between 0.25 and 60 seconds")
        self._lock = threading.RLock()
        # Keep state inspection/close independent of a blocked export call.
        self._rpc_lock = threading.Lock()
        self._closed = False
        self._detached = False
        self._session = None
        self._script = None
        self._init_push(stream_mode, push_queue_size)
        self._records = {}
        self._history_lock = threading.Lock()
        self._history_last_request = 0.0
        self._snapshot_lock = threading.Lock()
        self._snapshot_last_request = 0.0
        self._feed_lock = threading.Lock()
        self._feed_last_request = 0.0
        self._option_lock = threading.Lock()
        self._option_last_request = 0.0
        self._news_lock = threading.Lock()
        self._news_last_request = 0.0
        self._calendar_lock = threading.RLock()
        self._calendar_last_request = 0.0
        self._short_lock = threading.Lock()
        self._short_last_request = 0.0
        self.poll_interval = poll_interval
        catalog = catalog_path or Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "FTNN" / "2.0" / "Common" / "stock_v18.db"
        self.catalog = SymbolCatalog(catalog)
        frida = _frida()
        device = frida.get_local_device()
        if pid is None:
            candidates = [process.pid for process in device.enumerate_processes() if process.name.lower() == "ftnn.exe"]
            if len(candidates) != 1:
                raise ProviderError("desktop_process_unavailable", "Open one Futu desktop session, or specify its PID explicitly.", 503,
                                    {"matching_process_count": len(candidates)})
            pid = candidates[0]
        self.pid = int(pid)
        self.executable = _process_path(self.pid)
        self._process_started = self._capture_process_identity()
        if self.executable.name.lower() != "ftnn.exe":
            raise ProviderError("wrong_process", "The selected process is not FTNN.exe.", 400)
        self.module_path = self.executable.parent / "QotBase.dll"
        if not self.module_path.is_file():
            raise ProviderError("version_unsupported", "QotBase.dll was not found beside the desktop executable.", 503)
        self.module_sha256 = hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        if self.module_sha256 != QOTBASE_SHA256:
            raise ProviderError("version_unsupported", "QotBase.dll does not match the validated desktop build; refusing native calls.", 503,
                                {"supported_version": SUPPORTED_VERSION, "observed_sha256": self.module_sha256})
        self.core_module_path = self.executable.parent / "CoreBiz.dll"
        if not self.core_module_path.is_file() or hashlib.sha256(self.core_module_path.read_bytes()).hexdigest() != COREBIZ_SHA256:
            raise ProviderError("version_unsupported", "CoreBiz.dll does not match the validated transport build.", 503)
        self.news_module_path = self.executable.parent / "FTNews.dll"
        self._news_module_valid = (self.news_module_path.is_file() and
                                   hashlib.sha256(self.news_module_path.read_bytes()).hexdigest() == FTNEWS_SHA256)
        self.calendar_module_path = self.executable.parent / "FTQuote.dll"
        self._calendar_module_valid = (self.calendar_module_path.is_file() and
                                       hashlib.sha256(self.calendar_module_path.read_bytes()).hexdigest() == FTQUOTE_SHA256)
        try:
            self._attach_capture(device)
        except Exception as exc:
            self.close()
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("native_attach_failed", "Could not initialize the desktop market-data adapter.", 503,
                                {"error_type": type(exc).__name__}) from exc

    def _attach_capture(self, device):
        sources = [(Path(__file__).with_name("native_agent.js")).read_text(encoding="utf-8")]
        for extension_name in ("native_market.js", "native_ticks.js", "native_history.js", "native_snapshot.js", "native_options.js", "native_feed_requests.js", "native_push.js"):
            extension = Path(__file__).with_name(extension_name)
            if extension.is_file():
                sources.append(extension.read_text(encoding="utf-8"))
        if self._news_module_valid:
            sources.append(Path(__file__).with_name("native_news.js").read_text(encoding="utf-8"))
        if self._calendar_module_valid:
            sources.append(Path(__file__).with_name("native_calendar.js").read_text(encoding="utf-8"))
            sources.append(Path(__file__).with_name("native_reports.js").read_text(encoding="utf-8"))
            sources.append(Path(__file__).with_name("native_short.js").read_text(encoding="utf-8"))
        sources.append("rpc.exports = api;")
        self._capture_generation += 1
        generation = self._capture_generation
        session = device.attach(self.pid)
        self._session = session
        session.on("detached", lambda *args: self._on_detached(*args, generation=generation))
        script = session.create_script("\n".join(sources))
        self._script = script
        script.on("message", lambda message, data: self._on_push_message(message, data, generation))
        script.load()
        info = script.exports_sync.initialize()
        if info["architecture"] != "x64" or info["pointer_size"] != 8:
            raise ProviderError("architecture_unsupported", "Only the verified x64 native ABI is supported.", 503)
        if Path(info["module_path"]).resolve() != self.module_path.resolve():
            raise ProviderError("module_path_mismatch", "The loaded QotBase module differs from the verified file.", 503)
        if info.get("transport_module_path") and Path(info["transport_module_path"]).resolve() != self.core_module_path.resolve():
            raise ProviderError("module_path_mismatch", "The loaded transport module differs from the verified file.", 503)
        self.native_info = info
        if "newsstatus" in info.get("methods", []):
            news = script.exports_sync.newsstatus()
            info["news_available"] = bool(news.get("available") and news.get("module_path") and
                                          Path(news["module_path"]).resolve() == self.news_module_path.resolve())
        if "calendarstatus" in info.get("methods", []):
            calendar = script.exports_sync.calendarstatus()
            info["calendar_available"] = bool(calendar.get("available") and calendar.get("module_path") and
                                              Path(calendar["module_path"]).resolve() == self.calendar_module_path.resolve())
            info["report_link_available"] = bool(info["calendar_available"] and "requestreportlink" in info.get("methods", []))
        if "shortstatus" in info.get("methods", []):
            short = script.exports_sync.shortstatus()
            info["short_volume_available"] = bool(short.get("available") and short.get("module_path") and
                                                  Path(short["module_path"]).resolve() == self.calendar_module_path.resolve())
        self._detached = False

    def _on_detached(self, *args, generation=None):
        if generation is not None and generation != self._capture_generation:
            return
        self._detached = True
        self._push_disconnected(str(args[0]) if args else "desktop_disconnected")

    def _rpc(self, method, *args):
        with self._rpc_lock:
            with self._lock:
                if self._closed or self._detached or self._script is None:
                    raise ProviderError("desktop_disconnected", "The Futu desktop adapter is disconnected.", 503)
                script = self._script
            try:
                return getattr(script.exports_sync, method)(*args)
            except Exception as exc:
                with self._lock:
                    disconnected = self._closed or self._detached
                if disconnected:
                    raise ProviderError("desktop_disconnected", "The Futu desktop adapter disconnected during the request.", 503) from exc
                raise ProviderError("native_read_failed", "The desktop market-data call failed.", 503,
                                    {"method": method, "error_type": type(exc).__name__}) from exc

    def _resolve(self, symbol):
        with self._lock:
            if symbol in self._records:
                return dict(self._records[symbol])
        return self.catalog.resolve(symbol)

    def register_instruments(self, records):
        """Register provider-returned option identities; never synthesize IDs."""
        with self._lock:
            for record in records:
                if not record.get("symbol") or not str(record.get("stock_id", "")).isdigit():
                    raise ValueError("Instrument requires a native symbol and numeric stock_id")
                self._records[record["symbol"]] = dict(record)

    def _catalog_source(self):
        return {"provider": "futu-desktop-security-catalog", "kind": "security_master",
                "observed_at": utcnow(), "catalog_only": True, "market_data_verified": False,
                "catalog_path": str(self.catalog.path),
                "description": "Public security identifiers only; catalog presence does not establish market-data access or history."}

    def search_symbols(self, query: str, limit: int = 25) -> ProviderResult:
        return ProviderResult(self.catalog.search(query, limit), self._catalog_source(),
                              ["Instrument discovery does not verify market-data availability or permissions."])

    def resolve_symbol(self, symbol: str) -> ProviderResult:
        return ProviderResult(self._resolve(symbol), self._catalog_source(),
                              ["A resolved security ID does not establish market-data availability."])

    def _source(self, kind="desktop_cache"):
        requested = kind in ("snapshot", "historical", "option_chain", "orderbook_snapshot", "recent_trades", "stock_news", "earnings_calendar", "earnings_report_link")
        return {"provider": self.name, "kind": kind, "live_push": False,
                "entitlement": ("native subscription gates and desktop-session permissions" if kind == "native_push"
                                else "server-enforced desktop-session permissions" if requested else "app-selected mode 1"),
                "desktop_version": SUPPORTED_VERSION,
                "module_sha256": self.module_sha256, "pid": self.pid, "observed_at": utcnow(),
                "description": ("Market data requested through the running desktop session; inspect the returned market timestamp."
                                if requested else "Market data read from the running desktop cache; inspect the returned market timestamp.")}

    def capabilities(self):
        connected = not self._closed and not self._detached
        caps = {name: {"available": False, "reason": "No verified native getter is connected for this capability."}
                for name in CAPABILITIES}
        for capability in ("search_symbols", "resolve_symbol"):
            caps[capability] = {"available": self.catalog.path.is_file(), "mode": "security_master", "catalog_only": True}
        caps["stock_news"] = {"available": connected and self.native_info.get("news_available", False),
                              "mode": "desktop_session_request", "protocol": 7912, "max_records_per_page": 100,
                              "pagination": "opaque_cursor", "content": "headlines_and_metadata", "live_push": False}
        caps["earnings_calendar"] = {"available": connected and self.native_info.get("calendar_available", False),
                                     "mode": "desktop_session_request", "protocol": 20024,
                                     "validated_markets": ["US", "HK"], "max_range_days": 93,
                                     "pagination": "bounded_offset", "personal_reminders": False, "live_push": False}
        caps["earnings_report_link"] = {"available": connected and self.native_info.get("report_link_available", False),
                                        "mode": "desktop_session_request", "protocol": 20846,
                                        "validated_markets": ["US", "HK"], "content": "public_report_page_metadata",
                                        "original_pdf": False, "live_push": False}
        caps["daily_short_volume"] = {"available": connected and self.native_info.get("short_volume_available", False),
                                      "mode": "desktop_session_request", "validated_markets": ["US", "HK"],
                                      "max_records_per_page": 50, "pagination": "native_timestamp_cursor",
                                      "frequency": "daily", "outstanding_positions": False, "live_push": False}
        caps["earnings_call"] = {"available": caps["earnings_report_link"]["available"],
                                "mode": "native_report_link_then_anonymous_public_pages",
                                "validated_markets": ["US", "HK"], "issuer_verified_transcript": False,
                                "complete_history": False, "public_page_access_required": True}
        caps["quotes"] = {"available": connected, "mode": "desktop_session_request",
                          "delayed_mode": "cache_only_until_delayed_protocol_verified", "cached_symbols_only": False}
        if "readbook" in self.native_info.get("methods", []):
            caps["orderbook"] = {"available": connected, "mode": "desktop_session_request", "max_depth": 50,
                                 "may_return_top_of_book": True, "cached_symbols_only": False}
        if "readtrades" in self.native_info.get("methods", []):
            caps["trades"] = {"available": connected, "mode": "desktop_session_request",
                              "cached_symbols_only": False, "complete_history": False, "max_records": 1000}
        if "readbars" in self.native_info.get("methods", []):
            caps["bars"] = {"available": connected, "mode": "desktop_session_request",
                            "adjustment": "none", "adjustments": ["none", "forward", "backward"],
                            "session": "provider_default", "session_hours_verified": False,
                            "native_kline_type_prob": 0, "max_records": 100000,
                            "sessions": ["provider_default", "regular", "premarket", "postmarket", "extended", "overnight", "all"],
                            "explicit_session_scope": "US stocks and US equity options; intraday only",
                            "session_filter_method": "native_trade_section", "section_enum_labels_verified": False}
            caps["technical_indicators"] = {"available": connected, "mode": "locally_derived_from_bars",
                                              "native_chart_parity_verified": False, "prehistory_loaded": False}
        if "readoptionchain" in self.native_info.get("methods", []):
            caps["option_chain"] = {"available": connected, "mode": "desktop_session_request",
                                    "validated_markets": ["US"], "experimental_markets": ["HK"],
                                    "expiry_scope": "server_listed_current_expiries",
                                    "default_expiry": "nearest_current", "expired_contract_discovery": False}
        if self.stream_mode == "push":
            caps["stream"] = {"available": connected and "pushsubscribe" in self.native_info.get("methods", []),
                              "types": ["quotes", "trades", "orderbook"], "mode": "native_push", "live_push": True,
                              "protocol": 6212, "lossless": False, "exchange_sequence_verified": False,
                              "gap_detection": ["local_capture_sequence", "queue_overflow", "reconnect", "decode_error"],
                              "max_queue": self.push_queue_size, "replay": False}
        else:
            caps["stream"] = {"available": connected, "types": ["quotes", "trades", "orderbook"], "mode": "polled_snapshot",
                              "changes_only": True, "live_push": False, "poll_interval_seconds": self.poll_interval}
        return {"provider": self.name, "connected": connected, "capabilities": caps,
                "entitlement": "app-selected mode 1", "desktop_version": SUPPORTED_VERSION,
                "asset_coverage": "Determined per symbol and feed by the desktop session's returned data; catalog presence alone is not coverage."}

    def bars(self, symbol: str, timeframe: str, start: Optional[str], end: Optional[str],
             adjustment: str = "none", session: str = "provider_default") -> ProviderResult:
        """Select native session feeds, then use observed native section IDs.

        No exchange-hour clock rule is applied. Option responses include native
        pre/post sections even with KlineTypeProb NONE, so the enum alone cannot
        implement a regular-only request.
        """
        from .sessions import SESSION_PROBS, classify_record, filter_session
        if not isinstance(session, str) or session not in SESSION_PROBS:
            raise ProviderError("invalid_query", "Unsupported candle session.", 400)
        if session == "provider_default":
            return self._bars_request(symbol, timeframe, start, end, adjustment, 0)
        if timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h"):
            raise ProviderError("session_timeframe_unsupported", "Explicit session selection requires intraday candles.", 400)
        record = self._resolve(symbol)
        kind = classify_record(record)
        if kind is None:
            raise ProviderError("session_instrument_unsupported", "Explicit session selection is verified only for US stocks and US equity options.", 422,
                                {"symbol": record["symbol"]})
        results = [self._bars_request(symbol, timeframe, start, end, adjustment, prob)
                   for prob in SESSION_PROBS[session]]
        return filter_session(results, session, kind)

    def _bars_request(self, symbol: str, timeframe: str, start: Optional[str], end: Optional[str],
                      adjustment: str = "none", session_prob: int = 0) -> ProviderResult:
        from .history_bounds import validate_bar_range
        try:
            validate_bar_range(start, end)
        except ValueError as exc:
            raise ProviderError("invalid_query", str(exc), 400) from exc
        if not isinstance(timeframe, str):
            raise ProviderError("invalid_query", "Unsupported historical timeframe.", 400)
        # Exact names/numbers come from the installed api_FTCmdKline ExrightType
        # descriptor. Adjustments are applied by Futu, never recomputed locally.
        adjustment_codes = {"none": 0, "forward": 1, "backward": 2}
        if not isinstance(adjustment, str) or adjustment not in adjustment_codes:
            raise ProviderError("invalid_query", "adjustment must be none, forward, or backward.", 400)
        expected_adjustment = adjustment_codes[adjustment]
        if "readbars" not in self.native_info.get("methods", []):
            self.unavailable("bars")
        record = self._resolve(symbol)
        timeframe = "1d" if timeframe == "1Day" else timeframe
        now = dt.datetime.now(dt.timezone.utc)
        def bound(value, is_end):
            if value is None:
                return None
            if len(value) == 10:
                date = dt.date.fromisoformat(value)
                # Native daily bars use local midnight. Expand transport bounds,
                # then filter by the server's explicit market-date field.
                instant = dt.datetime.combine(date, dt.time(), dt.timezone.utc)
                return instant + dt.timedelta(days=2 if is_end else -2)
            instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ProviderError("invalid_query", "Bar timestamps must include a timezone.", 400)
            return instant
        try:
            lower, upper = bound(start, False), bound(end, True) or now
            cursor = min(int(upper.timestamp()), int(now.timestamp()))
        except (ValueError, OverflowError, OSError) as exc:
            raise ProviderError("invalid_query", "Bar bounds are outside the supported datetime range.", 400) from exc
        if cursor <= 0:
            raise ProviderError("invalid_query", "The native historical endpoint requires an end after the Unix epoch.", 400)
        page_size = 500 if start is None else 1000
        if start and timeframe in ("1d", "1w", "1mo"):
            page_size = min(1000, max(10, (upper - lower).days + 2))
        expected_type = {"1m": 1, "3m": 10, "5m": 6, "15m": 7, "30m": 8,
                         "1h": 9, "1d": 2, "1w": 3, "1mo": 4}.get(timeframe)
        if expected_type is None:
            raise ProviderError("invalid_query", "Unsupported historical timeframe.", 400)
        # Date bounds refer to the provider's market calendar, not UTC.  A
        # start beyond tomorrow UTC is unambiguously future in every market
        # timezone.  An inclusive end after today UTC always extends into the
        # future, even where that market date has already begun locally.
        # Aware timestamps can be compared to now exactly.
        def future_bound(value, instant, is_end=False):
            if value is None:
                return False
            return (dt.date.fromisoformat(value) > now.date() + dt.timedelta(days=0 if is_end else 1)
                    if len(value) == 10 else instant > now)
        future_only = future_bound(start, lower)
        extends_into_future = future_only or future_bound(end, upper, True)
        all_rows, requests, has_older = {}, [], True
        completion_reason = "future_range_no_observations" if future_only else "page_limit"
        if future_only:
            has_older = None
        for _ in range(0 if future_only else 100):
            with self._history_lock:
                delay = 0.27 - (time.monotonic() - self._history_last_request)
                if delay > 0:
                    time.sleep(delay)
                native = self._rpc("readbars", str(record["stock_id"]),
                                   {"period": timeframe, "limit": page_size, "end_time": cursor, "adjustment": adjustment,
                                    "native_kline_type_prob": session_prob})
                self._history_last_request = time.monotonic()
            if native.get("status") != "response_received" or native.get("transport_error") != 0:
                raise ProviderError("history_request_failed", "The desktop chart request did not return a successful transport response.", 503,
                                    {key:native.get(key) for key in ("status", "request_id", "transport_error")})
            try:
                parsed = parse_bars(bytes(native.get("payload_bytes", [])))
            except (ValueError, OverflowError, TypeError, OSError) as exc:
                raise ProviderError("history_decode_failed", "The chart response could not be validated.", 502,
                                    {"error_type": type(exc).__name__}) from exc
            if parsed["result"] != 0:
                raise ProviderError("history_provider_error", "Futu rejected this historical-data request.", 422,
                                    {"symbol": record["symbol"], "result_code": parsed["result"], "request_id": native.get("request_id")})
            if parsed["stock_id"] != str(record["stock_id"]):
                raise ProviderError("native_result_invalid", "Historical response identity did not match the requested instrument.", 502)
            if parsed["adjustment_code"] != expected_adjustment:
                raise ProviderError("native_result_invalid", "Historical response adjustment did not match the request.", 502,
                                    {"requested_adjustment": adjustment, "requested_code": expected_adjustment,
                                     "response_code": parsed["adjustment_code"]})
            if parsed.get("kline_type_prob", 0) != session_prob:
                raise ProviderError("native_result_invalid", "Historical response session enum did not match the request.", 502,
                                    {"requested_code": session_prob, "response_code": parsed.get("kline_type_prob", 0)})
            if parsed["kline_type"] != expected_type or parsed["range_type"] != 3:
                raise ProviderError("native_result_invalid", "Historical response timeframe or range did not match the request.", 502)
            requests.append({"request_id": native.get("request_id"), "count": len(parsed["bars"]),
                             "has_older_item": parsed["has_older_item"], "native_kline_type_prob": session_prob})
            has_older = parsed["has_older_item"]
            if not parsed["bars"]:
                completion_reason = "empty_response"
                break
            for row in parsed["bars"]:
                row["symbol"] = record["symbol"]
                stamp = row["timestamp_seconds"]
                previous = all_rows.get(stamp)
                if previous is not None and previous != row:
                    differing = sorted(key for key in previous.keys() | row.keys()
                                       if key not in previous or key not in row or previous[key] != row[key])
                    raise ProviderError("native_result_invalid",
                                        "Historical response contained conflicting values for the same bar timestamp.", 502,
                                        {"timestamp_seconds": stamp, "conflicting_fields": differing})
                all_rows[stamp] = row
            earliest = parsed["bars"][0]
            if start is None:
                completion_reason = "latest_window" if has_older else "provider_history_exhausted"
                break
            # A page may stop partway through the lower-bound market date.  Its
            # first row being on that date does not establish that earlier bars
            # from the same date have been read.  Exhaustion is the exception:
            # the provider explicitly says there is no earlier page to inspect.
            reached_boundary = ((earliest["date"] < start or
                                 (not has_older and earliest["date"] == start)) if len(start) == 10 else
                                earliest["timestamp_seconds"] <= lower.timestamp())
            if reached_boundary or not has_older:
                completion_reason = "lower_bound_reached" if reached_boundary else "provider_history_exhausted"
                break
            next_cursor = earliest["timestamp_seconds"] - 1
            if next_cursor >= cursor:
                raise ProviderError("history_pagination_stalled", "Historical pagination failed to advance.", 502)
            cursor = next_cursor
        rows = sorted(all_rows.values(), key=lambda row: row["timestamp_seconds"])
        def included(row):
            if start and (row["date"] < start if len(start) == 10 else row["timestamp_seconds"] < lower.timestamp()):
                return False
            if end and (row["date"] > end if len(end) == 10 else row["timestamp_seconds"] > upper.timestamp()):
                return False
            return True
        rows = [row for row in rows if included(row)]
        warnings = (["Prices are unadjusted; apply corporate actions separately for return backtests."]
                    if adjustment == "none" else
                    ["Prices use Futu's " + adjustment + " corporate-action adjustment; dividend methodology and point-in-time revision history are not independently certified."])
        warnings.append("Provider pagination does not certify continuity or coverage of every trading date.")
        warnings.append("The provider's default candle session is preserved; included trading hours vary by instrument and are not verified as regular hours.")
        if extends_into_future:
            warnings.append("The requested range extends into the future; future observations are unavailable and the requested range is not complete.")
        if completion_reason == "page_limit":
            warnings.append("The 100-page bound was reached; narrow the requested date range to retrieve the remainder.")
        elif completion_reason == "latest_window":
            warnings.append("Only the latest window is returned; specify a start date to request pagination.")
        elif completion_reason == "provider_history_exhausted" and start:
            warnings.append("The provider reported no older history before the requested lower bound was reached.")
        elif not rows:
            warnings.append("No candles were returned inside the requested date range; coverage is unverified.")
        source = self._source("historical")
        source.update({"transport": "existing desktop session", "protocol": 6161,
                       "entitlement": "server-enforced desktop-session permissions", "adjustment": adjustment,
                       "adjustment_code": expected_adjustment, "adjustment_origin": "provider",
                       "session": "provider_default", "session_hours_verified": False,
                       "native_kline_type_prob": session_prob})
        return ProviderResult({"symbol": record["symbol"], "stock_id": str(record["stock_id"]),
                               "timeframe": timeframe, "adjustment": adjustment, "adjustment_code": expected_adjustment,
                               "session": "provider_default", "session_hours_verified": False,
                               "native_kline_type_prob": session_prob,
                               "bars": rows, "count": len(rows), "requests": requests,
                               "complete_requested_range": (completion_reason == "lower_bound_reached" and
                                                            not extends_into_future) if start else None,
                               "requested_range_extends_into_future": extends_into_future,
                               "completion_reason": completion_reason,
                               "coverage_continuity_verified": False,
                               "older_history_available": has_older,
                               "truncated": completion_reason == "page_limit"}, source, warnings)

    def quotes(self, symbols: List[str]) -> ProviderResult:
        records = [self._resolve(symbol) for symbol in symbols]
        ids = [str(record["stock_id"]) for record in records]
        requested = None
        if "requestquotes" in self.native_info.get("methods", []):
            with self._snapshot_lock:
                delay = 0.27 - (time.monotonic() - self._snapshot_last_request)
                if delay > 0:
                    time.sleep(delay)
                requested = self._rpc("requestquotes", ids)
                self._snapshot_last_request = time.monotonic()
            if requested.get("status") == "response_received" and requested.get("transport_error") == 0:
                try:
                    fetched = {row["stock_id"]:row for row in parse_quote_snapshots(bytes(requested.get("payload_bytes", [])))}
                except (ValueError, TypeError) as exc:
                    raise ProviderError("quote_decode_failed", "The quote response could not be validated.", 502) from exc
            elif requested.get("status") == "no_eligible_symbols":
                fetched = {}
            else:
                raise ProviderError("quote_request_failed", "The desktop quote request did not return successfully.", 503,
                                    {key:requested.get(key) for key in ("status", "request_id", "transport_error")})
            unexpected = set(fetched) - set(ids)
            if unexpected:
                raise ProviderError("native_result_invalid", "Quote response included unrequested instruments.", 502)
            skipped = {row["stock_id"]:row["reason"] for row in requested.get("skipped", [])}
            native_rows = []
            for stock_id in ids:
                if stock_id in fetched:
                    native_rows.append(fetched[stock_id])
                elif skipped.get(stock_id) == "native_bmp_mode_requires_separate_unverified_protocol":
                    row = self._rpc("readquotes", [stock_id])[0]
                    row["origin"] = "desktop_delayed_cache"
                    native_rows.append(row)
                else:
                    native_rows.append({"stock_id": stock_id, "exists": False,
                                        "reason": skipped.get(stock_id, "No nominal-price record was returned.")})
        else:
            native_rows = self._rpc("readquotes", ids)
        observed = utcnow()
        quotes, missing = [], []
        if len(native_rows) != len(records):
            raise ProviderError("native_result_invalid", "Native quote result count did not match the request.", 502)
        for record, native in zip(records, native_rows):
            if str(native.get("stock_id")) != str(record["stock_id"]):
                raise ProviderError("native_result_invalid", "Native quote identity did not match the request.", 502)
            if not native.get("exists") or native.get("raw", {}).get("PriceNominal") is None:
                missing.append(record["symbol"])
                quotes.append({"symbol": record["symbol"], "stock_id": record["stock_id"], "status": "unavailable",
                               "reason": native.get("reason", "No nominal-price record in the desktop's selected cache.")})
            else:
                row = normalize_quote(record, native, observed)
                row["origin"] = native.get("origin", "desktop_cache")
                quotes.append(row)
        if len(missing) == len(records):
            reasons = [{"symbol": row["symbol"], "reason": row.get("reason")} for row in quotes]
            blocked = all(row.get("reason") == "native_quote_auth_blocked" for row in quotes)
            raise ProviderError("entitlement_required" if blocked else "feed_unavailable" if requested else "cache_miss",
                                "The desktop's quote permissions do not allow these instruments." if blocked else
                                "The desktop returned no nominal-price quotes for these instruments.", 422 if blocked else 503,
                                {"symbols": missing, "unavailable": reasons,
                                 "request_id": requested.get("request_id") if requested else None})
        warnings = ["Snapshot retrieval does not certify exchange-time latency; inspect the returned market timestamp."]
        if any(row.get("origin") == "desktop_delayed_cache" for row in quotes):
            warnings.append("Some delayed-mode instruments use the app's existing delayed cache; no fresh request was made for them.")
        if missing:
            warnings.append("Some instruments have no nominal-price quote; inspect each result status.")
        source = self._source("snapshot" if requested else "desktop_cache")
        if requested:
            source.update({"protocol": 6824, "request_id": requested.get("request_id"),
                           "entitlement": "native auth gates plus server-enforced desktop permissions"})
        return ProviderResult({"quotes": quotes, "count": len(quotes),
                               "available_count": len(quotes) - len(missing),
                               "unavailable_count": len(missing)}, source, warnings)

    def option_chain(self, underlying: str, expiry: Optional[str]) -> ProviderResult:
        if "readoptionchain" not in self.native_info.get("methods", []):
            self.unavailable("option_chain")
        record = self._resolve(underlying)
        market = {2: 7, 1: 9}.get(record.get("cs_market"))
        if market is None:
            raise ProviderError("option_market_unsupported", "Option-chain routing supports US and experimental HK underlyings.", 422)
        with self._option_lock:
            def request(method, options):
                delay = 0.52 - (time.monotonic() - self._option_last_request)
                if delay > 0:
                    time.sleep(delay)
                response = self._rpc(method, record["stock_id"], options)
                self._option_last_request = time.monotonic()
                if response.get("status") != "response_received" or response.get("transport_error") != 0:
                    raise ProviderError("option_request_failed", "The desktop option request did not return successfully.", 503,
                                        {key:response.get(key) for key in ("status", "request_id", "transport_error")})
                return response
            discovered = request("readoptionexpiries", {"option_market": market})
            try:
                expiries = parse_option_expiries(bytes(discovered["payload_bytes"]), record["stock_id"], option_market=market)
            except (ValueError, TypeError, OSError, OverflowError) as exc:
                raise ProviderError("option_decode_failed", "Option expiry identities could not be validated.", 502) from exc
            if expiries["ret_code"] != 0:
                raise ProviderError("option_provider_error", "Futu rejected option expiry discovery.", 422,
                                    {"result_code": expiries["ret_code"]})
            if not expiries["expiries"]:
                raise ProviderError("feed_unavailable", "Futu returned no option expiries for this underlying.", 422)
            available_dates = [item["expiry"] for item in expiries["expiries"]]
            if expiry is not None and expiry < dt.datetime.now(dt.timezone.utc).date().isoformat():
                raise ProviderError("option_expiry_unavailable", "Expired option discovery is unavailable through this current-chain route.", 404,
                                    {"expiry": expiry, "reason": "expired", "available_expiries": available_dates})
            if expiry is not None and expiry not in available_dates:
                raise ProviderError("option_expiry_unavailable", "The requested expiry is absent from the current server-listed expiries.", 404,
                                    {"expiry": expiry, "available_expiries": available_dates})
            requested = request("readoptionchain", {"option_market": market, "expiry": expiry})
            selected = requested.get("request", {}).get("expiration_filter")
            if not selected or selected not in available_dates or (expiry and selected != expiry):
                raise ProviderError("native_result_invalid", "Option request expiry did not match verified discovery.", 502)
            body = bytes(requested["payload_bytes"])
            try:
                parsed = parse_option_chain(body, underlying_stock_id=record["stock_id"], expiry=selected, option_market=market)
            except (ValueError, TypeError) as exc:
                raise ProviderError("option_decode_failed", "The option chain could not be validated.", 502) from exc
            if parsed["ret_code"] != 0:
                raise ProviderError("option_provider_error", "Futu rejected the option chain request.", 422,
                                    {"result_code": parsed["ret_code"]})
            identifiers = [row["stock_id"] for row in parsed["contracts"]]
            if len(identifiers) > 4000:
                raise ProviderError("option_result_too_large", "The chain exceeds the validated 4000-contract bound.", 502)
            metadata = {}
            if "readinstrumentmetadata" in self.native_info.get("methods", []):
                def fetch(ids, missing):
                    for offset in range(0, len(ids), 32):
                        for row in self._rpc("readinstrumentmetadata", ids[offset:offset + 32], missing):
                            if row.get("stock_id") not in identifiers:
                                raise ProviderError("native_result_invalid", "Option metadata returned an unrequested identity.", 502)
                            if row.get("exists"):
                                metadata[row["stock_id"]] = row["metadata"]
                fetch(identifiers, True)
                deadline = time.monotonic() + 3
                while len(metadata) < len(identifiers) and time.monotonic() < deadline:
                    time.sleep(0.2)
                    fetch([item for item in identifiers if item not in metadata], False)
                try:
                    parsed = parse_option_chain(body, underlying_stock_id=record["stock_id"], expiry=selected,
                                                option_market=market, metadata=metadata)
                except (ValueError, TypeError) as exc:
                    raise ProviderError("option_identity_mismatch", "Option metadata disagrees with the chain identity.", 502) from exc
        instruments = []
        for contract in parsed["contracts"]:
            native_meta = contract.get("native_metadata", {})
            alias = "ID." + contract["stock_id"]
            instruments.append({"symbol": alias, "stock_id": contract["stock_id"],
                                "eng_name": contract.get("symbol") or alias, "cs_market": market,
                                "exchange": native_meta.get("exchange"), "currency_code": record.get("currency_code"),
                                "side": contract.get("side"), "identity_status": contract.get("identity_status"),
                                "underlying_stock_id": contract.get("underlying_stock_id"),
                                "delisting_flag": int(bool(native_meta.get("is_delisted")))})
            if contract.get("symbol"):
                instruments.append(dict(instruments[-1], symbol=contract["symbol"]))
            contract["id_symbol"] = alias
        self.register_instruments(instruments)
        parsed.update(underlying=record["symbol"], expiries=expiries["expiries"], available_expiries=available_dates,
                      default_expiry_selected=expiry is None, observed_at=utcnow())
        source = self._source("option_chain")
        source.update(protocol=6736, request_id=requested.get("request_id"),
                      expiry_protocol=6311, expiry_request_id=discovered.get("request_id"))
        warnings = ["This is one server-listed expiry, not a complete historical or expired option universe."]
        if any(not row.get("symbol") for row in parsed["contracts"]):
            warnings.append("Some option labels could not be verified; use their returned ID aliases and inspect identity_status.")
        return ProviderResult(parsed, source, warnings)

    def orderbook(self, symbol: str, depth: int) -> ProviderResult:
        if "readbook" not in self.native_info.get("methods", []):
            self.unavailable("orderbook")
        record = self._resolve(symbol)
        requested = None
        if "requestbook" in self.native_info.get("methods", []):
            requested = self._request_feed("requestbook", str(record["stock_id"]), min(depth, 50))
            try:
                native = parse_book(bytes(requested.get("payload_bytes", [])), record["stock_id"], min(depth, 50))
            except (ValueError, TypeError) as exc:
                raise ProviderError("book_decode_failed", "The order-book response could not be validated.", 502) from exc
        else:
            native = self._rpc("readbook", str(record["stock_id"]), min(depth, 50))
        if str(native.get("stock_id")) != str(record["stock_id"]):
            raise ProviderError("native_result_invalid", "Native book identity did not match the request.", 502)
        if not native.get("exists") or not (native.get("bids") or native.get("asks")):
            raise ProviderError("feed_unavailable", "The desktop returned no order-book levels for this instrument.", 503,
                                {"symbol": record["symbol"]})
        result = normalize_book(record, native, depth)
        warnings = ["The order book is a snapshot; market-time freshness is not verified."]
        if native.get("native_book_prob") in (4, 8):
            result["book_kind"] = "top_of_book"
        if native.get("book_kind") == "top_of_book" and depth > 1:
            warnings.append("Only top-of-book was available; requested deeper levels were not returned.")
        elif depth > 50:
            warnings.append("The verified native reader is limited to 50 levels per side.")
        if depth > min(len(result["bids"]), len(result["asks"])):
            warnings.append("Returned %d bid and %d ask levels; the requested depth is not fully available."
                            % (len(result["bids"]), len(result["asks"])))
        if any(level["volume"] is None for side in ("bids", "asks") for level in result[side]):
            warnings.append("Some volume precision is unverified; normalized volume is null and raw values are preserved.")
        source = self._source("orderbook_snapshot" if requested else "desktop_cache")
        if requested:
            source.update({"protocol": 6824, "request_id": requested.get("request_id"),
                           "native_book_prob": requested.get("request", {}).get("native_book_prob")})
        return ProviderResult(result, source, warnings)

    def trades(self, symbol: str, limit: int) -> ProviderResult:
        if "readtrades" not in self.native_info.get("methods", []):
            self.unavailable("trades")
        record = self._resolve(symbol)
        if record.get("instrument_type_v2") == 6:
            raise ProviderError("index_ticks_unsupported", "Index tick updates are not executed market trade prints.", 422,
                                {"symbol": record["symbol"], "stock_id": record["stock_id"]})
        requested = None
        if "requesttrades" in self.native_info.get("methods", []):
            requested = self._request_feed("requesttrades", str(record["stock_id"]), min(limit, 1000))
            try:
                native = parse_trades(bytes(requested.get("payload_bytes", [])), record["stock_id"])
            except (ValueError, TypeError) as exc:
                raise ProviderError("trades_decode_failed", "The public-trades response could not be validated.", 502) from exc
            if native.get("result") != 0:
                raise ProviderError("trades_provider_error", "Futu rejected the public-trades request.", 422,
                                    {"symbol": record["symbol"], "result_code": native.get("result")})
        else:
            native = self._rpc("readtrades", str(record["stock_id"]), min(limit, 1000), 0)
        if str(native.get("stock_id")) != str(record["stock_id"]):
            raise ProviderError("native_result_invalid", "Native ticker identity did not match the request.", 502)
        if not native.get("exists") or not native.get("rows"):
            raise ProviderError("feed_unavailable", "The desktop returned no trade prints for this instrument.", 503,
                                {"symbol": record["symbol"]})
        result = normalize_trades(record, native)
        if not result["trades"]:
            raise ProviderError("native_result_incomplete", "Cached trades lacked price or exchange-time fields.", 502)
        if requested:
            result.update(next_ticker_key=native.get("next_ticker_key"), volume_precision=native.get("volume_precision"))
        source = self._source("recent_trades" if requested else "desktop_ticker_cache")
        if requested:
            source.update({"protocol": 6128, "request_id": requested.get("request_id")})
        return ProviderResult(result, source, [
            "Only a bounded recent trade window is returned; this is not complete historical time-and-sales.",
            "Tick direction is Futu's classification, not a verified aggressor-side execution report."])

    def _request_feed(self, method, stock_id, count):
        with self._feed_lock:
            delay = 0.27 - (time.monotonic() - self._feed_last_request)
            if delay > 0:
                time.sleep(delay)
            response = self._rpc(method, stock_id, count)
            self._feed_last_request = time.monotonic()
        if response.get("status") == "no_eligible_symbols":
            skipped = response.get("skipped", [])
            reason = skipped[0].get("reason") if skipped else "native_feed_unavailable"
            code = "entitlement_required" if reason == "native_quote_auth_blocked" else "feed_unavailable"
            raise ProviderError(code, "The desktop's normal market-data route is unavailable for this instrument.", 422,
                                {"stock_id": stock_id, "reason": reason})
        if response.get("status") != "response_received" or response.get("transport_error") != 0:
            raise ProviderError("feed_request_failed", "The desktop market-data request did not return successfully.", 503,
                                {key:response.get(key) for key in ("status", "request_id", "transport_error")})
        return response

    @staticmethod
    def _information_time(value, is_end=False):
        if value is None:
            return None
        from .client import _date
        parsed = _date(value)
        if len(value) == 10 and is_end:
            parsed += dt.timedelta(days=1, seconds=-1)
        return math.floor(parsed.timestamp()) if is_end else math.ceil(parsed.timestamp())

    def stock_news(self, symbol: str, limit: int = 20, cursor: Optional[str] = None,
                   start: Optional[str] = None, end: Optional[str] = None) -> ProviderResult:
        from .news_wire import parse_stock_news
        if not self.native_info.get("news_available"):
            raise ProviderError("news_unavailable", "The news module is not loaded or does not match the verified desktop build.", 503)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProviderError("invalid_query", "News limit must be an integer between 1 and 100.", 400)
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip() or not 1 <= len(cursor) <= 2048 or
                                   any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
            raise ProviderError("invalid_query", "News cursor must be a nonempty bounded string without controls.", 400)
        record = self._resolve(symbol)
        try:
            lower, upper = self._information_time(start), self._information_time(end, True)
        except (ValueError, OverflowError, OSError) as exc:
            raise ProviderError("invalid_query", "News dates must be supported ISO dates or timezone-aware timestamps.", 400) from exc
        if any(value is not None and not 0 <= value <= 4102444800 for value in (lower, upper)):
            raise ProviderError("invalid_query", "News timestamps must be between 1970 and 2100.", 400)
        if lower is not None and upper is None:
            upper = int(time.time())
        if lower is not None and upper is not None and lower > upper:
            raise ProviderError("invalid_query", "News start must not follow its end.", 400)
        with self._news_lock:
            delay = 0.52 - (time.monotonic() - self._news_last_request)
            if delay > 0:
                time.sleep(delay)
            requested = self._rpc("requestnews", record["stock_id"],
                                  {"limit": limit, "cursor": cursor, "start_time": lower, "end_time": upper})
            self._news_last_request = time.monotonic()
        if requested.get("status") != "response_received" or requested.get("transport_error") != 0:
            raise ProviderError("news_request_failed", "The native stock-news request did not return successfully.", 503,
                                {key: requested.get(key) for key in ("status", "request_id", "transport_error")})
        try:
            parsed = parse_stock_news(bytes(requested.get("payload_bytes", [])))
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise ProviderError("news_decode_failed", "The stock-news response could not be validated.", 502) from exc
        if parsed["result"] not in (0, 1):
            raise ProviderError("news_provider_error", "Futu rejected the stock-news request.", 422,
                                {"result_code": parsed["result"]})
        if parsed["stock_id"] != record["stock_id"]:
            raise ProviderError("native_result_invalid", "Stock-news response identity does not match the requested instrument.", 502)
        if parsed["result"] == 1 and (parsed["items"] or parsed["has_more"]):
            raise ProviderError("native_result_invalid", "The news end-of-data status disagrees with its records.", 502)
        if len(parsed["items"]) > limit:
            raise ProviderError("native_result_invalid", "Futu returned more news items than requested.", 502)
        returned_count = len(parsed["items"])
        if lower is not None or upper is not None:
            parsed["items"] = [item for item in parsed["items"] if item["published_at_unix"] is not None and
                               (lower is None or item["published_at_unix"] >= lower) and
                               (upper is None or item["published_at_unix"] <= upper)]
        parsed.update(symbol=record["symbol"], count=len(parsed["items"]), requested_limit=limit,
                      requested_cursor=cursor, start=start, end=end, complete_history=False,
                      returned_count=returned_count, filtered_out_count=returned_count - len(parsed["items"]),
                      no_more_data=not parsed["has_more"], end_status=parsed["result"] == 1)
        source = self._source("stock_news")
        source.update(protocol=7912, request_id=requested.get("request_id"), news_module_sha256=FTNEWS_SHA256,
                      native_flag=requested.get("request", {}).get("native_flag"), content="headlines_and_metadata")
        warnings = ["Futu's stock-news selection is not a complete historical news archive; when has_more is true, use the returned cursor for another page.",
                    "Publication time is not the first time a headline was available to this account; point-in-time backtest availability is unverified."]
        if parsed["has_more"] and not parsed["next_cursor"]:
            warnings.append("The provider indicated more results without a next cursor; pagination cannot advance.")
        if parsed["filtered_out_count"]:
            warnings.append("Some returned headlines lacked a publication time inside the requested bounds and were omitted; pagination metadata is preserved.")
        return ProviderResult(parsed, source, warnings)

    def stream(self, symbols: List[str], types: List[str]) -> EventStream:
        if self.stream_mode == "push":
            return self._stream_push(symbols, types)
        from .polling import PolledMarketStream
        return PolledMarketStream(self, symbols, types, poll_interval=self.poll_interval)

    def close(self):
        self._close_push_streams()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            session = self._session
            self._script = None
            self._session = None
        # Detaching normally destroys the script and wakes pending Frida RPCs.
        # It must run outside both locks so a pending RPC cannot prevent cleanup.
        # This is not a hard deadline: an unresponsive Frida detach can still wait.
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


class PolledQuoteStream(EventStream):
    """Changed snapshots only. It does not claim tick completeness or replay."""

    def __init__(self, provider: NativeProvider, symbols: List[str], initial: ProviderResult):
        self.provider = provider
        self.symbols = list(symbols)
        self.source = provider._source("polled_snapshot")
        self.source["poll_interval_seconds"] = provider.poll_interval
        self._stop = threading.Event()
        self._initial = initial
        self._last = None
        self._next = 0.0
        self._sequence = 0

    def read(self, timeout: float):
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            wait = max(0.0, self._next - time.monotonic())
            if wait:
                self._stop.wait(min(wait, remaining))
                continue
            result = self._initial or self.provider.quotes(self.symbols)
            self._initial = None
            self._next = time.monotonic() + self.provider.poll_interval
            fingerprint_rows = [{key: value for key, value in row.items() if key != "observed_at"}
                                for row in result.data["quotes"]]
            fingerprint = json.dumps(fingerprint_rows, sort_keys=True, separators=(",", ":"))
            if fingerprint == self._last:
                continue
            self._last = fingerprint
            self._sequence += 1
            source = self.provider._source("polled_snapshot")
            source["poll_interval_seconds"] = self.provider.poll_interval
            return ProviderEvent("quotes", ProviderResult(result.data, source,
                                 result.warnings + ["Polled changed snapshots; intermediate ticks may be missed."]),
                                 str(self._sequence))
        raise StopIteration

    def close(self):
        self._stop.set()


def create_provider():
    """Factory for FutuClient and --provider futu_bridge.native:create_provider."""
    pid = os.environ.get("FUTU_BRIDGE_PID")
    catalog = os.environ.get("FUTU_BRIDGE_CATALOG")
    interval = os.environ.get("FUTU_BRIDGE_POLL_INTERVAL", "1")
    return NativeProvider(pid=int(pid) if pid else None, catalog_path=Path(catalog) if catalog else None,
                          poll_interval=float(interval), stream_mode=os.environ.get("FUTU_BRIDGE_STREAM_MODE", "push"),
                          push_queue_size=int(os.environ.get("FUTU_BRIDGE_PUSH_QUEUE_SIZE", "1024")))
