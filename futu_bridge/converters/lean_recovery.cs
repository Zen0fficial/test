// Converter-owned deterministic offline recovery. No upstream source or real brokerage routing is changed.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using QuantConnect.Data;
using QuantConnect.Orders;
using QuantConnect.Orders.Fees;
using QuantConnect.Securities;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Reconstruct native simulated tickets, accounts and scheduled commands from immutable inputs.</summary>
    public class LocalHKRecoveryAlgorithm : LocalHKPaperAlgorithm
    {
        private JObject _task;
        private OrderTicket _target;
        private bool _checkpointed;
        private bool _boundaryVerified;
        private int _prefixEvents;
        private readonly List<object> _events = new();
        private readonly List<string> _failures = new();
        private readonly HashSet<string> _requested = new();
        private readonly HKLifecycleFillModel _lifecycle = new();
        private JObject _boundary;

        /// <summary>Validate immutable bindings before initializing the native offline account and scheduled actions.</summary>
        public override void Initialize()
        {
            VerifyManifest();
            base.Initialize();
            _task = JObject.Parse(File.ReadAllText(GetParameter("task")));
            if (_task["kind"].ToString() == "trailing")
                Securities[_symbol].SetFillModel(new HKRecoveryConditionalFillModel());
            else
                Securities[_symbol].SetFillModel(_lifecycle);
            AddData<HKRecoveryClock>("RECOVERYCLOCK", Resolution.Tick, TimeZone, fillForward: false);
            var date = _task["day"].ToObject<DateTime>();
            foreach (var action in _task["actions"].Children<JObject>())
            {
                var captured = action;
                var at = captured["time"].ToObject<DateTime>();
                Schedule.On("action-" + captured["id"], DateRules.On(date.Year, date.Month, date.Day),
                    TimeRules.At(at.Hour, at.Minute, at.Second), () => ApplyAction(captured));
            }
            if (_task["checkpoint_phase"].ToString() == "scheduled")
            {
                var boundary = _task["checkpoint_at"].ToObject<DateTime>();
                Schedule.On("recovery-boundary", DateRules.On(date.Year, date.Month, date.Day),
                    TimeRules.At(boundary.Hour, boundary.Minute, boundary.Second), () => Checkpoint("scheduled"));
            }
        }

        /// <summary>Reject altered market files, command journals, binaries and converter source before replay.</summary>
        private void VerifyManifest()
        {
            var path = GetParameter("recovery-manifest");
            var manifest = JObject.Parse(File.ReadAllText(path));
            if (manifest["schema"].ToObject<int>() != 1 || manifest["real_orders_enabled"].ToObject<bool>())
                throw new InvalidOperationException("Invalid offline recovery manifest");
            var actualManifest = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant();
            var sameTask = StringComparer.OrdinalIgnoreCase.Equals(Path.GetFullPath(GetParameter("task")),
                Path.GetFullPath(manifest["task"].ToString()));
            var sameData = StringComparer.OrdinalIgnoreCase.Equals(Path.GetFullPath(Globals.DataFolder),
                Path.GetFullPath(manifest["data_directory"].ToString()));
            if (actualManifest != GetParameter("recovery-manifest-sha256")
                || GetParameter("capital") != manifest["capital"].ToString()
                || GetParameter("capture-start") != manifest["day"].ToString()
                || GetParameter("capture-end") != manifest["day"].ToString()
                || !sameTask || !sameData)
                throw new InvalidOperationException("Recovery worker configuration differs from immutable manifest");
            foreach (var item in manifest["files"].Children<JProperty>())
            {
                using var stream = File.OpenRead(item.Name);
                var actual = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
                if (actual != item.Value.ToString())
                    throw new InvalidOperationException("Recovery input/source checksum mismatch: " + item.Name);
            }
        }

        /// <summary>Submit native tickets only at the first genuine equity quote; clock-only events never create prices.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol) || Time != _task["submit"].ToObject<DateTime>()) return;
            var quantity = _task["shares"].ToObject<int>();
            var kind = _task["kind"].ToString();
            var price = _task["price"].ToObject<decimal>();
            var trigger = _task["trigger"].ToObject<decimal>();
            _target = kind == "limit" ? LimitOrder(_symbol, quantity, price, tag: "TARGET")
                : kind == "stop" ? StopMarketOrder(_symbol, quantity, trigger, tag: "TARGET")
                : kind == "stop_limit" ? StopLimitOrder(_symbol, quantity, trigger, price, tag: "TARGET")
                : TrailingStopOrder(_symbol, quantity, trigger, _task["offset"].ToObject<decimal>(), false,
                    asynchronous: true, tag: "TARGET");
            var response = _target.GetMostRecentOrderResponse();
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Use the native schedule to submit delayed cancellation/amendment requests against the same ticket.</summary>
        private void ApplyAction(JObject action)
        {
            var id = action["id"].ToString();
            if (!_requested.Add(id)) throw new InvalidOperationException("Scheduled action duplicated: " + id);
            if (_target == null) throw new InvalidOperationException("Scheduled action precedes its native ticket");
            _events.Add(new {type = "command", id, time = Stamp(UtcTime), action = action["kind"].ToString()});
            if (_target.Status == OrderStatus.Filled || _target.Status == OrderStatus.Canceled) return;
            OrderResponse response;
            if (action["kind"].ToString() == "cancel")
                response = _target.Cancel("Offline scheduled cancellation");
            else
                response = _target.Update(new UpdateOrderFields {
                    LimitPrice = action["price"]?.ToObject<decimal>(),
                    StopPrice = action["trigger"]?.ToObject<decimal>(),
                    TrailingAmount = action["offset"]?.ToObject<decimal>()});
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Record authoritative native acknowledgements and optionally checkpoint inside a partial fill callback.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            if (e.FillQuantity != 0)
                _events.Add(new {type = "fill", id = e.OrderId, time = Stamp(e.UtcTime),
                    shares = Math.Abs(e.FillQuantity).ToString(CultureInfo.InvariantCulture),
                    price = e.FillPrice.ToString(CultureInfo.InvariantCulture),
                    fee = e.OrderFee.Value.Amount.ToString(CultureInfo.InvariantCulture)});
            else if (e.Status == OrderStatus.Canceled || e.Status == OrderStatus.UpdateSubmitted)
                _events.Add(new {type = e.Status == OrderStatus.Canceled ? "cancel" : "amend_ack",
                    id = e.OrderId, time = Stamp(e.UtcTime)});
            if (!_checkpointed && _task["checkpoint_phase"].ToString() == "partial_fill"
                && e.Status == OrderStatus.PartiallyFilled)
                Checkpoint("partial_fill");
        }

        /// <summary>Return an exact UTC clock representation, independent of output paths and host locale.</summary>
        private static string Stamp(DateTime time) => time.ToString("yyyy-MM-ddTHH:mm:ss.fffffff", CultureInfo.InvariantCulture);

        /// <summary>Export deterministic economic state; omit random run IDs, process IDs and wall-clock timings.</summary>
        private JObject State()
        {
            var security = Securities[_symbol];
            var orders = Transactions.GetOrders().OrderBy(x => x.Id).Select(order => new {
                id = order.Id, type = order.Type.ToString(), status = order.Status.ToString(),
                quantity = order.Quantity.ToString(CultureInfo.InvariantCulture),
                filled = Transactions.GetOrderTicket(order.Id).QuantityFilled.ToString(CultureInfo.InvariantCulture),
                limit = order is LimitOrder limit ? (decimal?)limit.LimitPrice
                    : order is StopLimitOrder stopLimit ? stopLimit.LimitPrice : null,
                stop = order is StopMarketOrder stop ? (decimal?)stop.StopPrice
                    : order is StopLimitOrder stop2 ? stop2.StopPrice : null,
                trailing = order is TrailingStopOrder trail ? (decimal?)trail.TrailingAmount : null,
                lifecycle_triggered = _lifecycle.Triggered(order.Id), time = Stamp(order.Time)}).ToList();
            return JObject.FromObject(new {
                time = Stamp(UtcTime), orders, events = _events.ToList(), failures = _failures.ToList(),
                pending_commands = _task["actions"].Children<JObject>()
                    .Where(x => !_requested.Contains(x["id"].ToString())).Select(x => x.DeepClone()).ToList(),
                requested_commands = _requested.OrderBy(x => x).ToList(),
                cash = Portfolio.CashBook["HKD"].Amount.ToString(CultureInfo.InvariantCulture),
                holding = Portfolio[_symbol].Quantity.ToString(CultureInfo.InvariantCulture),
                average_price = Portfolio[_symbol].AveragePrice.ToString(CultureInfo.InvariantCulture),
                profit = Portfolio[_symbol].Profit.ToString(CultureInfo.InvariantCulture),
                total_fees = Portfolio[_symbol].TotalFees.ToString(CultureInfo.InvariantCulture),
                market = new {bid = security.BidPrice.ToString(CultureInfo.InvariantCulture),
                    ask = security.AskPrice.ToString(CultureInfo.InvariantCulture),
                    last = security.Price.ToString(CultureInfo.InvariantCulture),
                    observed = Stamp(security.GetLastData()?.EndTime ?? DateTime.MinValue)},
                real_orders_enabled = false});
        }

        /// <summary>Flush a boundary and stop before cleanup, or verify reconstruction before releasing the suffix.</summary>
        private void Checkpoint(string phase)
        {
            if (_checkpointed) throw new InvalidOperationException("Recovery checkpoint called more than once");
            _checkpointed = true;
            _prefixEvents = _events.Count;
            _boundary = JObject.FromObject(new {phase,
                manifest_sha256 = GetParameter("recovery-manifest-sha256"), state = State()});
            var mode = GetParameter("recovery-mode");
            if (mode == "resume")
            {
                var saved = JsonConvert.DeserializeObject<JObject>(File.ReadAllText(GetParameter("recovery-checkpoint")),
                    new JsonSerializerSettings {DateParseHandling = DateParseHandling.None,
                        FloatParseHandling = FloatParseHandling.Decimal});
                if (!JToken.DeepEquals(saved, _boundary))
                {
                    Durable(GetParameter("verification-output") + ".boundary-mismatch.json", _boundary);
                    throw new InvalidOperationException("Reconstructed native LEAN boundary differs; suffix refused");
                }
                _boundaryVerified = true;
                return;
            }
            Durable(GetParameter("recovery-checkpoint"), _boundary);
            _boundaryVerified = true;
            if (mode == "interrupt") Thread.Sleep(Timeout.Infinite);
            else if (mode != "reference") throw new InvalidOperationException("Unknown recovery mode");
        }

        /// <summary>Publish a flushed immutable JSON artifact before announcing its existence to the coordinator.</summary>
        private static void Durable(string path, JObject value)
        {
            var temporary = path + ".pending";
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                var bytes = System.Text.Encoding.UTF8.GetBytes(value.ToString(Formatting.None));
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
            File.Move(temporary, path, false);
        }

        /// <summary>Publish the native final account and exact suffix only when the committed boundary was reached.</summary>
        public override void OnEndOfAlgorithm()
        {
            if (!_checkpointed || !_boundaryVerified)
                throw new InvalidOperationException("Requested native recovery boundary was not reached and verified");
            Durable(GetParameter("verification-output"), JObject.FromObject(new {
                boundary_verified = true, checkpoint = _boundary, state = State(),
                suffix = _events.Skip(_prefixEvents).ToList(), real_orders_enabled = false}));
        }
    }

    /// <summary>Advance only the virtual clock so delayed native actions do not invent equity ticks or prices.</summary>
    public class HKRecoveryClock : BaseData
    {
        /// <summary>Read local control instants and reject live-mode invocation.</summary>
        public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode)
        {
            if (isLiveMode) throw new InvalidOperationException("Recovery clock is offline only");
            return new SubscriptionDataSource(Path.Combine(Globals.DataFolder, "recovery-clock.csv"),
                SubscriptionTransportMedium.LocalFile);
        }

        /// <summary>Supply a time marker without updating the equity security or order book.</summary>
        public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
            => new HKRecoveryClock {Symbol = config.Symbol,
                Time = DateTime.Parse(line, CultureInfo.InvariantCulture), Value = 1m};
    }

    /// <summary>Reuse verified native conditional models but forbid capacity replenishment on clock-only instants.</summary>
    public class HKRecoveryConditionalFillModel : HKNativeConditionalFillModel
    {
        /// <summary>Permit native trailing matching only when a new real equity observation is available.</summary>
        public override OrderEvent TrailingStopFill(Security security, TrailingStopOrder order)
            => security.GetLastData()?.EndTime == security.LocalTime ? base.TrailingStopFill(security, order)
                : new OrderEvent(order, security.LocalTime.ConvertToUtc(security.Exchange.TimeZone), OrderFee.Zero);
    }
}
