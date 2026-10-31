"""Python-standard-library, read-only market-data HTTP API."""

import argparse
import datetime as dt
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .provider import MarketDataProvider, ProviderError, ProviderResult, UnavailableProvider, load_provider
from .client import _validate_information_range
from .history_bounds import validate_bar_range
from . import __version__
from .samples import DEFAULT_SAMPLE, SampleBars


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def result_body(result, request_id):
    if not isinstance(result, ProviderResult) or not isinstance(result.source, dict) or not result.source:
        raise RuntimeError("Provider returned a result without provenance")
    return {"request_id": request_id, "served_at": now(), "data": result.data,
            "source": result.source, "warnings": result.warnings}


class Query:
    def __init__(self, text):
        try:
            self.values = parse_qs(text, keep_blank_values=True, max_num_fields=32)
        except ValueError:
            raise ProviderError("invalid_query", "Too many query parameters.", 400)
        if any(len(values) != 1 for values in self.values.values()):
            raise ProviderError("invalid_query", "Repeated query parameters are not supported.", 400)

    def only(self, *names):
        unknown = sorted(set(self.values) - set(names))
        if unknown:
            raise ProviderError("invalid_query", "Unknown query parameters: " + ", ".join(unknown), 400)

    def text(self, name, default=None, required=False, max_length=256):
        value = self.values.get(name, [default])[0]
        if required and not value:
            raise ProviderError("invalid_query", "Missing query parameter: " + name, 400)
        if value is not None and (len(value) > max_length or any(ord(char) < 32 for char in value)):
            raise ProviderError("invalid_query", "Invalid value for: " + name, 400)
        return value

    def symbol(self, name="symbol"):
        value = self.text(name, required=True).strip()
        if not value or len(value) > 128:
            raise ProviderError("invalid_query", "Invalid symbol.", 400)
        return value

    def symbols(self):
        values = self.text("symbols", required=True, max_length=13000).split(",")
        if len(values) > 100 or any(not value.strip() or len(value.strip()) > 128 for value in values):
            raise ProviderError("invalid_query", "Provide between 1 and 100 comma-separated symbols.", 400)
        return list(dict.fromkeys(value.strip() for value in values))

    def integer(self, name, default, lower, upper):
        try:
            value = int(self.text(name, str(default)))
        except ValueError:
            raise ProviderError("invalid_query", name + " must be an integer.", 400)
        if not lower <= value <= upper:
            raise ProviderError("invalid_query", f"{name} must be between {lower} and {upper}.", 400)
        return value

    def date(self, name, date_only=False):
        value = self.text(name)
        if value is None:
            return None
        try:
            if date_only or len(value) == 10:
                dt.date.fromisoformat(value)
            else:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Timezone required")
        except ValueError:
            raise ProviderError("invalid_query", name + " must be an ISO date or timezone-aware timestamp.", 400)
        return value

    def date_range(self, calendar=False):
        start, end = self.date("start"), self.date("end")
        try:
            _validate_information_range(start, end, calendar=calendar)
        except ValueError as exc:
            raise ProviderError("invalid_query", str(exc), 400) from exc
        return start, end

    def news_arguments(self):
        self.only("symbol", "limit", "cursor", "start", "end")
        symbol = self.symbol()
        limit = self.integer("limit", 20, 1, 100)
        cursor = self.text("cursor", max_length=2048)
        if cursor is not None and (not cursor.strip() or any(ord(char) == 127 for char in cursor)):
            raise ProviderError("invalid_query", "cursor must be a nonempty native string without control characters.", 400)
        start, end = self.date_range()
        return symbol, limit, cursor, start, end

    def bar_arguments(self, sample=False, extra=()):
        self.only(*(("symbol", "timeframe", "start", "end") if sample else
                    ("symbol", "timeframe", "start", "end", "adjustment", "session")) + extra)
        symbol = self.symbol()
        timeframe = self.text("timeframe", "1d")
        if timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo", "1Day"):
            raise ProviderError("invalid_query", "Unsupported timeframe syntax.", 400)
        start, end = self.date("start"), self.date("end")
        try:
            validate_bar_range(start, end)
        except ValueError as error:
            raise ProviderError("invalid_query", str(error), 400) from error
        if sample:
            return symbol, timeframe, start, end
        adjustment = self.text("adjustment", "none")
        if adjustment not in ("none", "forward", "backward"):
            raise ProviderError("invalid_query", "adjustment must be none, forward, or backward.", 400)
        session = self.text("session", "provider_default")
        from .sessions import validate_session
        try:
            validate_session(session, timeframe)
        except ValueError as exc:
            raise ProviderError("invalid_query", str(exc), 400) from exc
        arguments = (symbol, timeframe, start, end, adjustment)
        return arguments if session == "provider_default" else arguments + (session,)

    def indicator_arguments(self):
        from .indicators import validate_selection
        arguments = self.bar_arguments(extra=("indicators",))
        selection = self.text("indicators", max_length=256)
        try:
            selection = validate_selection(selection.split(",") if selection is not None else None)
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_query", str(exc), 400) from exc
        return arguments + (selection,) if len(arguments) == 5 else arguments[:5] + (selection, arguments[5])

    def short_volume_arguments(self):
        self.only("symbol", "limit", "cursor", "start", "end")
        symbol = self.symbol()
        limit = self.integer("limit", 20, 1, 50)
        cursor = self.text("cursor", max_length=10)
        if cursor is not None and (not cursor.isascii() or not cursor.isdigit() or not 0 < int(cursor) <= 4102444800):
            raise ProviderError("invalid_query", "cursor must be the numeric string from a previous short-volume page.", 400)
        start, end = self.date_range(calendar=True)
        return symbol, limit, cursor, start, end


class MarketDataServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, provider=None, sample_path=DEFAULT_SAMPLE, token=None):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError("This service must bind to a loopback IP address.")
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.provider = provider or UnavailableProvider()
        self.samples = SampleBars(sample_path)
        self.token = token
        self._resource_lock = threading.Lock()
        self._connections = set()
        self._subscriptions = {}
        self._closing = False
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        with self._resource_lock:
            if self._closing:
                connection.close()
                raise OSError("Server is closing")
            self._connections.add(connection)
        return connection, address

    def close_request(self, request):
        with self._resource_lock:
            self._connections.discard(request)
        super().close_request(request)

    def register_subscription(self, subscription):
        with self._resource_lock:
            if self._closing:
                raise ProviderError("service_stopping", "The local API is stopping.", 503)
            self._subscriptions[id(subscription)] = subscription

    def unregister_subscription(self, subscription):
        with self._resource_lock:
            self._subscriptions.pop(id(subscription), None)

    def server_close(self):
        with self._resource_lock:
            self._closing = True
            subscriptions = list(self._subscriptions.values())
            connections = list(self._connections)
        for subscription in subscriptions:
            try:
                subscription.close()
            except Exception as exc:
                sys.stderr.write("Subscription cleanup failed: " + type(exc).__name__ + "\n")
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "FutuLocalBridge/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Avoid writing option identifiers/query values or authentication to logs.
        sys.stderr.write("%s %s\n" % (now(), fmt % args))

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s -> %s", self.command, urlsplit(self.path).path, code)

    def authorize(self):
        # A rebinding page can make same-origin GETs without an Origin header.
        # Only literal local authorities for this listener may reach the provider.
        host, port = self.server.server_address[:2]
        literal_host = "[" + host + "]" if ":" in host else host
        allowed_hosts = {f"{literal_host}:{port}".lower(), f"localhost:{port}"}
        if port == 80:
            allowed_hosts.update({literal_host.lower(), "localhost"})
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1 or host_headers[0].lower() not in allowed_hosts:
            raise ProviderError("invalid_host", "Host must identify this loopback listener.", 403)
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            raise ProviderError("cross_origin_denied", "Cross-site browser access is disabled.", 403)
        origin = self.headers.get("Origin")
        if origin:
            host, port = self.server.server_address[:2]
            try:
                parsed = urlsplit(origin)
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                raise ProviderError("cross_origin_denied", "Invalid browser origin.", 403)
            if parsed.scheme != "http" or parsed.hostname not in (host, "localhost") or origin_port != port:
                raise ProviderError("cross_origin_denied", "Cross-origin browser access is disabled.", 403)
        if self.server.token:
            expected = "Bearer " + self.server.token
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise ProviderError("unauthorized", "A bearer token is required.", 401)

    def json_response(self, status, value):
        payload = encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, error, request_id):
        self.json_response(error.status, {"request_id": request_id, "served_at": now(),
                           "error": {"code": error.code, "message": error.message,
                                     "details": error.details}})

    def do_GET(self):
        request_id = str(uuid.uuid4())
        try:
            self.authorize()
            split = urlsplit(self.path)
            if len(self.path) > 16384:
                raise ProviderError("invalid_query", "Request URL is too long.", 414)
            query = Query(split.query)
            provider = self.server.provider
            if split.path == "/health":
                query.only()
                caps = provider.capabilities()
                self.json_response(200, {"status": "ok", "service": "futu-local-market-data",
                                         "version": __version__, "read_only": True,
                                         "provider": caps.get("provider", provider.name),
                                         "provider_connected": caps.get("connected", False),
                                         "served_at": now()})
                return
            if split.path == "/v1/capabilities":
                query.only()
                self.json_response(200, {"request_id": request_id, "served_at": now(),
                                         "read_only": True, "market_data": provider.capabilities(),
                                         "samples": self.server.samples.metadata()})
                return
            if split.path == "/v1/quotes":
                query.only("symbols")
                result = provider.quotes(query.symbols())
            elif split.path == "/v1/instruments":
                query.only("query", "limit")
                result = provider.search_symbols(query.text("query", "", max_length=128), query.integer("limit", 25, 1, 100))
            elif split.path == "/v1/instruments/resolve":
                query.only("symbol")
                result = provider.resolve_symbol(query.symbol())
            elif split.path == "/v1/bars":
                result = provider.bars(*query.bar_arguments())
            elif split.path == "/v1/indicators":
                result = provider.technical_indicators(*query.indicator_arguments())
            elif split.path == "/v1/samples/bars":
                result = self.server.samples.bars(*query.bar_arguments(sample=True))
            elif split.path == "/v1/options/chain":
                query.only("underlying", "expiry")
                result = provider.option_chain(query.symbol("underlying"), query.date("expiry", True))
            elif split.path == "/v1/orderbook":
                query.only("symbol", "depth")
                result = provider.orderbook(query.symbol(), query.integer("depth", 10, 1, 100))
            elif split.path == "/v1/trades":
                query.only("symbol", "limit")
                result = provider.trades(query.symbol(), query.integer("limit", 100, 1, 1000))
            elif split.path == "/v1/news":
                result = provider.stock_news(*query.news_arguments())
            elif split.path == "/v1/short-selling/daily":
                result = provider.daily_short_volume(*query.short_volume_arguments())
            elif split.path == "/v1/earnings/calendar":
                query.only("symbol", "start", "end")
                start, end = query.date_range(calendar=True)
                result = provider.earnings_calendar(query.symbol(), start, end)
            elif split.path == "/v1/earnings/report":
                query.only("symbol", "fiscal_period")
                fiscal_period = query.text("fiscal_period", required=True, max_length=6)
                if not re.fullmatch(r"(19[7-9]\d|20\d\d)Q[1-4]", fiscal_period):
                    raise ProviderError("invalid_query", "fiscal_period must be YYYYQ1 through YYYYQ4, for years 1970..2099.", 400)
                result = provider.earnings_report_link(query.symbol(), fiscal_period)
            elif split.path == "/v1/earnings/call":
                query.only("symbol", "fiscal_period", "include_transcript", "language")
                fiscal_period = query.text("fiscal_period", required=True, max_length=6)
                include_transcript = query.text("include_transcript", "false")
                language = query.text("language", "page")
                if not re.fullmatch(r"(19[7-9]\d|20\d\d)Q[1-4]", fiscal_period):
                    raise ProviderError("invalid_query", "fiscal_period must be YYYYQ1 through YYYYQ4, for years 1970..2099.", 400)
                if include_transcript not in ("true", "false") or language not in ("page", "original", "en", "zh-Hans", "zh-Hant"):
                    raise ProviderError("invalid_query", "Invalid transcript selection.", 400)
                result = provider.earnings_call(query.symbol(), fiscal_period, include_transcript == "true", language)
            elif split.path == "/v1/reports/issuer":
                from .report_documents import issuer_reports
                query.only("symbol", "year")
                year = query.integer("year", 2026, 1970, 2099) if query.text("year") is not None else None
                result = issuer_reports(query.symbol(), year)
            elif split.path == "/v1/reports/announcement":
                from .report_documents import announcement_documents
                query.only("notice_id")
                result = announcement_documents(query.text("notice_id", required=True, max_length=20))
            elif split.path == "/v1/stream":
                query.only("symbols", "types")
                symbols = query.symbols()
                types = list(dict.fromkeys(query.text("types", "quotes,trades,orderbook").split(",")))
                if not types or not set(types).issubset({"quotes", "trades", "orderbook"}):
                    raise ProviderError("invalid_query", "Stream types must be quotes,trades,orderbook.", 400)
                self.serve_stream(provider.stream(symbols, types), request_id)
                return
            else:
                raise ProviderError("not_found", "Unknown API route.", 404)
            self.json_response(200, result_body(result, request_id))
        except ProviderError as exc:
            self.error_response(exc, request_id)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.log_message("Request failed: %s", type(exc).__name__)
            self.error_response(ProviderError("internal_error", "The provider could not complete this request.", 500), request_id)

    def serve_stream(self, subscription, request_id):
        # Availability must already have been checked by provider.stream().
        if not isinstance(subscription.source, dict) or not subscription.source:
            subscription.close()
            raise RuntimeError("Provider subscription lacks provenance")
        try:
            self.server.register_subscription(subscription)
        except Exception:
            try:
                subscription.close()
            except Exception:
                pass
            raise
        def emit(kind, body, event_id=None):
            if not re.fullmatch(r"[a-z_]+", kind):
                raise ValueError("Unsafe event type")
            prefix = "event: " + kind + "\n"
            if event_id is not None:
                if not isinstance(event_id, str) or any(char in event_id for char in "\r\n\x00"):
                    raise ValueError("Unsafe event ID")
                prefix += "id: " + event_id + "\n"
            self.wfile.write(prefix.encode() + b"data: " + encode(body) + b"\n\n")
            self.wfile.flush()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(30)
            emit("ready", {"request_id": request_id, "served_at": now(), "source": subscription.source})
            while True:
                try:
                    event = subscription.read(timeout=15.0)
                except StopIteration:
                    emit("end", {"served_at": now()})
                    break
                if event is None:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if event.type not in ("quotes", "trades", "orderbook", "heartbeat"):
                    raise ValueError("Unexpected provider event type")
                emit(event.type, result_body(event.result, request_id), event.id)
        except (OSError, TimeoutError):
            pass
        except ProviderError as exc:
            try:
                emit("error", {"error": {"code": exc.code, "message": exc.message, "details": exc.details}})
            except (OSError, TimeoutError):
                pass
        except Exception as exc:
            self.log_message("Stream failed: %s", type(exc).__name__)
            try:
                emit("error", {"error": {"code": "stream_failed", "message": "The provider stream stopped unexpectedly."}})
            except (OSError, TimeoutError):
                pass
        finally:
            self.server.unregister_subscription(subscription)
            try:
                subscription.close()
            except Exception as exc:
                self.log_message("Subscription cleanup failed: %s", type(exc).__name__)

    def do_POST(self):
        self.close_connection = True
        self.error_response(ProviderError("method_not_allowed", "This is a read-only GET API.", 405), str(uuid.uuid4()))

    do_PUT = do_DELETE = do_PATCH = do_POST


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Loopback IP only (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", help="Importable trusted Python module:factory")
    parser.add_argument("--sample-path", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--token-env", help="Environment variable containing an optional bearer token")
    args = parser.parse_args(argv)
    token = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            parser.error("The named token environment variable is empty or missing.")
    provider = load_provider(args.provider)
    server = None
    try:
        server = MarketDataServer((args.host, args.port), provider, args.sample_path, token)
        print(f"Futu read-only local API listening on {args.host}:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            provider.close()


if __name__ == "__main__":
    main()
