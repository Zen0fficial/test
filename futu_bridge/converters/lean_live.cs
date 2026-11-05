// Converter-owned live data queue. The Futu provider and LEAN source stay fixed.
using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NodaTime;
using QuantConnect.Configuration;
using QuantConnect.Data;
using QuantConnect.Data.Market;
using QuantConnect.Interfaces;
using QuantConnect.Orders;
using QuantConnect.Packets;
using QuantConnect.Securities;

namespace QuantConnect.Algorithm.CSharp
{
    public sealed class ConverterJournal
    {
        private readonly object _lock = new();
        private readonly string _path;
        public ConverterJournal(string path) { _path = path; Directory.CreateDirectory(Path.GetDirectoryName(path)); }
        public void Write(object value)
        {
            lock (_lock)
            {
                if (File.Exists(_path) && new FileInfo(_path).Length > 32 * 1024 * 1024)
                    for (var i = 3; i >= 1; i--)
                    {
                        var previous = i == 1 ? _path : _path + "." + (i - 1);
                        if (File.Exists(previous)) File.Move(previous, _path + "." + i, true);
                    }
                File.AppendAllText(_path, JsonConvert.SerializeObject(value) + "\n");
            }
        }
    }

    public sealed class FutuLiveTick : Tick
    {
        public string RecordId { get; set; }
        public string NativeTradeId { get; set; }
        public int Connection { get; set; }
        public long ReceivedNs { get; set; }
        public long EventNs { get; set; }
        public string TimestampBasis { get; set; }
        public override BaseData Clone() => (FutuLiveTick)MemberwiseClone();
    }

    public sealed class FutuTickEnumerator : IEnumerator<BaseData>
    {
        private readonly ConcurrentQueue<BaseData> _queue = new();
        private volatile bool _closed;
        public BaseData Current { get; private set; }
        object IEnumerator.Current => Current;
        public int Count => _queue.Count;
        public bool Enqueue(BaseData value)
        {
            if (_closed) return false;
            if (_queue.Count >= 8192) throw new IOException("Live consumer queue overflow; gap is unknown");
            _queue.Enqueue(value);
            return true;
        }
        public bool MoveNext()
        {
            if (_queue.TryDequeue(out var item)) { Current = item; return true; }
            Current = null;
            return !_closed;
        }
        public void Reset() => throw new NotSupportedException();
        public void Dispose() { _closed = true; }
    }

    public sealed class LocalFutuDataQueue : IDataQueueHandler
    {
        public static LocalFutuDataQueue Instance { get; private set; }
        private readonly object _lock = new();
        private readonly Dictionary<SubscriptionDataConfig, (FutuTickEnumerator Queue, EventHandler Notify)> _subscriptions = new();
        private readonly Dictionary<string, string> _trades = new();
        private readonly Queue<string> _seenOrder = new();
        private readonly CancellationTokenSource _stop = new();
        private readonly HttpClient _http = new() { Timeout = Timeout.InfiniteTimeSpan };
        private readonly ConcurrentDictionary<string, long> _counts = new();
        private readonly ConverterJournal _raw, _admitted, _status;
        private readonly string _root, _baseUrl, _symbol;
        private Task _worker;
        private long _sequence;
        private volatile bool _connected;
        private Symbol _nativeSymbol;
        public bool IsConnected => _connected;
        public static long NowNs() => (DateTime.UtcNow.Ticks - DateTime.UnixEpoch.Ticks) * 100L;
        public static long ToNs(DateTime utc) => (utc.Ticks - DateTime.UnixEpoch.Ticks) * 100L;
        public static string Dec(decimal value) => value.ToString(CultureInfo.InvariantCulture);
        private void Count(string key) => _counts.AddOrUpdate(key, 1, (_, n) => n + 1);

