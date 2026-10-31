// Native desktop market push, Futu 16.33.17808 only (loader verifies hashes).
// Sub/UnSub use a dedicated generated handle; never unsubscribe UI handles.
// Observe only the public market-data callback, without changing arguments.
var pushGenerateHandle = new NativeFunction(exportAddress('?GenerateHandle@INNBiz_Qot_PushQot@@'), 'uint64', []);
var pushNativeSub = new NativeFunction(exportAddress('?Sub@INNBiz_Qot_PushQot@@'), 'bool', ['uint64', 'uint64', 'pointer']);
var pushNativeUnSubHandle = new NativeFunction(exportAddress('?UnSubAllType@INNBiz_Qot_PushQot@@'), 'void', ['uint64']);
var pushCanSubscribe = new NativeFunction(exportAddress('?IsStockCanSubscription@IStockUtil@@'), 'bool', ['uint64']);
var pushNativeInited = new NativeFunction(exportAddress('?IsInited@INNBiz_Qot_PushQot@@'), 'bool', []);
var pushLocalExists = new NativeFunction(exportAddress('?IsLocalSubExist@INNBiz_Qot_PushQot@@'), 'bool', ['uint64', 'uint64', 'int', 'int', 'pointer']);
var pushServerExists = new NativeFunction(exportAddress('?IsSvrSubExist@INNBiz_Qot_PushQot@@'), 'bool', ['uint64', 'uint64', 'int', 'int', 'pointer']);
var pushSubscriptions = new Map();
var pushMaximumBytes = 2 * 1024 * 1024;

function pushValidateId(value) {
    if (typeof value !== 'string' || !/^[1-9]\d*$/.test(value) ||
        !Number.isSafeInteger(Number(value))) throw Error('Invalid stock ID');
    return value;
}

// A borrowed, single-node MSVC std::set<int> and two empty NN_Prob2Item maps.
// Verified native SubRequest ctor0x13f920 and consumer0x111b90: set+0,
// prob+0x10, two16B maps+0x18/+0x28, optional<broker>+0x38; total64B.
// Native Sub copies these const fields synchronously; it never owns these trees.
function pushRequest(bit, prob) {
    const request = Memory.alloc(64); request.writeByteArray(new Uint8Array(64));
    const head = Memory.alloc(32), node = Memory.alloc(32);
    head.writeByteArray(new Uint8Array(32)); node.writeByteArray(new Uint8Array(32));
    for (const offset of [0, 8, 16]) { head.add(offset).writePointer(node); node.add(offset).writePointer(head); }
    head.add(24).writeU16(0x101); node.add(24).writeU16(1); node.add(28).writeS32(bit);
    request.writePointer(head); request.add(8).writeU64(1); request.add(16).writeS32(prob);
    const maps = [];
    for (const offset of [24, 40]) {
        const sentinel = Memory.alloc(80); sentinel.writeByteArray(new Uint8Array(80));
        for (const link of [0, 8, 16]) sentinel.add(link).writePointer(sentinel);
        sentinel.add(24).writeU16(0x101); request.add(offset).writePointer(sentinel); maps.push(sentinel);
    }
    return {pointer: request, refs: [request, head, node].concat(maps)};
}

