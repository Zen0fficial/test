"""Native Nautilus catalogue, indicators, aggregation, batch composition and reports.

All execution is through the pinned offline BacktestNode and existing paper
strategy. Provider responses and dependency versions remain unchanged.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import shutil
from time import perf_counter

import numpy as np
import pandas as pd
from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.backtest.config import FeeModelConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import (
    ActorConfig, BacktestDataConfig, BacktestEngineConfig, BacktestRunConfig, BacktestVenueConfig,
    DataEngineConfig, ImportableActorConfig, ImportableFeeModelConfig,
    ImportableStrategyConfig, LoggingConfig, StrategyConfig,
)
from nautilus_trader.indicators import ExponentialMovingAverage, RelativeStrengthIndex, SimpleMovingAverage
from nautilus_trader.indicators.averages import MovingAverageType
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from .paper_common import capture, execution_price, schedule, utc_ns
from .paper_nautilus import HKFeeModel, PaperStrategy
from .paper_verify import verify as verify_paper
from .qlib_rl_data import ExecutionData
from .session import write_report

IDENTITY = InstrumentId.from_str("00700.HKFEATURES")
BAR_TYPE = BarType.from_str(f"{IDENTITY}-1-MINUTE-LAST-EXTERNAL")
AGGREGATE = BarType.from_str(f"{IDENTITY}-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL")


def native_indicators(close: np.ndarray, period: int = 20) -> np.ndarray:
    """Return SMA/EMA/simple RSI columns, reset per call, including partial warmup.

    EMA uses alpha=2/(period+1), seeded at the first close. RSI starts with a
    zero delta, uses simple rolling gains/losses, and returns a fraction [0,1].
    No loss yields 1. Indicators become initialized at ``period`` inputs.
    """
    values = np.asarray(close, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all() or type(period) is not int or period < 2:
        raise ValueError("Indicators require a finite one-dimensional close array and integer period >=2")
    indicators = (SimpleMovingAverage(period), ExponentialMovingAverage(period),
                  RelativeStrengthIndex(period, MovingAverageType.SIMPLE))
    output = np.empty((len(values), 3), dtype=float)
    for index, value in enumerate(values):
        for column, indicator in enumerate(indicators):
            indicator.update_raw(float(value))
            output[index, column] = indicator.value
    return output


def reference_indicators(close: np.ndarray, period: int = 20) -> np.ndarray:
    """Independent vectorized formula for the exact documented native contract."""
    values = pd.Series(np.asarray(close, dtype=float))
    delta = values.diff().fillna(0)
    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=1).mean()
    rsi = (gain / (gain + loss)).where(loss != 0, 1.0)
    return np.column_stack((values.rolling(period, min_periods=1).mean(),
                            values.ewm(span=period, adjust=False).mean(), rsi))


class IndicatorActorConfig(ActorConfig, frozen=True):
    """Explicit period for native actor/indicator composition."""

    period: int = 20


class IndicatorActor(Actor):
    """Register native indicators and aggregate completed real one-minute bars."""

    def __init__(self, config: IndicatorActorConfig) -> None:
        """Build fresh independent indicator and output state for each native run."""
        super().__init__(config)
        self.indicators = (SimpleMovingAverage(config.period), ExponentialMovingAverage(config.period),
                           RelativeStrengthIndex(config.period, MovingAverageType.SIMPLE))
        self.features: list[list[float]] = []
        self.times: list[int] = []
        self.aggregates: list[list[object]] = []

    def on_start(self) -> None:
        """Wire automatic native indicator updates before actor bar callbacks."""
        for indicator in self.indicators:
            self.register_indicator_for_bars(BAR_TYPE, indicator)
        self.subscribe_bars(BAR_TYPE)
        self.subscribe_bars(AGGREGATE)

    def on_bar(self, bar: Bar) -> None:
        """Retain native values and completed five-minute OHLCV for independent checks."""
        if self.clock.timestamp_ns() < bar.ts_event:
            raise ValueError("Actor received a bar before its availability time")
        if bar.bar_type == BAR_TYPE:
            self.times.append(bar.ts_event)
            self.features.append([i.value for i in self.indicators])
        else:
            self.aggregates.append(bar_row(bar))


class FeatureStrategyConfig(StrategyConfig, frozen=True):
    """Importable native batch strategy configuration using fixed prescribed schedules."""

    days: tuple[str, ...] = ()
    variant: str = "split"


class FeatureStrategy(PaperStrategy):
    """Reuse the existing tested native paper strategy through BacktestNode's factory."""

    def __init__(self, config: FeatureStrategyConfig) -> None:
        """Bind only the captured instrument and predeclared simulated order schedule."""
        super().__init__(IDENTITY, schedule(list(config.days), config.variant))


class ConfiguredFees(HKFeeModel):
    """Adapt the existing fee model to BacktestNode's importable configuration factory."""

    def __init__(self, config: FeeModelConfig) -> None:
        """Accept the native base fee configuration without altering the fee formula."""
        self.configuration = config


