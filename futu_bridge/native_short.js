// Read-only daily short-selling volume requests. Requires FTQuote hash gate.
// Only the desktop's two observed protocols are exposed, on channel 1.
var shortPending = null;
var shortLastRequestAt = 0;
function shortFinish(pending, fields) {
    if (shortPending !== pending) return;
    shortPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: pending.protocol, channel: 1,
        request_id: pending.requestId, request: pending.metadata,
        source_kind: 'desktop_session_daily_short_volume_request', readonly: true}, fields));
}
var shortReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
        const pending = shortPending;
        if (!pending || pending.requestId === null) return;
        try {
            if (uint64(args[1].toString()).toString() !== pending.requestId || args[2].toInt32() !== 1) return;
            const response = args[4];
            if (response.isNull()) throw Error('Native short-volume response is null');
            if (response.add(8).readU16() !== pending.protocol) return;
            const size = Number(response.add(0x18).readU64().toString());
            if (!Number.isSafeInteger(size) || size < 0 || size > 2 * 1024 * 1024)
                throw Error('Short-volume response exceeds bounded payload size');
            const body = response.add(0x10).readPointer();
            if (size && body.isNull()) throw Error('Native short-volume body is null');
            shortFinish(pending, {status: 'response_received', transport_error: args[3].toInt32(),
                transport_error_domain: 'f3cnet',
                payload_bytes: size ? Array.from(new Uint8Array(body.readByteArray(size))) : []});
        } catch (error) {
            shortFinish(pending, {status: 'response_error', message: String(error), payload_bytes: []});
        }
    }
});
function shortSend(protocol, bytes, metadata) {
    if (protocol !== 6699 && protocol !== 6379) throw Error('Unsupported short-volume protocol');
    if (shortPending) throw Error('A short-volume request is already pending');
    if (Date.now() - shortLastRequestAt < 500) throw Error('Short-volume requests are limited to two per second');
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1); body.writeByteArray(bytes);
        const data = Memory.alloc(40); data.writeByteArray(new Uint8Array(40)); data.writeU16(protocol);
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length); string.add(24).writeU64(Math.max(15, bytes.length));
        const callback = Memory.alloc(64); callback.writeByteArray(new Uint8Array(64));
        const pending = {resolve: resolve, requestId: null, protocol: protocol, metadata: metadata,
            refs: [body, data, callback], timer: null};
        shortPending = pending;
        pending.timer = setTimeout(function() {
            shortFinish(pending, {status: 'response_timeout', payload_bytes: []});
        }, 8000);
        try {
            shortLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, ptr(0), callback, ptr(0)).toString();
            if (pending.requestId === '0') shortFinish(pending, {status: 'request_rejected', payload_bytes: []});
        } catch (error) {
            shortFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
}
api.shortstatus = function() {
    const module = Process.findModuleByName('FTQuote.dll');
    return {module_path: module ? module.path : null, available: module !== null};
};
api.requestshortvolume = function(stockId, market, options) {
    options = options || {};
    if (typeof stockId !== 'string' || !/^\d+$/.test(stockId) ||
        !Number.isSafeInteger(Number(stockId)) || Number(stockId) <= 0) throw Error('Invalid stock ID');
    if (snapshotUnknown(uint64(stockId))) throw Error('Stock is unknown to the native catalog');
    if (market !== 'US' && market !== 'HK') throw Error('Daily short volume supports US/HK only');
    const limit = options.limit === undefined ? 20 : options.limit;
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) throw Error('Expected count 1..50');
    const cursor = options.cursor === undefined || options.cursor === null ? null : options.cursor;
    if (cursor !== null && (typeof cursor !== 'string' || !/^\d{1,10}$/.test(cursor) ||
        !Number.isSafeInteger(Number(cursor)) || Number(cursor) <= 0 || Number(cursor) > 4102444800)) throw Error('Invalid short-volume cursor');
    let bytes = historyField(1, Number(stockId));
    if (cursor !== null) bytes = bytes.concat(historyField(2, Number(cursor)));
    bytes = bytes.concat(historyField(3, limit));
    return shortSend(market === 'US' ? 6699 : 6379, bytes,
        {stock_id: stockId, market: market, limit: limit, cursor: cursor});
};
