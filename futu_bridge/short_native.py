"""Read-only daily short-selling volume integration for the native provider."""
import datetime as dt
import re
import time

from .provider import ProviderError, ProviderResult
from .short_wire import MAX_TIMESTAMP, parse_daily_short_volume

SHORT_MODULE_SHA256 = '899db0ccb14c1f4d818db87f976d4a0dd3994ba751687ed4d72e04c51e72bb33'


def _date_bound(value):
    if value is None:
        return None
    if not isinstance(value,str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) is None:
        raise ValueError('Expected YYYY-MM-DD')
    parsed = dt.date.fromisoformat(value)
    if not dt.date(1970,1,1) <= parsed <= dt.date(2100,1,1):
        raise ValueError('Date outside supported range')
    return parsed.isoformat()


class NativeShortVolumeMixin:
    def daily_short_volume(self, symbol, limit=20, cursor=None, start=None, end=None):
        if not self.native_info.get('short_volume_available'):
            raise ProviderError('short_volume_unavailable', 'The short-selling module does not match the verified desktop build.',503)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ProviderError('invalid_query','Daily short-volume limit must be an integer from 1 to 50.',400)
        if cursor is not None and (not isinstance(cursor,str) or re.fullmatch(r'\d{1,10}',cursor) is None or
                                   not 0 < int(cursor) <= MAX_TIMESTAMP):
            raise ProviderError('invalid_query','Use the numeric cursor returned by a previous short-volume page.',400)
        try:
            lower, upper = _date_bound(start), _date_bound(end)
        except ValueError as exc:
            raise ProviderError('invalid_query','Daily short-volume bounds must be YYYY-MM-DD dates from 1970 to 2100.',400) from exc
        if lower and upper and lower > upper:
            raise ProviderError('invalid_query','Start must not follow end.',400)
        record = self._resolve(symbol)
        cs_market = record.get('cs_market')
        market = 'US' if cs_market == 2 else 'HK' if cs_market == 1 else None
        if market is None:
            raise ProviderError('short_volume_market_unsupported','Daily short-selling data is verified for US and Hong Kong equities only.',422)
        with self._short_lock:
            delay = 0.52 - (time.monotonic() - self._short_last_request)
            if delay > 0:
                time.sleep(delay)
            requested = self._rpc('requestshortvolume',record['stock_id'],market,{'limit':limit,'cursor':cursor})
            self._short_last_request = time.monotonic()
        if requested.get('status') != 'response_received' or requested.get('transport_error') != 0:
            raise ProviderError('short_volume_request_failed','The desktop daily short-selling request failed.',503,
                                {key:requested.get(key) for key in ('status','request_id','transport_error')})
        try:
            parsed = parse_daily_short_volume(bytes(requested.get('payload_bytes',[])),market)
        except (ValueError,TypeError,OverflowError,OSError) as exc:
            raise ProviderError('short_volume_decode_failed','The daily short-selling response could not be validated.',502) from exc
        if parsed['result'] != 0:
            raise ProviderError('short_volume_provider_error','Futu rejected the daily short-selling request.',422,
                                {'result_code':parsed['result']})
        if parsed['stock_id'] != str(record['stock_id']) or len(parsed['items']) > limit:
            raise ProviderError('native_result_invalid','Daily short-selling identity or count differs from the request.',502)
        if cursor is not None and any(item['timestamp'] >= int(cursor) for item in parsed['items']):
            raise ProviderError('native_result_invalid','Daily short-selling continuation did not advance.',502)
        returned_count = len(parsed['items'])
        parsed['items'] = [item for item in parsed['items'] if (lower is None or item['date'] >= lower)
                           and (upper is None or item['date'] <= upper)]
        parsed.update(symbol=record['symbol'],market=market,count=len(parsed['items']),returned_count=returned_count,
                      filtered_out_count=returned_count-len(parsed['items']),requested_limit=limit,requested_cursor=cursor,
                      start=start,end=end,date_filter='local_filter_of_this_page',complete_history=False,
                      frequency='daily',quantity_unit='shares',no_more_data=not parsed['has_more'])
        source = self._source('daily_short_volume')
        source.update(protocol=6699 if market == 'US' else 6379,request_id=requested.get('request_id'),
                      kind='daily_short_volume',short_module_sha256=SHORT_MODULE_SHA256,live_push=False,
                      entitlement='server-enforced desktop-session permissions',
                      description='Daily short-selling statistics requested through the running Futu desktop session.',
                      coverage=('NYSE and Nasdaq and their affiliates, as supplied by Futu; not an all-venue short-sale total.'
                                if market == 'US' else 'HKEX daily short-selling data, as supplied by Futu.'),
                      definition_url='https://www.futuhk.com/en/support/topic1_535')
        warnings = ['Daily short-selling volume is a trading flow, not outstanding short interest or open short positions.',
                    'The ratio is computed from the returned share quantities; it is not an amount/turnover ratio.',
                    'Date bounds filter this page. Follow next_cursor to retrieve older pages; complete historical coverage is unverified.',
                    'Session dates are not publication or first-availability timestamps; point-in-time backtest availability is unverified.']
        if market == 'US':
            warnings.append('US short volume covers the NYSE/Nasdaq groups supplied by Futu and must not be treated as consolidated all-venue short selling.')
        return ProviderResult(parsed,source,warnings)
