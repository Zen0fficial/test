// Native optimizer strategy integration using the pinned assembly's public API.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json;
using QuantConnect.Optimizer.Objectives;
using QuantConnect.Optimizer.Parameters;

namespace QuantConnect.Algorithm.CSharp
{
    /// <summary>Generate and evaluate a native grid on pre-holdout validation scores only.</summary>
    public static class HKFeaturesOptimizer
    {
        /// <summary>Use public optimizer APIs without coupling the algorithm assembly to an extra project reference.</summary>
        public static object Select(string assemblyPath, string priorResults, int fast, decimal validationCash,
            string validationEnd)
        {
            var scores = JsonConvert.DeserializeObject<List<HKValidationScore>>(File.ReadAllText(priorResults));
            scores.Add(new HKValidationScore {fast = fast, validation_cash = validationCash});
            if (scores.Count != 3 || scores.Select(x => x.fast).Distinct().Count() != 3)
                throw new InvalidOperationException("Native optimization requires all three distinct real-data candidates");
            var assembly = Assembly.LoadFrom(assemblyPath);
            dynamic strategy = Activator.CreateInstance(
                assembly.GetType("QuantConnect.Optimizer.Strategies.GridSearchOptimizationStrategy", true));
            var objective = new Target("validation_cash", new Extremum((current, next) => next > current), null);
            var parameters = new HashSet<OptimizationParameter> {
                new OptimizationStepParameter("fast-period", 10, 30, 10),
                new StaticOptimizationParameter("slow-period", "60"),
                new StaticOptimizationParameter("risk-limit", ".004")};
            strategy.Initialize(objective, null, parameters, null);
            var generated = new List<ParameterSet>();
            EventHandler<ParameterSet> receive = (sender, value) => generated.Add(value);
            strategy.NewParameterSet += receive;
            var resultType = assembly.GetType("QuantConnect.Optimizer.OptimizationResult", true);
            strategy.PushNewResults((dynamic)resultType.GetField("Initial").GetValue(null));
            foreach (var set in generated)
            {
                var period = int.Parse(set.Value["fast-period"], CultureInfo.InvariantCulture);
                var score = scores.Single(x => x.fast == period);
                var payload = new {Statistics = new {validation_cash = score.validation_cash}};
                var result = Activator.CreateInstance(resultType, JsonConvert.SerializeObject(payload), set,
                    "real-capture-sma-" + period);
                strategy.PushNewResults((dynamic)result);
            }
            if (strategy.Solution == null)
                throw new InvalidOperationException("Native grid returned no solution: " + JsonConvert.SerializeObject(
                    new {generated, scores, objective = objective.Target, current = objective.Current}));
            ParameterSet selected = strategy.Solution.ParameterSet;
            return new {native_strategy = "GridSearchOptimizationStrategy",
                generated = generated.Select(x => new {id = x.Id, value = x.Value}).ToArray(),
                scores, selected_parameters = selected.Value, validation_cash = objective.Current,
                validation_end = validationEnd, expected_backtests = strategy.GetTotalBacktestEstimate(),
                holdout_used_for_selection = false};
        }
    }

    /// <summary>Verified validation-window objective supplied to the native optimizer.</summary>
    public class HKValidationScore
    {
        public int fast { get; set; }
        public decimal validation_cash { get; set; }
    }
}
