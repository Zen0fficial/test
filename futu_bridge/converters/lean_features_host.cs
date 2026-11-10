// Converter-owned offline host using QuantConnect LEAN's native initialization/engine/disposal APIs.
// Lifecycle follows Launcher/Program.cs at pinned commit 49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8
// (QuantConnect, Apache-2.0); only embedded-Python finalization ownership is adapted.
using System;
using System.IO;
using System.Reflection;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text.Json;
using Python.Runtime;
using QuantConnect;
using QuantConnect.Configuration;
using QuantConnect.Lean.Engine;
using QuantConnect.Logging;
using QuantConnect.Python;
using QuantConnect.Util;
using QuantConnect.Research;
using QuantConnect.Securities;
using QuantConnect.Data.Market;
using NodaTime;

/// <summary>Resolve pinned existing assemblies, then invoke the offline native engine.</summary>
public static class JointLeanPythonHost
{
    /// <summary>Run without installing dependencies, suppressing exit failures, or calling Environment.Exit.</summary>
    public static int Main(string[] args)
    {
        if (args.Length != 3 && args.Length != 4)
            throw new ArgumentException("Expected native assembly directory, config, report and optional research mode");
        var root = Path.GetFullPath(args[0]);
        AssemblyLoadContext.Default.Resolving += (context, name) => {
            var path = Path.Combine(root, name.Name + ".dll");
            return File.Exists(path) ? context.LoadFromAssemblyPath(path) : null;
        };
        return args.Length == 4 && args[3] == "research" ? RunResearch(args[1], args[2]) : Run(args[1], args[2]);
    }

    /// <summary>Run native QuantBook in an isolated process because its constructor resets global configuration/composition.</summary>
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static int RunResearch(string config, string report)
    {
        Config.MergeCommandLineArgumentsWithConfiguration(LeanArgumentParser.ParseArguments(new[] {"--config", config}));
        if (Config.GetBool("live-mode")) throw new InvalidOperationException("Offline research only");
        PythonInitializer.ActivatePythonVirtualEnvironment(Config.Get("python-venv"));
        PythonInitializer.Initialize();
        object result;
        try
        {
            result = QueryResearch(config);
        }
        finally
        {
            ShutdownPython();
        }
        File.WriteAllText(report, JsonSerializer.Serialize(new {passed = true, quantbook = result,
            python_finalized = !PythonEngine.IsInitialized, real_orders_enabled = false}));
        return 0;
    }

    /// <summary>Use the same explicit HK calendar and local real-capture files for native History and Pandas conversion.</summary>
    private static object QueryResearch(string config)
    {
        using var document = JsonDocument.Parse(File.ReadAllText(config));
        var parameters = document.RootElement.GetProperty("parameters");
        var first = DateTime.Parse(parameters.GetProperty("capture-start").GetString(), CultureInfo.InvariantCulture);
        var last = DateTime.Parse(parameters.GetProperty("capture-end").GetString(), CultureInfo.InvariantCulture);
        var book = new QuantBook();
        var hk = DateTimeZoneProviders.Tzdb["Asia/Hong_Kong"];
        book.SetTimeZone(hk);
        book.SetStartDate(last.AddDays(1));
        Market.Add("hkpaper", 999);
        var hours = new Dictionary<DayOfWeek, LocalMarketHours>();
        foreach (DayOfWeek day in Enum.GetValues(typeof(DayOfWeek)))
            hours[day] = day == DayOfWeek.Saturday || day == DayOfWeek.Sunday ? new LocalMarketHours(day)
                : new LocalMarketHours(day,
                    new MarketHoursSegment(MarketHoursState.Market, new TimeSpan(9, 30, 0), new TimeSpan(12, 0, 0)),
                    new MarketHoursSegment(MarketHoursState.Market, new TimeSpan(13, 0, 0), new TimeSpan(16, 0, 0)));
        MarketHoursDatabase.FromDataFolder().SetEntry("hkpaper", "00700", SecurityType.Equity,
            new SecurityExchangeHours(hk, Array.Empty<DateTime>(), hours,
                new Dictionary<DateTime, TimeSpan>(), new Dictionary<DateTime, TimeSpan>()), hk);
        SymbolPropertiesDatabase.FromDataFolder().SetEntry("hkpaper", "00700", SecurityType.Equity,
            new SymbolProperties("Captured HK equity", "HKD", 1m, .2m, 100m, "00700"));
        var security = book.AddEquity("00700", Resolution.Tick, "hkpaper", fillForward: false,
            leverage: 1m, extendedMarketHours: false, dataNormalizationMode: DataNormalizationMode.Raw);
        var queries = new List<object>();
        foreach (var date in new[] {first, last})
        {
            var rows = book.History<Tick>(security.Symbol, date.AddHours(9).AddMinutes(30), date.AddHours(10),
                Resolution.Tick, fillForward: false).Where(x => x.TickType == TickType.Trade).ToArray();
            int count;
            string columns, dataframe;
            using (Py.GIL())
            using (var frame = book.PandasConverter.GetDataFrame(rows))
            {
                count = frame.GetAttr("shape").GetItem(0).As<int>();
                columns = frame.GetAttr("columns").ToString();
                using var flat = frame.InvokeMethod("reset_index");
                // The index also contains native Symbol objects; serialize only the audited scalar columns.
                using var timeColumn = new PyString("time");
                using var priceColumn = new PyString("lastprice");
                using var selectedColumns = new PyList(new PyObject[] {timeColumn, priceColumn});
                using var selectedFrame = flat.GetItem(selectedColumns);
                using var encoded = selectedFrame.InvokeMethod("to_json");
                dataframe = encoded.As<string>();
            }
            queries.Add(new {date = date.ToString("yyyy-MM-dd"), dataframe_rows = count, columns, dataframe,
                rows = rows.Select(x => new {time = x.Time.ToString("yyyy-MM-dd HH:mm:ss"), price = x.Value}).ToArray()});
        }
        string prefix, pandas;
        using (Py.GIL())
        using (var sys = Py.Import("sys"))
        using (var pd = Py.Import("pandas"))
        {
            prefix = sys.GetAttr("prefix").As<string>();
            pandas = pd.GetAttr("__file__").As<string>();
        }
        book.ObjectStore.Dispose();
        // QuantBook initializes history services, not a backtest transaction manager.
        // Research mode must never access its uninitialized order pipeline.
        return new {queries, shared_prefix = prefix, pandas_file = pandas, history_only = true};
    }