// Bounded protobuf framing only. Tick and book values are decoded in Python.
// uint64 trade IDs remain untouched bytes; stock IDs must be JS-safe integers.
function pushReadVarint(bytes, cursor) {
    let value = 0, multiplier = 1;
    for (let i = 0; i < 10; ++i) {
        if (cursor.pos >= bytes.length) throw Error('Truncated push varint');
        const byte = bytes[cursor.pos++];
        value += (byte & 127) * multiplier;
        if (!(byte & 128)) return value;
        multiplier *= 128;
    }
    throw Error('Invalid push varint');
}
function pushWireFields(bytes) {
    const cursor = {pos: 0}, fields = [];
    while (cursor.pos < bytes.length) {
        const start = cursor.pos, key = pushReadVarint(bytes, cursor);
        const number = Math.floor(key / 8), wire = key % 8;
        if (!Number.isSafeInteger(key) || number <= 0 || fields.length >= 100000) throw Error('Invalid push field');
        let value = null, bodyStart = cursor.pos;
        if (wire === 0) value = pushReadVarint(bytes, cursor);
        else if (wire === 2) {
            const size = pushReadVarint(bytes, cursor); bodyStart = cursor.pos;
            if (!Number.isSafeInteger(size) || size < 0 || cursor.pos + size > bytes.length) throw Error('Invalid push field size');
            cursor.pos += size;
        } else if (wire === 1) cursor.pos += 8;
        else if (wire === 5) cursor.pos += 4;
        else throw Error('Unsupported push wire type');
        if (cursor.pos > bytes.length) throw Error('Truncated push field');
        fields.push({number: number, wire: wire, value: value,
            raw: bytes.subarray(start, cursor.pos), body: bytes.subarray(bodyStart, cursor.pos)});
    }
    return fields;
}
function pushConcat(parts) {
    const size = parts.reduce((count, part) => count + part.length, 0);
    const result = new Uint8Array(size); let offset = 0;
    for (const part of parts) { result.set(part, offset); offset += part.length; }
    return result;
}
function pushMessage(field, bytes) {
    return pushConcat([new Uint8Array(historyVarint(field * 8 + 2)), new Uint8Array(historyVarint(bytes.length)), bytes]);
}
function pushFilteredFrame(rootFields, subscription) {
    const securities = [], ids = new Set(), bits = new Set();
    for (const root of rootFields) {
        if (root.number !== 1 || root.wire !== 2) continue;
        const fields = pushWireFields(root.body);
        const security = fields.find(field => field.number === 1 && field.wire === 0);
        if (!security || !Number.isSafeInteger(security.value)) continue;
        const stockId = String(security.value), allowed = subscription.bits.get(stockId);
        if (!allowed) continue;
        const parts = [], matched = [];
        for (const field of fields) {
            if (field.number !== 2) { parts.push(field.raw); continue; }
            if (field.wire !== 2) throw Error('Malformed push bit wrapper');
            const bitField = pushWireFields(field.body).find(value => value.number === 1 && value.wire === 0);
            if (bitField && allowed.has(bitField.value)) { parts.push(field.raw); matched.push(bitField.value); }
        }
        if (matched.length) {
            securities.push(pushMessage(1, pushConcat(parts))); ids.add(stockId);
            for (const bit of matched) bits.add(bit);
        }
    }
    if (!securities.length) return null;
    // Retain non-security root fields for future sequence metadata if present.
    for (const root of rootFields) if (root.number !== 1) securities.push(root.raw);
    return {bytes: pushConcat(securities), ids: Array.from(ids), bits: Array.from(bits)};
}
function pushEnvelope(subscription, fields) {
    subscription.sequence += 1;
    return Object.assign({kind: 'native_push', subscription_id: subscription.id,
        capture_epoch: subscription.epoch, capture_sequence: subscription.sequence,
        captured_at_unix_ms: Date.now(), protocol: 6212,
        source_kind: 'desktop_native_push', readonly_market_data: true}, fields);
}
function pushNotifyStatus(subscription, event, extra) {
    if (!subscription.activated) return;
    send(pushEnvelope(subscription, Object.assign({event: event}, extra || {})));
}

var pushDataObserver = Interceptor.attach(historyResolveJump(exportAddress('?OnPush_PushQot@INNBiz_Qot_PushQot@@')), {
    onEnter(args) {
        if (!pushSubscriptions.size) return;
        try {
            const size = args[2].toInt32();
            if (size < 0 || size > pushMaximumBytes || (size && args[1].isNull())) throw Error('Push frame exceeds validated bound');
            const bytes = size ? new Uint8Array(args[1].readByteArray(size)) : new Uint8Array(0);
            const rootFields = pushWireFields(bytes);
            for (const subscription of pushSubscriptions.values()) {
                if (!subscription.activated) continue;
                const selected = pushFilteredFrame(rootFields, subscription);
                if (!selected) continue;
                subscription.frames += 1;
                send(pushEnvelope(subscription, {event: 'data', native_source_raw: args[0].toUInt32(),
                    stock_ids: selected.ids, bits: selected.bits, payload_size: selected.bytes.length}), selected.bytes.buffer);
            }
        } catch (error) {
            for (const subscription of pushSubscriptions.values())
                pushNotifyStatus(subscription, 'capture_gap', {reason: 'native_frame_capture_failed', message: String(error), continuity_unknown: true});
        }
    }
});
var pushResubObserver = Interceptor.attach(historyResolveJump(exportAddress('?OnPush_ReSubNotify@INNBiz_Qot_PushQot@@')), {
    onEnter(args) {
        // A resubscription notice is not proof of a transport disconnect. Do not
        // inspect its body or change the app's reconnect/permission handling.
        for (const subscription of pushSubscriptions.values())
            pushNotifyStatus(subscription, 'native_resubscribe_notice', {protocol: 6304,
                native_source_raw: args[0].toUInt32(), continuity_unknown: true});
    }
});
var pushResubAllObserver = Interceptor.attach(historyResolveJump(exportAddress('?ReSubAll@INNBiz_Qot_PushQot@@')), {
    onEnter() {
        for (const subscription of pushSubscriptions.values())
            pushNotifyStatus(subscription, 'native_resubscribe_notice', {protocol: null,
                reason: 'native_resubscribe_all', continuity_unknown: true});
    }
});

