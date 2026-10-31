// Read-only getters validated against QotBase.dll in Futu 16.33.17808.
// Python verifies the on-disk SHA-256 before attachment. No hooks or subscriptions.
var api = {};
var qotModule = Process.getModuleByName('QotBase.dll');
var qotExports = qotModule.enumerateExports();

function exportAddress(prefix) {
    const found = qotExports.filter(x => x.type === 'function' && x.name.startsWith(prefix));
    if (found.length !== 1) throw Error('Ambiguous or missing quote export: ' + prefix);
    return found[0].address;
}

var getNativePrice = new NativeFunction(exportAddress('?GetPrice@sub@quote'), 'pointer',
    ['pointer', 'uint64', 'int', 'uint64']);
var nativePriceExists = new NativeFunction(exportAddress('?IsDataExist@SubDataPrice'), 'bool', ['pointer']);
var nativePriceGetters = {};
for (const field of ['PriceNominal', 'PriceLastClose', 'ExchangeTime', 'ServerTime', 'RecvPushTime']) {
    nativePriceGetters[field] = new NativeFunction(exportAddress('?Get' + field + '@SubDataPrice'),
        'pointer', ['pointer', 'pointer']);
}

api.initialize = function() {
    return {module_path: qotModule.path, architecture: Process.arch,
        pointer_size: Process.pointerSize, price_mode: 1, readonly: true,
        transport_module_path: typeof historyCoreModule !== 'undefined' ? historyCoreModule.path : null,
        methods: Object.keys(api)};
};

api.readquotes = function(stockIds) {
    if (!Array.isArray(stockIds) || stockIds.length < 1 || stockIds.length > 100)
        throw Error('Expected between 1 and 100 stock IDs');
    return stockIds.map(function(stockId) {
        if (typeof stockId !== 'string' || !/^\d+$/.test(stockId))
            throw Error('Invalid stock ID');
        // Validated SubDataPrice is a 72-byte POD with no owning pointers.
        const data = Memory.alloc(128);
        // Mode 1 is the app-selected delay/permission mode. Do not change to mode 0.
        getNativePrice(data, uint64(stockId), 1, uint64(0));
        const result = {stock_id: stockId, exists: Boolean(nativePriceExists(data)), raw: {}};
        if (!result.exists) return result;
        for (const [field, getter] of Object.entries(nativePriceGetters)) {
            const value = Memory.alloc(16);
            getter(data, value);
            result.raw[field] = value.add(8).readU8() ? value.readS64().toString() : null;
        }
        return result;
    });
};

// The loader appends optional native_market.js, then `rpc.exports = api;`.
