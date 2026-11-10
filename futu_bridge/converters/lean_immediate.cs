// Converter-owned adapted IOC/FOK intent. Native market/limit tickets and accounting remain native LEAN.
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
    /// <summary>Explicit joint IOC/FOK controller evaluated immediately on the current observed simulated BBO.</summary>
    public class LocalHKImmediateAlgorithm : LocalHKPaperAlgorithm
    {
        private JObject _task;
        private readonly List<object> _events = new();
        private readonly List<string> _failures = new();
        private readonly Dictionary<int, string> _identities = new();
        private OrderTicket _target;
        private string _mapping;

        /// <summary>Reuse the frozen HK cash setup and install a bounded native market-candidate adapter.</summary>
        public override void Initialize()
        {
            base.Initialize();
            _task = JObject.Parse(File.ReadAllText(GetParameter("task")));
            var mode = _task["mode"].ToString();
            if (mode != "IOC" && mode != "FOK") throw new ArgumentException("Unknown joint immediate intent");
            var size = _task["shares"].ToObject<int>();
            if (_task["capacity"].ToObject<int>() != 100 || size <= 0 || size > 200 || size % 100 != 0)
                throw new ArgumentException("Immediate policy requires declared 100-share capacity and 100/200-share intent");
            Securities[_symbol].SetFillModel(new HKImmediateFillModel(_task));
        }

        /// <summary>Convert immediate intent to native tickets only after checking current limit and complete capacity.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol)) return;
            if (_task["side"].ToString() == "sell" && Time == _task["seed"].ToObject<DateTime>())
                Register(MarketOrder(_symbol, 200, asynchronous: true, tag: "SEED"), "SEED");
            if (Time != _task["submit"].ToObject<DateTime>()) return;
            var buy = _task["side"].ToString() == "buy";
            var size = _task["shares"].ToObject<int>();
            var quantity = buy ? size : -size;
            var current = buy ? Securities[_symbol].AskPrice : Securities[_symbol].BidPrice;
            var limit = _task["limit"].ToObject<decimal>();
            if (!buy && Portfolio[_symbol].Quantity < size)
                throw new InvalidOperationException("Immediate sell requires funded long inventory");
            var marketable = buy ? current <= limit : current >= limit;
            var complete = size <= 100;
            if (marketable && (_task["mode"].ToString() == "IOC" || complete))
            {
                _mapping = "joint_limit_guard_to_native_market";
                _target = MarketOrder(_symbol, quantity, asynchronous: true, tag: "TARGET");
                Register(_target, "TARGET");
            }
            else
            {
                _mapping = "native_limit_immediately_canceled_without_execution";
                _target = LimitOrder(_symbol, quantity, limit, asynchronous: true, tag: "TARGET");
                Register(_target, "TARGET");
                Check(_target.Cancel("Joint immediate intent cannot execute at the observed BBO/capacity"));
            }
        }

        /// <summary>Retain immutable parent identities independent of mutable native cancellation tags.</summary>
        private void Register(OrderTicket ticket, string identity)
        {
            _identities[ticket.OrderId] = identity;
            Check(ticket.GetMostRecentOrderResponse());
        }

        /// <summary>Do not hide native submission or cancellation failures.</summary>
        private void Check(OrderResponse response)
        {
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Cancel only an IOC target's partial remainder; native fills and portfolios are authoritative.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            var order = Transactions.GetOrderById(e.OrderId);
            var identity = _identities.TryGetValue(e.OrderId, out var known) ? known : order.Tag;
            _identities.TryAdd(e.OrderId, identity);
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            _events.Add(new {id = identity, time = e.UtcTime.ConvertFromUtc(TimeZone)
                .ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture),
                status = e.Status.ToString(), shares = e.FillQuantity, price = e.FillPrice,
                fee = e.OrderFee.Value.Amount, cash = Portfolio.CashBook["HKD"].Amount,
                holding = Portfolio[_symbol].Quantity});
            if (identity == "TARGET" && e.Status == OrderStatus.PartiallyFilled)
            {
                if (_task["mode"].ToString() != "IOC")
                    throw new InvalidOperationException("Joint FOK intent must never emit a partial fill");
                Check(Transactions.GetOrderTicket(e.OrderId).Cancel("Joint IOC cancels its remaining quantity immediately"));
            }
            if (Portfolio.CashBook["HKD"].Amount < 0 || Portfolio[_symbol].Quantity < 0)
                _failures.Add("Immediate intent violated no-leverage/long-only constraints");
        }

        /// <summary>Expose the adapted parent intent separately from native ticket type and native GTC property.</summary>
        public override void OnEndOfAlgorithm()
        {
            var orders = Transactions.GetOrders().Select(order => new {
                id = _identities[order.Id], type = order.Type.ToString(), status = order.Status.ToString(),
                quantity = order.Quantity, filled = Transactions.GetOrderTicket(order.Id).QuantityFilled,
                native_tif = order.TimeInForce.GetType().Name}).ToArray();
            if (_target == null || !_target.Status.IsClosed()) _failures.Add("Immediate parent remained working");
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", events = _events, orders, failures = _failures, joint_tif = _task["mode"].ToString(),
                integration = "adapted_immediate_controller", mapping = _mapping,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity,
                real_orders_enabled = false}));
        }
    }

    /// <summary>Native market candidates bounded by the joint limit and explicit observed-time liquidity budget.</summary>
    public sealed class HKImmediateFillModel : FillModel
    {
        private readonly JObject _task;
        private readonly Dictionary<int, decimal> _filled = new();
        private readonly Dictionary<int, decimal> _notionals = new();
        private readonly Dictionary<int, decimal> _fees = new();
        private readonly Dictionary<(DateTime, OrderDirection), decimal> _used = new();

        /// <summary>Bind the immutable parent limit/quantity policy before any native order is submitted.</summary>
        public HKImmediateFillModel(JObject task) { _task = task; }

        /// <summary>Select the actual simulated executable-side quote instead of the required placeholder trade.</summary>
        protected override Prices GetPrices(Security security, OrderDirection direction)
        {
            var price = direction == OrderDirection.Buy ? security.AskPrice : security.BidPrice;
            var time = security.GetLastData()?.EndTime ?? DateTime.MinValue;
            return new Prices(time, time, price, price, price, price, price);
        }

        /// <summary>Guard native market candidates by limit, FOK atomic capacity and cumulative per-ticket HK fees.</summary>
        public override OrderEvent MarketFill(Security security, MarketOrder order)
        {
            var candidate = base.MarketFill(security, order);
            var empty = new OrderEvent(order, candidate.UtcTime, OrderFee.Zero);
            if (candidate.FillQuantity == 0 || order.Status.IsClosed() || order.Status == OrderStatus.CancelPending
                || security.GetLastData()?.EndTime != security.LocalTime) return empty;
            var key = (security.LocalTime, order.Direction);
            _used.TryGetValue(key, out var used);
            _filled.TryGetValue(order.Id, out var filled);
            var remainder = order.AbsoluteQuantity - filled;
            var capacity = Math.Max(0m, 100m - used);
            if (order.Tag == "TARGET")
            {
                var limit = _task["limit"].ToObject<decimal>();
                if ((order.Direction == OrderDirection.Buy ? candidate.FillPrice > limit : candidate.FillPrice < limit)
                    || (_task["mode"].ToString() == "FOK" && capacity < remainder)) return empty;
            }
            var quantity = Math.Min(remainder, capacity);
            if (quantity <= 0) return empty;
            _notionals.TryGetValue(order.Id, out var notional);
            _fees.TryGetValue(order.Id, out var charged);
            notional += quantity * candidate.FillPrice;
            var total = LocalHKTieredFeeModel.Calculate(notional);
            _filled[order.Id] = filled + quantity;
            _notionals[order.Id] = notional;
            _fees[order.Id] = total;
            _used[key] = used + quantity;
            candidate.FillQuantity = order.Direction == OrderDirection.Buy ? quantity : -quantity;
            candidate.OrderFee = new OrderFee(new CashAmount(total - charged, "HKD"));
            candidate.Status = quantity == remainder ? OrderStatus.Filled : OrderStatus.PartiallyFilled;
            return candidate;
        }
    }
}