api.pushprepare = function(subscriptionId) {
    if (typeof subscriptionId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(subscriptionId)) throw Error('Invalid subscription ID');
    if (pushSubscriptions.has(subscriptionId)) throw Error('Subscription ID already prepared');
    if (pushSubscriptions.size >= 100) throw Error('Too many push subscriptions');
    if (!pushNativeInited()) throw Error('Native push subsystem is not initialized');
    const subscription = {id: subscriptionId, handle: pushGenerateHandle().toString(),
        epoch: subscriptionId, sequence: 0, frames: 0, activated: false,
        bits: new Map(), accepted: [], skipped: []};
    pushSubscriptions.set(subscriptionId, subscription);
    // No market-data subscription is active yet. The Python caller retains this
    // handle before activating Sub, allowing cleanup if activation is interrupted.
    return {status: 'prepared', subscription_id: subscriptionId,
        capture_epoch: subscription.epoch, handles: [subscription.handle]};
};
api.pushsubscribe = function(subscriptionId, stockIds, types) {
    if (typeof subscriptionId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(subscriptionId)) throw Error('Invalid subscription ID');
    if (pushSubscriptions.has(subscriptionId) && pushSubscriptions.get(subscriptionId).activated) throw Error('Subscription ID already active');
    if (!Array.isArray(stockIds) || !stockIds.length || stockIds.length > 100) throw Error('Expected 1..100 stock IDs');
    if (!Array.isArray(types) || !types.length || types.some(value => !['quotes', 'orderbook', 'trades'].includes(value))) throw Error('Invalid push types');
    const ids = Array.from(new Set(stockIds.map(pushValidateId)));
    if (!pushNativeInited()) throw Error('Native push subsystem is not initialized');
    if (!pushSubscriptions.has(subscriptionId)) api.pushprepare(subscriptionId);
    const subscription = pushSubscriptions.get(subscriptionId);
    subscription.activated = true;
    try {
        for (const stockId of ids) {
            const reason = feedValidate(stockId);
            if (reason) { subscription.skipped.push({stock_id: stockId, reason: reason}); continue; }
            const id = uint64(stockId);
            if (!pushCanSubscribe(id)) { subscription.skipped.push({stock_id: stockId, reason: 'native_subscription_not_supported'}); continue; }
            const requests = [];
            if (types.includes('quotes')) requests.push({bit: 0, prob: 0, type: 'quotes'});
            if (types.includes('orderbook')) {
                requests.push({bit: 4, prob: 0, type: 'orderbook'});
                const prob = feedBookProb(id);
                if (!feedBookBmp(id) && [0, 1, 2, 4, 8, 16].includes(prob)) requests.push({bit: 3, prob: prob, type: 'orderbook'});
                else subscription.skipped.push({stock_id: stockId, type: 'orderbook', bit: 3, reason: 'native_full_book_not_eligible'});
            }
            // api_FTCmdStockQuoteSubData: API_SBIT_TICK35, NORMAL period prob1.
            if (types.includes('trades')) requests.push({bit: 35, prob: 1, type: 'trades'});
            const allowed = new Set(); subscription.bits.set(stockId, allowed);
            for (const item of requests) {
                const request = pushRequest(item.bit, item.prob);
                allowed.add(item.bit); // Capture callbacks during the native call.
                const accepted = Boolean(pushNativeSub(uint64(subscription.handle), id, request.pointer));
                if (accepted) subscription.accepted.push(Object.assign({stock_id: stockId, local_accepted: true}, item));
                else { allowed.delete(item.bit); subscription.skipped.push(Object.assign({stock_id: stockId, reason: 'native_local_subscription_rejected'}, item)); }
            }
        }
        return {status: subscription.accepted.length ? 'locally_subscribed' : 'no_eligible_symbols',
            subscription_id: subscriptionId, capture_epoch: subscription.epoch, handles: [subscription.handle],
            accepted: subscription.accepted, skipped: subscription.skipped,
            native_auth_gates_checked: true, server_acceptance_verified: false,
            transport: 'native_push', polling: false, protocol: 6212};
    } catch (error) {
        // Retain provenance if cleanup itself fails, so status/closeall can retry.
        if (pushNativeInited()) pushNativeUnSubHandle(uint64(subscription.handle));
        pushSubscriptions.delete(subscriptionId);
        throw error;
    }
};
api.pushunsubscribe = function(subscriptionId) {
    const subscription = pushSubscriptions.get(subscriptionId);
    if (!subscription) return {status: 'already_closed', subscription_id: subscriptionId};
    if (pushNativeInited()) pushNativeUnSubHandle(uint64(subscription.handle));
    pushSubscriptions.delete(subscriptionId);
    return {status: 'closed', subscription_id: subscriptionId, handles: [subscription.handle], captured_frames: subscription.frames};
};
api.pushcloseall = function() {
    const results = [];
    for (const id of Array.from(pushSubscriptions.keys())) results.push(api.pushunsubscribe(id));
    return {status: 'closed', subscriptions: results};
};
api.pushrecover = function(handles) {
    if (!Array.isArray(handles) || handles.length > 100 || handles.some(value => typeof value !== 'string' || !/^[1-9]\d{0,19}$/.test(value) || uint64(value).toString() !== value)) throw Error('Invalid retained handle list');
    // Internal RPC only: Python supplies solely handles created by this provider
    // in the same verified PID+process-creation generation before detachment.
    const active = new Set(Array.from(pushSubscriptions.values()).map(item => item.handle));
    if (handles.some(handle => active.has(handle))) throw Error('Cannot recover an active handle');
    if (pushNativeInited()) for (const handle of new Set(handles)) pushNativeUnSubHandle(uint64(handle));
    return {status: 'recovered', handles: Array.from(new Set(handles))};
};
api.pushsettle = function(keys, timeoutMs) {
    timeoutMs = timeoutMs === undefined ? 2500 : timeoutMs;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 0 || timeoutMs > 2500) throw Error('Invalid subscription settle timeout');
    if (!Array.isArray(keys) || keys.length > 400) throw Error('Expected at most 400 subscription keys');
    const unique = new Map();
    for (const key of keys) {
        if (!key || typeof key !== 'object') throw Error('Invalid subscription settle key');
        const stockId = pushValidateId(key.stock_id), bit = key.bit, prob = key.prob;
        if (!Number.isInteger(bit) || !Number.isInteger(prob) ||
            !((bit === 0 || bit === 4) && prob === 0 || bit === 35 && prob === 1 ||
              bit === 3 && [0, 1, 2, 4, 8, 16].includes(prob))) throw Error('Unsupported subscription settle key');
        unique.set(stockId + ':' + bit + ':' + prob, {stock_id: stockId, bit: bit, prob: prob});
    }
    // Only subscription bookkeeping is checked. No quote request, subscription,
    // cache reset, global resubscribe or other owner's handle is changed here.
    // IsLocalSubExist/IsSvrSubExist take an empty NN_Prob2Item reference, verified
    // by static ABI evidence and the read-only live state probe.
    const pending = Array.from(unique.values()).map(key => ({key: key, request: pushRequest(key.bit, key.prob)}));
    const started = Date.now();
    return new Promise(function(resolve) {
        function check() {
            try {
                const initialized = Boolean(pushNativeInited()), states = [];
                let shared = 0, clearing = 0;
                for (const entry of pending) {
                    const key = entry.key, id = uint64(key.stock_id), prob2 = entry.request.pointer.add(24);
                    const local = initialized && Boolean(pushLocalExists(id, uint64(0), key.bit, key.prob, prob2));
                    const server = initialized && Boolean(pushServerExists(id, uint64(0), key.bit, key.prob, prob2));
                    if (local) shared += 1;
                    else if (server) clearing += 1;
                    states.push(Object.assign({}, key, {local_exists: local, server_exists: server}));
                }
                const elapsed = Date.now() - started;
                if (clearing === 0 || elapsed >= timeoutMs) {
                    resolve({status: clearing ? 'settle_timeout' : 'settled', elapsed_ms: elapsed,
                        timeout_ms: timeoutMs, native_initialized: initialized,
                        checked_keys: states.length, shared_active_keys: shared,
                        pending_server_keys: clearing, cleared_keys: states.length - shared - clearing,
                        states: states, lifecycle_only: true, market_data_requests: 0});
                    return;
                }
                setTimeout(check, Math.min(50, Math.max(1, timeoutMs - elapsed)));
            } catch (error) {
                resolve({status: 'settle_error', elapsed_ms: Date.now() - started,
                    message: String(error), lifecycle_only: true, market_data_requests: 0});
            }
        }
        check();
    });
};
api.pushstatus = function() {
    return {transport: 'native_push', protocol: 6212, subscriptions: Array.from(pushSubscriptions.values()).map(item => ({
        subscription_id: item.id, capture_epoch: item.epoch, capture_sequence: item.sequence,
        phase: item.activated ? 'activated' : 'prepared',
        handles: [item.handle], captured_frames: item.frames, accepted: item.accepted, skipped: item.skipped}))};
};
