// Read-only cached ticker extension for the parent's hash-gated QotBase.dll.
// No subscription, history request, permission change, account, or order calls.
// ABI evidence: coverage_audit/evidence/native_ticker_abi.json.
var tickerCount = new NativeFunction(exportAddress('?GetCount@ticks@sub@quote'),
  'int', ['uint64', 'int', 'uint64']);
var tickerGet = new NativeFunction(exportAddress('?GetDataByIndex@ticks@sub@quote'),
  'bool', ['uint64', 'uint', 'pointer', 'int', 'uint64']);
// Internal nondeleting destructor, checked against this exact module hash.
// It releases all four owning std::strings and does not free the record buffer.
var tickerDestroy = new NativeFunction(qotModule.base.add(0xfb770),
  'void', ['pointer']);

function tickerUnsigned(value) {
  const raw = value.readU64().toString();
  return raw === '9223372036854775807' || raw === '18446744073709551615' ? null : raw;
}

function tickerString(record, offset) {
  const value = record.add(offset);
  const size = Number(value.add(0x10).readU64().toString());
  const capacity = Number(value.add(0x18).readU64().toString());
  if (!Number.isSafeInteger(size) || !Number.isSafeInteger(capacity) ||
      size < 0 || size > 4096 || capacity < size) {
    return {value: null, error: 'invalid_or_oversized_native_string'};
  }
  if (size === 0) return {value: '', error: null};
  const storage = capacity < 16 ? value : value.readPointer();
  if (storage.isNull()) return {value: null, error: 'null_native_string_pointer'};
  try { return {value: storage.readUtf8String(size), error: null}; }
  catch (_) { return {value: null, error: 'native_string_read_failed'}; }
}

api.readtrades = function(stockId, limit, period) {
  if (typeof stockId !== 'string' || !/^\d+$/.test(stockId)) throw Error('Invalid stock ID');
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw Error('Invalid ticker limit');
  if (period === undefined || period === null) period = 0;
  // NORMAL=0 in the embedded api_FTCmdTick protocol. The native cache session
  // mapping is retained as default_0 until its enum mapping is independently verified.
  if (period !== 0) throw Error('Only default ticker period 0 is verified for this adapter');
  const total = tickerCount(uint64(stockId), period, uint64(0));
  const result = {stock_id: stockId, period: period, period_name: 'default_0',
    total_cached: total, exists: total > 0, rows: [], skipped_indices: [],
    source_kind: 'desktop_ticker_cache', price_scale_verified: false,
    volume_unit: 'native_volume_integer; hp_volume preserved separately',
    order: 'native_cache_index_order'};
  if (total <= 0) return result;
  if (!Number.isInteger(total) || total > 10000000) throw Error('Invalid native ticker count');
  const data = Memory.alloc(0xd8);
  data.writeByteArray(new Uint8Array(0xd8));
  // Valid empty MSVC basic_string<char> objects (SSO capacity 15).
  for (const offset of [0x30, 0x50, 0x70, 0x90]) data.add(offset + 0x18).writeU64(15);
  try {
    for (let index = Math.max(0, total - limit); index < total; index++) {
      if (!tickerGet(uint64(stockId), index, data, period, uint64(0))) {
        result.skipped_indices.push(index);
        continue;
      }
      const row = {cache_index: index, tick_key: tickerUnsigned(data),
        type_raw: data.add(0x08).readS32(),
        exchange_data_time_ms: tickerUnsigned(data.add(0x10)),
        price_raw: tickerUnsigned(data.add(0x18)),
        price_last_close_raw: tickerUnsigned(data.add(0x20)),
        volume_raw: tickerUnsigned(data.add(0x28)),
        strategy_raw: data.add(0xb0).readS32(),
        order_type_raw: data.add(0xb4).readS32(),
        raw_unclassified: {
          offset_b8: tickerUnsigned(data.add(0xb8)),
          offset_c0: tickerUnsigned(data.add(0xc0)),
          offset_c8: tickerUnsigned(data.add(0xc8)),
          offset_d0: data.add(0xd0).readS32()
        }, string_errors: {}};
      for (const entry of [['hp_volume', 0x30], ['trade_type', 0x50],
                           ['exchange_name', 0x70], ['cond_code', 0x90]]) {
        const text = tickerString(data, entry[1]);
        row[entry[0]] = text.value;
        if (text.error) row.string_errors[entry[0]] = text.error;
      }
      if (row.type_raw === 2147483647) row.type_raw = null;
      result.rows.push(row);
    }
    return result;
  } finally {
    // Required on false-result/exception paths too: copy assignment can own heap strings.
    tickerDestroy(data);
  }
};
