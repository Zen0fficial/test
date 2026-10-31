// Read-only chart-history requests through the app's existing signed-in session.
// Build 16.33.17808 only. The Python loader must hash-gate CoreBiz.dll AND QotBase.dll.
// No credentials, headers, subscription permissions, or app callbacks are replaced.
// Static evidence: FTQuote.dll RVA 0xc45b30 calls SendCommand(channel=1) with
// CSProtoData{uint16 protocol; std::string payload}, empty callback, null owner.
// Protocol/field enums: coverage_audit/evidence/desktop_kline_enums.json.

var historyCoreModule = Process.getModuleByName('CoreBiz.dll');
var historyCoreExports = historyCoreModule.enumerateExports();
var historySendExports = historyCoreExports.filter(x => x.type === 'function' &&
    x.name.startsWith('?SendCommand@IFTChannelProxy@@'));
if (historySendExports.length !== 1) throw Error('Missing or ambiguous session-aware chart transport');
var historySendCommand = new NativeFunction(historySendExports[0].address, 'uint64',
    ['int', 'pointer', 'pointer', 'pointer', 'pointer']);
// FTQuote RVA0xc456da/0xc456f0 obtains the ordinary selected crypto broker and
// writes KlineReq.broker_id11 only when the native optional is engaged.
var historyCryptoBroker = new NativeFunction(exportAddress('?GetEffectiveCryptoCSBrokerID@crypto_broker@quote@@'),
    'pointer', ['pointer', 'uint64', 'uint64']);
var historyPending = null;
var historyLastRequestAt = 0;

function historyResolveJump(address) {
    for (let step = 0; step < 4; step++) {
        const instruction = Instruction.parse(address);
        if (instruction.mnemonic !== 'jmp' || instruction.operands[0].type !== 'imm') break;
        address = ptr(instruction.operands[0].value);
    }
    return address;
}

function historyVarint(value) {
    // All allowed inputs are nonnegative safe integers; do not use JS bitwise
    // operators on entire stock IDs because Futu identifiers exceed 32 bits.
    if (!Number.isSafeInteger(value) || value < 0) throw Error('Invalid protobuf integer');
    const out = [];
    do {
        const byte = value % 128;
        value = Math.floor(value / 128);
        out.push(byte + (value ? 128 : 0));
    } while (value);
    return out;
}

function historyField(number, value) {
    return historyVarint(number * 8).concat(historyVarint(value));
}

function historyReadPayload(data) {
    const string = data.add(8);
    const size = Number(string.add(16).readU64().toString());
    const capacity = Number(string.add(24).readU64().toString());
    if (!Number.isSafeInteger(size) || !Number.isSafeInteger(capacity) ||
        size < 0 || size > 2 * 1024 * 1024 || capacity < size) {
        throw Error('Chart response exceeds the validated bounded payload size');
    }
    if (!size) return [];
    const storage = capacity < 16 ? string : string.readPointer();
    if (storage.isNull()) throw Error('Chart response has a null payload pointer');
    return Array.from(new Uint8Array(storage.readByteArray(size)));
}

function historyFinish(pending, fields) {
    if (historyPending !== pending) return;
    historyPending = null;
    clearTimeout(pending.timer);
    pending.resolve(Object.assign({protocol: 6161, channel: 1,
        request_id: pending.requestId, request: pending.metadata,
        source_kind: 'desktop_session_history_request', readonly: true}, fields));
}

// Observe the native reply callback; leave all arguments, return values, and
// existing chart callback registrations unchanged. Read only our request's payload.
var historyReplyObserver = Interceptor.attach(
    historyResolveJump(exportAddress('?OnRecvReply@INNProtoCenter@@')), {
    onEnter(args) {
        const pending = historyPending;
        if (!pending) return;
        try {
            const requestId = uint64(args[0].toString()).toString();
            if (pending.requestId !== null && requestId !== pending.requestId) return;
            if (args[2].readU16() !== 6161) return;
            const fields = {status: 'response_received', request_id: requestId,
                transport_error: args[1].toInt32(), payload_bytes: historyReadPayload(args[2])};
            // The dispatcher may invoke an immediate failure before SendCommand
            // returns. Retain only a few quote responses, then match its exact ID.
            if (pending.requestId === null) {
                if (pending.early.length < 4) pending.early.push(fields);
            } else historyFinish(pending, fields);
        } catch (error) {
            historyFinish(pending, {status: 'response_error', message: String(error)});
        }
    }
});

