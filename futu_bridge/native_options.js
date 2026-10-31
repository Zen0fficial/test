// Read-only option-chain requests using the already authenticated desktop channel.
// Load after native_agent.js and native_history.js; Python hash-gates modules.
// Hard allowlist: option chain6736 and expiry discovery6311 only.
// Provenance and field numbers: coverage_audit/evidence/native_option_chain_abi.json.
var optionChainPending = null;
var optionChainLastRequestAt = 0;
var optionChainExpiryCache = {};

// Small bounded wire reader. Numeric values that exceed JS's exact range stay
// uninterpreted; raw bytes retain their exact representation for native reuse.
function optionChainWire(bytes) {
  let offset = 0;
  function varint() {
    let value = 0, factor = 1;
    for (let i = 0; i < 10; i++) {
      if (offset >= bytes.length) throw Error('Truncated option protobuf varint');
      const byte = bytes[offset++];
      value += (byte & 127) * factor;
      if (byte < 128) return Number.isSafeInteger(value) ? value : null;
      factor *= 128;
    }
    throw Error('Oversized option protobuf varint');
  }
  const fields = [];
  while (offset < bytes.length) {
    const start = offset, tag = varint();
    if (tag === null || tag < 8) throw Error('Invalid option protobuf tag');
    const number = Math.floor(tag / 8), wire = tag % 8;
    let value = null;
    if (wire === 0) value = varint();
    else if (wire === 2) {
      const length = varint();
      if (length === null || length < 0 || offset + length > bytes.length)
        throw Error('Invalid option protobuf length');
      value = bytes.slice(offset, offset + length); offset += length;
    } else if (wire === 1 || wire === 5) {
      const length = wire === 1 ? 8 : 4;
      if (offset + length > bytes.length) throw Error('Truncated option protobuf fixed field');
      value = bytes.slice(offset, offset + length); offset += length;
    } else throw Error('Unsupported option protobuf wire type');
    fields.push({number: number, wire: wire, value: value, raw: bytes.slice(start, offset)});
  }
  return fields;
}

function optionChainDecodeExpiries(pending, bytes) {
  const fields = optionChainWire(bytes);
  const ret = fields.find(f => f.number === 1 && f.wire === 0);
  if (!ret || ret.value !== 0) return {ret_code: ret ? ret.value : null, expiries: []};
  const entries = [];
  for (const item of fields.filter(f => f.number === 9 && f.wire === 2)) {
    const nested = optionChainWire(item.value);
    const expiry = nested.find(f => f.number === 1 && f.wire === 0);
    const underlying = nested.find(f => f.number === 9 && f.wire === 0);
    const dateField = nested.find(f => f.number === 12 && f.wire === 0);
    if (!expiry || !underlying || underlying.value !== Number(pending.metadata.stock_id) ||
        expiry.value === null || expiry.value <= 0) continue;
    // CorePB NewStrikeDateItem serializer0xf5560 and the app's conversion at
    // FTQuote0x18a8290 copy fields1..11 into chain request's repeated field8.
    const requestBytes = [].concat.apply([], nested.filter(f => f.number <= 11).map(f => f.raw));
    // Prefer the server's explicit calendar date; a Hong Kong midnight marker
    // can fall on the preceding UTC date. This field is not forwarded to6736.
    let expiryDate = new Date((expiry.value + (pending.metadata.option_market === 9 ? 28800 : 0)) * 1000)
      .toISOString().slice(0, 10);
    if (dateField && Number.isInteger(dateField.value)) {
      const dateText = String(dateField.value);
      if (!/^\d{8}$/.test(dateText)) throw Error('Invalid explicit option expiry date');
      expiryDate = dateText.slice(0, 4) + '-' + dateText.slice(4, 6) + '-' + dateText.slice(6, 8);
      if (new Date(expiryDate + 'T00:00:00Z').toISOString().slice(0, 10) !== expiryDate)
        throw Error('Invalid explicit option expiry calendar date');
    }
    entries.push({expiry_timestamp: expiry.value,
      expiry_date: expiryDate,
      underlying_stock_id: String(underlying.value), request_item_bytes: requestBytes});
  }
  const key = pending.metadata.stock_id + ':' + pending.metadata.option_market;
  optionChainExpiryCache[key] = {received_at_ms: Date.now(), entries: entries};
  return {ret_code: 0, expiries: entries.map(e => ({expiry_timestamp: e.expiry_timestamp,
    expiry_date: e.expiry_date, underlying_stock_id: e.underlying_stock_id})),
    expiry_count: entries.length};
}