    /// <summary>Retain native job loading, handlers, engine loop and deterministic teardown.</summary>
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static int Run(string config, string report)
    {
        Config.MergeCommandLineArgumentsWithConfiguration(LeanArgumentParser.ParseArguments(new[] {"--config", config}));
        if (Config.GetBool("live-mode")) throw new InvalidOperationException("This host is offline only");
        Initializer.Start();
        var system = Initializer.GetSystemHandlers();
        var job = system.JobQueue.NextJob(out var algorithmPath);
        var handlers = Initializer.GetAlgorithmHandlers();
        AlgorithmManager manager = null;
        var code = 1;
        try
        {
            if (job == null || job.Redelivered) throw new InvalidOperationException("Invalid offline job");
            PythonInitializer.ActivatePythonVirtualEnvironment(job.PythonVirtualEnvironment);
            manager = new AlgorithmManager(false, job);
            system.LeanManager.Initialize(system, handlers, job, manager);
            OS.Initialize();
            var engine = new Engine(system, handlers, false);
            engine.Run(job, manager, algorithmPath, WorkerThread.Instance);
            code = manager.State == AlgorithmStatus.Completed ? 0 : 1;
        }
        finally
        {
            if (job != null) system.JobQueue.AcknowledgeJob(job);
            system.DisposeSafely();
            handlers.DisposeSafely();
            OS.Dispose();
            ShutdownPython();
            File.WriteAllText(report, JsonSerializer.Serialize(new {
                completed = code == 0, exit_code = code, python_finalized = !PythonEngine.IsInitialized,
                engine_status = manager?.State.ToString(), real_orders_enabled = false}));
            Log.LogHandler.DisposeSafely();
        }
        return code;
    }

    /// <summary>Finalize only after native worker disposal; do not release a GIL into a destroyed interpreter.</summary>
    private static void ShutdownPython()
    {
        if (!PythonEngine.IsInitialized) return;
        var ownedGil = Py.GIL();
        try
        {
            PythonEngine.Shutdown();
            if (PythonEngine.IsInitialized) throw new InvalidOperationException("Native Python finalization failed");
            // Py_Finalize consumes the interpreter/thread state. Releasing this handle
            // afterward is invalid; the pinned stock host leaves its throwing finalizer alive.
            GC.SuppressFinalize(ownedGil);
        }
        catch
        {
            if (PythonEngine.IsInitialized) ownedGil.Dispose();
            else GC.SuppressFinalize(ownedGil);
            throw;
        }
    }
}
