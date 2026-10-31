"""Daily short-selling quantity decoders for FTQuote 16.33.17808.

US6699: response 1=result,2=stock,3=items,4=has_more; item1=session
timestamp,2=short quantity,6=total quantity. HK6379 has the same response
envelope; item2=total quantity,4=legacy32-bit short quantity,12=64-bit short
quantity. Other fields include prices and separate position statistics: they
are deliberately not mislabeled as daily short-selling volume.
"""
import datetime as dt

from .wire import fields

MAX_TIMESTAMP = 4102444800


def _integers(parts, known):
    result = {}
    for number, wire, value in parts:
        if number not in known:
            continue
        if wire != 0 or number in result:
            raise ValueError('Invalid or duplicate short-volume scalar field')
        result[number] = value
    return result


def _quantity(value, *, bits=64):
    if value is None or value == (1 << (bits - 1)) - 1:
        return None
    if not isinstance(value, int) or not 0 <= value < (1 << (bits - 1)):
        raise ValueError('Invalid short-volume quantity')
    return value


def parse_daily_short_volume(payload, market):
    if market not in ('US', 'HK'):
        raise ValueError('Daily short volume supports US/HK')
    parts = fields(payload)
    head = _integers(parts, (1,2,4))
    if any(n == 3 and w != 2 for n,w,v in parts):
        raise ValueError('Invalid short-volume record container')
    if 1 not in head:
        raise ValueError('Short-volume response lacks result code')
    code = head[1]
    if code >= 1 << 63:
        code -= 1 << 64
    elif code >= 1 << 31:
        code -= 1 << 32
    result = {'result':code, 'stock_id':str(head[2]) if 2 in head else None,
              'has_more':bool(head.get(4,0)), 'next_cursor':None, 'items':[]}
    if code != 0:
        return result
    if result['stock_id'] is None or head[2] <= 0 or head.get(4,0) not in (0,1):
        raise ValueError('Invalid short-volume identity or pagination')
    previous = None
    for number, wire, body in parts:
        if number != 3 or wire != 2:
            continue
        if len(result['items']) >= 50:
            raise ValueError('Too many daily short-volume records')
        values = _integers(fields(body), range(1,10) if market == 'US' else range(1,13))
        timestamp = values.get(1)
        if timestamp is None or not 0 < timestamp <= MAX_TIMESTAMP:
            raise ValueError('Invalid daily short-volume timestamp')
        if previous is not None and timestamp >= previous:
            raise ValueError('Short-volume records must be strictly newest first')
        previous = timestamp
        if market == 'US':
            short_volume, total_volume = _quantity(values.get(2)), _quantity(values.get(6))
        else:
            short_volume = (_quantity(values[12]) if 12 in values
                            else _quantity(values.get(4),bits=32))
            total_volume = _quantity(values.get(2))
        if short_volume is not None and total_volume is not None and short_volume > total_volume:
            raise ValueError('Short-selling quantity exceeds total quantity')
        ratio = short_volume / total_volume if short_volume is not None and total_volume else None
        # US timestamps represent local trading-date midnight (UTC04/05), so
        # their UTC date is unchanged. HK midnight requires the fixed UTC+8.
        zone = dt.timezone(dt.timedelta(hours=8)) if market == 'HK' else dt.timezone.utc
        result['items'].append({
            'date':dt.datetime.fromtimestamp(timestamp,zone).date().isoformat(),
            'timestamp':timestamp, 'short_volume':short_volume, 'total_volume':total_volume,
            'short_volume_ratio':ratio,
            'short_volume_ratio_percent':ratio * 100 if ratio is not None else None,
            'ratio_basis':'short_volume / total_volume', 'ratio_source':'computed_from_returned_quantities',
            'raw':{str(n):str(v) for n,v in values.items()},
        })
    if result['has_more']:
        if not result['items']:
            raise ValueError('Short-volume continuation lacks a cursor-bearing record')
        result['next_cursor'] = str(result['items'][-1]['timestamp'])
    return result
