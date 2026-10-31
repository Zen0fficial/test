// Normal quote snapshot fetch, protocol 6824. Requires native_history.js helpers.
// Uses the app's native blocked/unknown/BMP decisions before sending. Delayed
// symbols are explicitly deferred because their separate protocol is unverified.
// Source schema: coverage_audit/evidence/desktop_quote_fetch_schemas.json.
var snapshotUnknown = new NativeFunction(exportAddress('?IsStockUnknow@IStockUtil@@'), 'bool', ['uint64']);
var snapshotBlocked = new NativeFunction(exportAddress('?IsStockAuthQuoteBlocked@auth@quote@@'), 'bool', ['uint64']);
var snapshotBmp = new NativeFunction(exportAddress('?IsStockAuthBMP@auth@quote@@'), 'bool', ['uint64']);
var snapshotPending = null;
var snapshotLastRequestAt = 0;

function snapshotMessage(field, bytes) {
    return historyVarint(field * 8 + 2).concat(historyVarint(bytes.length), bytes);
}

function snapshotFinish(pending, fields) {
    if (snapshotPending !== pending) return;
    snapshotPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: 6824, channel: 1, request_id: pending.requestId,
        requested_stock_ids: pending.ids, skipped: pending.skipped,
        source_kind: 'desktop_session_quote_request', native_auth_gates_checked: true}, fields));
}

var snapshotReplyObserver = Interceptor.attach(
    historyResolveJump(exportAddress('?OnRecvReply@INNProtoCenter@@')), {
    onEnter(args) {
        const pending = snapshotPending;
        if (!pending) return;
        try {
            const requestId = uint64(args[0].toString()).toString();
            if (pending.requestId !== null && requestId !== pending.requestId) return;
            if (args[2].readU16() !== 6824) return;
            const fields = {status: 'response_received', request_id: requestId,
                transport_error: args[1].toInt32(), payload_bytes: historyReadPayload(args[2])};
            if (pending.requestId === null) {
                if (pending.early.length < 4) pending.early.push(fields);
            } else snapshotFinish(pending, fields);
        } catch (error) {
            snapshotFinish(pending, {status: 'response_error', message: String(error)});
        }
    }
});

api.requestquotes = function(stockIds) {
    if (!Array.isArray(stockIds) || stockIds.length < 1 || stockIds.length > 100)
        throw Error('Expected 1..100 stock IDs');
    if (snapshotPending) throw Error('A quote snapshot request is already pending');
    if (Date.now() - snapshotLastRequestAt < 250) throw Error('Quote snapshots are limited to four requests per second');
    const ids = [], skipped = [];
    let bytes = [];
    for (const stockId of Array.from(new Set(stockIds))) {
        if (typeof stockId !== 'string' || !/^\d+$/.test(stockId) ||
            !Number.isSafeInteger(Number(stockId)) || Number(stockId) <= 0)
            throw Error('Invalid stock ID');
        const nativeId = uint64(stockId);
        let reason = null;
        if (snapshotUnknown(nativeId)) reason = 'unknown_to_native_catalog';
        else if (snapshotBlocked(nativeId)) reason = 'native_quote_auth_blocked';
        else if (snapshotBmp(nativeId)) reason = 'native_bmp_mode_requires_separate_unverified_protocol';
        if (reason) { skipped.push({stock_id: stockId, reason: reason}); continue; }
        ids.push(stockId);
        // SecuritySubscribe -> repeated BitInfo PRICE0 and STOCK_STATE1.
        let security = historyField(1, Number(stockId));
        security = security.concat(snapshotMessage(2, historyField(1, 0)),
            snapshotMessage(2, historyField(1, 1)));
        bytes = bytes.concat(snapshotMessage(1, security));
    }
    if (!ids.length) return Promise.resolve({status: 'no_eligible_symbols', protocol: 6824,
        requested_stock_ids: [], skipped: skipped, payload_bytes: [], native_auth_gates_checked: true});
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1); body.writeByteArray(bytes);
        const data = Memory.alloc(40); data.writeByteArray(new Uint8Array(40)); data.writeU16(6824);
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length); string.add(24).writeU64(Math.max(15, bytes.length));
        const callback = Memory.alloc(64); callback.writeByteArray(new Uint8Array(64));
        const pending = {resolve: resolve, requestId: null, ids: ids, skipped: skipped,
            refs: [body, data, callback], early: [], timer: null};
        snapshotPending = pending;
        pending.timer = setTimeout(function() {
            snapshotFinish(pending, {status: 'response_timeout', payload_bytes: []});
        }, 8000);
        try {
            snapshotLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, ptr(0), callback, ptr(0)).toString();
            if (pending.requestId === '0') {
                snapshotFinish(pending, {status: 'request_rejected', payload_bytes: []}); return;
            }
            const early = pending.early.find(item => item.request_id === pending.requestId);
            if (early) snapshotFinish(pending, early);
        } catch (error) {
            snapshotFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
};