api.readbars = function(stockId, options) {
    options = options || {};
    if (typeof stockId !== 'string' || !/^\d+$/.test(stockId)) throw Error('Invalid stock ID');
    const id = Number(stockId);
    if (!Number.isSafeInteger(id) || id <= 0) throw Error('Stock ID is outside the verified integer range');
    if (historyPending) throw Error('A chart-history request is already pending');
    if (Date.now() - historyLastRequestAt < 250) throw Error('Chart history is limited to four requests per second');
    const periods = {
        'minute': [1, 2, 1], '1m': [1, 2, 1], '3m': [10, 2, 3],
        '5m': [6, 2, 5], '15m': [7, 2, 15], '30m': [8, 2, 30],
        '60m': [9, 2, 60], '1h': [9, 2, 60],
        'day': [2, 3, 1], '1d': [2, 3, 1],
        'week': [3, 4, 1], '1w': [3, 4, 1],
        'month': [4, 5, 1], '1mo': [4, 5, 1]
    };
    const period = options.period || 'day';
    if (!Object.prototype.hasOwnProperty.call(periods, period)) throw Error('Unsupported chart period');
    const limit = options.limit === undefined ? 100 : options.limit;
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw Error('Candle limit must be 1..1000');
    const end = options.end_time === undefined || options.end_time === null ?
        Math.floor(Date.now() / 1000) : options.end_time;
    const start = options.start_time === undefined || options.start_time === null ? 0 : options.start_time;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 ||
        end <= start || end > 4102444800) throw Error('Expected a valid Unix-second chart time range');
    // Installed api_FTCmdKline.ExrightType names/numbers, not inferred ordering.
    // Explicit dividend-include/exclude variants3..6 are not exposed here.
    const adjustments = {none: 0, forward: 1, backward: 2};
    const adjustment = options.adjustment || 'none';
    if (!Object.prototype.hasOwnProperty.call(adjustments, adjustment)) throw Error('Unsupported chart adjustment');
    const type = periods[period];
    // Installed KlineTypeProb descriptor: NONE0, US_WITH_BA1,
    // WITH_OVERNIGHT2. Internal enum option is bounded to these values.
    const sessionProb = options.native_kline_type_prob === undefined ? 0 : options.native_kline_type_prob;
    if (!Number.isInteger(sessionProb) || sessionProb < 0 || sessionProb > 2) throw Error('Unverified chart-session enum');
    const fields = {1: id, 2: type[0], 3: adjustments[adjustment], 4: 0,
        5: start ? 1 : 3, 6: start, 7: end, 8: limit, 9: 0, 10: sessionProb};
    const brokerOptional = Memory.alloc(8);
    brokerOptional.writeByteArray(new Uint8Array(8));
    historyCryptoBroker(brokerOptional, uint64(stockId), uint64(0));
    const broker = brokerOptional.add(4).readU8() ? brokerOptional.readS32() : null;
    if (broker !== null) {
        if (broker < 0) throw Error('Unverified negative native broker enum');
        fields[11] = broker;
    }
    let bytes = [];
    for (const [number, value] of Object.entries(fields)) bytes = bytes.concat(historyField(Number(number), value));
    const typeV2 = historyField(1, type[1]).concat(historyField(2, type[2]));
    bytes = bytes.concat(historyVarint(12 * 8 + 2), historyVarint(typeV2.length), typeV2);
    const metadata = {stock_id: stockId, period: period, limit: limit,
        start_time: start || null, end_time: end, adjustment: adjustment,
        adjustment_code: adjustments[adjustment],
        native_broker_id: broker,
        kline_type: type[0], data_range_type: start ? 1 : 3,
        session: sessionProb === 0 ? 'provider_default' : 'native_feed_selection', session_hours_verified: false,
        native_kline_type_prob_name: ['KLINE_PROB_NONE', 'KLINE_PROB_US_WITH_BA', 'KLINE_PROB_WITH_OVERNIGHT'][sessionProb],
        native_kline_type_prob: sessionProb, timeout_ms: 8000};
    return new Promise(function(resolve) {
        const body = Memory.alloc(bytes.length + 1);
        body.writeByteArray(bytes);
        const data = Memory.alloc(40);
        data.writeByteArray(new Uint8Array(40));
        data.writeU16(6161); // Hard allowlist: this adapter cannot send any other command.
        const string = data.add(8);
        if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
        string.add(16).writeU64(bytes.length);
        string.add(24).writeU64(Math.max(15, bytes.length));
        const emptyCallback = Memory.alloc(64);
        emptyCallback.writeByteArray(new Uint8Array(64));
        const pending = {resolve: resolve, requestId: null, metadata: metadata,
            early: [], refs: [body, data, emptyCallback], timer: null};
        historyPending = pending;
        pending.timer = setTimeout(function() {
            historyFinish(pending, {status: 'response_timeout', payload_bytes: []});
        }, 8000);
        try {
            historyLastRequestAt = Date.now();
            pending.requestId = historySendCommand(1, data, ptr(0), emptyCallback, ptr(0)).toString();
            if (pending.requestId === '0') {
                historyFinish(pending, {status: 'request_rejected', payload_bytes: []});
                return;
            }
            const early = pending.early.find(item => item.request_id === pending.requestId);
            if (early) historyFinish(pending, early);
        } catch (error) {
            historyFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
        }
    });
};