        public LocalFutuDataQueue()
        {
            Instance = this;
            _root = Config.Get("futu-live-output");
            _baseUrl = Config.Get("futu-live-url", "http://127.0.0.1:8765").TrimEnd('/');
            var uri = new Uri(_baseUrl);
            if (!uri.IsLoopback || uri.Scheme != "http" || uri.UserInfo != "" || uri.AbsolutePath != "/" || uri.Query != "")
                throw new ArgumentException("Futu live source must be an unauthenticated loopback URL");
            _symbol = Config.Get("futu-live-symbol", "HK.00700");
            if (_symbol != "HK.00700") throw new NotSupportedException("This live converter is validated for HK.00700");
            _raw = new ConverterJournal(Path.Combine(_root, "raw.jsonl"));
            _admitted = new ConverterJournal(Path.Combine(_root, "admitted.jsonl"));
            _status = new ConverterJournal(Path.Combine(_root, "transport.jsonl"));
        }

        public void SetJob(LiveNodePacket job)
        {
            if (job.Brokerage != "PaperBrokerage") throw new InvalidOperationException("Live data probe requires the local paper brokerage");
        }

        public IEnumerator<BaseData> Subscribe(SubscriptionDataConfig config, EventHandler notify)
        {
            if (config.Symbol.Value != "00700" || config.Resolution != Resolution.Tick || config.Type != typeof(Tick))
                throw new NotSupportedException("Only native HK.00700 tick subscriptions are supported");
            lock (_lock)
            {
                var queue = new FutuTickEnumerator();
                _subscriptions.Add(config, (queue, notify));
                _nativeSymbol = config.Symbol;
                _worker ??= Task.Run(ReadLoop);
                return queue;
            }
        }

        public void Unsubscribe(SubscriptionDataConfig config)
        {
            lock (_lock)
                if (_subscriptions.Remove(config, out var subscription)) subscription.Queue.Dispose();
        }

        private void Status(string state, string reason = "")
        {
            _status.Write(new {at_ns = NowNs(), state, reason, unknown_gap = state == "disconnected", exchange_replay_available = false});
            File.WriteAllText(Path.Combine(_root, "transport_status.json"), JsonConvert.SerializeObject(Snapshot()));
        }
        public object Snapshot() => new {connected = _connected, counts = _counts.ToDictionary(x => x.Key, x => x.Value),
            exchange_lossless = false, replay_on_reconnect = false};