def bar_row(bar: Bar) -> list[object]:
    """Serialize native OHLCV and event time without dropping decimal precision."""
    return [bar.ts_event, *[str(getattr(bar, name).as_decimal()) for name in
                           ("open", "high", "low", "close", "volume")]]


def make_data(frame: pd.DataFrame) -> tuple[Equity, list[Bar], list[QuoteTick]]:
    """Map completed real OHLCV and explicitly derived open quotes to native records."""
    instrument = Equity(IDENTITY, Symbol("00700"), HKD, 1, Price.from_str(".2"), Quantity.from_int(100), 0, 0)
    bars, quotes = [], []
    for time, row in frame.iterrows():
        stamp = utc_ns(time)
        bars.append(Bar(BAR_TYPE, *[Price.from_str(format(Decimal(str(row[f])), ".1f")) for f in
                                   ("open", "high", "low", "close")],
                        Quantity.from_int(int(row.volume)), stamp + 60_000_000_000, stamp + 60_000_000_000))
        quotes.append(QuoteTick(IDENTITY,
                                Price.from_str(str(execution_price(Decimal(str(row.open)), "sell"))),
                                Price.from_str(str(execution_price(Decimal(str(row.open)), "buy"))),
                                Quantity.from_int(200), Quantity.from_int(200), stamp, stamp))
    return instrument, bars, quotes


def verify_bars(observed: list[Bar], frame: pd.DataFrame) -> None:
    """Compare catalogue records directly against real source fields and bar-end times."""
    expected = [[utc_ns(t) + 60_000_000_000, *[Decimal(str(r[f])) for f in
                 ("open", "high", "low", "close", "volume")]] for t, r in frame.iterrows()]
    actual = [[b.ts_event, *[getattr(b, f).as_decimal() for f in
                ("open", "high", "low", "close", "volume")]] for b in observed]
    if actual != expected:
        raise ValueError("Native catalogue OHLCV/timestamps differ from real source")


def verify_aggregate(rows: list[list[object]], frame: pd.DataFrame) -> int:
    """Check complete five-minute native bars against independent pandas aggregation."""
    grouped = frame.resample("5min", label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    expected = [[utc_ns(t), *[Decimal(str(r[f])) for f in ("open", "high", "low", "close", "volume")]]
                for t, r in grouped.iterrows()]
    actual = [[r[0], *[Decimal(v) for v in r[1:]]] for r in rows]
    if actual != expected:
        raise ValueError(f"Native aggregation mismatch: {len(actual)} != {len(expected)}; {actual[:2]}")
    return len(actual)


def batch_config(catalog: Path, days: list[str], variant: str) -> BacktestRunConfig:
    """Compose native catalogue data, Actor, Strategy, venue and fee configurations."""
    module = "futu_bridge.joint.nautilus_features"
    engine = BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True),
        data_engine=DataEngineConfig(time_bars_build_with_no_updates=False, time_bars_timestamp_on_close=True),
        actors=[ImportableActorConfig(actor_path=module + ":IndicatorActor",
                                      config_path=module + ":IndicatorActorConfig", config={"period": 20})],
        strategies=[ImportableStrategyConfig(strategy_path=module + ":FeatureStrategy",
                    config_path=module + ":FeatureStrategyConfig", config={"days": days, "variant": variant})])
    venue = BacktestVenueConfig(name="HKFEATURES", oms_type="NETTING", account_type="CASH",
        base_currency="HKD", starting_balances=["100000 HKD"], bar_execution=False,
        fee_model=ImportableFeeModelConfig(fee_model_path=module + ":ConfiguredFees",
                    config_path="nautilus_trader.backtest.config:FeeModelConfig", config={}))
    start, end = utc_ns(days[0] + " 09:30:00"), utc_ns(days[-1] + " 16:00:00")
    datasets = [BacktestDataConfig(catalog_path=catalog.as_posix(), data_cls=kind, instrument_id=IDENTITY,
                 start_time=start, end_time=end) for kind in (Bar, QuoteTick)]
    return BacktestRunConfig(engine=engine, venues=[venue], data=datasets,
                             dispose_on_completion=False, raise_exception=True)