function optionChainFinish(pending, fields) {
  if (optionChainPending !== pending) return;
  optionChainPending = null;
  clearTimeout(pending.timer);
  pending.resolve(Object.assign({protocol: pending.protocol, channel: 1,
    request_id: pending.requestId, request: pending.metadata,
    source_kind: 'desktop_session_option_chain_request', readonly: true}, fields));
}

// Options use a service callback rather than QotBase's globally bound reply.
// Observe the common receive entry, filtered by exact request ID before reading
// any response body. Static CoreBiz FTChannelProxy::OnPackageReceived RVA0xa4f80:
// this, requestID, channel, f3cnetError, RecvData*; RecvData protocol@8,
// payload pointer@0x10, payload byte count@0x18. No args/results are changed.
var optionChainReplyObserver = Interceptor.attach(historyCoreModule.base.add(0xa4f80), {
    onEnter(args) {
      const pending = optionChainPending;
      if (!pending || pending.requestId === null) return;
      try {
        const requestId = uint64(args[1].toString()).toString();
        if (requestId !== pending.requestId || args[2].toInt32() !== 1) return;
        const response = args[4];
        if (response.isNull()) {
          optionChainFinish(pending, {status: 'response_error', transport_error: args[3].toInt32(),
            transport_error_domain: 'f3cnet', message: 'Native response data is null', payload_bytes: []});
          return;
        }
        if (response.add(8).readU16() !== pending.protocol) return;
        const size = Number(response.add(0x18).readU64().toString());
        if (!Number.isSafeInteger(size) || size < 0 || size > 2 * 1024 * 1024)
          throw Error('Option-chain response exceeds validated payload bound');
        const body = response.add(0x10).readPointer();
        if (size && body.isNull()) throw Error('Null option-chain response payload');
        const fields = {status: 'response_received', request_id: requestId,
          transport_error: args[3].toInt32(), transport_error_domain: 'f3cnet',
          payload_bytes: size ? Array.from(new Uint8Array(body.readByteArray(size))) : []};
        if (pending.protocol === 6311 && fields.transport_error === 0)
          Object.assign(fields, optionChainDecodeExpiries(pending, fields.payload_bytes));
        optionChainFinish(pending, fields);
      } catch (error) {
        optionChainFinish(pending, {status: 'response_error', message: String(error), payload_bytes: []});
      }
    }
  });

