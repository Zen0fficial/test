// Converter-owned offline algorithm. Native LEAN equities, fills, transactions and accounting.
// Derived quotes are explicit simulation inputs, not historical exchange BBO observations.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using NodaTime;
using QuantConnect.Data;
using QuantConnect.Brokerages;
using QuantConnect.Orders;
using QuantConnect.Orders.Fees;
using QuantConnect.Orders.Fills;
using QuantConnect.Orders.Slippage;
using QuantConnect.Securities;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Offline HK execution using native transaction and cash-account models.</summary>
    public class LocalHKPaperAlgorithm : QCAlgorithm
    {
        protected Symbol _symbol;
        private readonly List<object> _fills = new();
        private readonly List<string> _failures = new();
        private readonly Dictionary<DateTime, PaperCommand> _commands = new();

        /// <summary>Define the supported captured HK session and forbid live mode.</summary>
        public override void Initialize()
        {
            if (LiveMode) throw new InvalidOperationException("Offline simulation only");
            var hk = DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"];
            SetTimeZone(hk);
            SetStartDate(DateTime.Parse(GetParameter("capture-start"), CultureInfo.InvariantCulture));
            SetEndDate(DateTime.Parse(GetParameter("capture-end"), CultureInfo.InvariantCulture));
            SetAccountCurrency("HKD", decimal.Parse(GetParameter("capital"), CultureInfo.InvariantCulture));
            SetBrokerageModel(BrokerageName.Default, AccountType.Cash);
            SetBenchmark(time => 1m);
            const string market = "hkpaper";
            Market.Add(market, 999);
            var hours = new Dictionary<DayOfWeek, LocalMarketHours>();
            foreach (DayOfWeek day in Enum.GetValues(typeof(DayOfWeek)))
                hours[day] = day == DayOfWeek.Saturday || day == DayOfWeek.Sunday
                    ? new LocalMarketHours(day)
                    : new LocalMarketHours(day,
                        new MarketHoursSegment(MarketHoursState.Market, new TimeSpan(9, 30, 0), new TimeSpan(12, 0, 0)),
                        new MarketHoursSegment(MarketHoursState.Market, new TimeSpan(13, 0, 0), new TimeSpan(16, 0, 0)));
            MarketHoursDatabase.SetEntry(market, "00700", SecurityType.Equity,
                new SecurityExchangeHours(hk, Array.Empty<DateTime>(), hours,
                    new Dictionary<DateTime, TimeSpan>(), new Dictionary<DateTime, TimeSpan>()), hk);
            SymbolPropertiesDatabase.SetEntry(market, "00700", SecurityType.Equity,
                new SymbolProperties("Captured Tencent HK paper equity", "HKD", 1m, .2m, 100m, "00700"));
            var equity = AddEquity("00700", Resolution.Tick, market, fillForward: false,
                leverage: 1m, extendedMarketHours: false, dataNormalizationMode: DataNormalizationMode.Raw);
            _symbol = equity.Symbol;
            equity.SetFillModel(new EquityFillModel());
            equity.SetSlippageModel(new ConstantSlippageModel(0m)); // Already encoded in derived bid/ask.
            equity.SetFeeModel(new LocalHKTieredFeeModel());
            equity.SetBuyingPowerModel(new SecurityMarginModel(1m)); // Native cash-equity model; no leverage.
            equity.SetSettlementModel(new ImmediateSettlementModel()); // Explicit simulation assumption.
            foreach (var command in JsonConvert.DeserializeObject<List<PaperCommand>>(
                File.ReadAllText(GetParameter("commands"))))
            {
                if (command.shares <= 0 || command.shares % 100 != 0 ||
                    (command.side != "buy" && command.side != "sell"))
                    throw new ArgumentException("Positive whole HK lots and a valid side are required");
                _commands.Add(DateTime.Parse(command.time, CultureInfo.InvariantCulture), command);
            }
        }

        /// <summary>Use only predetermined commands at an observed quote instant.</summary>
        public override void OnData(Slice data)
        {
            if (!data.Ticks.ContainsKey(_symbol) || !_commands.Remove(Time, out var command)) return;
            if (command.side == "sell" && Portfolio[_symbol].Quantity < command.shares)
            {
                _failures.Add("Long-only simulation: insufficient inventory");
                return;
            }
            if (command.side == "buy")
            {
                var cash = Portfolio.CashBook["HKD"].Amount;
                var markNotional = Securities[_symbol].Price * command.shares;
                var fillNotional = Securities[_symbol].AskPrice * command.shares;
                if (cash >= markNotional && cash < fillNotional + LocalHKTieredFeeModel.Calculate(fillNotional))
                {
                    _failures.Add("Fee reservation: available cash cannot cover side quote plus actual HK charges");
                    return;
                }
            }
            var ticket = MarketOrder(_symbol, command.side == "buy" ? command.shares : -command.shares);
            var response = ticket.GetMostRecentOrderResponse();
            if (response.IsError) _failures.Add(response.ToString());
        }

        /// <summary>Record actual native fills after native portfolio processing.</summary>
        public override void OnOrderEvent(OrderEvent orderEvent)
        {
            if (orderEvent.Status == OrderStatus.Invalid)
                _failures.Add(orderEvent.ToString());
            if (orderEvent.Status != OrderStatus.Filled && orderEvent.Status != OrderStatus.PartiallyFilled) return;
            var local = orderEvent.UtcTime.ConvertFromUtc(TimeZone);
            _fills.Add(new {time = local.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture),
                side = orderEvent.FillQuantity > 0 ? "buy" : "sell", shares = Math.Abs(orderEvent.FillQuantity),
                price = orderEvent.FillPrice, notional = Math.Abs(orderEvent.FillQuantity) * orderEvent.FillPrice,
                fee = orderEvent.OrderFee.Value.Amount, cash_after = Portfolio.CashBook["HKD"].Amount,
                holding_after = Portfolio[_symbol].Quantity, status = "filled"});
        }

        /// <summary>Publish authoritative native account state and unmatched commands.</summary>
        public override void OnEndOfAlgorithm()
        {
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                platform = "lean", engine = "BacktestingTransactionHandler/EquityFillModel/SecurityMarginModel(1x)",
                fills = _fills, ending_cash = Portfolio.CashBook["HKD"].Amount,
                ending_holding = Portfolio[_symbol].Quantity, order_count = Transactions.GetOrders().Count(),
                failures = _failures, unconsumed_commands = _commands.Count, real_orders_enabled = false}));
        }
    }

    /// <summary>Serialized prescribed command; no signal or future price is contained.</summary>
    public class PaperCommand
    {
        public string time { get; set; }
        public string side { get; set; }
        public int shares { get; set; }
    }

    /// <summary>IBKR Tier I HK scenario, debited by native LEAN portfolio accounting.</summary>
    public class LocalHKTieredFeeModel : FeeModel
    {
        /// <summary>Use the market order's side quote, matching native EquityFillModel.</summary>
        public override OrderFee GetOrderFee(OrderFeeParameters parameters)
        {
            var price = parameters.Order.Direction == OrderDirection.Buy
                ? parameters.Security.AskPrice : parameters.Security.BidPrice;
            var notional = Math.Abs(parameters.Order.Quantity) * price;
            return new OrderFee(new CashAmount(Calculate(notional), "HKD"));
        }

        /// <summary>Exact charge estimate shared by the narrow fee-reservation guard.</summary>
        public static decimal Calculate(decimal notional)
        {
            var commission = Math.Max(18m, notional * .0005m);
            var levies = notional * (.0000565m + .000042m + .000027m + .0000015m);
            var stamp = Math.Ceiling(notional * .001m);
            var fee = decimal.Round(commission + levies + stamp, 2, MidpointRounding.AwayFromZero);
            return fee;
        }
    }
}
