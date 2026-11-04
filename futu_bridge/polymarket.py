"""Public Polymarket data, alongside (and independent of) the Futu provider.

REST uses only the standard library. Payloads are preserved, including unknown
fields, non-binary markets, decimal strings, cursors and upstream timestamps.
Only the bundled catalog's public read operations can be invoked.
"""

import datetime as dt
import hashlib
import importlib.util
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .client import MarketDataResponse
from .provider import ProviderError, ProviderResult

BASE_URLS = {"gamma": "https://gamma-api.polymarket.com",
             "clob": "https://clob.polymarket.com", "data": "https://data-api.polymarket.com"}
ALIASES = {
    "markets": ("gamma", "/markets/keyset"), "events": ("gamma", "/events/keyset"),
    "search": ("gamma", "/public-search"), "tags": ("gamma", "/tags"),
    "sports": ("gamma", "/sports"), "teams": ("gamma", "/teams"),
    "series": ("gamma", "/series"), "comments": ("gamma", "/comments"),
    "profile": ("gamma", "/public-profile"), "orderbook": ("clob", "/book"),
    "price": ("clob", "/price"), "midpoint": ("clob", "/midpoint"),
    "spread": ("clob", "/spread"), "last-trade-price": ("clob", "/last-trade-price"),
    **{name: ("data", "/v2/" + name) for name in
       ("trades", "positions", "activity", "prices-history", "oi", "resolutions", "holders",
        "tokens", "value", "user-pnl", "user-stats", "user-volume", "leaderboard", "live-volume", "status")},
}
BATCH_PATHS = {"books", "prices", "midpoints", "spreads", "last-trades-prices"}
MAX_RESPONSE = 32 * 1024 * 1024


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def invalid(message):
    return ProviderError("invalid_query", message, 400)


@lru_cache(maxsize=1)
def endpoint_catalog():
    return json.loads(Path(__file__).with_name("polymarket_endpoints.json").read_text(encoding="utf-8"))


