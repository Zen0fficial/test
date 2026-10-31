// Read-only book/tape requests through the desktop's authenticated quote channel.
// Load after native_history.js and native_snapshot.js. The Python loader verifies
// QotBase/CoreBiz hashes. No arbitrary protocol or permission override is exposed.
var feedBookProb = new NativeFunction(exportAddress('?GetMaxAuthGearProbType@IStockUtil@@'), 'int', ['uint64']);
var feedBookBmp = new NativeFunction(exportAddress('?IsOrderBookLv2AuthBMP@auth@quote@@'), 'bool', ['uint64']);
var feedMarket = new NativeFunction(exportAddress('?GetMktID@IStockUtil@@'), 'int', ['uint64', 'int']);
var feedTradeDate = new NativeFunction(exportAddress('?GetMarketTradeDate@market_status@quote@@'), 'uint', ['int', 'uint64']);
var feedCsMarket = new NativeFunction(exportAddress('?GetStockCSMktType@IStockListData@@'), 'int', ['uint64']);
var feedCryptoBroker = new NativeFunction(exportAddress('?GetEffectiveCryptoCSBrokerID@crypto_broker@quote@@'), 'pointer', ['pointer', 'uint64', 'uint64']);
var feedPending = null;
var feedLastRequestAt = 0;

function feedValidate(stockId) {
    if (typeof stockId !== 'string' || !/^\d+$/.test(stockId) ||
        !Number.isSafeInteger(Number(stockId)) || Number(stockId) <= 0)
        throw Error('Invalid stock ID');
    const id = uint64(stockId);
    if (snapshotUnknown(id)) return 'unknown_to_native_catalog';
    if (snapshotBlocked(id)) return 'native_quote_auth_blocked';
    if (snapshotBmp(id)) return 'native_bmp_mode_requires_separate_unverified_protocol';
    return null;
}

function feedFinish(pending, fields) {
    if (feedPending !== pending) return;
    feedPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: pending.protocol, channel: 1,
        request_id: pending.requestId, request: pending.metadata,
        source_kind: 'desktop_session_market_data_request', readonly: true,
        native_auth_gates_checked: true}, fields));
}

// Exact request-ID/protocol filtering occurs before reading any payload. This is
// the normal receive callback; no application arguments or return values change.
var feedReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
        const pending = feedPending;
        if (!pending || pending.requestId === null) return;
        try {
            const requestId = uint64(args[1].toString()).toString();
            if (requestId !== pending.requestId || args[2].toInt32() !== 1) return;
            const response = args[4];
            if (response.isNull()) throw Error('Native response data is null');
            if (response.add(8).readU16() !== pending.protocol) return;
            const size = Number(response.add(0x18).readU64().toString());
            if (!Number.isSafeInteger(size) || size < 0 || size > 2 * 1024 * 1024)
                throw Error('Market-data response exceeds validated payload bound');
            const body = response.add(0x10).readPointer();
            if (size && body.isNull()) throw Error('Native response body is null');
            feedFinish(pending, {status: 'response_received', request_id: requestId,
                transport_error: args[3].toInt32(), transport_error_domain: 'f3cnet',
                payload_bytes: size ? Array.from(new Uint8Array(body.readByteArray(size))) : []});
        } catch (error) {
            feedFinish(pending, {status: 'response_error', message: String(error), payload_bytes: []});
        }
    }
});

function feedSend(protocol, bytes, metadata, csMarket) {
    if (protocol !== 6824 && protocol !== 6128) throw Error('Unsupported market-data protocol');
    if (feedPending) throw Error('A book or public-trades request is already pending');
    if (Date.now() - feedLastRequestAt < 250) throw Error('Book/tape requests are limited to four per second');
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1); body.writeByteArray(bytes);
        const data = Memory.alloc(40); data.writeByteArray(new Uint8Array(40)); data.writeU16(protocol);
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length); string.add(24).writeU64(Math.max(15, bytes.length));
        const callback = Memory.alloc(64); callback.writeByteArray(new Uint8Array(64));
        let extension = ptr(0);
        if (csMarket !== undefined) {
            // Mirrors QotBase tick request: CSRequestExt10000, [CS market,0].
            extension = Memory.alloc(56); extension.writeByteArray(new Uint8Array(56));
            extension.writeU16(10000); extension.add(8).writeByteArray([csMarket, 0]);
            extension.add(24).writeU64(2); extension.add(32).writeU64(15);
        }
        const pending = {resolve: resolve, requestId: null, protocol: protocol, metadata: metadata,
            refs: [body, data, callback, extension], timer: null};
        feedPending = pending;
        pending.timer = setTimeout(function() {
            feedFinish(pending, {status: 'response_timeout', payload_bytes: []});
        }, 8000);
        try {
            feedLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, extension, callback, ptr(0)).toString();
            if (pending.requestId === '0') feedFinish(pending, {status: 'request_rejected', payload_bytes: []});
        } catch (error) {
            feedFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
}

