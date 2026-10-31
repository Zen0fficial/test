"""Real HTTP integration audit; no fixture response, mock, or sample is a pass.

Run after the bridge has an actual native provider:
  python -m futu_bridge.integration_audit --base-url http://127.0.0.1:8765

Asset fixtures identify securities only. All observations in the report come
from HTTP requests during this run. Missing feeds and unavailable entitlements
are separate from invalid responses. This tool never sends trading requests.
"""
import argparse
import collections
import datetime as dt
import json
import math
from pathlib import Path
import re
import select
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_SYMBOLS = ["US.AAPL", "HK.00700", "US.SPY", "US..SPX",
                   "FX.USDCNH", "FX.EURUSD", "US.ES2612", "US.GC2612",
                   "HK.04235", "HK.04236"]
FEEDS = ("quotes", "bars", "orderbook", "trades")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
BAD_SOURCE = re.compile(r"(?:^|[^a-z])(sample|mock|fixture|synthetic|simulated|test|bundled|unconnected)(?:$|[^a-z])", re.I)
OPTION_SYMBOL = re.compile(r"^[A-Z]+\.[A-Z0-9.]+\d{6}[CP]\d+$")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def objects(value):
    if isinstance(value, dict):
        yield value
        for name, item in value.items():
            if name in ("raw", "native_book", "raw_integer_fields", "raw_text_fields", "raw_times", "native_session_feed_variants"):
                continue
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def lookup(value, *names):
    mapped = {key(k): v for k, v in value.items()}
    for name in names:
        if key(name) in mapped:
            return mapped[key(name)]
    return None


def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    return math.isfinite(value) and (value > 0 if positive else value >= 0)


def market_price(value):
    # Futures can trade at zero or negative prices; quantities still cannot.
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Reject index counters, bar offsets and accidental YYYYMMDD integers.
        return 946684800 <= value <= 4102444800 or 946684800000 <= value <= 4102444800000
    if not isinstance(value, str):
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return len(value) >= 10
    except ValueError:
        return False


def source_check(source):
    if not isinstance(source, dict) or not source:
        return False, "Missing nonempty source provenance."
    text = json.dumps(source, ensure_ascii=False)
    if BAD_SOURCE.search(text):
        return False, "Source describes a sample, fixture, mock, or unconnected provider."
    if not source.get("provider") or not source.get("kind"):
        return False, "Source must identify provider and kind."
    if not timestamp(source.get("observed_at")):
        return False, "Source lacks a valid observed_at timestamp."
    if source.get("kind") in ("offline", "catalog", "security_master"):
        return False, "Instrument catalog/offline data does not establish a market feed."
    return True, None