def run(response: Path, output: Path, repo: Path, *, depth_source: Path | None = None) -> dict[str, object]:
    """Exercise native persistence, composed batch replay, analysis and real depth conversion."""
    response, output, repo = response.resolve(), output.resolve(), repo.resolve()
    if response.parent == output or response.parent in output.parents:
        raise ValueError("Feature output must be outside the source capture")
    data = capture(response)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(response, output / "provider_response.json")
    write_report(output / "status.json", {"state": "running"})
    try:
        raw = depth_source or repo / "output/joint/shared_live_20261109/nautilus/raw.jsonl"
        report = execute(data, output, raw.resolve())
        report.update(passed=True, provenance=data.provenance, providers_modified=False, real_orders_enabled=False)
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def execute(data: ExecutionData, output: Path, depth_source: Path) -> dict[str, object]:
    """Run independent native feature families and retain all native reports for review."""
    instrument, bars, quotes = make_data(data.minutes)
    path = output / "catalog"
    catalog = ParquetDataCatalog(path.as_posix())
    started = perf_counter()
    catalog.write_data([instrument])
    catalog.write_data(bars)
    catalog.write_data(quotes)
    write_seconds = perf_counter() - started
    read_started = perf_counter()
    loaded = catalog.bars(bar_types=[str(BAR_TYPE)])
    verify_bars(loaded, data.minutes)
    loaded_quotes = catalog.quote_ticks(instrument_ids=[str(IDENTITY)])
    if [QuoteTick.to_dict(q) for q in loaded_quotes] != [QuoteTick.to_dict(q) for q in quotes]:
        raise ValueError("Catalogue quote round trip lost values or timestamps")
    read_seconds = perf_counter() - read_started
    close = data.minutes.close.to_numpy(dtype=float)
    np.testing.assert_allclose(native_indicators(close), reference_indicators(close), rtol=1e-12, atol=1e-10)
    days = data.partitions["test"]
    frame = data.minutes.loc[days[0]:days[-1]]
    configs = [batch_config(path, days, v) for v in ("split", "immediate")]
    node = BacktestNode(configs)
    outputs = {}
    try:
        results = node.run()
        if len(results) != len(configs):
            raise ValueError("Missing native batch runs")
        for config, result, variant in zip(configs, results, ("split", "immediate")):
            engine = node.get_engine(config.id)
            actor, strategy = engine.trader.actors()[0], engine.trader.strategies()[0]
            if actor.times != [utc_ns(t) + 60_000_000_000 for t in frame.index]:
                raise ValueError("Actor replay lost or duplicated real bars")
            np.testing.assert_allclose(actor.features, reference_indicators(frame.close.to_numpy()),
                                       rtol=1e-12, atol=1e-10)
            count = verify_aggregate(actor.aggregates, frame)
            native = {"platform": "nautilus", "fills": strategy.fills, "failures": strategy.failures,
                      "ending_cash": str(engine.cache.account_for_venue(Venue("HKFEATURES"))
                                         .balance_total(HKD).as_decimal()),
                      "ending_holding": sum(p.signed_qty for p in engine.cache.positions_open()),
                      "order_count": len(engine.cache.orders()), "unconsumed_commands": len(strategy.commands),
                      "real_orders_enabled": False, "days": days, "schedule": variant, "bps": 5}
            check = verify_paper(data, native)
            destination = output / variant
            destination.mkdir()
            reports = {"orders": engine.trader.generate_orders_report(),
                       "fills": engine.trader.generate_fills_report(),
                       "positions": engine.trader.generate_positions_report(),
                       "account": engine.trader.generate_account_report(Venue("HKFEATURES"))}
            for name, value in reports.items():
                value.to_csv(destination / (name + ".csv"))
            if len(reports["fills"]) != len(strategy.fills) or len(reports["orders"]) != len(strategy.fills):
                raise ValueError("Native report counts differ from independently verified fills")
            pnl = Decimal(native["ending_cash"]) - 100000
            reported = sum(Decimal(str(v).split()[0]) for v in reports["positions"]["realized_pnl"])
            if (abs(reported - pnl) > Decimal(".000001")
                    or abs(Decimal(str(result.stats_pnls["HKD"]["PnL (total)"])) - pnl) > Decimal(".000001")
                    or Decimal(str(reports["account"].iloc[-1]["total"])) != Decimal(native["ending_cash"])):
                raise ValueError("Native position/account/performance reports disagree with independently audited PnL")
            pd.DataFrame(actor.features, columns=["sma20", "ema20", "rsi20"]).to_csv(
                destination / "features.csv", index=False)
            write_report(destination / "native.json", native)
            write_report(destination / "result.json", clean(asdict(result)))
            outputs[variant] = {"passed": True, "bars": len(actor.features), "aggregate_bars": count,
                                "account_verification": check,
                                "native_reports": {k: len(v) for k, v in reports.items()},
                                "native_pnl_verified": str(pnl)}
    finally:
        node.dispose()
    from .nautilus_features_depth import run_depth
    from .nautilus_features_orders import run_orders

    depth = run_depth(depth_source, output / "depth")
    extra_orders = run_orders(data, output / "extra_orders")
    return {"catalogue": {"bars": len(bars), "quotes": len(quotes), "instruments": len(catalog.instruments()),
                           "write_seconds": write_seconds, "read_and_verify_seconds": read_seconds},
            "indicators": {"rows": len(close), "columns": ["SMA20", "EMA20", "simple RSI20"],
                           "independent_parity": True}, "native_batch": outputs, "depth": depth,
            "additional_native_orders": extra_orders,
            "limitations": ["Timings include concurrent agent load and are not cross-framework rankings",
                            "Two prescribed single-stock strategies, not universal strategy composition",
                            "Actual source depth is snapshot-level, not lossless exchange deltas or queue positions",
                            "Orders use declared derived quotes, not the captured depth stream"]}


def clean(value: object) -> object:
    """Represent undefined native performance statistics as null in strict JSON."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def main() -> None:
    """Expose the native feature acceptance runner without a live execution route."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--depth-source", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, args.repo, depth_source=args.depth_source), indent=2))


if __name__ == "__main__":
    main()
