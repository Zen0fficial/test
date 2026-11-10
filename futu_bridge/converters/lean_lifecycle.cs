// Offline HK lifecycle adapter. Prices are real-open-derived; capacity is assumed.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using QuantConnect.Data;
using QuantConnect.Orders;
using QuantConnect.Orders.Fees;
using QuantConnect.Orders.Fills;
using QuantConnect.Securities;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Native LEAN order tickets and cancellations with finite offline fills.</summary>
    public class LocalHKLifecycleAlgorithm : LocalHKPaperAlgorithm
    {
        private List<LifecycleCommand> _requests;
        private readonly Dictionary<string, OrderTicket> _tickets = new();
        private readonly Dictionary<int, string> _identities = new();
        private readonly Dictionary<string, decimal> _filled = new();
        private readonly List<object> _events = new();
        private readonly List<string> _failures = new();
        private readonly Dictionary<string, JObject> _updates = new();
        private readonly HashSet<string> _partialAmended = new();
        private readonly HKLifecycleFillModel _fillModel = new();

        /// <summary>Reuse the tested HK security/calendar; configure only the lifecycle model.</summary>
        public override void Initialize()
        {
            base.Initialize(); // Also refuses LiveMode.
            _requests = JObject.Parse(File.ReadAllText(GetParameter("task")))["orders"]
                .ToObject<List<LifecycleCommand>>();
            Securities[_symbol].SetFillModel(_fillModel);
            if (GetParameter("lifecycle-clock") == "true")
                AddData<HKLifecycleClock>("LIFECLOCK", Resolution.Tick, TimeZone, fillForward: false);
        }

        /// <summary>Requests run after native matching of the current observation.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol)) return;
            foreach (var request in _requests)
            {
                if (Time == request.time)
                {
                    var quantity = request.side == "buy" ? request.shares : -request.shares;
                    if (quantity < 0 && Portfolio[_symbol].Quantity < -quantity)
                        throw new InvalidOperationException("Unfunded sell request in long-only lifecycle scenario");
                    var properties = new OrderProperties {TimeInForce = request.tif == "DAY" ? TimeInForce.Day
                        : request.tif == "GTD" ? new HKExactTimeInForce(request.expiry.Value.ConvertToUtc(TimeZone))
                        : TimeInForce.GoodTilCanceled};
                    var ticket = request.kind == "limit"
                        ? LimitOrder(_symbol, quantity, request.price, tag: request.id, orderProperties: properties)
                        : request.kind == "stop_limit"
                        ? StopLimitOrder(_symbol, quantity, request.trigger, request.price, tag: request.id,
                            orderProperties: properties)
                        : StopMarketOrder(_symbol, quantity, request.price, tag: request.id, orderProperties: properties);
                    _tickets.Add(request.id, ticket);
                    _identities[ticket.OrderId] = request.id;
                    if (ticket.GetMostRecentOrderResponse().IsError)
                        _failures.Add(ticket.GetMostRecentOrderResponse().ToString());
                }
                foreach (var change in request.amendments)
                    if (Time == change["time"].ToObject<DateTime>()) Amend(request, change);
                if (Time == request.cancel && _tickets.TryGetValue(request.id, out var pending)
                    && pending.Status != OrderStatus.Filled && pending.Status != OrderStatus.Canceled)
                {
                    var response = pending.Cancel("Prescribed offline cancellation");
                    if (response.IsError) _failures.Add(response.ToString());
                }
            }
        }

        /// <summary>Amend the original native ticket; retain cumulative fills and commission state.</summary>
        private void Amend(LifecycleCommand request, JObject change)
        {
            var ticket = _tickets[request.id];
            var quantity = change["shares"]?.ToObject<decimal>();
            if (ticket.Status == OrderStatus.Filled || ticket.Status == OrderStatus.Canceled
                || quantity <= Math.Abs(ticket.QuantityFilled))
                throw new InvalidOperationException("Amendment must leave a positive remainder; otherwise cancel");
            var fields = new UpdateOrderFields {
                Quantity = request.side == "sell" ? -quantity : quantity,
                LimitPrice = request.kind == "stop" ? null : change["price"]?.ToObject<decimal>(),
                StopPrice = (request.kind == "stop" ? change["price"] : change["trigger"])?.ToObject<decimal>()};
            _updates[request.id] = change;
            var response = ticket.Update(fields);
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Capture native acknowledgements and partial quantities; cancel via native ticket.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            var order = Transactions.GetOrderById(e.OrderId);
            var key = _identities.TryGetValue(e.OrderId, out var identity) ? identity : order.Tag;
            var time = e.UtcTime.ConvertFromUtc(TimeZone).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            if (e.Status == OrderStatus.Canceled)
                _events.Add(new {id = key, time, type = e.Message.Contains("expired") ? "expire" : "cancel"});
            if (e.Status == OrderStatus.UpdateSubmitted && _updates.Remove(key, out var change))
            {
                var entry = (JObject)change.DeepClone();
                entry["id"] = key;
                entry["time"] = time;
                entry["type"] = "amend";
                _events.Add(entry);
            }
            if (e.FillQuantity == 0) return;
            _filled.TryGetValue(key, out var prior);
            _filled[key] = prior + Math.Abs(e.FillQuantity);
            _events.Add(new {id = key, time, type = "fill", shares = Math.Abs(e.FillQuantity),
                price = e.FillPrice, fee = e.OrderFee.Value.Amount, filled = _filled[key]});
            var request = _requests.Single(x => x.id == key);
            if (request.cancel_partial && e.Status == OrderStatus.PartiallyFilled)
            {
                var response = Transactions.GetOrderTicket(e.OrderId).Cancel("Cancel remaining partial quantity");
                if (response.IsError) _failures.Add(response.ToString());
            }
            if (request.after_partial != null && e.Status == OrderStatus.PartiallyFilled && _partialAmended.Add(key))
                Amend(request, request.after_partial);
        }

        /// <summary>Publish native ticket state and native account balances.</summary>
        public override void OnEndOfAlgorithm()
        {
            var orders = _tickets.ToDictionary(x => x.Key, x => new {
                status = x.Value.Status.ToString().ToUpperInvariant(), filled = Math.Abs(x.Value.QuantityFilled),
                triggered = _fillModel.Triggered(x.Value.OrderId)});
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", events = _events, failures = _failures, orders,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity,
                real_orders_enabled = false}));
        }
    }

    /// <summary>Validated task requests produced by the Python converter.</summary>
    public class LifecycleCommand
    {
        public string id { get; set; }
        public DateTime time { get; set; }
        public string side { get; set; }
        public string kind { get; set; }
        public int shares { get; set; }
        public decimal price { get; set; }
        public DateTime? cancel { get; set; }
        public bool cancel_partial { get; set; }
        public decimal trigger { get; set; }
        public string tif { get; set; } = "GTC";
        public DateTime? expiry { get; set; }
        public List<JObject> amendments { get; set; } = new();
        public JObject after_partial { get; set; }
    }

    /// <summary>Timestamp GTD: native equity GoodTilDate instead expires at the end of its date.</summary>
    public class HKExactTimeInForce : TimeInForce
    {
        private readonly DateTime _expiry;
        public HKExactTimeInForce(DateTime expiry) { _expiry = expiry; }
        public override bool IsOrderExpired(Security security, Order order)
            => security.LocalTime.ConvertToUtc(security.Exchange.TimeZone) >= _expiry;
        public override bool IsFillValid(Security security, Order order, OrderEvent fill) => fill.UtcTime < _expiry;
    }

    /// <summary>Control stream advances time at close without replacing any equity quote.</summary>
    public class HKLifecycleClock : BaseData
    {
        public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode)
        {
            if (isLiveMode) throw new InvalidOperationException("Offline clock only");
            return new SubscriptionDataSource(Path.Combine(Globals.DataFolder, "lifecycle-clock.csv"),
                SubscriptionTransportMedium.LocalFile);
        }
        public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
            => new HKLifecycleClock {Symbol = config.Symbol,
                Time = DateTime.Parse(line, CultureInfo.InvariantCulture), Value = 1m};
    }

    /// <summary>Quote-only limit/stop fill policy with 100-share liquidity per side/instant.</summary>
    public class HKLifecycleFillModel : FillModel
    {
        private readonly Dictionary<int, decimal> _filled = new();
        private readonly Dictionary<int, decimal> _notionals = new();
        private readonly Dictionary<int, decimal> _fees = new();
        private readonly HashSet<int> _triggered = new();
        private readonly Dictionary<(DateTime, OrderDirection), decimal> _consumed = new();

        /// <summary>Expose the converter's latched trigger for independent verification.</summary>
        public bool Triggered(int id) => _triggered.Contains(id);

        /// <summary>Execute marketable native limit tickets at a bounded conservative price.</summary>
        public override OrderEvent LimitFill(Security security, LimitOrder order)
            => Execute(security, order, order.LimitPrice, false);

        /// <summary>Latch the trigger and use the actual current side quote, including gaps.</summary>
        public override OrderEvent StopMarketFill(Security security, StopMarketOrder order)
            => Execute(security, order, order.StopPrice, true);

        /// <summary>Latch native stop-limit trigger state, then enforce the remaining limit.</summary>
        public override OrderEvent StopLimitFill(Security security, StopLimitOrder order)
            => Execute(security, order, order.LimitPrice, true, order.StopPrice);

        /// <summary>Return partial native events; portfolio and transaction updates remain native.</summary>
        private OrderEvent Execute(Security security, Order order, decimal bound, bool stop, decimal? trigger = null)
        {
            var utc = security.LocalTime.ConvertToUtc(security.Exchange.TimeZone);
            var result = new OrderEvent(order, utc, OrderFee.Zero);
            if (order.Status == OrderStatus.Canceled || order.Status == OrderStatus.CancelPending
                || order.Status == OrderStatus.Filled || utc < order.Time
                || order.TimeInForce.IsOrderExpired(security, order)
                || !security.Exchange.Hours.IsOpen(security.LocalTime, false)
                || security.GetLastData()?.EndTime != security.LocalTime) return result;
            var buy = order.Direction == OrderDirection.Buy;
            var quote = buy ? security.AskPrice : security.BidPrice;
            if (stop && (buy ? quote >= (trigger ?? bound) : quote <= (trigger ?? bound)))
            {
                _triggered.Add(order.Id);
                // LEAN exposes StopTriggered with an internal setter. The custom fill
                // model therefore owns this latch without reflection or upstream edits.
            }
            var eligible = stop ? _triggered.Contains(order.Id) : (buy ? quote <= bound : quote >= bound);
            if (trigger.HasValue) eligible &= buy ? quote <= bound : quote >= bound;
            if (!eligible) return result;
            _filled.TryGetValue(order.Id, out var filled);
            var capacityKey = (security.LocalTime, order.Direction);
            _consumed.TryGetValue(capacityKey, out var used);
            var quantity = Math.Min(order.AbsoluteQuantity - filled, Math.Max(0m, 100m - used));
            if (quantity == 0m) return result;
            var price = !stop && utc > order.Time ? bound : quote;
            _notionals.TryGetValue(order.Id, out var notional);
            _fees.TryGetValue(order.Id, out var charged);
            notional += quantity * price;
            var totalFee = LocalHKTieredFeeModel.Calculate(notional);
            _filled[order.Id] = filled + quantity;
            _notionals[order.Id] = notional;
            _fees[order.Id] = totalFee;
            _consumed[capacityKey] = used + quantity;
            result.FillQuantity = buy ? quantity : -quantity;
            result.FillPrice = price;
            result.OrderFee = new OrderFee(new CashAmount(totalFee - charged, "HKD"));
            result.Status = _filled[order.Id] == order.AbsoluteQuantity ? OrderStatus.Filled : OrderStatus.PartiallyFilled;
            return result;
        }
    }
}
