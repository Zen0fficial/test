"""Read-only, bounded native financial-calendar acquisition and reconciliation."""
import datetime as dt
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar_wire import event_key, parse_earnings_calendar
from .report_wire import parse_report_link
from .provider import ProviderError, ProviderResult


FTQUOTE_SHA256 = "899db0ccb14c1f4d818db87f976d4a0dd3994ba751687ed4d72e04c51e72bb33"
MAX_CALENDAR_PAGES = 50


def _bound(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Calendar bounds must be YYYY-MM-DD dates; timestamps are not accepted")
    result = dt.date.fromisoformat(value)
    if not 1970 <= result.year <= 2099:
        raise ValueError("Calendar dates must be between 1970 and 2099")
    return result


class NativeCalendarMixin:
    """Requires _calendar_lock/_calendar_last_request initialized by the adapter."""

    def earnings_report_link(self, symbol, fiscal_period):
        if not self.native_info.get("report_link_available"):
            raise ProviderError("report_unavailable", "The reporting module does not match the verified desktop build.", 503)
        record = self._resolve(symbol)
        if record.get("cs_market") not in (1, 2) or record.get("instrument_type_v2") != 3:
            raise ProviderError("report_market_unsupported", "Reporting links are verified for US and Hong Kong stocks.", 422)
        if not isinstance(fiscal_period, str) or not re.fullmatch(r"(19[7-9]\d|20\d\d)Q[1-4]", fiscal_period):
            raise ProviderError("invalid_query", "fiscal_period must be YYYYQ1 through YYYYQ4, for years 1970..2099.", 400)
        with self._calendar_lock:
            delay = .52 - (time.monotonic() - self._calendar_last_request)
            if delay > 0:
                time.sleep(delay)
            response = self._rpc("requestreportlink", record["stock_id"], fiscal_period)
            self._calendar_last_request = time.monotonic()
        if response.get("status") != "response_received" or response.get("transport_error") != 0:
            raise ProviderError("report_request_failed", "The report-link request did not return successfully.", 503)
        try:
            parsed = parse_report_link(bytes(response.get("payload_bytes", [])))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ProviderError("report_decode_failed", "The report-link response could not be validated.", 502) from exc
        if parsed["result"] != 0:
            raise ProviderError("report_provider_error", "Futu rejected the report-link request.", 422, {"result_code": parsed["result"]})
        item = parsed["items"][0] if parsed["items"] else None
        if item and (item["stock_id"] != record["stock_id"] or item["fiscal_period"] != fiscal_period):
            raise ProviderError("native_result_invalid", "Report response differs from the requested security or fiscal period.", 502)
        data = {"symbol": record["symbol"], "stock_id": record["stock_id"], "fiscal_period": fiscal_period,
                "available": bool(item and item["url"]), "report": item, "content_type": "earnings_hub_web_page",
                "pdf_available": None, "complete_history": False}
        source = self._source("earnings_report_link")
        source.update(protocol=20846, calendar_module_sha256=FTQUOTE_SHA256,
                      request_id=response.get("request_id"), live_push=False, content="public_report_page_metadata")
        warnings = ["This is Futu's earnings hub page; it is not an original quarterly/annual report PDF or an official transcript.",
                    "Fiscal period and URL period can differ; both provider values are preserved without inferring a reporting-date conversion.",
                    "Only public report identity parameters are retained in the URL; personalized client parameters are removed."]
        return ProviderResult(data, source, warnings)

    def earnings_calendar(self, symbol, start=None, end=None):
        if not self.native_info.get("calendar_available"):
            raise ProviderError("calendar_unavailable", "The calendar module does not match the verified desktop build.", 503)
        record = self._resolve(symbol)
        market = record.get("cs_market")
        if market not in (1, 2) or record.get("instrument_type_v2") != 3:
            raise ProviderError("calendar_market_unsupported", "Reporting calendar is verified for US and Hong Kong stocks.", 422)
        timezone = "Asia/Hong_Kong" if market == 1 else "America/New_York"
        try:
            # Windows may require the tzdata dependency. Explicit bounds do not.
            if start is None:
                lower = dt.datetime.now(ZoneInfo(timezone)).date()
            else:
                lower = _bound(start)
            upper = _bound(end) if end is not None else lower + dt.timedelta(days=62)
            if not 0 <= (upper - lower).days <= 92 or upper.year > 2099:
                raise ValueError("Calendar range must contain between 1 and 93 inclusive dates")
        except ZoneInfoNotFoundError as exc:
            raise ProviderError("timezone_unavailable", "Install tzdata or supply explicit start and end calendar dates.", 503) from exc
        except (ValueError, OverflowError) as exc:
            raise ProviderError("invalid_query", str(exc), 400) from exc
        daily = {lower + dt.timedelta(days=i): {"date": (lower + dt.timedelta(days=i)).isoformat(),
                 "total_events": None, "downloaded_events": 0, "matching_events": 0,
                 "count_changed": False, "pagination_overlap": False}
                 for i in range((upper - lower).days + 1)}
        matches, seen, requests = [], set(), []
        page_limit_reached = False
        chunk_start = lower
        with self._calendar_lock:
            while chunk_start <= upper:
                chunk_end = min(upper, chunk_start + dt.timedelta(days=6))
                offset = 0
                while True:
                    if len(requests) >= MAX_CALENDAR_PAGES:
                        page_limit_reached = True
                        break
                    delay = 0.52 - (time.monotonic() - self._calendar_last_request)
                    if delay > 0:
                        time.sleep(delay)
                    response = self._rpc("requestcalendar", market, chunk_start.strftime("%Y%m%d"),
                                         chunk_end.strftime("%Y%m%d"), offset)
                    self._calendar_last_request = time.monotonic()
                    if response.get("status") != "response_received" or response.get("transport_error") != 0:
                        raise ProviderError("calendar_request_failed", "The native calendar request did not return successfully.", 503,
                                            {k: response.get(k) for k in ("status", "request_id", "transport_error")})
                    try:
                        parsed = parse_earnings_calendar(bytes(response.get("payload_bytes", [])))
                    except (TypeError, ValueError, OverflowError, OSError) as exc:
                        raise ProviderError("calendar_decode_failed", "The calendar response could not be validated.", 502) from exc
                    if parsed["result"] != 0:
                        raise ProviderError("calendar_provider_error", "Futu rejected the financial-calendar request.", 422,
                                            {"result_code": parsed["result"]})
                    requests.append({"request_id": response.get("request_id"), "start": chunk_start.isoformat(),
                                     "end": chunk_end.isoformat(), "offset": offset, "returned_count": parsed["returned_count"]})
                    overlap = False
                    for group in parsed["groups"]:
                        date = dt.date.fromisoformat(group["date"])
                        if not chunk_start <= date <= chunk_end:
                            raise ProviderError("native_result_invalid", "Calendar returned a date outside its requested window.", 502)
                        coverage = daily[date]
                        if coverage["total_events"] is not None and coverage["total_events"] != group["total_events"]:
                            coverage["count_changed"] = True
                        coverage["total_events"] = group["total_events"]
                        for item in group["items"]:
                            key = event_key(item)
                            if key in seen:
                                coverage["pagination_overlap"] = overlap = True
                                continue
                            seen.add(key)
                            coverage["downloaded_events"] += 1
                            if item["stock_id"] == record["stock_id"]:
                                coverage["matching_events"] += 1
                                matches.append(dict(item, symbol=record["symbol"]))
                    if parsed["returned_count"] < 1000 or overlap:
                        break
                    offset += 1000
                if page_limit_reached:
                    break
                chunk_start = chunk_end + dt.timedelta(days=1)
        coverage = list(daily.values())
        for item in coverage:
            item["complete"] = (item["total_events"] is not None and
                                item["downloaded_events"] == item["total_events"] and
                                not item["count_changed"] and not item["pagination_overlap"])
        complete = all(item["complete"] for item in coverage)
        matches.sort(key=lambda item: event_key(item)[:2])
        source = self._source("earnings_calendar")
        source.update(protocol=20024, calendar_module_sha256=FTQUOTE_SHA256,
                      requests=requests, live_push=False, content="reporting_schedule_metadata")
        warnings = ["Dates and fiscal labels are reported by Futu; confirmed versus estimated status and the separate timestamp's meaning are unverified.",
                    "The current calendar is not a point-in-time historical archive and does not establish what was known before a report.",
                    "This reads public reporting schedules and does not read or change personalized reminder subscriptions."]
        if not complete:
            warnings.append("Calendar coverage is incomplete for some dates; an absent stock event on those dates is unknown, not proof that no report exists.")
        data = {"symbol": record["symbol"], "stock_id": record["stock_id"], "start": lower.isoformat(), "end": upper.isoformat(),
                "end_inclusive": True, "date_basis": "provider_calendar_date", "market_timezone": timezone,
                "events": matches, "count": len(matches), "complete_for_native_selection": complete,
                "complete_history": False, "page_limit_reached": page_limit_reached,
                "coverage": coverage, "provider_selection": {"native_market": market, "native_event_types": [2, 1, 4],
                    "stock_filter": "local_exact_stock_id", "page_size": 1000, "max_pages": MAX_CALENDAR_PAGES}}
        return ProviderResult(data, source, warnings)
