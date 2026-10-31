"""Primary Python interface. HTTP is optional; direct calls share the provider contract."""

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, Sequence, TypeVar, Union

from .provider import EventStream, MarketDataProvider, ProviderError, ProviderEvent, ProviderResult, load_provider
from .samples import DEFAULT_SAMPLE, SampleBars
from .history_bounds import validate_bar_range


T = TypeVar("T")


@dataclass(frozen=True)
class MarketDataResponse(Generic[T]):
    """Consistent response envelope, shared by direct and HTTP API semantics.

    `served_at` records local handling time. Only the provider's source/data
    timestamps describe the market observation. Data schemas are adapter-defined.
    """

    data: T
    source: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    served_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"))

    def to_dict(self) -> Dict[str, Any]:
        return {"request_id": self.request_id, "served_at": self.served_at,
                "data": self.data, "source": self.source, "warnings": self.warnings}

    @classmethod
    def from_provider(cls, result: ProviderResult) -> "MarketDataResponse":
        if not isinstance(result, ProviderResult) or not isinstance(result.source, dict) or not result.source:
            raise ProviderError("provider_contract_error", "The provider returned data without provenance.", 500)
        return cls(result.data, dict(result.source), list(result.warnings))


class MarketDataStream:
    """Closeable subscription. Use with-blocks to reliably release subscriptions."""

    def __init__(self, stream: EventStream):
        if not isinstance(stream.source, dict) or not stream.source:
            stream.close()
            raise ProviderError("provider_contract_error", "The provider subscription lacks provenance.", 500)
        self._stream = stream
        self._closed = False
        self.source = dict(stream.source)

    def read(self, timeout: float = 15.0) -> Optional[ProviderEvent]:
        """Return one event, None on timeout, or raise StopIteration at stream end."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self._closed:
            raise StopIteration
        try:
            event = self._stream.read(timeout)
            if event is not None:
                if not isinstance(event, ProviderEvent) or event.type not in ("quotes", "trades", "orderbook", "heartbeat"):
                    raise ProviderError("provider_contract_error", "Unexpected provider event.", 500)
                MarketDataResponse.from_provider(event.result)
            return event
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            event = self.read()
            if event is not None:
                return event

    def close(self):
        if not self._closed:
            self._closed = True
            self._stream.close()

    def reconnect(self):
        """Re-establish a supported native subscription and report the unknown gap.

        This does not replay all missed exchange events. Read the resulting
        heartbeat/status events before treating subsequent data as continuous.
        """
        if self._closed:
            raise RuntimeError("MarketDataStream is closed")
        reconnect = getattr(self._stream, "reconnect", None)
        if reconnect is None:
            raise ProviderError("reconnect_unsupported", "This stream does not support reconnecting.", 422)
        result = reconnect()
        self.source = dict(self._stream.source)
        return result

    def status(self):
        """Return native delivery, deduplication, and gap counters when available."""
        status = getattr(self._stream, "status", None)
        return status() if status else {"closed": self._closed, "source": dict(self.source)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("Provide a nonempty native symbol of at most 128 characters")
    return value.strip()


def _symbols(values: Union[str, Sequence[str]]) -> List[str]:
    if isinstance(values, str):
        values = [values]
    result = list(dict.fromkeys(_symbol(value) for value in values))
    if not 1 <= len(result) <= 100:
        raise ValueError("Provide between 1 and 100 symbols")
    return result


def _date(value: Optional[str], date_only: bool = False):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Dates must be ISO-formatted strings")
    try:
        if date_only or len(value) == 10:
            return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), dt.timezone.utc)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamp timezone is required")
        return parsed
    except ValueError as exc:
        raise ValueError("Dates must be ISO dates or timezone-aware timestamps") from exc


def _validate_information_range(start: Optional[str], end: Optional[str], *, calendar: bool = False):
    """Validate information bounds without turning market dates into instants."""
    if calendar:
        for value in (start, end):
            if value is not None:
                if not isinstance(value, str) or len(value) != 10:
                    raise ValueError("Calendar bounds must be dates in YYYY-MM-DD form; timestamps are unsupported")
                parsed = _date(value, date_only=True)
                if parsed.date().isoformat() != value:
                    raise ValueError("Calendar bounds must be dates in YYYY-MM-DD form; timestamps are unsupported")
    first, last = _date(start), _date(end)
    if last is not None and len(end) == 10:
        # News date-only ends include that UTC day. replace also avoids overflow
        # for 9999-12-31; the native provider enforces its narrower supported years.
        last = last.replace(hour=23, minute=59, second=59, microsecond=999999)
    if first is not None and last is not None and first > last:
        raise ValueError("start must not follow end")


class FutuClient:
    """Read market data through a connected, explicitly selected provider.

    Symbols are provider-native identifiers for stocks, options, bonds, forex,
    or futures. Acceptance of a symbol does not imply that the adapter or Futu
    subscription supports its asset class. Consult capabilities and error details.
    """

    def __init__(self, provider: Union[MarketDataProvider, str, None] = None):
        self.provider = load_provider(provider) if provider is None or isinstance(provider, str) else provider
        if not isinstance(self.provider, MarketDataProvider):
            raise TypeError("provider must be a MarketDataProvider or module:factory string")
        self._closed = False
        self._streams: List[MarketDataStream] = []

    @classmethod
    def desktop(cls, pid=None, catalog_path=None, poll_interval=1.0,
                stream_mode="push", push_queue_size=1024):
        """Connect explicitly to the validated running desktop; native import is lazy."""
        from .native import NativeProvider
        return cls(NativeProvider(pid=pid, catalog_path=catalog_path, poll_interval=poll_interval,
                                 stream_mode=stream_mode, push_queue_size=push_queue_size))

    def _check_open(self):
        if self._closed:
            raise RuntimeError("FutuClient is closed")

    def capabilities(self) -> Dict[str, Any]:
        self._check_open()
        return self.provider.capabilities()

    def quotes(self, symbols: Union[str, Sequence[str]]) -> MarketDataResponse:
        self._check_open()
        return MarketDataResponse.from_provider(self.provider.quotes(_symbols(symbols)))

    def search_symbols(self, query: str, limit: int = 25) -> MarketDataResponse:
        """Search the public security catalog; results do not establish data coverage."""
        self._check_open()
        if not isinstance(query, str) or len(query) > 128 or any(ord(char) < 32 for char in query):
            raise ValueError("query must be a string of at most 128 characters without control characters")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        return MarketDataResponse.from_provider(self.provider.search_symbols(query, limit))

    def resolve_symbol(self, symbol: str) -> MarketDataResponse:
        """Resolve a native identifier to a security ID without claiming a market feed."""
        self._check_open()
        return MarketDataResponse.from_provider(self.provider.resolve_symbol(_symbol(symbol)))

    def bars(self, symbol: str, timeframe: str = "1d", start: Optional[str] = None,
             end: Optional[str] = None, adjustment: str = "none",
             session: str = "provider_default") -> MarketDataResponse:
        """Request provider bars, with optional explicit US intraday sessions.

        session: provider_default, regular, premarket, postmarket, extended
        (pre+regular+post), overnight, or all. Explicit sessions use native
        section IDs; their observed mapping is build-bound and fails on unknown
        sections. Other instrument classes retain provider_default support.
        """
        from .sessions import validate_session
        self._check_open()
        symbol = _symbol(symbol)
        if adjustment not in ("none", "forward", "backward"):
            raise ValueError("adjustment must be none, forward, or backward")
        if timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo", "1Day"):
            raise ValueError("Unsupported timeframe syntax")
        validate_session(session, timeframe)
        validate_bar_range(start, end)
        arguments = (symbol, timeframe, start, end, adjustment)
        return MarketDataResponse.from_provider(self.provider.bars(*arguments) if session == "provider_default" else
                                                self.provider.bars(*arguments, session=session))

    def option_chain(self, underlying: str, expiry: Optional[str] = None) -> MarketDataResponse:
        self._check_open()
        _date(expiry, date_only=True)
        return MarketDataResponse.from_provider(self.provider.option_chain(_symbol(underlying), expiry))

    def technical_indicators(self, symbol: str, timeframe: str = "1d", start: Optional[str] = None,
                             end: Optional[str] = None, adjustment: str = "none",
                             indicators: Optional[Sequence[str]] = None,
                             session: str = "provider_default") -> MarketDataResponse:
        """Fetch bars once and calculate trailing indicators locally.

        Families: SMA(5/10/20/60), EMA(12/26), MACD(12/26/9), RSI(6/12/24),
        KDJ(9/3/3), Bollinger(20/2), ATR(14), OBV. Names are case-insensitive;
        MA and BOLL alias SMA and Bollinger. Inspect formula, seed, warm-up and
        underlying provenance metadata; this does not retrieve Futu chart values.
        No extra prehistory is loaded. Missing volume makes OBV unavailable.
        """
        from .indicators import validate_selection
        from .sessions import validate_session
        self._check_open()
        symbol = _symbol(symbol)
        selected = validate_selection(indicators)
        if adjustment not in ("none", "forward", "backward"):
            raise ValueError("adjustment must be none, forward, or backward")
        if timeframe not in ("1m", "3m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo", "1Day"):
            raise ValueError("Unsupported timeframe syntax")
        validate_session(session, timeframe)
        validate_bar_range(start, end)
        arguments = (symbol, timeframe, start, end, adjustment, selected)
        return MarketDataResponse.from_provider(self.provider.technical_indicators(*arguments) if session == "provider_default" else
                                                self.provider.technical_indicators(*arguments, session=session))

    def orderbook(self, symbol: str, depth: int = 10) -> MarketDataResponse:
        self._check_open()
        if type(depth) is not int or not 1 <= depth <= 100:
            raise ValueError("depth must be an integer between 1 and 100")
        return MarketDataResponse.from_provider(self.provider.orderbook(_symbol(symbol), depth))

    def trades(self, symbol: str, limit: int = 100) -> MarketDataResponse:
        self._check_open()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        return MarketDataResponse.from_provider(self.provider.trades(_symbol(symbol), limit))

    def stock_news(self, symbol: str, limit: int = 20, cursor: Optional[str] = None,
                   start: Optional[str] = None, end: Optional[str] = None) -> MarketDataResponse:
        """Read native stock news with an opaque continuation cursor.

        Dates may be ISO dates or timezone-aware timestamps. The provider's source
        and warnings describe coverage and date boundaries; publication time alone
        does not establish a point-in-time backtesting archive.
        """
        self._check_open()
        symbol = _symbol(symbol)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()
                                   or len(cursor) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
            raise ValueError("cursor must be a nonempty native string of at most 2048 characters without control characters")
        _validate_information_range(start, end)
        return MarketDataResponse.from_provider(self.provider.stock_news(symbol, limit, cursor, start, end))

    def earnings_calendar(self, symbol: str, start: Optional[str] = None,
                          end: Optional[str] = None) -> MarketDataResponse:
        """Read the provider's reporting schedule when available; no alert is created.

        Bounds are inclusive YYYY-MM-DD market dates. Timestamps are unsupported
        because the native calendar does not identify an instant within the day.
        Inspect metadata for confirmed versus estimated dates and coverage.
        """
        self._check_open()
        symbol = _symbol(symbol)
        _validate_information_range(start, end, calendar=True)
        return MarketDataResponse.from_provider(self.provider.earnings_calendar(symbol, start, end))

    def earnings_report_link(self, symbol: str, fiscal_period: str) -> MarketDataResponse:
        """Resolve Futu's earnings-hub page for a provider fiscal label YYYYQ1..4.

        The page is distinct from an issuer PDF. The fiscal and URL periods can
        differ; consult the returned labels and provenance before combining data.
        """
        self._check_open()
        if not isinstance(fiscal_period, str) or not re.fullmatch(r"(19[7-9]\d|20\d\d)Q[1-4]", fiscal_period):
            raise ValueError("fiscal_period must be YYYYQ1 through YYYYQ4, for years 1970..2099")
        return MarketDataResponse.from_provider(self.provider.earnings_report_link(_symbol(symbol), fiscal_period))

    def earnings_call(self, symbol: str, fiscal_period: str, include_transcript: bool = False,
                      language: str = "page") -> MarketDataResponse:
        """Read historical recording links and optionally an unverified AI transcript.

        language='page' returns untimed public-page text when present. Other
        selections request subtitle files; an unavailable file is not substituted.
        """
        self._check_open()
        if not isinstance(fiscal_period, str) or not re.fullmatch(r"(19[7-9]\d|20\d\d)Q[1-4]", fiscal_period):
            raise ValueError("fiscal_period must be YYYYQ1 through YYYYQ4, for years 1970..2099")
        if type(include_transcript) is not bool or language not in ("page", "original", "en", "zh-Hans", "zh-Hant"):
            raise ValueError("include_transcript must be boolean; language must be page, original, en, zh-Hans, or zh-Hant")
        return MarketDataResponse.from_provider(self.provider.earnings_call(
            _symbol(symbol), fiscal_period, include_transcript, language))

    def daily_short_volume(self, symbol: str, limit: int = 20, cursor: Optional[str] = None,
                           start: Optional[str] = None, end: Optional[str] = None) -> MarketDataResponse:
        """Read a page of daily short-sale shares and ratios for US/HK stocks.

        Date bounds filter the downloaded page; follow its native cursor for
        older records. Daily trading flow does not measure outstanding positions.
        """
        self._check_open()
        symbol = _symbol(symbol)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer between 1 and 50")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.isascii() or
                                   not cursor.isdigit() or len(cursor) > 10 or not 0 < int(cursor) <= 4102444800):
            raise ValueError("cursor must be the numeric string returned by a previous short-volume page")
        _validate_information_range(start, end, calendar=True)
        return MarketDataResponse.from_provider(self.provider.daily_short_volume(symbol, limit, cursor, start, end))

    def stream(self, symbols: Union[str, Sequence[str]],
               types: Sequence[str] = ("quotes", "trades", "orderbook")) -> MarketDataStream:
        self._check_open()
        if isinstance(types, str):
            raise ValueError("types must be a sequence such as ['quotes', 'trades']")
        kinds = list(dict.fromkeys(types))
        if not kinds or not set(kinds).issubset({"quotes", "trades", "orderbook"}):
            raise ValueError("types must contain quotes, trades, and/or orderbook")
        subscription = MarketDataStream(self.provider.stream(_symbols(symbols), kinds))
        self._streams = [existing for existing in self._streams if not existing._closed]
        self._streams.append(subscription)
        return subscription

    @staticmethod
    def sample_bars(symbol: str = "AAPL", timeframe: str = "1d", start: Optional[str] = None,
                    end: Optional[str] = None, sample_path=DEFAULT_SAMPLE) -> MarketDataResponse:
        """Explicit offline sample access, independent of live provider availability."""
        validate_bar_range(start, end)
        return MarketDataResponse.from_provider(SampleBars(sample_path).bars(_symbol(symbol), timeframe, start, end))

    def close(self):
        if not self._closed:
            self._closed = True
            first_error = None
            for subscription in list(self._streams):
                try:
                    subscription.close()
                except Exception as exc:
                    first_error = first_error or exc
            try:
                self.provider.close()
            except Exception as exc:
                first_error = first_error or exc
            self._streams.clear()
            if first_error is not None:
                raise first_error

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *exc):
        self.close()
