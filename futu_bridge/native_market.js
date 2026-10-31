// Read-only extensions for the exact, hash-gated QotBase.dll build.
// No subscription, account, order-entry, authentication, or patching calls.
var bookGet = new NativeFunction(exportAddress('?GetOrderBook@sub@quote'),
  'pointer', ['pointer', 'uint64', 'int', 'int', 'uint64']);
var bookExists = new NativeFunction(exportAddress('?IsDataExist@SubDataOrderBook@'),
  'bool', ['pointer']);
var bookDestroy = new NativeFunction(exportAddress('??1SubDataOrderBook@'),
  'void', ['pointer']);
var bookAccessors = {};
for (const side of ['Bid', 'Ask']) {
  for (const field of ['Price', 'Volume', 'Count', 'VolumePrecision']) {
    const args = ['pointer', 'pointer', 'int'];
    if (field === 'Price') args.push('bool');
    bookAccessors[side + field] = new NativeFunction(
      exportAddress('?Get' + side + field + '@SubDataOrderBook@'), 'pointer', args);
  }
}
var bookTimes = {};
for (const field of ['SvrRecvAskTime', 'SvrRecvBidTime', 'SvrSendToClientTime']) {
  bookTimes[field] = new NativeFunction(exportAddress('?Get' + field + '@SubDataOrderBook@'),
    'pointer', ['pointer', 'pointer']);
}
var simpleBookGet = new NativeFunction(exportAddress('?GetOrderBookSimple@sub@quote'),
  'pointer', ['pointer', 'uint64', 'int', 'uint64']);
var simpleBookExists = new NativeFunction(exportAddress('?IsDataExist@SubDataOrderBookSimple@'),
  'bool', ['pointer']);
var simpleBookAccessors = {};
for (const name of ['BidPrice', 'AskPrice', 'BidVolume', 'AskVolume']) {
  simpleBookAccessors[name] = new NativeFunction(exportAddress('?Get' + name + '@SubDataOrderBookSimple@'),
    'pointer', ['pointer', 'pointer']);
}
var simpleBookPrecision = new NativeFunction(exportAddress('?GetOrderVolumePrecision@SubDataOrderBookSimple@'),
  'pointer', ['pointer', 'pointer']);
function bookOptional(getter, data, level, price) {
  const out = Memory.alloc(16);
  if (level === null) getter(data, out);
  else if (price) getter(data, out, level, false);
  else getter(data, out, level);
  return out.add(8).readU8() ? out.readS64().toString() : null;
}
api.readbook = function(stockId, depth) {
  if (!Number.isInteger(depth) || depth < 1 || depth > 50) throw Error('Invalid depth');
  const data = Memory.alloc(128);
  // Mode 1 follows the app's normal/delayed entitlement selection.
  bookGet(data, uint64(stockId), 0, 1, uint64(0));
  try {
    const result = {stock_id: stockId, exists: !!bookExists(data), bids: [], asks: [], raw_times: {}};
    if (!result.exists) {
      const simple = Memory.alloc(64);
      simpleBookGet(simple, uint64(stockId), 1, uint64(0));
      result.exists = !!simpleBookExists(simple);
      result.book_kind = 'top_of_book';
      if (!result.exists) return result;
      const precisionOut = Memory.alloc(16);
      simpleBookPrecision(simple, precisionOut);
      const precision = precisionOut.add(4).readU8() ? precisionOut.readS32() : null;
      for (const side of ['Bid', 'Ask']) {
        const price = bookOptional(simpleBookAccessors[side + 'Price'], simple, null, false);
        const volume = bookOptional(simpleBookAccessors[side + 'Volume'], simple, null, false);
        if (price !== null && volume !== null) {
          result[side === 'Bid' ? 'bids' : 'asks'].push({level:1, price_raw:price,
            volume_raw:volume, order_count:null, volume_precision:precision});
        }
      }
      return result;
    }
    result.book_kind = 'depth';
    for (const side of ['Bid', 'Ask']) {
      for (let i = 0; i < depth; i++) {
        const price = bookOptional(bookAccessors[side + 'Price'], data, i, true);
        const volume = bookOptional(bookAccessors[side + 'Volume'], data, i, false);
        if (price === null || volume === null) break;
        result[side === 'Bid' ? 'bids' : 'asks'].push({
          level: i + 1, price_raw: price, volume_raw: volume,
          order_count: bookOptional(bookAccessors[side + 'Count'], data, i, false),
          volume_precision: bookOptional(bookAccessors[side + 'VolumePrecision'], data, i, false)
        });
      }
    }
    for (const [name, getter] of Object.entries(bookTimes)) {
      result.raw_times[name] = bookOptional(getter, data, null, false);
    }
    return result;
  } finally { bookDestroy(data); }
};

// Ordinary quote refresh through the signed-in application's own dispatcher.
// The vector is borrowed for the call; the native request copies its IDs.
var pullPrimary = new NativeFunction(exportAddress('?PullPrimaryInfo@sub@quote'),
  'void', ['pointer', 'uint64']);
api.requestcachedquotes = function(stockIds) {
  if (!Array.isArray(stockIds) || stockIds.length < 1 || stockIds.length > 100) {
    throw Error('Provide 1..100 stock IDs');
  }
  const values = Memory.alloc(stockIds.length * 8);
  for (let i = 0; i < stockIds.length; i++) values.add(i * 8).writeU64(uint64(stockIds[i]));
  const vector = Memory.alloc(24);
  vector.writePointer(values);
  vector.add(8).writePointer(values.add(stockIds.length * 8));
  vector.add(16).writePointer(values.add(stockIds.length * 8));
  pullPrimary(vector, uint64(0));
  return {request_invoked:true, response_received:false, stock_ids:stockIds};
};
