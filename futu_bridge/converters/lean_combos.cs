// Converter-owned two-equity native combo simulation. No brokerage routing or synthetic price paths.
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
using QuantConnect.Orders.TimeInForces;
using QuantConnect.Securities;
using QuantConnect.Orders.Slippage;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Native complete-group market/net-limit/leg-limit execution over aligned real daily-open observations.</summary>
    public class LocalHKCombosAlgorithm : LocalHKPaperAlgorithm
    {
        private JObject _task;
        private Symbol[] _symbols;
        private readonly List<object> _events = new();
        private readonly List<object> _states = new();
        private readonly List<string> _failures = new();
        private readonly Dictionary<int, string> _identities = new();
        private readonly List<OrderTicket> _buy = new();

        /// <summary>Retain the existing local cash setup and add one explicitly configured simulated HK equity.</summary>
        public override void Initialize()
        {
            base.Initialize();
            _task = JObject.Parse(File.ReadAllText(GetParameter("task")));
            if (_task["group_completion"].ToString() != "all_legs_complete")
                throw new ArgumentException("Finite partial combo ratios are not implemented; explicit complete groups only");
            MarketHoursDatabase.SetEntry("hkpaper", "00388", SecurityType.Equity,
                Securities[_symbol].Exchange.Hours, TimeZone);
            SymbolPropertiesDatabase.SetEntry("hkpaper", "00388", SecurityType.Equity,
                new SymbolProperties("Captured HKEX paper equity; declared 100-share simulation lot", "HKD", 1m, .2m,
                    100m, "00388"));
            var other = AddEquity("00388", Resolution.Tick, "hkpaper", fillForward: false, leverage: 1m,
                extendedMarketHours: false, dataNormalizationMode: DataNormalizationMode.Raw);
            _symbols = new[] {_symbol, other.Symbol};
            foreach (var symbol in _symbols)
            {
                var security = Securities[symbol];
                security.SetFillModel(new HKCompleteComboFillModel());
                security.SetFeeModel(new LocalHKTieredFeeModel());
                security.SetSlippageModel(new ConstantSlippageModel(0m));
                security.SetBuyingPowerModel(new SecurityMarginModel(1m));
                security.SetSettlementModel(new ImmediateSettlementModel());
            }
            AddData<HKComboClock>("COMBOCLOCK", Resolution.Tick, TimeZone, fillForward: false);
        }

        /// <summary>Submit only whole funded groups at actual observation times; clock markers never create prices.</summary>
        public override void OnData(Slice data)
        {
            var current = Stamp(Time);
            var name = _task["case"].ToString();
            if (_symbols.All(symbol => data.Ticks.ContainsKey(symbol)))
            {
                if (current == _task["buy"].ToString()) Submit(true, name);
                if (current == _task["action"].ToString())
                {
                    if (name.EndsWith("_amend"))
                    {
                        var limit = name.StartsWith("leg_") ? Securities[_symbols[0]].AskPrice + 20m
                            : _symbols.Sum(symbol => Securities[symbol].AskPrice) / 100m + 1m;
                        Check(_buy[0].Update(new UpdateOrderFields {LimitPrice = limit}));
                    }
                    else if (name.EndsWith("_cancel")) Check(_buy[0].Cancel("Cancel whole native combo"));
                }
                if (current == _task["sell"].ToString() && Portfolio[_symbols[0]].Quantity > 0)
                    Submit(false, name);
            }
            _states.Add(new {time = current, orders = Orders(), cash = Portfolio.CashBook["HKD"].Amount,
                holdings = Holdings()});
        }

        /// <summary>Use native group factories, ratio normalization and native GTC/Day/date-end GTD properties.</summary>
        private void Submit(bool buy, string name)
        {
            if (!buy && _symbols.Any(symbol => Portfolio[symbol].Quantity != 100))
                throw new InvalidOperationException("Combo exit requires exactly 100 funded shares of each leg");
            var properties = new OrderProperties {TimeInForce = name.EndsWith("_day") ? TimeInForce.Day
                : name.EndsWith("_gtd")
                ? new GoodTilDateTimeInForce(DateTime.Parse(_task["expiry"].ToString(), CultureInfo.InvariantCulture))
                : TimeInForce.GoodTilCanceled};
            var resting = buy && !name.EndsWith("roundtrip");
            var legs = _symbols.Select((symbol, index) => name.StartsWith("leg_")
                ? Leg.Create(symbol, 1, Securities[symbol].Price + (buy ? 20m : -20m)
                    - (resting && index == 0 ? 120m : 0m))
                : Leg.Create(symbol, 1)).ToList();
            var quantity = buy ? 100 : -100;
            var tag = buy ? "BUY" : "SELL";
            List<OrderTicket> tickets;
            if (name.StartsWith("market_"))
                tickets = ComboMarketOrder(legs, quantity, asynchronous: true, tag: tag, orderProperties: properties);
            else if (name.StartsWith("leg_"))
                tickets = ComboLegLimitOrder(legs, quantity, asynchronous: true, tag: tag, orderProperties: properties);
            else
            {
                // Pinned native equity combo net limits use price * leg ratio / 100.
                var limit = _symbols.Sum(symbol => Securities[symbol].Price) / 100m
                    + (buy ? 1m : -1m) - (resting ? 3m : 0m);
                tickets = ComboLimitOrder(legs, quantity, limit, asynchronous: true, tag: tag,
                    orderProperties: properties);
            }
            foreach (var ticket in tickets)
            {
                _identities[ticket.OrderId] = tag + "_" + ticket.Symbol.Value;
                Check(ticket.GetMostRecentOrderResponse());
            }
            if (buy) _buy.AddRange(tickets);
        }

        /// <summary>Fail on rejected native group submissions or amendments.</summary>
        private void Check(OrderResponse response)
        {
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Capture actual native leg events and account state without imposing identical callback timing.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            var order = Transactions.GetOrderById(e.OrderId);
            var id = _identities.TryGetValue(e.OrderId, out var known) ? known : order.Tag + "_" + order.Symbol.Value;
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            _events.Add(new {id, group = order.GroupId, time = Stamp(e.UtcTime.ConvertFromUtc(TimeZone)),
                status = e.Status.ToString(), shares = e.FillQuantity, price = e.FillPrice,
                fee = e.OrderFee.Value.Amount, message = e.Message, cash = Portfolio.CashBook["HKD"].Amount,
                holdings = Holdings()});
            if (Portfolio.CashBook["HKD"].Amount < 0 || _symbols.Any(symbol => Portfolio[symbol].Quantity < 0))
                _failures.Add("Native combo violated long-only/no-leverage account constraints");
        }

        /// <summary>Return native final group quantities, identities and exact time-in-force type.</summary>
        private object Orders() => Transactions.GetOrders().OrderBy(order => order.Id).Select(order => new {
            id = _identities.TryGetValue(order.Id, out var known) ? known : order.Tag + "_" + order.Symbol.Value,
            group = order.GroupId, members = order.GroupOrderManager?.Count,
            type = order.Type.ToString(), status = order.Status.ToString(), quantity = order.Quantity,
            filled = Transactions.GetOrderTicket(order.Id).QuantityFilled,
            tif = order.TimeInForce.GetType().Name,
            net_limit = order.GroupOrderManager?.LimitPrice,
            leg_limit = order is ComboLegLimitOrder leg ? (decimal?)leg.LimitPrice : null
        }).ToArray();

        /// <summary>Retain exact native shares per actual equity leg.</summary>
        private object Holdings() => _symbols.ToDictionary(symbol => symbol.Value, symbol => Portfolio[symbol].Quantity);

        /// <summary>Use the capture's explicit Hong Kong wall-clock representation.</summary>
        private static string Stamp(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

        /// <summary>Publish complete native group journals and cash/positions for an independent Decimal audit.</summary>
        public override void OnEndOfAlgorithm()
        {
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", events = _events, states = _states, orders = Orders(), failures = _failures,
                cash = Portfolio.CashBook["HKD"].Amount, holdings = Holdings(), real_orders_enabled = false,
                group_completion = "all_legs_complete", partial_ratio_supported = false}));
        }
    }

    /// <summary>Retain native atomic complete-group matching; supply only explicit fresh-price and fee boundaries.</summary>
    public sealed class HKCompleteComboFillModel : FillModel
    {
        /// <summary>Use the actual daily-open-derived bid/ask observation, with declared zero spread.</summary>
        protected override Prices GetPrices(Security security, OrderDirection direction)
        {
            var price = direction == OrderDirection.Buy ? security.AskPrice : security.BidPrice;
            var time = security.GetLastData()?.EndTime ?? DateTime.MinValue;
            return new Prices(time, time, price, price, price, price, price);
        }

        /// <summary>Use the native whole-group market fill implementation.</summary>
        public override List<OrderEvent> ComboMarketFill(Order order, FillModelParameters parameters)
            => Fresh(parameters) ? Cost(base.ComboMarketFill(order, parameters), parameters) : new List<OrderEvent>();

        /// <summary>Use the native net-limit aggregation, including its equity ratio scaling.</summary>
        public override List<OrderEvent> ComboLimitFill(Order order, FillModelParameters parameters)
            => Fresh(parameters) ? Cost(base.ComboLimitFill(order, parameters), parameters) : new List<OrderEvent>();

        /// <summary>Use native all-leg limit eligibility; a failing leg prevents every fill.</summary>
        public override List<OrderEvent> ComboLegLimitFill(Order order, FillModelParameters parameters)
            => Fresh(parameters) ? Cost(base.ComboLegLimitFill(order, parameters), parameters) : new List<OrderEvent>();

        /// <summary>Clock-only callbacks may expire orders but cannot reuse old prices as fresh liquidity.</summary>
        private static bool Fresh(FillModelParameters parameters)
            => parameters.SecuritiesForOrders.Values.All(security => security.GetLastData()?.EndTime == security.LocalTime);

        /// <summary>Accept native all-leg complete fills only and apply the existing per-leg HK tiered costs.</summary>
        private static List<OrderEvent> Cost(List<OrderEvent> fills, FillModelParameters parameters)
        {
            if (fills.Count == 0) return fills;
            if (fills.Count != parameters.SecuritiesForOrders.Count || fills.Any(fill => fill.Status != OrderStatus.Filled))
                throw new InvalidOperationException("Partial native combo fills are outside this declared complete-group model");
            foreach (var fill in fills)
            {
                var total = LocalHKTieredFeeModel.Calculate(Math.Abs(fill.FillQuantity) * fill.FillPrice);
                fill.OrderFee = new OrderFee(new CashAmount(total, "HKD"));
            }
            return fills;
        }
    }

    /// <summary>Control instants for actual native day-end expiry without invented equity prices.</summary>
    public class HKComboClock : BaseData
    {
        /// <summary>Read the local control schedule, rejecting live mode.</summary>
        public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode)
        {
            if (isLiveMode) throw new InvalidOperationException("Offline combo clock only");
            return new SubscriptionDataSource(Path.Combine(Globals.DataFolder, "combo-clock.csv"),
                SubscriptionTransportMedium.LocalFile);
        }

        /// <summary>Advance simulation time without changing the two equity price caches.</summary>
        public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
            => new HKComboClock {Symbol = config.Symbol,
                Time = DateTime.Parse(line, CultureInfo.InvariantCulture), Value = 1m};
    }
}