function optionChainRequest(stockId, options, discoverExpiries) {
  options = options || {};
  if (typeof stockId !== 'string' || !/^\d+$/.test(stockId)) throw Error('Invalid underlying stock ID');
  const id = Number(stockId);
  if (!Number.isSafeInteger(id) || id <= 0) throw Error('Underlying stock ID exceeds verified numeric range');
  if (optionChainPending) throw Error('An option-chain request is already pending');
  if (Date.now() - optionChainLastRequestAt < 500) throw Error('Option chain limited to two requests per second');
  // FTQuote's actual CS-market switch maps CSmarket2 (US) to7 and1 (HK) to9.
  // Caller must select this from the public security master, never an entitlement.
  const market = options.option_market === undefined ? 7 : options.option_market;
  if (market !== 7 && market !== 9) throw Error('Only verified US7/HK9 option market routing is supported');
  // Strategy1 is explicitly selected by the app's chain request path. Do not
  // expose arbitrary strategy numbers or infer call/put semantics from it.
  if (options.strategy !== undefined && options.strategy !== 1) throw Error('Only app-used strategy1 is supported');
  const protocol = discoverExpiries ? 6311 : 6736;
  let bytes = discoverExpiries ? historyField(1, id).concat(historyField(2, 0), historyField(3, market === 7 ? 0 : 1)) :
    historyField(1, 1).concat(historyField(2, id));
  const metadata = {stock_id: stockId, strategy_raw: 1, option_market: market,
    expiration_filter: null, protocol_name: discoverExpiries ? 'FTCMDOptionChain.StrikeDateReq' : 'option_list_svr.GetComboOptionListReq',
    request_extension: {type: 10000, bytes: [market, 0]}, timeout_ms: 8000};
  if (discoverExpiries) {
    delete metadata.strategy_raw;
    metadata.index_option_type = 0;
    metadata.request_market = market === 7 ? 0 : 1;
  } else {
    const cached = optionChainExpiryCache[stockId + ':' + market];
    if (!cached || Date.now() - cached.received_at_ms > 300000)
      throw Error('Discover server-listed option expiries with readoptionexpiries first');
    // StrikeDate is a calendar-day marker, not the last trading time. Keep
    // the entire UTC expiry date eligible so intraday0DTE is not discarded.
    // The server's actual chain and entitlement checks decide availability.
    const today = new Date().toISOString().slice(0, 10);
    const future = cached.entries.filter(e => e.expiry_date >= today);
    const selected = options.expiry ? future.find(e => e.expiry_date === options.expiry) : future[0];
    if (!selected) throw Error('Requested expiry is not a current or future expiry returned by the server');
    const item = selected.request_item_bytes;
    bytes = bytes.concat(historyField(6, 0), historyField(7, 0),
      historyVarint(8 * 8 + 2), historyVarint(item.length), item);
    metadata.expiration_filter = selected.expiry_date;
    metadata.expiry_timestamp = selected.expiry_timestamp;
    metadata.index_option_type = 0;
    metadata.standard_type = 0;
    metadata.expiry_provenance = 'Same-session successful protocol6311 response field9';
  }
  // Chain requests select only an expiry listed by the server in this session.
  return new Promise(function(resolve) {
    const body = Memory.alloc(bytes.length + 1);
    body.writeByteArray(bytes);
    const data = Memory.alloc(40);
    data.writeByteArray(new Uint8Array(40));
    data.writeU16(protocol);
    const string = data.add(8);
    if (bytes.length < 16) string.writeByteArray(bytes); else string.writePointer(body);
    string.add(16).writeU64(bytes.length);
    string.add(24).writeU64(Math.max(15, bytes.length));
    // CSRequestExt mirrored from COptionChainService::RequestOptionChains:
    // type@0=10000; std::string@8 containing[market,0]; next@40=null; flags@48=0.
    const extension = Memory.alloc(56);
    extension.writeByteArray(new Uint8Array(56));
    extension.writeU16(10000);
    extension.add(8).writeByteArray([market, 0]);
    extension.add(24).writeU64(2);
    extension.add(32).writeU64(15);
    const emptyCallback = Memory.alloc(64);
    emptyCallback.writeByteArray(new Uint8Array(64));
    const pending = {resolve: resolve, requestId: null, metadata: metadata, protocol: protocol,
      early: [], refs: [body, data, extension, emptyCallback], timer: null};
    optionChainPending = pending;
    pending.timer = setTimeout(function() {
      optionChainFinish(pending, {status: 'response_timeout', payload_bytes: []});
    }, 8000);
    try {
      optionChainLastRequestAt = Date.now();
      pending.requestId = historySendCommand(1, data, extension, emptyCallback, ptr(0)).toString();
      if (pending.requestId === '0') {
        optionChainFinish(pending, {status: 'request_rejected', payload_bytes: []});
        return;
      }
      const early = pending.early.find(item => item.request_id === pending.requestId);
      if (early) optionChainFinish(pending, early);
    } catch (error) {
      optionChainFinish(pending, {status: 'request_error', message: String(error), payload_bytes: []});
    }
  });
}

api.readoptionchain = function(stockId, options) {
  return optionChainRequest(stockId, options, false);
};

api.readoptionexpiries = function(stockId, options) {
  return optionChainRequest(stockId, options, true);
};

// IOptionListData::GetItem(out shared_ptr16, uint64 ID, bool fetch_missing).
// Getters return scalars or borrowed strings; copy before releasing the owner.
// QotBase0x44b50 is the complete native shared_ptr destructor: strong-count
// atomic decrement, virtual destroy on last owner, then weak-count decrement.
var optionMetadataGetItem = new NativeFunction(qotModule.base.add(0xde3b),
  'pointer', ['pointer', 'uint64', 'uint8']);