def _endpoint(api, path, method):
    if api == "goldsky":
        raise ProviderError("source_replaced", "Goldsky Feeds were replaced with free Data API activity/positions and Polygon onchain reads.", 410,
                            {"replacements": ["/v1/polymarket/activity", "/v1/polymarket/positions", "/v1/polymarket/onchain/catalog"],
                             "note": "Use the replacement's parameters and response schema; these are not Goldsky-compatible payloads."})
    if api not in BASE_URLS or not isinstance(path, str) or not path.startswith("/"):
        raise invalid("Use a catalog API and absolute endpoint path, not a URL.")
    if any(part in ("", ".", "..") for part in path[1:].split("/")) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
        raise invalid("Invalid Polymarket endpoint path.")
    # Literal routes such as /markets/keyset take precedence over /markets/{id}.
    entries = sorted(endpoint_catalog()["endpoints"], key=lambda e: "{" in e["path"])
    for entry in entries:
        if entry["api"] != api or entry["method"] != method:
            continue
        pattern = re.sub(r"\{[^}]+\}", r"[A-Za-z0-9_.-]+", entry["path"])
        if re.fullmatch(pattern, path):
            return entry
    raise ProviderError("endpoint_not_allowed", "Only catalogued public data reads are supported.", 404,
                        {"api": api, "path": path, "method": method})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonTransport:
    """Bounded public reads. Honors OS/HTTP(S)_PROXY; never logs query values."""

    def __init__(self, timeout=20, retries=2):
        if not 0 < timeout <= 120 or not 0 <= retries <= 5:
            raise ValueError("timeout must be 0..120 seconds and retries 0..5")
        self.timeout, self.retries = timeout, retries

    def request(self, url, *, params=None, body=None, method="GET", headers=None):
        pairs = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            for item in value if isinstance(value, (list, tuple)) else [value]:
                if isinstance(item, bool):
                    item = str(item).lower()
                pairs.append((key, item))
        if pairs:
            url += "?" + urlencode(pairs)
        request = Request(url, data=json_bytes(body) if body is not None else None, method=method,
                          headers={"Accept": "application/json", "Content-Type": "application/json",
                                   "User-Agent": "futu-desktop-bridge/polymarket", **(headers or {})})
        for attempt in range(self.retries + 1):
            try:
                # A new opener per request is safe for concurrent HTTP handler threads.
                with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ProviderError("upstream_response_too_large", "Use a smaller page size.", 502)
                try:
                    return json.loads(raw, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                except (ValueError, UnicodeError) as exc:
                    raise ProviderError("upstream_invalid_json", "The data source returned invalid JSON.", 502) from exc
            except HTTPError as exc:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    wait = max(0.0, float(retry_after))
                except ValueError:
                    wait = 0.5 * 2 ** attempt + random.uniform(0, 0.25)
                raw = exc.read(4096)
                exc.close()
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries and wait <= 10:
                    time.sleep(wait)
                    continue
                detail = {"upstream_status": exc.code}
                if retry_after:
                    detail["retry_after"] = retry_after
                try:
                    error = json.loads(raw)
                    detail["upstream_error"] = str(error.get("error", error.get("message", "")))[:500]
                except (ValueError, AttributeError, UnicodeError):
                    pass
                code = "upstream_rate_limited" if exc.code == 429 else "upstream_error"
                raise ProviderError(code, "Polymarket data request failed.",
                                    exc.code if 400 <= exc.code < 500 else 502, detail) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise ProviderError("upstream_unavailable", "Could not reach the data source.", 502) from exc


def _validate_params(entry, params):
    if not isinstance(params, dict) or len(params) > 64:
        raise invalid("params must be an object with at most 64 fields.")
    definitions = {p["name"]: p for p in entry["parameters"] if p["in"] == "query"}
    unknown = set(params) - set(definitions)
    if unknown:
        raise invalid("Unknown upstream parameters: " + ", ".join(sorted(unknown)))
    for key, definition in definitions.items():
        value = params.get(key)
        if value is None:
            if definition.get("required"):
                raise invalid("Missing parameter: " + key)
            continue
        schema = definition.get("schema", {})
        values = value if isinstance(value, (list, tuple)) else [value]
        max_items = min(500, schema.get("maxItems", 500))
        if len(values) > max_items:
            raise invalid(key + " accepts at most " + str(max_items) + " values per request.")
        for item in values:
            if not isinstance(item, (str, int, float, bool)) or isinstance(item, float) and not math.isfinite(item):
                raise invalid(key + " must contain scalar values.")
            if len(str(item)) > 16384 or any(ord(c) < 32 for c in str(item)):
                raise invalid("Invalid value for " + key)
        if "minimum" in schema or "maximum" in schema:
            try:
                number = float(value)
                if not math.isfinite(number) or number < schema.get("minimum", -math.inf) or number > schema.get("maximum", math.inf):
                    raise ValueError()
            except (TypeError, ValueError):
                raise invalid("Parameter outside documented bounds: " + key)
    if entry["api"] == "data" and any(key in params for key in ("offset", "market", "market_id")):
        raise invalid("Data API v2 uses condition and cursor, not market or offset.")
    if entry["path"].endswith("/keyset") and "offset" in params:
        raise invalid("Keyset pagination uses after_cursor, not offset.")


def page_items(payload):
    """Find rows without flattening or losing the original response envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "markets", "events", "results", "history"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def next_page(api, path, params, payload):
    """Return next request parameters or None. Never turn a null cursor into page 1."""
    result = dict(params)
    if isinstance(payload, dict):
        if isinstance(payload.get("pagination"), dict):
            pagination = payload["pagination"]
            if api == "gamma" and "hasMore" in pagination:
                if not pagination["hasMore"]:
                    return None
                if path == "/public-search":
                    result["page"] = int(params.get("page", 1)) + 1
                else:
                    rows = page_items(payload)
                    if not rows:
                        raise ProviderError("pagination_error", "Continuing page contains no rows.", 502)
                    result["offset"] = int(params.get("offset", 0)) + len(rows)
                return result
            cursor = pagination.get("next_cursor")
            if not cursor:
                if pagination.get("has_more"):
                    raise ProviderError("pagination_error", "Upstream has_more lacks a cursor.", 502)
                return None
            result["cursor"] = cursor
            return result
        if "next_cursor" in payload:
            cursor = payload["next_cursor"]
            if not cursor or cursor == "LTE=":
                return None
            result["after_cursor" if api == "gamma" else "next_cursor"] = cursor
            return result
    # Offset is used only on documented older Gamma listing endpoints.
    if api == "gamma" and isinstance(payload, list) and "limit" in params:
        limit = int(params["limit"])
        if limit > 0 and len(payload) >= limit:
            result["offset"] = int(params.get("offset", 0)) + limit
            return result
    return None


class PolymarketClient:
    """One entry point for discovery, prices, history, public wallets and streams.

    ``get('markets', closed=False)`` and ``request('gamma', '/markets/keyset')``
    return MarketDataResponse objects with the complete upstream JSON in data.
    """

    def __init__(self, *, timeout=20, retries=2, transport=None):
        self._transport = transport or JsonTransport(timeout, retries)
        self._onchain = None
        self._equity = None
        self._lock = threading.Lock()
        self._streams = set()
        self._closed = False

    def _check_open(self):
        if self._closed:
            raise ProviderError("client_closed", "Polymarket client is closed.", 503)

    def catalog(self):
        from .polymarket_onchain import catalog as onchain_catalog
        result = json.loads(json.dumps(endpoint_catalog()))
        for entry in result["endpoints"]:
            entry.update(billing="none", authentication="none")
        result.update({"provider": "polymarket", "read_only": True, "base_urls": BASE_URLS,
                       "billing_policy": "free_only", "authentication": "none",
                       "onchain": onchain_catalog(),
                       "aliases": ALIASES, "streams": ["market", "activity", "sports", "rtds"],
                       "source_policy": {
                           "provider_count": 2,
                           "primary": {"provider": "polymarket", "scope": "Official metadata, prices, books, history and analytics",
                                       "market_transport": "websocket", "market_transport_fallback": "clob_rest_snapshots",
                                       "automatic_transport_fallback": True},
                           "backup": {"provider": "drpc", "url": "https://polygon.drpc.org", "scope": "Raw chain logs, balances and receipts",
                                      "activation": "Explicit onchain requests; not a full replacement for Polymarket"}},
                       "stream_dependency_installed": importlib.util.find_spec("websocket") is not None,
                       "equity_events": {"pipeline": ["popularity", "polymarket_category", "deepseek"],
                                         "results": "/v1/polymarket/equity-events", "refresh": "/v1/polymarket/equity-events/refresh",
                                         "market_data_billing": "free", "classifier_billing": "user_deepseek_api_when_configured",
                                         "get_requests_call_llm": False},
                       "notes": ["Availability is established by requests, not by this catalog.",
                                 "Only Polymarket and dRPC are configured; all reads are free and keyless.",
                                 "Market streams use CLOB REST snapshots during WebSocket interruptions, then return to WebSocket.",
                                 "The dRPC backup is limited to chain data; it cannot supply off-chain order books or market descriptions.",
                                 "External reference feeds and legacy Goldsky subgraphs have been removed.",
                                 "Data API history may lag; use the market stream for current prices."]})
        return result

    def request(self, api, path, *, params=None, body=None, method=None):
        self._check_open()
        method = method or ("POST" if body is not None else "GET")
        entry = _endpoint(api, path, method)
        if params is not None and not isinstance(params, dict):
            raise invalid("params must be an object.")
        params = dict(params or {})
        _validate_params(entry, params)
        if method == "GET" and body is not None:
            raise invalid("GET reads do not accept a body.")
        if method == "POST":
            if body is None:
                raise invalid("This batch read requires a JSON body.")
            if len(json_bytes(body)) > 1024 * 1024:
                raise invalid("Read request body exceeds 1 MiB.")
            if isinstance(body, list) and not 1 <= len(body) <= 500:
                raise invalid("Provide between 1 and 500 batch items.")
        payload = self._transport.request(BASE_URLS[api] + path, params=params, body=body, method=method)
        kind = "historical" if "history" in path or api == "data" and any(
            part in path for part in ("trades", "activity", "resolutions")) else "metadata" if api == "gamma" else "snapshot"
        source = {"provider": "polymarket", "api": api, "kind": kind, "upstream": BASE_URLS[api],
                  "endpoint": path, "method": method, "request_params": params,
                  "observed_at": utcnow(), "read_only": True,
                  "authentication": "none", "billing": "none",
                  "payload_sha256": hashlib.sha256(json_bytes(payload)).hexdigest()}
        return MarketDataResponse(payload, source)

    def get(self, resource, **params):
        try:
            api, path = ALIASES[resource]
        except KeyError:
            raise invalid("Unknown resource. See catalog aliases or use request(api, path).")
        if resource in ("markets", "events"):
            params.setdefault("version", ["v1", "v2"])
        return self.request(api, path, params=params)

    def markets(self, **params):
        return self.get("markets", **params)

    def events(self, **params):
        return self.get("events", **params)

    def equity_filter(self):
        from .equity_filter import EquityEventFilter
        self._check_open()
        with self._lock:
            if self._equity is None:
                self._equity = EquityEventFilter(self)
            return self._equity

    def equity_events(self, **params):
        return self.equity_filter().results(**params)

    def refresh_equity_events(self, *, top_n=None, analyze=True):
        return self.equity_filter().refresh(top_n=top_n, analyze=analyze)

    def history(self, token_id, **params):
        return self.get("prices-history", token_id=token_id, **params)

    def onchain(self, resource="status", **params):
        from .polymarket_onchain import PolygonPublicClient
        self._check_open()
        with self._lock:
            if self._onchain is None:
                self._onchain = PolygonPublicClient()
        return self._onchain.get(resource, **params)

    def batch(self, resource, token_ids, *, side="BUY"):
        if resource not in BATCH_PATHS:
            raise invalid("Unsupported batch resource.")
        if isinstance(token_ids, str):
            token_ids = token_ids.split(",")
        if not 1 <= len(token_ids) <= 500 or any(not isinstance(v, str) or not re.fullmatch(r"\d{1,78}", v) for v in token_ids):
            raise invalid("Provide 1..500 decimal token IDs as strings.")
        if side not in ("BUY", "SELL"):
            raise invalid("side must be BUY or SELL.")
        body = [{"token_id": token, **({"side": side} if resource == "prices" else {})} for token in token_ids]
        return self.request("clob", "/" + resource, body=body)

    def iter_pages(self, resource=None, *, api=None, path=None, params=None, max_pages=10):
        """Yield complete page envelopes; max_pages=None explicitly walks all pages."""
        params = dict(params or {})
        if resource is not None:
            if resource not in ALIASES:
                raise invalid("Unknown resource.")
            api, path = ALIASES[resource]
            if resource in ("markets", "events"):
                params.setdefault("version", ["v1", "v2"])
        definition = _endpoint(api, path, "GET")
        limit = next((p for p in definition["parameters"] if p["name"] == "limit" and p["in"] == "query"), None)
        if limit is not None and "limit" not in params and "as_of" not in params:
            schema = limit.get("schema", {})
            params["limit"] = schema.get("default") or min(100, schema.get("maximum", 100))
        if max_pages is not None and (isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1):
            raise invalid("max_pages must be positive, or None for an unrestricted scan.")
        seen, count = set(), 0
        while True:
            key = json.dumps(params, sort_keys=True)
            if key in seen:
                raise ProviderError("pagination_error", "Upstream pagination repeated a cursor.", 502)
            seen.add(key)
            response = self.request(api, path, params=params)
            following = next_page(api, path, params, response.data)
            if following is not None and json.dumps(following, sort_keys=True) in seen:
                raise ProviderError("pagination_error", "Upstream pagination did not advance.", 502)
            count += 1
            response.source["pagination"] = {"page": count, "next_params": following,
                                              "complete": following is None,
                                              "limited": following is not None and max_pages is not None and count >= max_pages}
            yield response
            if following is None or max_pages is not None and count >= max_pages:
                return
            params = following

    def snapshot(self, slug, *, history_interval="1d"):
        """Resolve a market slug and collect metadata, books, history and analytics.

        Individual unavailable resources remain explicit errors in the snapshot.
        A discovery failure is fatal. This is not an atomic market snapshot.
        """
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", slug):
            raise invalid("Provide a market slug.")
        market = self.request("gamma", "/markets/slug/" + slug)
        tokens = market.data.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        tokens = tokens or market.data.get("positionIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        condition = market.data.get("conditionId") or market.data.get("condition")
        calls = {}
        if tokens:
            for kind in ("books", "midpoints", "spreads", "last-trades-prices"):
                calls[kind] = lambda kind=kind: self.batch(kind, tokens)
            for token in tokens:
                calls["history:" + token] = lambda token=token: self.history(token, interval=history_interval)
        if condition:
            for kind in ("trades", "oi", "resolutions", "tokens"):
                calls[kind] = lambda kind=kind: self.get(kind, condition=condition)
        values = {"market": market.to_dict()}
        warnings = ["Snapshot components are observed at different times; history may be paginated."]
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {key: pool.submit(call) for key, call in calls.items()}
            for key, future in pending.items():
                try:
                    values[key] = future.result().to_dict()
                except ProviderError as exc:
                    values[key] = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
                    warnings.append(key + " unavailable: " + exc.code)
        return MarketDataResponse(values, {"provider": "polymarket", "kind": "composite_snapshot",
                                          "observed_at": utcnow(), "slug": slug}, warnings)

    def subgraph(self, name, query, *, variables=None, operation_name=None):
        self._check_open()
        raise ProviderError("source_removed", "Goldsky subgraphs were removed. Use official Polymarket data or the dRPC onchain backup.", 410)

    def stream(self, channel="market", **options):
        from .polymarket_stream import PolymarketStream
        self._check_open()
        stream = PolymarketStream(channel, **options)
        with self._lock:
            if self._closed:
                stream.close()
                self._check_open()
            self._streams.add(stream)
        stream.on_close = lambda: self._discard_stream(stream)
        return stream

    def _discard_stream(self, stream):
        with self._lock:
            self._streams.discard(stream)

    def close(self):
        with self._lock:
            self._closed = True
            streams, self._streams = list(self._streams), set()
        for stream in streams:
            stream.close()
        if self._equity is not None:
            self._equity.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()


def as_provider_result(response):
    return ProviderResult(response.data, response.source, response.warnings)