def option_symbols(data):
    found = []
    def walk(value):
        if isinstance(value, str) and OPTION_SYMBOL.fullmatch(value):
            found.append(value)
        elif isinstance(value, dict):
            for candidate in (value.get("symbol"), value.get("option_symbol"), value.get("code")):
                if isinstance(candidate, str) and OPTION_SYMBOL.fullmatch(candidate):
                    found.append(candidate)
            for item in value.values():
                if isinstance(item, (list, dict)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(data)
    return list(dict.fromkeys(found))


def validate(feed, payload, expected_symbol=None):
    source = payload.get("source")
    valid, reason = source_check(source)
    if not valid:
        return "UNVERIFIED_SOURCE", [reason], {}
    data = payload.get("data")
    if data in (None, [], {}, ""):
        return "EMPTY_DATA", ["HTTP succeeded but returned no market observations."], {}
    records = list(objects(data))
    if expected_symbol and feed != "option_chain":
        echoed_symbols = {str(lookup(r, "symbol", "contract", "code")) for r in records
                          if isinstance(lookup(r, "symbol", "contract", "code"), str)}
        if isinstance(data, dict) and expected_symbol in data:
            echoed_symbols.add(expected_symbol)
        if expected_symbol not in echoed_symbols:
            return "INCOMPLETE_DATA", ["Response does not echo the full requested instrument identifier; cannot rule out an asset mapping error."], {"observed_identifiers": sorted(echoed_symbols)}
    if feed == "option_chain":
        symbols = option_symbols(data)
        return ("PASS", [], {"contract_count": len(symbols), "contracts": symbols}) if symbols else ("INCOMPLETE_DATA", ["No complete native option contract symbols found."], {})
    if feed == "quotes":
        found = [r for r in records if market_price(lookup(r, "price", "current_price", "last", "last_price", "close"))]
        if not found:
            return "INCOMPLETE_DATA", ["No finite numeric quote price found."], {}
        bad = [r for r in found if lookup(r, "bid", "bid_price") is not None and lookup(r, "ask", "ask_price") is not None
               and market_price(lookup(r, "bid", "bid_price")) and market_price(lookup(r, "ask", "ask_price"))
               and lookup(r, "bid", "bid_price") > lookup(r, "ask", "ask_price")]
        return ("INVALID_DATA", ["Crossed bid/ask."], {}) if bad else ("PASS", [], {"quote_count": len(found)})
    if feed == "bars":
        if isinstance(data, dict) and data.get("bars") == []:
            return "EMPTY_DATA", ["The provider explicitly returned an empty bar array for this request."], {"bar_count": 0}
        if isinstance(data, dict) and "bars" in data:
            if not isinstance(data["bars"], list):
                return "INCOMPLETE_DATA", ["The canonical bar array is malformed."], {}
            records = data["bars"]
        found = [r for r in records if isinstance(r, dict) and all(lookup(r, name) is not None for name in ("open", "high", "low", "close"))]
        if not found:
            return "INCOMPLETE_DATA", ["No complete OHLC records found."], {}
        reasons = []
        if len(found) != len(records):
            reasons.append("The canonical bar array contains incomplete records.")
        if isinstance(data, dict) and "count" in data and (type(data["count"]) is not int or data["count"] != len(found)):
            reasons.append("The declared bar count differs from the canonical bar array.")
        for r in found:
            if expected_symbol and r.get("symbol") not in (None, expected_symbol):
                reasons.append("A bar identifies an unrequested instrument.")
            o, h, l, c = (lookup(r, n) for n in ("open", "high", "low", "close"))
            if not all(market_price(v) for v in (o, h, l, c)) or not (l <= min(o, c) <= max(o, c) <= h):
                reasons.append("Invalid OHLC price bounds.")
            if not timestamp(lookup(r, "timestamp", "time", "time_key", "date", "datetime", "start_time")):
                reasons.append("No market timestamp for bar; retrieval time is not bar time.")
            volume = lookup(r, "volume", "vol")
            if volume is not None and not number(volume):
                reasons.append("Negative/non-numeric volume.")
        return ("INVALID_DATA", list(dict.fromkeys(reasons)), {"bar_count": len(found)}) if reasons else ("PASS", [], {"bar_count": len(found)})
    if feed == "orderbook":
        candidates = [r for r in records if isinstance(lookup(r, "bids"), list) or isinstance(lookup(r, "asks"), list)]
        if not candidates:
            return "INCOMPLETE_DATA", ["No bids/asks level arrays; scalar snapshots do not establish a book."], {}
        levels, best = [], {"bids": [], "asks": []}
        for book in candidates:
            for side in best:
                for level in lookup(book, side) or []:
                    price = lookup(level, "price") if isinstance(level, dict) else level[0] if isinstance(level, (list, tuple)) and len(level) >= 2 else None
                    size = lookup(level, "size", "qty", "quantity", "volume") if isinstance(level, dict) else level[1] if isinstance(level, (list, tuple)) and len(level) >= 2 else None
                    if not market_price(price) or not number(size):
                        return "INVALID_DATA", ["Invalid book price/size."], {}
                    levels.append((price, size))
                    best[side].append(price)
        if not levels:
            return "EMPTY_DATA", ["Book has no levels."], {}
        if best["bids"] and best["asks"] and max(best["bids"]) > min(best["asks"]):
            return "INVALID_DATA", ["Crossed book."], {}
        return "PASS", [], {"level_count": len(levels)}
    if feed == "trades":
        source_kind = source.get("kind", "")
        if source_kind != "polled_trade_cache" and any(s in str(source_kind).lower() for s in ("poll", "sampled", "snapshot")):
            return "UNVERIFIED_SOURCE", ["Polled quote snapshots are not executed trade prints."], {}
        found = [r for r in records if market_price(lookup(r, "price", "trade_price"))
                 and number(lookup(r, "size", "qty", "quantity", "volume"), True)
                 and timestamp(lookup(r, "timestamp", "time", "time_key", "datetime"))]
        if source_kind == "polled_trade_cache" and any(lookup(row, "trade_id") is None for row in found):
            return "INCOMPLETE_DATA", ["Polled trade-cache rows lack native trade IDs."], {}
        return ("PASS", [], {"trade_count": len(found)}) if found else ("INCOMPLETE_DATA", ["No trade with finite price, positive size and market timestamp."], {})
    return "UNSUPPORTED", ["No validator defined for feed."], {}


def bounded_sse_lines(response, deadline):
    """Read this local server's unchunked SSE body without blocking beyond the deadline.

    HTTPResponse's buffered readline can wait for a full line after the deadline.
    Here its BufferedReader drains existing bytes in nonblocking mode, with short
    select waits for more input. No stream reader thread survives the audit.
    """
    if response.getheader("Transfer-Encoding", "").lower() == "chunked":
        raise ValueError("The bounded audit expects the local API's unchunked SSE stream.")
    reader = response.fp
    stream_socket = reader.raw._sock
    stream_socket.setblocking(False)
    pending = bytearray()
    while time.monotonic() < deadline:
        try:
            chunk = reader.read1(65536)
        except BlockingIOError:
            chunk = b""
        if not chunk:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([stream_socket], [], [], min(0.2, remaining))
            if not ready:
                continue
            try:
                chunk = reader.read1(65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
        pending.extend(chunk)
        while b"\n" in pending:
            end = pending.index(b"\n")
            if end > MAX_RESPONSE_BYTES:
                raise ValueError("SSE line exceeds the 8MiB audit bound.")
            line = pending[:end]
            del pending[:end + 1]
            yield line.rstrip(b"\r").decode("utf-8")
        if len(pending) > MAX_RESPONSE_BYTES:
            raise ValueError("SSE line exceeds the 8MiB audit bound.")


class Audit:
    def __init__(self, base_url, timeout, start, end):
        self.base_url = base_url.rstrip("/")
        self.timeout, self.start, self.end = timeout, start, end
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.results = []

    def request(self, path, params=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        started = time.monotonic()
        result = {"requested_at": utcnow(), "url": url}
        try:
            with self.opener.open(url, timeout=self.timeout) as response:
                result["http_status"] = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Response exceeds audit 8MiB bound.")
                result["response"] = json.loads(raw)
        except urllib.error.HTTPError as exc:
            result["http_status"] = exc.code
            try:
                result["response"] = json.loads(exc.read(1024 * 1024))
            except (ValueError, OSError):
                result["transport_error"] = "Non-JSON HTTP error."
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
            result["transport_error"] = str(exc)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result

    def probe(self, feed, symbol, asset_class="unknown"):
        paths = {"quotes": ("/v1/quotes", {"symbols": symbol}),
                 "bars": ("/v1/bars", {"symbol": symbol, "timeframe": "1d", "start": self.start, "end": self.end}),
                 "orderbook": ("/v1/orderbook", {"symbol": symbol, "depth": 5}),
                 "trades": ("/v1/trades", {"symbol": symbol, "limit": 10}),
                 "option_chain": ("/v1/options/chain", {"underlying": symbol})}
        path, params = paths[feed]
        record = dict(self.request(path, params), feed=feed, symbol=symbol, asset_class=asset_class)
        payload = record.get("response", {})
        if record.get("transport_error"):
            record.update(status="UNAVAILABLE", reasons=[record["transport_error"]])
        elif not isinstance(payload, dict):
            record.update(status="INVALID_DATA", reasons=["Response is not a JSON object."])
        elif record.get("http_status", 0) >= 400 or payload.get("error"):
            error = payload.get("error") or {}
            code = str(error.get("code", "http_error"))
            text = (code + " " + str(error.get("message", ""))).lower()
            status = "UNSUPPORTED" if "unsupported" in text or "not_implemented" in text else "UNAVAILABLE"
            if "entitlement" in text or "permission" in text or "subscription_required" in text:
                status = "ENTITLEMENT_REQUIRED"
            record.update(status=status, reasons=[code + ": " + str(error.get("message", ""))])
        else:
            status, reasons, observations = validate(feed, payload, symbol)
            source = payload.get("source") or {}
            record.update(status=status, reasons=reasons, observations=observations,
                          realtime_claimed_by_source=source.get("live") is True,
                          realtime_verified=False)
            # A successful snapshot alone cannot establish real-time latency.
        self.results.append(record)
        print(json.dumps({"asset_class": asset_class, "symbol": symbol, "feed": feed, "status": record["status"]}), flush=True)
        return record

    def stream_probe(self, seconds):
        url = self.base_url + "/v1/stream?" + urllib.parse.urlencode({"symbols": "US.AAPL", "types": "quotes,orderbook,trades"})
        record = {"feed": "stream", "symbol": "US.AAPL", "asset_class": "stock", "requested_at": utcnow(), "url": url,
                  "status": "NO_UPDATES", "reasons": ["No validated market update observed; a heartbeat or closed market is not a feed pass."], "events": [],
                  "realtime_verified": False, "initial_snapshot_count": 0, "noninitial_update_count": 0}
        started = time.monotonic()
        deadline = time.monotonic() + seconds
        try:
            with self.opener.open(url, timeout=min(self.timeout, seconds)) as response:
                record["http_status"] = response.status
                event_type, data = None, []
                for line in bounded_sse_lines(response, deadline):
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        payload = json.loads("\n".join(data))
                        feed = event_type or payload.get("type")
                        if feed in FEEDS:
                            status, reasons, observations = validate(feed, payload, "US.AAPL")
                            markers = [part.get("initial_snapshot") for part in (payload.get("data"), payload.get("source"))
                                       if isinstance(part, dict) and "initial_snapshot" in part]
                            initial = True if True in markers else False if False in markers else None
                            record["events"].append({"type": feed, "validation": status, "initial_snapshot": initial,
                                                     "reasons": reasons, "observations": observations, "response": payload})
                            if status == "PASS":
                                if initial is True:
                                    record["initial_snapshot_count"] += 1
                                    record.update(status="INITIAL_SNAPSHOT_ONLY",
                                                  reasons=["Validated cached initial snapshots arrived, but no subsequent market update was observed."])
                                elif initial is False:
                                    record["noninitial_update_count"] += 1
                                    record.update(status="PASS", reasons=[], realtime_verified=False)
                                    break
                                elif record["status"] != "INITIAL_SNAPSHOT_ONLY":
                                    record.update(status="NO_UPDATES", reasons=["Valid data arrived without an explicit noninitial-update marker; no update pass is claimed."])
                        elif feed == "error":
                            record.update(status="UNAVAILABLE", reasons=["The stream reported an error."], response=payload)
                            break
                        event_type, data = None, []
        except urllib.error.HTTPError as exc:
            record.update(http_status=exc.code, status="UNAVAILABLE", reasons=["Stream HTTP " + str(exc.code)])
            try:
                record["response"] = json.loads(exc.read(1024 * 1024))
            except (ValueError, OSError):
                pass
        except (TimeoutError, socket.timeout):
            pass
        except (urllib.error.URLError, OSError, ValueError) as exc:
            record.update(status="UNAVAILABLE", reasons=[str(exc)])
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self.results.append(record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--symbols", nargs="*", help="Default: 1-2 symbols per requested asset category.")
    parser.add_argument("--feeds", nargs="+", choices=FEEDS, default=list(FEEDS))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--option-count", type=int, default=2)
    parser.add_argument("--skip-options", action="store_true")
    parser.add_argument("--stream-seconds", type=float, default=0, help="Optional bounded real SSE observation; no updates is not a pass.")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "coverage_audit" / "real_integration_report.json")
    args = parser.parse_args(argv)
    if not (0 < args.timeout <= 60) or not (0 <= args.option_count <= 4) or not (0 <= args.stream_seconds <= 30):
        parser.error("timeout must be (0,60], option-count [0,4], stream-seconds [0,30].")
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password:
        parser.error("The audit intentionally supports only the unauthenticated local HTTP bridge.")
    today = dt.datetime.now(dt.timezone.utc).date()
    end = args.end or (today - dt.timedelta(days=1)).isoformat()
    start = args.start or (dt.date.fromisoformat(end) - dt.timedelta(days=6)).isoformat()
    if dt.date.fromisoformat(start) > dt.date.fromisoformat(end):
        parser.error("start must not follow end.")
    fixture_path = Path(__file__).with_name("asset_fixtures.json")
    fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixtures = {f["symbol"]: f for f in fixture_data["fixtures"] if f.get("symbol")}
    audit = Audit(args.base_url, args.timeout, start, end)
    report = {"schema_version": 1, "started_at": utcnow(), "mode": "actual_http_requests_only",
              "base_url": args.base_url, "timeout_seconds": args.timeout,
              "bar_window": {"start": start, "end": end, "timeframe": "1d", "calendar_days": 7},
              "fixture_source": str(fixture_path), "policy": "PASS requires nonempty actual market response, provenance and field sanity. PASS does not certify exchange entitlement, adjustment correctness, complete coverage or real-time latency. Mock/sample/offline bundled data never pass.",
              "health": audit.request("/health"), "capabilities": audit.request("/v1/capabilities"),
              "catalog_only_unmapped": [f for f in fixture_data["fixtures"] if not f.get("symbol")]}
    for symbol in (args.symbols if args.symbols is not None else DEFAULT_SYMBOLS):
        fixture = fixtures.get(symbol, {})
        for feed in args.feeds:
            audit.probe(feed, symbol, fixture.get("asset_class", "user_selected"))
    if not args.skip_options:
        chain = audit.probe("option_chain", "US.AAPL", "option")
        if chain["status"] == "PASS":
            contracts = chain.get("observations", {}).get("contracts", [])[:args.option_count]
            report["dynamic_option_contracts"] = contracts
            for symbol in contracts:
                for feed in args.feeds:
                    audit.probe(feed, symbol, "option")
        else:
            report["dynamic_option_contracts"] = []
            report["option_data_tests_status"] = "NOT_RUN_NO_VERIFIED_CHAIN"
    if args.stream_seconds:
        audit.stream_probe(args.stream_seconds)
    report.update(finished_at=utcnow(), results=audit.results,
                  summary=dict(collections.Counter(r["status"] for r in audit.results)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}), flush=True)
    return 0 if any(r["status"] == "PASS" for r in audit.results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
