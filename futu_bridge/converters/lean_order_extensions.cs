// Converter-owned offline native conditional orders. No brokerage connection is constructed.
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
    /// <summary>Exercise native trailing and touched tickets using real-open-derived quote observations.</summary>
    public class LocalHKOrderExtensionsAlgorithm : LocalHKPaperAlgorithm
    {
        private JObject _task;
        private OrderTicket _target;
        private readonly List<object> _events = new();
        private readonly List<string> _failures = new();
        private readonly Dictionary<int, string> _identities = new();
        private readonly HKNativeConditionalFillModel _fills = new();

        /// <summary>Reuse the unchanged offline cash-account, security and captured-session setup.</summary>
        public override void Initialize()
        {
            base.Initialize();
            _task = JObject.Parse(File.ReadAllText(GetParameter("task")));
            Securities[_symbol].SetFillModel(_fills);
        }

        /// <summary>Submit native factories at causal quote instants and cancel the prescribed remainder.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol)) return;
            if (_task["seed"].Type != JTokenType.Null && Time == _task["seed"].ToObject<DateTime>())
            {
                var seed = MarketOrder(_symbol, _task["shares"].ToObject<int>(), asynchronous: true, tag: "SEED");
                _identities[seed.OrderId] = "SEED";
            }
            if (Time == _task["submit"].ToObject<DateTime>())
            {
                var sell = _task["side"].ToString() == "sell";
                var quantity = _task["shares"].ToObject<int>() * (sell ? -1 : 1);
                if (sell && Portfolio[_symbol].Quantity < -quantity)
                    throw new InvalidOperationException("Conditional order would exceed funded long inventory");
                var trigger = _task["trigger"].ToObject<decimal>();
                var percentage = _task["case"].ToString().Contains("percent");
                _target = _task["case"].ToString().StartsWith("trailing")
                    ? TrailingStopOrder(_symbol, quantity, trigger, percentage ? .001m : .4m,
                        percentage, asynchronous: true, tag: "TARGET")
                    : LimitIfTouchedOrder(_symbol, quantity, trigger, _task["price"].ToObject<decimal>(),
                        asynchronous: true, tag: "TARGET");
                var response = _target.GetMostRecentOrderResponse();
                _identities[_target.OrderId] = "TARGET";
                if (response.IsError) _failures.Add(response.ToString());
            }
            if (Time == _task["cancel"].ToObject<DateTime>() && _target != null
                && _target.Status != OrderStatus.Filled && _target.Status != OrderStatus.Canceled)
            {
                var response = _target.Cancel("Prescribed offline conditional-order cancellation");
                if (response.IsError) _failures.Add(response.ToString());
            }
        }

        /// <summary>Record authoritative native fill, update and cancellation acknowledgements.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            var order = Transactions.GetOrderById(e.OrderId);
            var identity = _identities.TryGetValue(e.OrderId, out var known) ? known : order.Tag;
            var time = e.UtcTime.ConvertFromUtc(TimeZone).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            if (e.Status == OrderStatus.Canceled) _events.Add(new {id = identity, time, type = "cancel"});
            if (e.FillQuantity != 0)
                _events.Add(new {id = identity, time, type = "fill", shares = Math.Abs(e.FillQuantity),
                    price = e.FillPrice, fee = e.OrderFee.Value.Amount});
        }

        /// <summary>Publish native ticket/trigger state and cash together with exact observed fill journals.</summary>
        public override void OnEndOfAlgorithm()
        {
            var orders = Transactions.GetOrders().ToDictionary(x => _identities[x.Id], x => new {
                status = x.Status.ToString().ToUpperInvariant(),
                filled = Math.Abs(Transactions.GetOrderTicket(x.Id).QuantityFilled),
                touched = x is LimitIfTouchedOrder lit && lit.TriggerTouched,
                stop = x is TrailingStopOrder trail ? (decimal?)trail.StopPrice : null});
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", events = _events, failures = _failures, orders,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity,
                native_fill_model = "FillModel.TrailingStopFill/LimitIfTouchedFill with finite-capacity guard",
                real_orders_enabled = false}));
        }
    }

    /// <summary>Retain native triggers and trailing updates; bound fills to executable side quotes and capacity.</summary>
    public class HKNativeConditionalFillModel : FillModel
    {
        private readonly Dictionary<int, decimal> _filled = new();
        private readonly Dictionary<int, decimal> _notionals = new();
        private readonly Dictionary<int, decimal> _fees = new();
        private readonly Dictionary<(DateTime, OrderDirection), decimal> _used = new();
        private readonly HashSet<int> _activatedTrailing = new();

        /// <summary>Select current BBO instead of the required placeholder trade in LEAN's generic tick cache.</summary>
        protected override Prices GetPrices(Security security, OrderDirection direction)
        {
            var current = direction == OrderDirection.Sell ? security.BidPrice : security.AskPrice;
            var observed = security.GetLastData()?.EndTime ?? DateTime.MinValue;
            return new Prices(observed, observed, current, current, current, current, current);
        }

        /// <summary>Use native market eligibility for the explicitly funded seed position.</summary>
        public override OrderEvent MarketFill(Security security, MarketOrder order)
            => Bound(security, order, base.MarketFill(security, order));

        /// <summary>Use native conditional triggering and native mutable trailing-stop updates.</summary>
        public override OrderEvent TrailingStopFill(Security security, TrailingStopOrder order)
        {
            if (!_activatedTrailing.Contains(order.Id))
            {
                var candidate = base.TrailingStopFill(security, order);
                if (candidate.FillQuantity != 0) _activatedTrailing.Add(order.Id);
                return Bound(security, order, candidate);
            }
            // A triggered trailing stop is a market order. Native FillModel assumes a full fill;
            // this latch preserves that intent when the converter supplies finite partial capacity.
            var utc = security.LocalTime.ConvertToUtc(security.Exchange.TimeZone);
            var fill = new OrderEvent(order, utc, OrderFee.Zero);
            if (order.Status == OrderStatus.Canceled || order.Status == OrderStatus.CancelPending
                || order.Status == OrderStatus.Filled || !security.Exchange.Hours.IsOpen(security.LocalTime, false)
                || security.GetLastData()?.EndTime != security.LocalTime || utc <= order.Time) return fill;
            fill.FillQuantity = order.Quantity;
            fill.FillPrice = order.Direction == OrderDirection.Buy ? security.AskPrice : security.BidPrice;
            return Bound(security, order, fill);
        }

        /// <summary>Retain native touched latch while requiring the executable side to cross its limit.</summary>
        public override OrderEvent LimitIfTouchedFill(Security security, LimitIfTouchedOrder order)
        {
            var fill = base.LimitIfTouchedFill(security, order);
            var executable = order.Direction == OrderDirection.Buy
                ? security.AskPrice <= order.LimitPrice : security.BidPrice >= order.LimitPrice;
            if (!executable) return new OrderEvent(order, fill.UtcTime, OrderFee.Zero);
            return Bound(security, order, fill);
        }

        /// <summary>Apply explicit 100-share per-side capacity and incremental IBKR fees to native candidate fills.</summary>
        private OrderEvent Bound(Security security, Order order, OrderEvent fill)
        {
            if (fill.FillQuantity == 0) return fill;
            var key = (security.LocalTime, order.Direction);
            _used.TryGetValue(key, out var used);
            _filled.TryGetValue(order.Id, out var prior);
            var quantity = Math.Min(order.AbsoluteQuantity - prior, Math.Max(0m, 100m - used));
            if (quantity == 0) return new OrderEvent(order, fill.UtcTime, OrderFee.Zero);
            _notionals.TryGetValue(order.Id, out var notional);
            _fees.TryGetValue(order.Id, out var charged);
            notional += quantity * fill.FillPrice;
            var total = LocalHKTieredFeeModel.Calculate(notional);
            _used[key] = used + quantity;
            _filled[order.Id] = prior + quantity;
            _notionals[order.Id] = notional;
            _fees[order.Id] = total;
            fill.FillQuantity = order.Direction == OrderDirection.Buy ? quantity : -quantity;
            fill.OrderFee = new OrderFee(new CashAmount(total - charged, "HKD"));
            fill.Status = prior + quantity == order.AbsoluteQuantity ? OrderStatus.Filled : OrderStatus.PartiallyFilled;
            return fill;
        }
    }
}