api.requestbook = function(stockId, depth) {
    depth = depth === undefined ? 10 : depth;
    if (!Number.isInteger(depth) || depth < 1 || depth > 50) throw Error('Expected book depth 1..50');
    const reason = feedValidate(stockId);
    if (reason) return Promise.resolve({status: 'no_eligible_symbols', protocol: 6824,
        skipped: [{stock_id: stockId, reason: reason}], payload_bytes: [], native_auth_gates_checked: true});
    const id = uint64(stockId), prob = feedBookProb(id), delayedBook = feedBookBmp(id);
    // Bit4 requests best bid/ask. Bit3 uses exactly the app's entitlement selector.
    let security = historyField(1, Number(stockId)).concat(snapshotMessage(2, historyField(1, 4)));
    let fullBook = false;
    if (!delayedBook && [0, 1, 2, 4, 8, 16].indexOf(prob) !== -1) {
        security = security.concat(snapshotMessage(2, historyField(1, 3).concat(historyField(2, prob))));
        fullBook = true;
    }
    return feedSend(6824, snapshotMessage(1, security), {stock_id: stockId, depth: depth,
        native_book_prob: prob, full_book_requested: fullBook, simple_book_requested: true,
        full_book_skip_reason: fullBook ? null : delayedBook ? 'native_level2_bmp_mode' : 'unverified_native_book_prob'});
};

api.requesttrades = function(stockId, limit) {
    limit = limit === undefined ? 200 : limit;
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw Error('Expected trade count 1..1000');
    const reason = feedValidate(stockId);
    if (reason) return Promise.resolve({status: 'no_eligible_symbols', protocol: 6128,
        skipped: [{stock_id: stockId, reason: reason}], payload_bytes: [], native_auth_gates_checked: true});
    const id = uint64(stockId), csMarket = feedCsMarket(id), market = feedMarket(id, 1);
    if (csMarket <= 0 || csMarket > 255) return Promise.resolve({status: 'unsupported_native_market',
        protocol: 6128, stock_id: stockId, native_cs_market: csMarket, payload_bytes: []});
    // Native optional broker is obtained from the ordinary app context; it is not
    // chosen by the caller. This also matches the normal crypto-market route.
    const brokerResult = Memory.alloc(8); brokerResult.writeByteArray(new Uint8Array(8));
    feedCryptoBroker(brokerResult, id, uint64(0));
    const brokerOptional = brokerResult.readU64();
    const broker = brokerResult.add(4).readU8() ? brokerResult.readS32() : null;
    const date = feedTradeDate(market, brokerOptional);
    // Latest cursor UINT64_MAX is used by the app at QotBase RVA0xfd8b2/0xfd90d.
    // Encode as unsigned varint directly; never round it through a JS Number.
    const latest = [255, 255, 255, 255, 255, 255, 255, 255, 255, 1];
    let bytes = historyField(1, Number(stockId)).concat(historyVarint(3 * 8), latest,
        historyField(4, limit), historyField(5, 0), historyField(6, 0), historyField(9, date));
    if (broker !== null) {
        if (broker < 0) throw Error('Unverified negative native broker enum');
        bytes = bytes.concat(historyField(10, broker));
    }
    // req_auth field7 is intentionally absent, as in the normal native path.
    return feedSend(6128, bytes, {stock_id: stockId, limit: limit, period: 'regular', period_raw: 0,
        begin_tick_key: '18446744073709551615', native_market_id: market, native_cs_market: csMarket,
        native_trade_date_raw: date, native_broker_id: broker}, csMarket);
};