        private async Task ReadLoop()
        {
            var retry = 0;
            var forced = false;
            var connection = 0;
            while (!_stop.IsCancellationRequested)
            {
                try
                {
                    Count("http_stream_requests");
                    using var request = new HttpRequestMessage(HttpMethod.Get, _baseUrl + "/v1/stream?symbols=HK.00700&types=quotes,trades,orderbook");
                    request.Headers.Accept.ParseAdd("text/event-stream");
                    using var connect = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
                    connect.CancelAfter(TimeSpan.FromSeconds(20));
                    using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, connect.Token);
                    response.EnsureSuccessStatusCode();
                    if (response.Content.Headers.ContentType?.MediaType != "text/event-stream") throw new IOException("Expected SSE");
                    using var stream = await response.Content.ReadAsStreamAsync(_stop.Token);
                    using var reader = new StreamReader(stream);
                    _connected = true;
                    connection++;
                    Count("connections");
                    Status("connected");
                    var started = DateTime.UtcNow;
                    string kind = "message", id = "";
                    var lines = new List<string>();
                    var length = 0;
                    while (!_stop.IsCancellationRequested)
                    {
                        var line = await reader.ReadLineAsync(_stop.Token).AsTask().WaitAsync(TimeSpan.FromSeconds(20), _stop.Token);
                        if (line == null) throw new IOException("Provider stream ended");
                        length += line.Length;
                        if (length > 2 * 1024 * 1024) throw new IOException("SSE event exceeds size limit");
                        if (line.StartsWith(':')) { Count("keepalive"); if (lines.Count == 0) length = 0; continue; }
                        if (line.Length != 0)
                        {
                            var split = line.IndexOf(':');
                            if (split < 0) continue;
                            var key = line[..split];
                            var value = line[(split + 1)..].TrimStart(' ');
                            if (key == "event") kind = value;
                            if (key == "id") id = value;
                            if (key == "data") lines.Add(value);
                            continue;
                        }
                        if (lines.Count > 0)
                        {
                            // Keep ISO timestamps as strings. Json.NET's automatic
                            // DateTime conversion followed by a string cast can lose
                            // the UTC offset and sub-second precision on Windows.
                            using var jsonReader = new JsonTextReader(new StringReader(string.Join("\n", lines)))
                                {DateParseHandling = DateParseHandling.None, FloatParseHandling = FloatParseHandling.Decimal};
                            var body = JObject.Load(jsonReader);
                            Count(kind);
                            if (kind == "ready" && ((string)body["source"]?["provider"] != "futu-desktop-session" || (bool?)body["source"]?["live_push"] != true))
                                throw new IOException("Expected the native Futu push provider");
                            if (kind == "error" || kind == "end") throw new IOException("Provider reported " + kind);
                            var sequence = ++_sequence;
                            var received = NowNs();
                            _raw.Write(new {sequence, connection, received_ns = received, type = kind, sse_id = id, body});
                            if (kind == "heartbeat" && ((string)body["data"]?["event"] == "gap" || (string)body["data"]?["status"] is "disconnected" or "closed"))
                            { Count("provider_gaps"); throw new IOException("Provider reported a delivery gap or disconnection"); }
                            if (kind is "trades" or "orderbook" or "quotes")
                            {
                                try { ConvertEvent(body, kind, sequence, received, connection); }
                                catch { Count("conversion_errors"); throw; }
                            }
                        }
                        kind = "message"; id = ""; lines.Clear(); length = 0;
                        if (DateTime.UtcNow - started > TimeSpan.FromSeconds(10)) retry = 0;
                        var fault = Config.GetDouble("futu-live-disconnect-after", 0);
                        if (fault > 0 && !forced && DateTime.UtcNow - started >= TimeSpan.FromSeconds(fault))
                        {
                            forced = true; Count("forced_disconnects");
                            throw new IOException("Converter-only disconnect test");
                        }
                    }
                }
                catch (Exception error) when (!_stop.IsCancellationRequested)
                {
                    _connected = false; Count("disconnects"); Status("disconnected", error.Message);
                    try { await Task.Delay(TimeSpan.FromSeconds(Math.Min(10, 0.5 * Math.Pow(2, Math.Min(retry++, 5)))), _stop.Token); }
                    catch (OperationCanceledException) { }
                }
                catch (Exception) when (_stop.IsCancellationRequested) { }
                finally { _connected = false; }
            }
            Status("stopped");
        }

