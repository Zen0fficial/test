// Converter-owned native contingent simulation. The upstream engine owns grouping and lifecycle transitions.
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
    /// <summary>Native OCO/OUO/complete-parent OTO with real-open-derived prices and simulated tickets only.</summary>
    public class LocalHKContingentAlgorithm : LocalHKPaperAlgorithm
    {
        private JObject _task;
        private readonly List<object> _events = new();
        private readonly List<object> _states = new();
        private readonly List<string> _failures = new();
        private readonly List<object> _rejections = new();
        private readonly Dictionary<string, OrderTicket> _tickets = new();
        private bool _parentCanceled;

        /// <summary>Reuse the frozen cash-account setup, replacing only this scenario's bounded fill adapter.</summary>
        public override void Initialize()
        {
            base.Initialize();
            _task = JObject.Parse(File.ReadAllText(GetParameter("task")));
            Securities[_symbol].SetFillModel(new HKContingentFillModel());
        }

        /// <summary>Submit native grouped requests and prescribed causal amendments/cancellations.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol)) return;
            var name = _task["case"].ToString();
            if (Time == _task["start"].ToObject<DateTime>())
            {
                if (name.StartsWith("reject_")) RejectUnsupported(name);
                else if (name.StartsWith("oco_") || name.StartsWith("ouo_"))
                    Register(Order(OrderFactory.MarketOrder(_symbol, 200, true, "SEED")));
            }
            if (!name.StartsWith("reject_") && Time == _task["submit"].ToObject<DateTime>())
                SubmitGroup(name);
            foreach (var action in _task["actions"])
            {
                if (Time != action["time"].ToObject<DateTime>()) continue;
                var ticket = _tickets[action["id"].ToString()];
                if (action["kind"].ToString() == "cancel") Check(ticket.Cancel("Prescribed group cancellation"));
                else
                {
                    var buy = ticket.Quantity > 0;
                    var price = (buy ? Securities[_symbol].AskPrice : Securities[_symbol].BidPrice)
                        + (buy ? 10m : -10m);
                    Check(ticket.Update(new UpdateOrderFields {LimitPrice = price}));
                    _events.Add(new {type = "requested_amend", id = action["id"].ToString(), time = Stamp(Time), price});
                }
            }
            if (Time == _task["cleanup"].ToObject<DateTime>())
                foreach (var ticket in _tickets.Values.Where(x => !x.Status.IsClosed()).ToArray())
                    Check(ticket.Cancel("End of bounded acceptance scenario"));
            _states.Add(new {time = Stamp(Time), orders = Orders(), cash = Portfolio.CashBook["HKD"].Amount,
                holding = Portfolio[_symbol].Quantity});
        }

        /// <summary>Use native group composition; OTO children remain held until the entire parent fills.</summary>
        private void SubmitGroup(string name)
        {
            var initial = _task["initial"];
            var bid = initial["bid"].ToObject<decimal>();
            var ask = initial["ask"].ToObject<decimal>();
            var immediateExit = name == "oto_complete";
            var take = OrderFactory.LimitOrder(_symbol, -200, bid + (immediateExit ? -10m : 20m), true, "TP");
            var stop = OrderFactory.StopMarketOrder(_symbol, -200, bid - 20m, true, "SL");
            var siblings = name.StartsWith("oco_")
                ? OrderFactory.OneCancelsOther(take, stop) : OrderFactory.OneUpdatesOther(take, stop);
            if (!name.StartsWith("oto_"))
            {
                if (Portfolio[_symbol].Quantity != 200)
                    throw new InvalidOperationException("Standalone exit group requires funded long inventory");
                Register(Order(siblings));
                return;
            }
            var resting = name == "oto_held_amend" || name == "oto_child_cancel";
            var entry = OrderFactory.LimitOrder(_symbol, 200, resting ? bid - 20m : ask + 10m, true, "ENTRY");
            Register(Order(entry.Triggers(siblings)));
        }

        /// <summary>Retain actual upstream construction errors without submitting malformed native groups.</summary>
        private void RejectUnsupported(string name)
        {
            try
            {
                var first = OrderFactory.LimitOrder(_symbol, 100, 300m, true, "REJECT");
                if (name == "reject_single_oco") OrderFactory.OneCancelsOther(first);
                else if (name == "reject_duplicate_ouo") OrderFactory.OneUpdatesOther(first, first);
                else if (name == "reject_combo_bracket")
                    OrderFactory.ComboMarketOrder(new List<Leg> {Leg.Create(_symbol, 1)}, 100, true)[0]
                        .Bracket(450m, 350m);
                else throw new InvalidOperationException("Unknown native rejection case");
                _failures.Add("Unsupported native group unexpectedly succeeded");
            }
            catch (ArgumentException exception)
            {
                _rejections.Add(new {kind = name, exception = exception.GetType().Name, message = exception.Message});
            }
            catch (InvalidOperationException exception) when (name == "reject_combo_bracket")
            {
                _rejections.Add(new {kind = name, exception = exception.GetType().Name, message = exception.Message});
            }
        }

        /// <summary>Associate native ticket tags after whole-set native submission has completed.</summary>
        private void Register(IEnumerable<OrderTicket> tickets)
        {
            foreach (var ticket in tickets)
            {
                _tickets.Add(ticket.Tag, ticket);
                Check(ticket.GetMostRecentOrderResponse());
            }
        }

        /// <summary>Fail visibly on rejected updates instead of silently accepting incomplete groups.</summary>
        private void Check(OrderResponse response)
        {
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Journal native updates/fills and exercise canceling a partially filled OTO parent.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            var order = Transactions.GetOrderById(e.OrderId);
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            var identity = Identity(order);
            _events.Add(new {type = e.FillQuantity == 0 ? "status" : "fill", id = identity,
                time = Stamp(e.UtcTime.ConvertFromUtc(TimeZone)), status = e.Status.ToString(),
                quantity = order.Quantity, shares = e.FillQuantity, price = e.FillPrice,
                fee = e.OrderFee.Value.Amount, message = e.Message,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity});
            if (_task["case"].ToString() == "oto_parent_cancel" && identity == "ENTRY"
                && e.Status == OrderStatus.PartiallyFilled && !_parentCanceled)
            {
                _parentCanceled = true;
                Check(Transactions.GetOrderTicket(e.OrderId).Cancel("Cancel native partially filled parent"));
            }
            if (Portfolio.CashBook["HKD"].Amount < 0 || Portfolio[_symbol].Quantity < 0)
                _failures.Add("Native contingent account violated long-only/no-leverage constraints");
        }

        /// <summary>Expose actual native links, held flags, trigger times, quantities and ticket fills.</summary>
        private object Orders()
        {
            return Transactions.GetOrders().OrderBy(x => x.Id).Select(order => new {
                id = Identity(order), native_id = order.Id, status = order.Status.ToString(), quantity = order.Quantity,
                filled = Transactions.GetOrderTicket(order.Id).QuantityFilled,
                price = order is LimitOrder limit ? (decimal?)limit.LimitPrice : null,
                stop = order is StopMarketOrder stop ? (decimal?)stop.StopPrice : null,
                held = order.IsWaitingForTrigger(),
                triggered = order.GetTriggeredTime()?.ConvertFromUtc(TimeZone).ToString("yyyy-MM-dd HH:mm:ss"),
                set = order.Contingency?.Id, count = order.Contingency?.Count,
                links = order.Contingency?.Links.Select(link => new {id = link.Id, type = link.Type.ToString(),
                    role = link.Role?.ToString(), triggered = link.Triggered}).ToArray()
            }).ToArray();
        }

        /// <summary>Keep command identities stable when native cancellation text replaces mutable order tags.</summary>
        private string Identity(Order order)
        {
            var known = _tickets.FirstOrDefault(pair => pair.Value.OrderId == order.Id);
            return known.Key ?? order.Tag;
        }

        /// <summary>Use one unambiguous local capture time format for every native event.</summary>
        private static string Stamp(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

        /// <summary>Publish native events and account state, explicitly preserving residual unprotected inventory.</summary>
        public override void OnEndOfAlgorithm()
        {
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", events = _events, states = _states, orders = Orders(), failures = _failures,
                rejections = _rejections, cash = Portfolio.CashBook["HKD"].Amount,
                holding = Portfolio[_symbol].Quantity, real_orders_enabled = false,
                native_contingency = "OrderFactory/OrderContingency/BacktestingBrokerage",
                oto_release = "complete_parent_only"}));
        }
    }

    /// <summary>Use native candidate matching and explicit finite fills, without reproducing group lifecycle logic.</summary>
    public sealed class HKContingentFillModel : FillModel
    {
        private readonly Dictionary<int, decimal> _filled = new();
        private readonly Dictionary<int, decimal> _notionals = new();
        private readonly Dictionary<int, decimal> _fees = new();
        private readonly Dictionary<(DateTime, OrderDirection), decimal> _used = new();

        /// <summary>Select simulated executable-side quotes, never the placeholder last-trade tick.</summary>
        protected override Prices GetPrices(Security security, OrderDirection direction)
        {
            var price = direction == OrderDirection.Buy ? security.AskPrice : security.BidPrice;
            var time = security.GetLastData()?.EndTime ?? DateTime.MinValue;
            return new Prices(time, time, price, price, price, price, price);
        }

        /// <summary>Apply finite capacity to native market candidates used to seed funded exit groups.</summary>
        public override OrderEvent MarketFill(Security security, MarketOrder order)
            => Bound(security, order, base.MarketFill(security, order));

        /// <summary>Retain native strict limit crossing and stale-data checks before bounding quantity.</summary>
        public override OrderEvent LimitFill(Security security, LimitOrder order)
            => Bound(security, order, base.LimitFill(security, order));

        /// <summary>Retain native stop triggering for protective siblings.</summary>
        public override OrderEvent StopMarketFill(Security security, StopMarketOrder order)
            => Bound(security, order, base.StopMarketFill(security, order));

        /// <summary>Limit fills to 100 shares per side/observation and charge cumulative per-ticket HK tiered costs.</summary>
        private OrderEvent Bound(Security security, Order order, OrderEvent candidate)
        {
            if (candidate.FillQuantity == 0) return candidate;
            if (security.GetLastData()?.EndTime != security.LocalTime || order.Status.IsClosed()
                || order.Status == OrderStatus.CancelPending)
                return new OrderEvent(order, candidate.UtcTime, OrderFee.Zero);
            var key = (security.LocalTime, order.Direction);
            _used.TryGetValue(key, out var used);
            _filled.TryGetValue(order.Id, out var filled);
            var quantity = Math.Min(order.AbsoluteQuantity - filled, Math.Max(0m, 100m - used));
            if (quantity <= 0) return new OrderEvent(order, candidate.UtcTime, OrderFee.Zero);
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
            candidate.Status = filled + quantity == order.AbsoluteQuantity
                ? OrderStatus.Filled : OrderStatus.PartiallyFilled;
            return candidate;
        }
    }
}
