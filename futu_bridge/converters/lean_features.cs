// Converter-owned native Algorithm Framework integration; exclusively offline.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using QuantConnect.Algorithm.Framework.Alphas;
using QuantConnect.Algorithm.Framework.Execution;
using QuantConnect.Algorithm.Framework.Portfolio;
using QuantConnect.Algorithm.Framework.Risk;
using QuantConnect.Algorithm.Framework.Selection;
using QuantConnect.Data;
using QuantConnect.Data.Consolidators;
using QuantConnect.Data.Market;
using QuantConnect.Data.UniverseSelection;
using QuantConnect.Indicators;
using QuantConnect.Orders;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Exercise native framework composition, research data operations and simulated accounting.</summary>
    public class LocalHKFeaturesAlgorithm : LocalHKPaperAlgorithm
    {
        private readonly List<object> _trace = new();
        private readonly List<object> _features = new();
        private readonly List<object> _bars = new();
        private readonly List<object> _history = new();
        private readonly List<object> _universes = new();
        private readonly List<string> _clocks = new();
        private readonly Dictionary<string, decimal> _dailyCash = new();
        private readonly List<string> _failures = new();
        private readonly List<decimal> _observed = new();
        private TickConsolidator _consolidator;
        private SimpleMovingAverage _fast;
        private SimpleMovingAverage _slow;
        private ExponentialMovingAverage _ema;
        private int _fastPeriod;
        private int _slowPeriod;
        private decimal _riskLimit;

        /// <summary>Initialize the fixed HK security and native Algorithm Framework models.</summary>
        public override void Initialize()
        {
            base.Initialize(); // Rejects LiveMode and installs the fixed offline HK market contract.
            Settings.FreePortfolioValuePercentage = 0m; // The 45% target already leaves ample cash.
            Settings.MinimumOrderMarginPortfolioPercentage = 0m;
            _fastPeriod = int.Parse(GetParameter("fast-period"), CultureInfo.InvariantCulture);
            _slowPeriod = int.Parse(GetParameter("slow-period"), CultureInfo.InvariantCulture);
            _riskLimit = decimal.Parse(GetParameter("risk-limit"), CultureInfo.InvariantCulture);
            if (_fastPeriod < 2 || _slowPeriod <= _fastPeriod || _riskLimit <= 0 || _riskLimit > .05m)
                throw new ArgumentException("Invalid framework parameters");
            _fast = new SimpleMovingAverage(_fastPeriod);
            _slow = new SimpleMovingAverage(_slowPeriod);
            _ema = new ExponentialMovingAverage(_fastPeriod);
            _consolidator = new TickConsolidator(TimeSpan.FromMinutes(5));
            _consolidator.DataConsolidated += (sender, bar) => _bars.Add(new {
                time = Stamp(bar.Time), end = Stamp(bar.EndTime), available = Stamp(Time),
                open = bar.Open, high = bar.High, low = bar.Low, close = bar.Close, volume = bar.Volume});
            SubscriptionManager.AddConsolidator(_symbol, _consolidator);
            AddData<HKFeatureClock>("FEATURECLOCK", Resolution.Tick, TimeZone, fillForward: false);
            UniverseSettings.Resolution = Resolution.Tick;
            UniverseSettings.FillForward = false;
            SetUniverseSelection(new ScheduledUniverseSelectionModel(DateRules.EveryDay(_symbol),
                TimeRules.At(9, 30), time => {
                    _universes.Add(new {time = Stamp(time.ConvertFromUtc(TimeZone)), symbol = _symbol.Value});
                    return new[] {_symbol};
                }));
            SetAlpha(new HKFeatureAlpha(this));
            SetPortfolioConstruction(new HKFeaturePortfolio(this));
            SetRiskManagement(new HKFeatureRisk(this, _riskLimit));
            SetExecution(new ImmediateExecutionModel(asynchronous: false));
            InsightsGenerated += (algorithm, args) => {
                foreach (var insight in args.Insights)
                    _trace.Add(new {type = "insight", time = Stamp(Time), direction = insight.Direction.ToString(),
                        weight = insight.Weight, generated = Stamp(insight.GeneratedTimeUtc.ConvertFromUtc(TimeZone)),
                        expires = Stamp(insight.CloseTimeUtc.ConvertFromUtc(TimeZone))});
            };
        }

        /// <summary>Normalize persisted local times without losing observation ordering.</summary>
        public static string Stamp(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

        /// <summary>Update native indicators from observed trade ticks; fetch causally bounded native history.</summary>
        public override void OnData(Slice slice)
        {
            if (slice.Get<HKFeatureClock>().Count != 0)
            {
                _clocks.Add(Stamp(Time));
                _consolidator.Scan(Time);
                if (Portfolio[_symbol].Quantity != 0)
                    throw new InvalidOperationException("Expected prescribed end-of-day flat portfolio");
                _dailyCash[Time.ToString("yyyy-MM-dd")] = Portfolio.CashBook["HKD"].Amount;
            }
            if (!slice.Ticks.TryGetValue(_symbol, out var ticks)) return;
            foreach (var tick in ticks.Where(x => x.TickType == TickType.Trade))
            {
                _fast.Update(tick.Time, tick.Value);
                _slow.Update(tick.Time, tick.Value);
                _ema.Update(tick.Time, tick.Value);
                _observed.Add(tick.Value);
                _features.Add(new {time = Stamp(tick.Time), source = tick.Value, fast = _fast.Current.Value,
                    slow = _slow.Current.Value, ema = _ema.Current.Value, ready = _slow.IsReady});
            }
            if (Time.TimeOfDay == new TimeSpan(10, 0, 0))
            {
                var history = History<Tick>(_symbol, Time.AddMinutes(-30), Time, Resolution.Tick,
                    fillForward: false).Where(x => x.TickType == TickType.Trade).ToArray();
                _history.Add(new {requested = Stamp(Time), rows = history.Select(x => new {
                    time = Stamp(x.Time), value = x.Value}).ToArray()});
            }
        }

        /// <summary>Record native partial/final fills and native account states for an external ledger audit.</summary>
        public override void OnOrderEvent(OrderEvent e)
        {
            if (e.Status == OrderStatus.Invalid) _failures.Add(e.ToString());
            if (e.FillQuantity == 0) return;
            _trace.Add(new {type = "fill", time = Stamp(e.UtcTime.ConvertFromUtc(TimeZone)), id = e.OrderId,
                quantity = e.FillQuantity, price = e.FillPrice, fee = e.OrderFee.Value.Amount,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity});
        }

        /// <summary>Persist native results and full benchmark samples; never route or deploy a model.</summary>
        public override void OnEndOfAlgorithm()
        {
            var optimizer = string.IsNullOrEmpty(GetParameter("optimizer-input")) ? null
                : HKFeaturesOptimizer.Select(GetParameter("optimizer-assembly"), GetParameter("optimizer-input"),
                    _fastPeriod, _dailyCash[GetParameter("validation-end")], GetParameter("validation-end"));
            File.WriteAllText(GetParameter("verification-output"), JsonConvert.SerializeObject(new {
                passed = _failures.Count == 0, platform = "lean", trace = _trace, features = _features,
                consolidated = _bars, history = _history, universes = _universes, clocks = _clocks, failures = _failures,
                fast_period = _fastPeriod, slow_period = _slowPeriod, risk_limit = _riskLimit,
                daily_cash = _dailyCash, optimizer,
                cash = Portfolio.CashBook["HKD"].Amount, holding = Portfolio[_symbol].Quantity,
                benchmark = Benchmark(_observed.ToArray()), real_orders_enabled = false}));
        }

        /// <summary>Compare identical rolling means on real prices after warmup, with repeated full-series timings.</summary>
        private static object Benchmark(decimal[] prices)
        {
            var nativeTimes = new List<double>();
            var scanTimes = new List<double>();
            var rollingTimes = new List<double>();
            var nativeValues = new decimal[prices.Length];
            var scanValues = new decimal[prices.Length];
            var rollingValues = new decimal[prices.Length];
            decimal nativeResult = 0, scanResult = 0, rollingResult = 0;
            for (var repeat = -1; repeat < 7; repeat++)
            {
                var clock = Stopwatch.StartNew();
                var mean = new SimpleMovingAverage(60);
                for (var i = 0; i < prices.Length; i++)
                {
                    mean.Update(new DateTime(2026, 1, 1).AddMinutes(i), prices[i]);
                    nativeResult = mean.Current.Value;
                    nativeValues[i] = nativeResult;
                }
                clock.Stop();
                if (repeat >= 0) nativeTimes.Add(clock.Elapsed.TotalMilliseconds);
                clock.Restart();
                for (var i = 0; i < prices.Length; i++)
                {
                    decimal sum = 0;
                    for (var j = Math.Max(0, i - 59); j <= i; j++) sum += prices[j];
                    scanResult = sum / Math.Min(60, i + 1);
                    scanValues[i] = scanResult;
                }
                clock.Stop();
                if (repeat >= 0) scanTimes.Add(clock.Elapsed.TotalMilliseconds);
                clock.Restart();
                decimal rolling = 0;
                for (var i = 0; i < prices.Length; i++)
                {
                    rolling += prices[i];
                    if (i >= 60) rolling -= prices[i - 60];
                    rollingResult = rolling / Math.Min(60, i + 1);
                    rollingValues[i] = rollingResult;
                }
                clock.Stop();
                if (repeat >= 0) rollingTimes.Add(clock.Elapsed.TotalMilliseconds);
            }
            if (!nativeValues.SequenceEqual(scanValues) || !nativeValues.SequenceEqual(rollingValues))
                throw new InvalidOperationException("Rolling mean benchmark changed the numerical contract");
            var timings = new Dictionary<string, double> {
                ["native"] = nativeTimes.OrderBy(x => x).ElementAt(3),
                ["window_scan"] = scanTimes.OrderBy(x => x).ElementAt(3),
                ["rolling_sum"] = rollingTimes.OrderBy(x => x).ElementAt(3)};
            var selected = timings.OrderBy(x => x.Value).First().Key;
            var series = selected == "native" ? nativeValues : selected == "window_scan" ? scanValues : rollingValues;
            return new {observations = prices.Length, period = 60, native_ms = nativeTimes,
                window_scan_ms = scanTimes, rolling_sum_ms = rollingTimes, final_value = nativeResult,
                parity = true, selected_batch_implementation = selected, selected_series = series,
                streaming_implementation = "native_incremental_SMA",
                reason = "Select lowest median of seven post-warmup runs after full-series exact parity"};
        }

        /// <summary>Causal long/flat alpha using current native moving averages.</summary>
        private sealed class HKFeatureAlpha : AlphaModel
        {
            private readonly LocalHKFeaturesAlgorithm _owner;
            private DateTime _last;

            /// <summary>Bind the algorithm's observed native indicator state.</summary>
            public HKFeatureAlpha(LocalHKFeaturesAlgorithm owner) { _owner = owner; }

            /// <summary>Generate timed native insights without reading future bars or external data.</summary>
            public override IEnumerable<Insight> Update(QCAlgorithm algorithm, Slice data)
            {
                if (!data.Ticks.ContainsKey(_owner._symbol) || !_owner._slow.IsReady
                    || algorithm.Time == _last || algorithm.Time.Minute % 30 != 0)
                    return Enumerable.Empty<Insight>();
                _last = algorithm.Time;
                var flat = algorithm.Time.TimeOfDay >= new TimeSpan(15, 30, 0)
                    || _owner._fast.Current.Value <= _owner._slow.Current.Value;
                return new[] {Insight.Price(_owner._symbol, TimeSpan.FromMinutes(45),
                    flat ? InsightDirection.Flat : InsightDirection.Up, weight: .45)};
            }
        }

        /// <summary>Retain native insight weighting and lot sizing while journaling generated targets.</summary>
        private sealed class HKFeaturePortfolio : InsightWeightingPortfolioConstructionModel
        {
            private readonly LocalHKFeaturesAlgorithm _owner;

            /// <summary>Use native long-only portfolio weighting and event-driven rebalancing.</summary>
            public HKFeaturePortfolio(LocalHKFeaturesAlgorithm owner)
                : base(Resolution.Daily, PortfolioBias.Long) { _owner = owner; }

            /// <summary>Expose targets from the native implementation without replacing its calculations.</summary>
            public override IEnumerable<IPortfolioTarget> CreateTargets(QCAlgorithm algorithm, Insight[] insights)
            {
                var targets = base.CreateTargets(algorithm, insights).ToArray();
                foreach (var target in targets)
                    _owner._trace.Add(new {type = "target", time = Stamp(algorithm.Time), quantity = target.Quantity});
                return targets;
            }
        }

        /// <summary>Retain native drawdown management and insight cancellation with independently auditable inputs.</summary>
        private sealed class HKFeatureRisk : MaximumDrawdownPercentPerSecurity
        {
            private readonly LocalHKFeaturesAlgorithm _owner;

            /// <summary>Bind the configured per-security loss threshold.</summary>
            public HKFeatureRisk(LocalHKFeaturesAlgorithm owner, decimal limit) : base(limit) { _owner = owner; }

            /// <summary>Record pre-execution holdings and native override targets for every invested observation.</summary>
            public override IEnumerable<IPortfolioTarget> ManageRisk(QCAlgorithm algorithm, IPortfolioTarget[] targets)
            {
                var security = algorithm.Securities[_owner._symbol];
                var overrides = base.ManageRisk(algorithm, targets).ToArray();
                if (security.Invested)
                    _owner._trace.Add(new {type = "risk", time = Stamp(algorithm.Time),
                        holding = security.Holdings.Quantity, average = security.Holdings.AveragePrice,
                        bid = security.BidPrice, pnl = security.Holdings.UnrealizedProfitPercent,
                        override_quantity = overrides.Length == 0 ? (decimal?)null : overrides.Single().Quantity});
                return overrides;
            }
        }
    }

    /// <summary>Clock-only observations close native time consolidators without inventing equity prices.</summary>
    public class HKFeatureClock : BaseData
    {
        /// <summary>Read only converter-produced local clock control files.</summary>
        public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode)
        {
            if (isLiveMode) throw new InvalidOperationException("Offline clock only");
            return new SubscriptionDataSource(Path.Combine(Globals.DataFolder, "feature-clock",
                date.ToString("yyyyMMdd") + ".csv"), SubscriptionTransportMedium.LocalFile);
        }

        /// <summary>Create a non-equity control event with no trade or quote interpretation.</summary>
        public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
            => new HKFeatureClock {Symbol = config.Symbol,
                Time = DateTime.Parse(line, CultureInfo.InvariantCulture), Value = 1m};
    }
}