        private void ConvertEvent(JObject body, string kind, long sequence, long received, int connection)
        {
            var data = (JObject)body["data"];
            if ((string)body["source"]?["provider"] != "futu-desktop-session" || (bool?)body["source"]?["live_push"] != true || (string)data["symbol"] != _symbol)
                throw new IOException("Unexpected live source or symbol");
            if ((bool?)data["initial_snapshot"] == true || (bool?)data["backfill"] == true) { Count("bootstrap_skipped"); return; }
            if (kind == "trades")
                foreach (var row in (JArray)data["trades"])
                {
                    if (row["trade_id"]?.Type != JTokenType.String) throw new IOException("Exact string trade ID required");
                    var id = (string)row["trade_id"];
                    var eventNs = (long)row["exchange_timestamp_ms"] * 1000000L;
                    var price = decimal.Parse(row["price"].ToString(), CultureInfo.InvariantCulture);
                    var quantity = decimal.Parse((string)row["size_decimal"], CultureInfo.InvariantCulture);
                    if (price <= 0 || quantity <= 0) throw new IOException("Invalid trade price/size");
                    var signature = eventNs + ":" + Dec(price) + ":" + Dec(quantity);
                    if (_trades.TryGetValue(id, out var previous)) { Count(previous == signature ? "duplicate_trades" : "trade_revisions"); continue; }
                    _trades[id] = signature; _seenOrder.Enqueue(id);
                    if (_seenOrder.Count > 100000) { _trades.Remove(_seenOrder.Dequeue()); Count("dedupe_evictions"); }
                    Publish(new FutuLiveTick {Symbol = _nativeSymbol, TickType = TickType.Trade, Value = price, Quantity = quantity,
                        Time = DateTime.UnixEpoch.AddTicks(eventNs / 100).ConvertFromUtc(DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"]),
                        EventNs = eventNs, ReceivedNs = received, NativeTradeId = id, Connection = connection,
                        RecordId = sequence + ":trade:" + id, TimestampBasis = "exchange"});
                }
            else if (kind == "orderbook" && (string)data["book_kind"] == "top_of_book")
            {
                if ((data["bids"] as JArray)?.Count == 0 || (data["asks"] as JArray)?.Count == 0 || (bool?)data["push"]?["state_complete"] != true)
                { Count("incomplete_books"); return; }
                var bid = data["bids"][0]; var ask = data["asks"][0];
                var bp = (decimal)bid["price"]; var ap = (decimal)ask["price"];
                var bs = (decimal)bid["volume"]; var ass = (decimal)ask["volume"];
                if (bp <= 0 || ap < bp || bs < 0 || ass < 0) throw new IOException("Invalid best bid/ask");
                var eventTime = DateTimeOffset.Parse((string)data["observed_at"], CultureInfo.InvariantCulture).UtcDateTime;
                Publish(new FutuLiveTick {Symbol = _nativeSymbol, TickType = TickType.Quote, Value = (bp + ap) / 2,
                    BidPrice = bp, AskPrice = ap, BidSize = bs, AskSize = ass,
                    Time = eventTime.ConvertFromUtc(DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"]),
                    EventNs = ToNs(eventTime), ReceivedNs = received, Connection = connection,
                    RecordId = sequence + ":quote", TimestampBasis = "provider_observed"});
            }
            else Count("archived_only_" + kind);
        }

        public static object Row(FutuLiveTick tick, long? callback = null) => tick.TickType == TickType.Trade
            ? (object)new {record_id = tick.RecordId, kind = "trade", symbol = "HK.00700", instrument_id = tick.Symbol.Value, connection = tick.Connection,
                received_ns = tick.ReceivedNs, event_ns = callback.HasValue ? ToNs(tick.Time.ConvertToUtc(DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"])) : tick.EventNs, timestamp_basis = tick.TimestampBasis,
                trade_id = tick.NativeTradeId, price = Dec(tick.Value), size = Dec(tick.Quantity), callback_ns = callback}
            : new {record_id = tick.RecordId, kind = "quote", symbol = "HK.00700", instrument_id = tick.Symbol.Value, connection = tick.Connection,
                received_ns = tick.ReceivedNs, event_ns = callback.HasValue ? ToNs(tick.Time.ConvertToUtc(DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"])) : tick.EventNs, timestamp_basis = tick.TimestampBasis,
                bid = Dec(tick.BidPrice), ask = Dec(tick.AskPrice), bid_size = Dec(tick.BidSize), ask_size = Dec(tick.AskSize), callback_ns = callback};

        private void Publish(FutuLiveTick tick)
        {
            lock (_lock)
                foreach (var pair in _subscriptions)
                    if (pair.Key.TickType == tick.TickType && pair.Value.Queue.Enqueue(tick))
                    {
                        _admitted.Write(Row(tick)); Count("admitted_" + (tick.TickType == TickType.Trade ? "trade" : "quote"));
                        pair.Value.Notify?.Invoke(this, EventArgs.Empty);
                    }
        }

        public void StopIngest()
        {
            _stop.Cancel();
            if (_worker != null && !_worker.Wait(TimeSpan.FromSeconds(25))) throw new TimeoutException("SSE converter did not stop");
        }
        public void Dispose()
        {
            StopIngest();
            lock (_lock) foreach (var value in _subscriptions.Values) value.Queue.Dispose();
            _http.Dispose();
        }
    }

    public class LocalFutuLiveAlgorithm : QCAlgorithm
    {
        private ConverterJournal _callbacks;
        private System.Threading.Timer _timer;
        private readonly ConcurrentDictionary<string, long> _counts = new();
        private DateTime? _draining;
        public override void Initialize()
        {
            if (!LiveMode) throw new InvalidOperationException("Use LEAN live-data mode");
            SetTimeZone(DateTimeZone.Utc);
            SetAccountCurrency("HKD", 100000m);
            SetBenchmark(time => 1m);
            Market.Add("hk-futu", 191);
            var hk = DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"];
            // Data ingestion must not apply US sessions or invent HK holidays.
            // AlwaysOpen is a data-only pass-through policy, not trading hours.
            MarketHoursDatabase.SetEntry("hk-futu", "00700", SecurityType.Equity, SecurityExchangeHours.AlwaysOpen(hk), hk);
            SymbolPropertiesDatabase.SetEntry("hk-futu", "00700", SecurityType.Equity,
                new SymbolProperties("Tencent live data only", "HKD", 1m, 0.000000001m, 1m, "00700"));
            AddSecurity(QuantConnect.Symbol.Create("00700", SecurityType.Equity, "hk-futu"), Resolution.Tick, fillForward: false,
                        extendedMarketHours: true, dataNormalizationMode: DataNormalizationMode.Raw);
            _callbacks = new ConverterJournal(Path.Combine(Config.Get("futu-live-output"), "callbacks.jsonl"));
            var started = DateTime.UtcNow;
            _timer = new System.Threading.Timer(_ => {
                var seconds = Config.GetDouble("futu-live-seconds", 0);
                var stopFile = Path.Combine(Config.Get("futu-live-output"), "STOP");
                if (!_draining.HasValue && ((seconds > 0 && DateTime.UtcNow - started >= TimeSpan.FromSeconds(seconds)) || File.Exists(stopFile)))
                {
                    _draining = DateTime.UtcNow;
                    LocalFutuDataQueue.Instance?.StopIngest();
                }
                else if (_draining.HasValue && DateTime.UtcNow - _draining.Value > TimeSpan.FromSeconds(3))
                    SetStatus(AlgorithmStatus.Stopped);
            }, null, 1000, 1000);
        }

        public override void OnData(Slice data)
        {
            foreach (var values in data.Ticks.Values)
                foreach (var value in values)
                {
                    if (value is not FutuLiveTick tick) throw new InvalidOperationException("Converter metadata was lost");
                    var kind = tick.TickType == TickType.Trade ? "trade" : "quote";
                    _callbacks.Write(LocalFutuDataQueue.Row(tick, LocalFutuDataQueue.NowNs()));
                    _counts.AddOrUpdate("callbacks_" + kind, 1, (_, n) => n + 1);
                    if (tick.Connection > 1) _counts.AddOrUpdate("callbacks_after_reconnect", 1, (_, n) => n + 1);
                }
        }
        public override void OnOrderEvent(OrderEvent orderEvent) => throw new InvalidOperationException("Orders are not part of this live-data observer");
        public override void OnEndOfAlgorithm()
        {
            _timer?.Dispose();
            LocalFutuDataQueue.Instance?.StopIngest();
            File.WriteAllText(Path.Combine(Config.Get("futu-live-output"), "run.json"), JsonConvert.SerializeObject(new {
                platform = "LEAN", mode = "live-data-only", symbol = "HK.00700", transport = LocalFutuDataQueue.Instance?.Snapshot(),
                counts = _counts.ToDictionary(x => x.Key, x => x.Value), live_orders = Transactions.GetOrders().Count(),
                brokerage = "local PaperBrokerage", calendar_policy = "data-only pass-through; no HK trading calendar claimed"
            }, Formatting.Indented));
        }
    }
}
