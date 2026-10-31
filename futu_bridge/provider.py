"""Provider contract. Importing this module never connects to a brokerage."""

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


CAPABILITIES = ("quotes", "bars", "option_chain", "orderbook", "trades", "stream", "search_symbols", "resolve_symbol",
                "stock_news", "earnings_calendar", "earnings_report_link", "earnings_call", "technical_indicators", "daily_short_volume")


class ProviderError(Exception):
    """A safe, client-facing error from a provider."""

    def __init__(self, code: str, message: str, status: int = 503,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


@dataclass
class ProviderResult:
    """Provider data and its origin; source must be a nonempty dictionary.

    Recommended source keys: provider, kind (live/historical/cache/sample),
    observed_at, market_timestamp, entitlement, and upstream. Historical and
    cached observations must not be described as live. Values stay in the
    provider's documented units and schema; no silent price adjustment occurs.
    """

    data: Any
    source: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


@dataclass
class ProviderEvent:
    """One update. type is quotes, trades, orderbook, or heartbeat."""

    type: str
    result: ProviderResult
    id: Optional[str] = None


class EventStream:
    """Open subscription with bounded waits and explicit cleanup.

    read(timeout) returns an event, None when the timeout expires, or raises
    StopIteration on normal completion. close() must unblock/readily terminate
    pending work. Providers perform availability checks before returning this
    object so the server can return an HTTP error before opening SSE headers.
    """

    source: Dict[str, Any]

    def read(self, timeout: float) -> Optional[ProviderEvent]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MarketDataProvider:
    """Implement only capabilities that the actual desktop source supports.

    Methods may be called by multiple HTTP request threads. A provider that
    cannot safely run concurrently must serialize its own upstream operations.
    Quotes may contain stock and native option identifiers. Option identifiers
    should come from option_chain; the API does not invent a contract mapping.
    """

    name = "unconnected"

    def capabilities(self) -> Dict[str, Any]:
        return {"provider": self.name, "connected": False,
                "capabilities": {
                    name: {"available": False,
                           "reason": "No verified live desktop adapter is connected."}
                    for name in CAPABILITIES}}

    def unavailable(self, capability: str) -> None:
        raise ProviderError("feed_unavailable",
                            "No verified desktop source is connected for " + capability + ".",
                            details={"capability": capability, "provider": self.name})

    def quotes(self, symbols: List[str]) -> ProviderResult:
        self.unavailable("quotes")

    def search_symbols(self, query: str, limit: int = 25) -> ProviderResult:
        self.unavailable("search_symbols")

    def resolve_symbol(self, symbol: str) -> ProviderResult:
        self.unavailable("resolve_symbol")

    def bars(self, symbol: str, timeframe: str, start: Optional[str],
             end: Optional[str], adjustment: str = "none", session: str = "provider_default") -> ProviderResult:
        """Return provider-adjusted bars; adjustment is none, forward, or backward."""
        self.unavailable("bars")

    def option_chain(self, underlying: str, expiry: Optional[str]) -> ProviderResult:
        self.unavailable("option_chain")

    def technical_indicators(self, symbol: str, timeframe: str = "1d", start: Optional[str] = None,
                             end: Optional[str] = None, adjustment: str = "none", indicators=None,
                             session: str = "provider_default") -> ProviderResult:
        """Compute documented trailing indicators from a single actual bar response."""
        from .indicators import derive_indicator_result, validate_selection
        from .sessions import validate_session
        selection = validate_selection(indicators)
        validate_session(session, timeframe)
        result = (self.bars(symbol, timeframe, start, end, adjustment) if session == "provider_default" else
                  self.bars(symbol, timeframe, start, end, adjustment, session=session))
        return derive_indicator_result(result, selection)

    def orderbook(self, symbol: str, depth: int) -> ProviderResult:
        self.unavailable("orderbook")

    def trades(self, symbol: str, limit: int) -> ProviderResult:
        self.unavailable("trades")

    def stream(self, symbols: List[str], types: List[str]) -> EventStream:
        self.unavailable("stream")

    def stock_news(self, symbol: str, limit: int = 20, cursor: Optional[str] = None,
                   start: Optional[str] = None, end: Optional[str] = None) -> ProviderResult:
        """Read stock news while preserving native pagination and publication times.

        Cursors are opaque native strings. A publication timestamp alone does not
        identify the first moment an item was available for a trading strategy.
        """
        self.unavailable("stock_news")

    def earnings_calendar(self, symbol: str, start: Optional[str] = None,
                          end: Optional[str] = None) -> ProviderResult:
        """Read a verified reporting schedule; never modify personal alerts.

        Preserve confirmed versus estimated dates when available. An unavailable
        calendar must raise an error rather than return a guessed reporting date.
        """
        self.unavailable("earnings_calendar")

    def earnings_report_link(self, symbol: str, fiscal_period: str) -> ProviderResult:
        """Resolve a public earnings-hub page; this does not promise an original PDF."""
        self.unavailable("earnings_report_link")

    def earnings_call(self, symbol: str, fiscal_period: str, include_transcript: bool = False,
                      language: str = "page") -> ProviderResult:
        """Resolve an actual native report link, then read its public call page."""
        from .earnings_calls import earnings_call
        if type(include_transcript) is not bool or language not in ("page", "original", "en", "zh-Hans", "zh-Hant"):
            raise ProviderError("invalid_query", "Invalid transcript selection.", 400)
        return earnings_call(self.earnings_report_link(symbol, fiscal_period), include_transcript, language)

    def daily_short_volume(self, symbol: str, limit: int = 20, cursor: Optional[str] = None,
                           start: Optional[str] = None, end: Optional[str] = None) -> ProviderResult:
        """Read daily short-sale share quantities with native pagination; not positions."""
        self.unavailable("daily_short_volume")

    def close(self) -> None:
        pass


class UnavailableProvider(MarketDataProvider):
    """Default provider. It never substitutes cached sample data for a feed."""

    pass


def load_provider(spec: Optional[str]) -> MarketDataProvider:
    """Load a trusted local adapter factory. Never download or execute remote code."""
    if not spec:
        return UnavailableProvider()
    module_name, separator, factory_name = spec.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("Provider spec requires module:factory")
    factory = getattr(importlib.import_module(module_name), factory_name)
    provider = factory()
    if not isinstance(provider, MarketDataProvider):
        raise TypeError("Provider factory must return MarketDataProvider")
    return provider
