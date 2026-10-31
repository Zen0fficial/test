// Read-only stock news through the desktop's normal channel-1 request path.
// Load after native_history.js/native_snapshot.js. The loader must additionally
// hash-gate FTNews.dll before calling these APIs. No news/account state is changed.
var newsPending = null;
var newsLastRequestAt = 0;

function newsFinish(pending, fields) {
    if (newsPending !== pending) return;
    newsPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: pending.protocol, channel: 1,
        request_id: pending.requestId, request: pending.metadata,
        source_kind: 'desktop_session_stock_news_request', readonly: true}, fields));
}

var newsReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
        const pending = newsPending;
        if (!pending || pending.requestId === null) return;
        try {
            if (uint64(args[1].toString()).toString() !== pending.requestId || args[2].toInt32() !== 1) return;
            const response = args[4];
            if (response.isNull()) throw Error('Native news response is null');
            if (response.add(8).readU16() !== pending.protocol) return;
            const size = Number(response.add(0x18).readU64().toString());
            if (!Number.isSafeInteger(size) || size < 0 || size > 2 * 1024 * 1024)
                throw Error('News response exceeds the bounded payload size');
            const body = response.add(0x10).readPointer();
            if (size && body.isNull()) throw Error('Native news response body is null');
            newsFinish(pending, {status: 'response_received', transport_error: args[3].toInt32(),
                transport_error_domain: 'f3cnet',
                payload_bytes: size ? Array.from(new Uint8Array(body.readByteArray(size))) : []});
        } catch (error) {
            newsFinish(pending, {status: 'response_error', message: String(error), payload_bytes: []});
        }
    }
});

function newsString(number, value) {
    // UTF-8 via the runtime, avoiding unsafe manual Unicode conversion.
    const encoded = unescape(encodeURIComponent(value));
    const bytes = [];
    for (let index = 0; index < encoded.length; index++) bytes.push(encoded.charCodeAt(index));
    return snapshotMessage(number, bytes);
}

function newsSend(protocol, bytes, metadata) {
    if (protocol !== 7912) throw Error('Unsupported stock news protocol');
    if (newsPending) throw Error('A news request is already pending');
    if (Date.now() - newsLastRequestAt < 500) throw Error('News requests are limited to two per second');
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1); body.writeByteArray(bytes);
        const data = Memory.alloc(40); data.writeByteArray(new Uint8Array(40)); data.writeU16(protocol);
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length); string.add(24).writeU64(Math.max(15, bytes.length));
        const callback = Memory.alloc(64); callback.writeByteArray(new Uint8Array(64));
        const pending = {resolve: resolve, requestId: null, protocol: protocol, metadata: metadata,
            refs: [body, data, callback], timer: null};
        newsPending = pending;
        pending.timer = setTimeout(function() {
            newsFinish(pending, {status: 'response_timeout', payload_bytes: []});
        }, 8000);
        try {
            newsLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, ptr(0), callback, ptr(0)).toString();
            if (pending.requestId === '0') newsFinish(pending, {status: 'request_rejected', payload_bytes: []});
        } catch (error) {
            newsFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
}

api.newsstatus = function() {
    const module = Process.findModuleByName('FTNews.dll');
    return {module_path: module ? module.path : null, available: module !== null};
};

api.requestnews = function(stockId, options) {
    options = options || {};
    if (typeof stockId !== 'string' || !/^\d+$/.test(stockId) ||
        !Number.isSafeInteger(Number(stockId)) || Number(stockId) <= 0) throw Error('Invalid stock ID');
    if (snapshotUnknown(uint64(stockId))) throw Error('Stock is unknown to the native catalog');
    const limit = options.limit === undefined ? 20 : options.limit;
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw Error('Expected news count 1..100');
    const cursor = options.cursor === undefined || options.cursor === null ? '' : options.cursor;
    if (typeof cursor !== 'string' || cursor.length > 2048 || /[\x00-\x1f\x7f]/.test(cursor) ||
        (cursor.length > 0 && !cursor.trim())) throw Error('Invalid news cursor');
    const start = options.start_time === undefined || options.start_time === null ? 0 : options.start_time;
    const end = options.end_time === undefined || options.end_time === null ? 0 : options.end_time;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < 0 ||
        start > 4102444800 || end > 4102444800 || (start && (!end || end < start)))
        throw Error('Expected a valid Unix-second news range');
    // FTNews 0x100b40/0x101050 calls this normal native boolean getter for field9.
    // Its meaning is deliberately not renamed to an unverified permission/language.
    const module = Process.getModuleByName('FTNews.dll');
    const nativeFlag = new NativeFunction(module.base.add(0x69c80), 'uint', [])();
    if (nativeFlag !== 0 && nativeFlag !== 1) throw Error('Unknown native news flag');
    let bytes = [];
    for (const [number, value] of Object.entries({1:Number(stockId), 2:start, 3:end, 4:limit, 5:1, 6:0, 9:nativeFlag}))
        bytes = bytes.concat(historyField(Number(number), value));
    bytes = bytes.concat(newsString(10, cursor));
    return newsSend(7912, bytes, {stock_id: stockId, limit: limit,
        start_time: start || null, end_time: end || null, cursor: cursor || null,
        stock_kind: 1, native_flag: nativeFlag});
};
