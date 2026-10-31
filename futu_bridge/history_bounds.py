"""Validate historical bounds without treating a market date as UTC midnight."""
import datetime as dt
import re


def parse_bar_bound(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Dates must be ISO dates or timezone-aware timestamps')
    try:
        if len(value) == 10:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Expected YYYY-MM-DD')
            return dt.date.fromisoformat(value)
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError('Timestamp timezone is required')
        return result
    except (ValueError, OverflowError) as error:
        raise ValueError('Dates must be ISO dates or timezone-aware timestamps') from error


def validate_bar_range(start, end):
    """Reject reversed ranges only when their ordering is unambiguous.

    Date-only bounds select native market dates. The routing layer does not
    know a provider's market timezone, so mixed bounds use the widest possible
    UTC envelope for civil UTC offsets (-14 through +14). Exact filtering
    remains with the provider's native market dates and timestamps.
    """
    first, last = parse_bar_bound(start), parse_bar_bound(end)
    if first is None or last is None:
        return
    first_instant = isinstance(first, dt.datetime)
    last_instant = isinstance(last, dt.datetime)
    if first_instant == last_instant:
        reversed_range = first > last
    else:
        # Use numeric ordinals, avoiding datetime overflow at years1/9999.
        day = 86_400_000_000
        hour = 3_600_000_000
        def microseconds(value):
            offset = value.utcoffset()
            offset_us = offset.days * day + offset.seconds * 1_000_000 + offset.microseconds
            return (value.date().toordinal() * day + value.hour * hour + value.minute * 60_000_000
                    + value.second * 1_000_000 + value.microsecond - offset_us)
        lower = microseconds(first) if first_instant else first.toordinal() * day - 14 * hour
        upper = microseconds(last) if last_instant else (last.toordinal() + 1) * day + 14 * hour
        reversed_range = lower > upper or (not last_instant and lower == upper)
    if reversed_range:
        raise ValueError('start must not follow end')