var optionMetadataRelease = new NativeFunction(qotModule.base.add(0x44b50),
  'void', ['pointer']);
var optionMetadataScalarSpecs = {
  option_id: [0x49c6, 'uint64'], underlying_stock_id: [0xdac6, 'uint64'],
  strike_date_raw: [0x4d77, 'int64'], strike_price_raw: [0xd594, 'int64'],
  option_type_raw: [0x1b40, 'int'], real_expiration_date_raw: [0xcd10, 'int64'],
  last_trading_time_raw: [0xeb15, 'int64'], is_delisted: [0xd477, 'uint8'],
  market_id_raw: [0xceeb, 'int'], standard_type_raw: [0x8cdd, 'int'],
  contract_share_size_raw: [0x68de, 'uint64'], multiplier_raw: [0x3300, 'uint64']
};
var optionMetadataStringSpecs = {
  futu_symbol: 0x41ba, display_code: 0x58f3, underlying_symbol: 0x1b22,
  exchange: 0xb8e8, origin_symbol: 0x818e, timezone: 0x7a04
};
var optionMetadataScalars = {}, optionMetadataStrings = {};
Object.keys(optionMetadataScalarSpecs).forEach(function(name) {
  const spec = optionMetadataScalarSpecs[name];
  optionMetadataScalars[name] = new NativeFunction(qotModule.base.add(spec[0]), spec[1], ['pointer']);
});
Object.keys(optionMetadataStringSpecs).forEach(function(name) {
  optionMetadataStrings[name] = new NativeFunction(qotModule.base.add(optionMetadataStringSpecs[name]),
    'pointer', ['pointer']);
});
function optionMetadataReadString(address) {
  if (address.isNull()) return null;
  const size = Number(address.add(16).readU64().toString());
  const capacity = Number(address.add(24).readU64().toString());
  if (!Number.isSafeInteger(size) || !Number.isSafeInteger(capacity) ||
      size < 0 || size > 4096 || capacity < size) throw Error('Invalid option metadata string bound');
  if (!size) return '';
  const bytes = capacity < 16 ? address : address.readPointer();
  if (bytes.isNull()) throw Error('Null option metadata string body');
  return bytes.readUtf8String(size);
}
api.readinstrumentmetadata = function(stockIds, fetchMissing) {
  if (!Array.isArray(stockIds) || stockIds.length < 1 || stockIds.length > 32)
    throw Error('Read one to32 option IDs per metadata request');
  if (fetchMissing !== undefined && typeof fetchMissing !== 'boolean')
    throw Error('fetchMissing must be boolean');
  return stockIds.map(function(stockId) {
    if (typeof stockId !== 'string' || !/^\d+$/.test(stockId) || Number(stockId) <= 0 ||
        !Number.isSafeInteger(Number(stockId))) throw Error('Invalid option stock ID');
    const holder = Memory.alloc(16); holder.writeByteArray(new Uint8Array(16));
    const result = {stock_id: stockId, exists: false, fetch_missing: fetchMissing === true,
      source_kind: 'desktop_option_metadata_cache', readonly: true};
    try {
      optionMetadataGetItem(holder, uint64(stockId), fetchMissing === true ? 1 : 0);
      const option = holder.readPointer();
      if (option.isNull()) return result;
      const item = {};
      for (const name of Object.keys(optionMetadataScalars)) {
        const value = optionMetadataScalars[name](option);
        const type = optionMetadataScalarSpecs[name][1];
        item[name] = type === 'uint64' || type === 'int64' ? value.toString() :
          (name === 'is_delisted' ? Boolean(value) : value);
      }
      if (item.option_id !== stockId) throw Error('Option metadata identity mismatch');
      for (const name of Object.keys(optionMetadataStrings))
        item[name] = optionMetadataReadString(optionMetadataStrings[name](option));
      result.exists = true; result.metadata = item;
      return result;
    } finally {
      optionMetadataRelease(holder);
    }
  });
};
