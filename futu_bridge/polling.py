"""Changed market snapshots and bounded trade windows, not a lossless feed."""

from collections import OrderedDict
import datetime as dt
import json
import threading
import time
from typing import Dict, List, Optional, Tuple

from .provider import EventStream, ProviderError, ProviderEvent, ProviderResult


_EPHEMERAL_KEYS = {"observed_at", "served_at", "request_id", "RecvPushTime", "ServerTime",
                   "ServerSendTimeMs", "ServerRecvTimeMs", "raw_times", "native_book"}


def _comparison_value(value):
    """Keep market timestamps/prices; omit local receipt clocks and duplicate native records."""
    if isinstance(value, dict):
        return {key: _comparison_value(item) for key, item in value.items() if key not in _EPHEMERAL_KEYS}
    if isinstance(value, (list, tuple)):
        return [_comparison_value(item) for item in value]
    return value


def _utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class PolledMarketStream(EventStream):
    """Poll provider snapshots and recent trade windows; no inferred trades.

    Initial snapshots are explicitly labeled. Subsequent quote/book events contain
    changed snapshots. Trade events contain IDs not seen in the bounded local
    history; they are not guaranteed to be newly executed trades. The native
    provider returns normalized rows oldest to newest, making the final row the
    previous window's newest trade. A disappearing tail with unseen rows marks a possible
    gap; absence of that warning never proves completeness.

    Optional initial_snapshots maps (kind, symbol) to ProviderResult, with the
    aggregate quote snapshot under ('quotes', ''). Missing entries are read in
    the constructor so errors occur before opening HTTP SSE headers.
    """

    def __init__(self, provider, symbols: List[str], types: List[str], poll_interval: float = 1.0,
                 initial_snapshots: Optional[Dict[Tuple[str, str], ProviderResult]] = None,
                 max_seen_trade_ids: int = 10_000):
        if not symbols or not types or not set(types).issubset({"quotes", "orderbook", "trades"}):
            raise ValueError("Provide symbols and quote/orderbook/trade stream types")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if max_seen_trade_ids < 1000:
            raise ValueError("max_seen_trade_ids must retain at least one 1,000-record request window")
        self.provider = provider
        self.symbols = list(dict.fromkeys(symbols))
        self.types = list(dict.fromkeys(types))
        self.poll_interval = poll_interval
        self.max_seen_trade_ids = max_seen_trade_ids
        self._stop = threading.Event()
        self._tasks = [(kind, "") for kind in self.types if kind == "quotes"]
        self._tasks += [(kind, symbol) for kind in self.types if kind != "quotes" for symbol in self.symbols]
        self._initial = dict(initial_snapshots or {})
        self._next_due = {task: 0.0 for task in self._tasks}
        self._fingerprints = {}
        self._seen: Dict[str, OrderedDict] = {}
        self._trade_tails = {}
        self._sequence = 0
        self._cursor = 0
        for task in self._tasks:
            if task not in self._initial:
                self._initial[task] = self._fetch(task)
            self._validate(self._initial[task])
        first_source = self._initial[self._tasks[0]].source
        self.source = self._source(first_source, "polled_trade_cache" if self.types == ["trades"] else "polled_snapshot")
        self.source["types"] = self.types

    @staticmethod
    def _validate(result):
        if not isinstance(result, ProviderResult) or not isinstance(result.source, dict) or not result.source:
            raise ProviderError("provider_contract_error", "A polled market-data result lacks provenance.", 502)
        if not isinstance(result.data, dict):
            raise ProviderError("provider_contract_error", "A polled market-data result must contain a data dictionary.", 502)

    def _fetch(self, task):
        kind, symbol = task
        if kind == "quotes":
            return self.provider.quotes(self.symbols)
        if kind == "orderbook":
            return self.provider.orderbook(symbol, 10)
        return self.provider.trades(symbol, 1000)

    def _source(self, source, kind):
        result = dict(source)
        result.update({"kind": kind, "upstream_kind": source.get("kind"),
                       "live_push": False, "gaps_possible": True,
                       "poll_interval_seconds": self.poll_interval,
                       "observed_at": _utcnow(), "deduplication_replays_supported": False})
        if kind == "polled_trade_cache":
            # Historical kind retained for API compatibility; upstream_kind and
            # description distinguish server-requested windows from local cache.
            result["max_seen_trade_ids_per_symbol"] = self.max_seen_trade_ids
        if source.get("kind") == "recent_trades":
            result["description"] = "Bounded recent trade windows requested through the desktop session and deduplicated locally."
        elif source.get("kind") in ("snapshot", "orderbook_snapshot"):
            result["description"] = "Market snapshots requested through the desktop session and polled for changes."
        elif source.get("kind") in ("desktop_cache", "desktop_ticker_cache"):
            result["description"] = "Locally cached desktop market observations polled for changes."
        else:
            result["description"] = "Provider market observations polled for changes; inspect upstream_kind for their origin."
        return result

    def _event(self, kind, data, result, initial, warnings=()):
        self._sequence += 1
        data = dict(data)
        data.update({"initial_snapshot": initial, "gaps_possible": True})
        source_kind = "polled_trade_cache" if kind == "trades" else "polled_snapshot"
        source = self._source(result.source, source_kind)
        source["initial_snapshot"] = initial
        messages = list(result.warnings) + list(warnings)
        if result.source.get("kind") == "recent_trades":
            messages.append("Polled bounded recent-trade windows requested through the desktop session; intermediate executions may be missed.")
        elif result.source.get("kind") in ("snapshot", "orderbook_snapshot"):
            messages.append("Polled market snapshots requested through the desktop session; intermediate market updates may be missed.")
        elif result.source.get("kind") in ("desktop_cache", "desktop_ticker_cache"):
            messages.append("Polled application cache; intermediate market updates may be missed.")
        else:
            messages.append("Polled provider observations; intermediate market updates may be missed.")
        if initial:
            messages.append("Initial observed snapshot; these records are not claimed to be new market activity.")
        return ProviderEvent(kind, ProviderResult(data, source, messages), str(self._sequence))

    def _snapshot_event(self, task, result):
        fingerprint = json.dumps(_comparison_value(result.data), sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        initial = task not in self._fingerprints
        if not initial and self._fingerprints[task] == fingerprint:
            return None
        self._fingerprints[task] = fingerprint
        return self._event(task[0], result.data, result, initial)

    def _trade_event(self, task, result):
        rows = result.data.get("trades")
        if not isinstance(rows, list):
            raise ProviderError("provider_contract_error", "A trade-window response must contain a trades list.", 502)
        symbol = result.data.get("symbol") or task[1]
        initial = symbol not in self._seen
        seen = self._seen.setdefault(symbol, OrderedDict())
        window_ids, new_rows = [], []
        for row in rows:
            if not isinstance(row, dict) or row.get("trade_id") is None or str(row["trade_id"]) == "":
                raise ProviderError("provider_contract_error", "Trade deduplication requires a native trade_id for every row.", 502)
            trade_id = str(row["trade_id"])
            window_ids.append(trade_id)
            if trade_id not in seen:
                new_rows.append(row)
            seen[trade_id] = None
            seen.move_to_end(trade_id)
            while len(seen) > self.max_seen_trade_ids:
                seen.popitem(last=False)
        prior_tail = self._trade_tails.get(symbol)
        potential_gap = bool(not initial and new_rows and prior_tail is not None and prior_tail not in set(window_ids))
        if window_ids:
            self._trade_tails[symbol] = window_ids[-1]
        if not initial and not new_rows:
            return None
        data = dict(result.data)
        # Preserve upstream window metadata while exposing newly observed IDs.
        data.update({"trades": new_rows, "count": len(new_rows), "window_count": len(rows),
                     "complete_history": False, "potential_gap": potential_gap})
        warnings = ["Trade IDs are deduplicated only within this stream's bounded local memory; reconnections begin with a new snapshot."]
        if potential_gap:
            warnings.append("The previous window tail disappeared before unseen trade IDs arrived; trades may have been skipped between polls.")
        return self._event("trades", data, result, initial, warnings)

    def read(self, timeout: float):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            now = time.monotonic()
            due = None
            for offset in range(len(self._tasks)):
                index = (self._cursor + offset) % len(self._tasks)
                task = self._tasks[index]
                if self._next_due[task] <= now:
                    due = task
                    self._cursor = (index + 1) % len(self._tasks)
                    break
            if due is None:
                wait = min(self._next_due.values()) - now
                self._stop.wait(min(max(0.0, wait), remaining))
                continue
            result = self._initial.pop(due) if due in self._initial else self._fetch(due)
            self._next_due[due] = time.monotonic() + self.poll_interval
            self._validate(result)
            if self._stop.is_set():
                break
            event = self._trade_event(due, result) if due[0] == "trades" else self._snapshot_event(due, result)
            if event is not None:
                return event
        raise StopIteration

    def close(self):
        self._stop.set()
