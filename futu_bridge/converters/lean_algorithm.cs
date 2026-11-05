// Local historical data observer. Trading is manual; this probe submits no orders.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using QuantConnect.Data;
using QuantConnect.Orders;
using NodaTime;

namespace QuantConnect.Algorithm.CSharp
{
    public class LocalProviderCompatibilityAlgorithm : QCAlgorithm
    {
        private readonly List<string[]> _rows = new();
        private readonly HashSet<Symbol> _equities = new();

        public override void Initialize()
        {
            if (LiveMode) throw new InvalidOperationException("This probe is backtest-only");
            SetTimeZone(DateTimeZone.Utc);
            SetStartDate(DateTime.Parse(GetParameter("capture-start"), CultureInfo.InvariantCulture));
            // Include the final UTC midnight probability bucket delivery.
            SetEndDate(DateTime.Parse(GetParameter("capture-end"), CultureInfo.InvariantCulture).AddDays(1));
            if (EndDate < DateTime.Parse(GetParameter("capture-end"), CultureInfo.InvariantCulture).AddDays(1))
                throw new InvalidOperationException("LEAN clamped the final probability delivery date; select an earlier capture window");
            SetCash(1000000);
            SetBenchmark(time => 1m); // No implicit external benchmark data subscription.
            foreach (var ticker in GetParameter("tickers").Split(','))
            {
                var equity = AddEquity(ticker, Resolution.Minute, Market.USA, fillForward: false,
                    extendedMarketHours: false, dataNormalizationMode: DataNormalizationMode.Raw);
                _equities.Add(equity.Symbol);
            }
            for (var i = 0; i < int.Parse(GetParameter("outcome-count"), CultureInfo.InvariantCulture); i++)
                AddData<LocalPolymarketProbability>("PM" + i, Resolution.Minute, DateTimeZone.Utc, fillForward: false);
        }

        private static string Ns(DateTime utc) =>
            ((utc.Ticks - new DateTime(1970, 1, 1).Ticks) * 100L).ToString(CultureInfo.InvariantCulture);
        private static string Dec(decimal value) => value.ToString(CultureInfo.InvariantCulture);

        public override void OnData(Slice data)
        {
            foreach (var pair in data.Bars)
            {
                if (!_equities.Contains(pair.Key)) continue;
                var bar = pair.Value;
                // US equity timestamps are exchange-local; the algorithm clock is UTC.
                var endUtc = bar.EndTime.ConvertToUtc(Securities[pair.Key].Exchange.TimeZone);
                if (UtcTime < endUtc) throw new InvalidOperationException("Bar delivered before its close");
                _rows.Add(new[] {"bar", "US." + pair.Key.Value, Ns(endUtc),
                    Dec(bar.Open), Dec(bar.High), Dec(bar.Low), Dec(bar.Close), Dec(bar.Volume)});
            }
            foreach (var item in data.Get<LocalPolymarketProbability>().Values)
            {
                if (UtcTime < item.EndTime) throw new InvalidOperationException("Probability delivered before bucket end");
                _rows.Add(new[] {"probability", item.TokenId, Ns(item.EndTime), Dec(item.Value)});
            }
        }

        public override void OnOrderEvent(OrderEvent orderEvent)
        {
            throw new InvalidOperationException("Orders are not part of this data-only observer");
        }

        public override void OnEndOfAlgorithm()
        {
            File.WriteAllText(GetParameter("verification-output"),
                JsonConvert.SerializeObject(new {rows = _rows, submitted_orders = Transactions.GetOrders().Count()}));
        }
    }

    public class LocalPolymarketProbability : BaseData
    {
        public string TokenId { get; set; }
        public DateTime ProviderTimestamp { get; set; }
        public int ResolutionSeconds { get; set; }

        public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode)
        {
            if (isLiveMode) throw new InvalidOperationException("Historical capture reader only");
            return new SubscriptionDataSource(Path.Combine(Globals.DataFolder, "custom", "polymarket",
                config.Symbol.Value.ToLowerInvariant() + "_" + date.ToString("yyyyMMdd") + ".csv"),
                SubscriptionTransportMedium.LocalFile);
        }

        public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
        {
            if (string.IsNullOrWhiteSpace(line)) return null;
            var fields = line.Split(',');
            var providerTime = DateTimeOffset.FromUnixTimeSeconds(long.Parse(fields[0], CultureInfo.InvariantCulture)).UtcDateTime;
            var width = int.Parse(fields[1], CultureInfo.InvariantCulture);
            return new LocalPolymarketProbability {
                Symbol = config.Symbol, Time = providerTime.AddSeconds(width),
                ProviderTimestamp = providerTime, ResolutionSeconds = width,
                Value = decimal.Parse(fields[2], CultureInfo.InvariantCulture), TokenId = fields[3]
            };
        }
    }
}
