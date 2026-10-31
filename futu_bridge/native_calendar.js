// Read-only financial-calendar list requests. Load after native_history.js and
// native_snapshot.js, independently of FTNews. The loader hash-gates FTQuote.
var calendarPending = null;
var calendarLastRequestAt = 0;

function calendarFinish(pending, result) {
    if (calendarPending !== pending) return;
    calendarPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: 20024, channel: 1,
        request_id: pending.requestId, request: pending.metadata,
        source_kind: 'desktop_session_financial_calendar_request', readonly: true}, result));
}

var calendarReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
        const pending = calendarPending;
        if (!pending || pending.requestId === null) return;
        try {
            if (uint64(args[1].toString()).toString() !== pending.requestId || args[2].toInt32() !== 1) return;
            const response = args[4];
            if (response.isNull()) throw Error('Native calendar response is null');
            if (response.add(8).readU16() !== 20024) return;
            const size = Number(response.add(0x18).readU64().toString());
            if (!Number.isSafeInteger(size) || size < 0 || size > 2 * 1024 * 1024)
                throw Error('Calendar response exceeds bounded size');
            const body = response.add(0x10).readPointer();
            if (size && body.isNull()) throw Error('Native calendar response body is null');
            calendarFinish(pending, {status: 'response_received', transport_error: args[3].toInt32(),
                transport_error_domain: 'f3cnet',
                payload_bytes: size ? Array.from(new Uint8Array(body.readByteArray(size))) : []});
        } catch (error) {
            calendarFinish(pending, {status: 'response_error', message: String(error), payload_bytes: []});
        }
    }
});

function calendarDate(value) {
    if (typeof value !== 'string' || !/^\d{8}$/.test(value)) throw Error('Expected YYYYMMDD calendar date');
    const year = Number(value.slice(0, 4)), month = Number(value.slice(4, 6)), day = Number(value.slice(6));
    const time = Date.UTC(year, month - 1, day), check = new Date(time);
    if (year < 1970 || year > 2099 || check.getUTCFullYear() !== year ||
        check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) throw Error('Invalid calendar date');
    return time;
}

api.calendarstatus = function() {
    const module = Process.findModuleByName('FTQuote.dll');
    return {module_path: module ? module.path : null, available: module !== null, protocol: 20024};
};

api.requestcalendar = function(market, start, end, offset) {
    if (market !== 1 && market !== 2) throw Error('Verified calendar markets are HK=1 and US=2');
    const duration = calendarDate(end) - calendarDate(start);
    if (duration < 0 || duration > 6 * 86400000) throw Error('Calendar request is limited to seven inclusive dates');
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > 49000 || offset % 1000 !== 0)
        throw Error('Invalid bounded calendar pagination offset');
    if (calendarPending) throw Error('A calendar request is already pending');
    if (Date.now() - calendarLastRequestAt < 500) throw Error('Calendar requests are limited to two per second');
    let bytes = historyField(1, market).concat(historyField(4, offset), historyField(5, 1000));
    bytes = bytes.concat(snapshotMessage(6, Array.from(start).map(c => c.charCodeAt(0))),
        snapshotMessage(13, Array.from(end).map(c => c.charCodeAt(0))));
    // CEarningsListViewService normal desktop defaults; no guessed stock filters.
    for (const eventType of [2, 1, 4]) bytes = bytes.concat(historyField(8, eventType));
    let selector = [];
    for (const field of [1,2,3,4,5,6,7,8,9,10,11,12,13,15]) selector = selector.concat(historyField(field, 1));
    bytes = bytes.concat(snapshotMessage(101, selector));
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1); body.writeByteArray(bytes);
        const data = Memory.alloc(40); data.writeByteArray(new Uint8Array(40)); data.writeU16(20024);
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length); string.add(24).writeU64(Math.max(15, bytes.length));
        const callback = Memory.alloc(64); callback.writeByteArray(new Uint8Array(64));
        const pending = {resolve: resolve, requestId: null, refs: [body, data, callback], timer: null,
            metadata: {native_market: market, start: start, end: end, end_inclusive: true,
                offset: offset, count: 1000, native_event_types: [2,1,4]}};
        calendarPending = pending;
        pending.timer = setTimeout(function() {calendarFinish(pending, {status: 'response_timeout', payload_bytes: []});}, 8000);
        try {
            calendarLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, ptr(0), callback, ptr(0)).toString();
            if (pending.requestId === '0') calendarFinish(pending, {status: 'request_rejected', payload_bytes: []});
        } catch (error) {
            calendarFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
};
