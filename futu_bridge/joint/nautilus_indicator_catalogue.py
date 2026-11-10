"""Typed, closed registry of every concrete public indicator in pinned Nautilus.

Parameters are explicit JSON-compatible values, translated to native enums at
the boundary. No user-supplied class import or executable expression is accepted.
Native parameter validation remains authoritative after the joint type checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Literal, TypeAlias

import nautilus_trader.indicators as native
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import InstrumentId

Parameter: TypeAlias = int | float | bool | str | tuple[float, ...]
Route: TypeAlias = Literal["bar", "quote"]
GapPolicy: TypeAlias = Literal["carry", "session_reset"]
IDENTITY = "00700.HKINDICATORS"


@dataclass(frozen=True)
class IndicatorSpec:
    """Native constructor defaults, raw input signature and explicitly exported fields."""

    name: str
    defaults: tuple[tuple[str, Parameter], ...]
    inputs: tuple[str, ...]
    fields: tuple[str, ...] = ("value",)


def spec(name: str, inputs: str, fields: str = "value", **defaults: Parameter) -> IndicatorSpec:
    """Declare a native signature without inferred constructor parameters."""
    return IndicatorSpec(name, tuple(defaults.items()), tuple(inputs.split()), tuple(fields.split()))


SPECS = (
    spec("AdaptiveMovingAverage", "close", period_er=10, period_alpha_fast=2,
         period_alpha_slow=30, price_type="LAST"),
    spec("ArcherMovingAveragesTrends", "close", "long_run short_run", fast_period=5,
         slow_period=10, signal_period=3, ma_type="EXPONENTIAL"),
    spec("AroonOscillator", "high low", "aroon_down aroon_up value", period=14),
    spec("AverageTrueRange", "high low close", period=14, ma_type="SIMPLE", use_previous=True, value_floor=0.0),
    spec("Bias", "close", period=14, ma_type="SIMPLE"),
    spec("BollingerBands", "high low close", "lower middle upper", period=20, k=2.0, ma_type="SIMPLE"),
    spec("ChandeMomentumOscillator", "close", period=14, ma_type="SIMPLE"),
    spec("CommodityChannelIndex", "high low close", period=14, scalar=0.015, ma_type="SIMPLE"),
    spec("DirectionalMovement", "high low", "neg pos value", period=14, ma_type="EXPONENTIAL"),
    spec("DonchianChannel", "high low", "lower middle upper", period=14),
    spec("DoubleExponentialMovingAverage", "close", period=14, price_type="LAST"),
    spec("EfficiencyRatio", "close", period=14),
    spec("ExponentialMovingAverage", "close", period=14, price_type="LAST"),
    spec("FuzzyCandlesticks", "open high low close", "direction size body_size upper_wick_size lower_wick_size",
         period=14, threshold1=0.5, threshold2=1.0, threshold3=2.0, threshold4=3.0),
    spec("HullMovingAverage", "close", period=14, price_type="LAST"),
    spec("IchimokuCloud", "high low close", "chikou_span kijun_sen senkou_span_a senkou_span_b tenkan_sen",
         tenkan_period=9, kijun_period=26, senkou_period=52, displacement=26),
    spec("KeltnerChannel", "high low close", "lower middle upper", period=14, k_multiplier=2.0,
         ma_type="EXPONENTIAL", ma_type_atr="SIMPLE", use_previous=True, atr_floor=0.0),
    spec("KeltnerPosition", "high low close", period=14, k_multiplier=2.0,
         ma_type="EXPONENTIAL", ma_type_atr="SIMPLE", use_previous=True, atr_floor=0.0),
    spec("KlingerVolumeOscillator", "high low close volume", fast_period=34,
         slow_period=55, signal_period=13, ma_type="EXPONENTIAL"),
    spec("LinearRegression", "close", "R2 cfo degree intercept slope value", period=14),
    spec("MovingAverageConvergenceDivergence", "close", fast_period=12, slow_period=26,
         ma_type="EXPONENTIAL", price_type="LAST"),
    spec("OnBalanceVolume", "open close volume", period=0),
    spec("Pressure", "high low close volume", "value value_cumulative",
         period=14, ma_type="EXPONENTIAL", atr_floor=0.0),
    spec("PsychologicalLine", "close", period=14, ma_type="SIMPLE"),
    spec("RateOfChange", "close", period=14, use_log=False),
    spec("RelativeStrengthIndex", "close", period=14, ma_type="SIMPLE"),
    spec("RelativeVolatilityIndex", "close", period=14, scalar=100.0, ma_type="SIMPLE"),
    spec("SimpleMovingAverage", "close", period=14, price_type="LAST"),
    spec("SpreadAnalyzer", "", "current average", instrument_id=IDENTITY, capacity=20),
    spec("Stochastics", "high low close", "value_k value_d", period_k=14, period_d=3,
         slowing=1, ma_type="SIMPLE", d_method="ratio"),
    spec("Swings", "high low timestamp", "changed direction duration high_datetime high_price length "
         "low_datetime low_price since_high since_low", period=14),
    spec("VariableIndexDynamicAverage", "close", "value cmo_pct", period=14,
         price_type="LAST", cmo_ma_type="SIMPLE"),
    spec("VerticalHorizontalFilter", "close", period=14, ma_type="SIMPLE"),
    spec("VolatilityRatio", "high low close", fast_period=5, slow_period=14,
         ma_type="SIMPLE", use_previous=True, value_floor=0.0),
    spec("VolumeWeightedAveragePrice", "typical volume timestamp"),
    spec("WeightedMovingAverage", "close", period=14, weights=tuple(float(x) for x in range(1, 15)),
         price_type="LAST"),
    spec("WilderMovingAverage", "close", period=14, price_type="LAST"),
)
REGISTRY = {item.name: item for item in SPECS}


@dataclass(frozen=True)
class IndicatorConfig:
    """Validated immutable request; gap policy is explicit and defaults to carrying state."""

    name: str
    parameters: tuple[tuple[str, Parameter], ...]
    route: Route = "bar"
    gap_policy: GapPolicy = "carry"

    def __post_init__(self) -> None:
        """Reject unknown names, duplicate parameters and implicit scalar coercions."""
        if self.name not in REGISTRY or self.route not in ("bar", "quote"):
            raise ValueError("Unknown indicator or native input route")
        if self.gap_policy not in ("carry", "session_reset"):
            raise ValueError("Unknown gap policy")
        defaults, values = dict(REGISTRY[self.name].defaults), dict(self.parameters)
        if len(values) != len(self.parameters) or values.keys() != defaults.keys():
            raise ValueError("Supply exactly the declared parameter names once")
        for key, value in values.items():
            default = defaults[key]
            if type(value) is not type(default):
                raise TypeError(f"{key} requires {type(default).__name__}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Indicator parameters must be finite")
            if type(value) is int:
                minimum = 0 if (self.name == "OnBalanceVolume" and key == "period") or key == "displacement" else 1
                if value < minimum or value > 1_000_000:
                    raise ValueError("Period/capacity must satisfy the declared native domain and bounded allocation")
            if isinstance(value, tuple) and (not value or any(type(x) is not float or not math.isfinite(x)
                                                             or x < 0 for x in value)):
                raise ValueError("Weights require a nonempty tuple of finite nonnegative floats")
        if "weights" in values and len(values["weights"]) != values["period"]:
            raise ValueError("Weights must match the requested moving-average period")
        if not supported(self.name, self.route):
            raise ValueError(f"Native {self.name} has no {self.route} handler")
        if self.route == "quote" and values.get("price_type") == "LAST":
            raise ValueError("A quote has no LAST price; request BID, ASK or MID explicitly")
        instantiate(self)


def configuration(name: str, route: Route = "bar", gap_policy: GapPolicy = "carry",
                  **overrides: Parameter) -> IndicatorConfig:
    """Create a typed config from declared defaults and explicit per-platform overrides."""
    if name not in REGISTRY:
        raise ValueError("Unknown native indicator")
    values = dict(REGISTRY[name].defaults)
    if set(overrides) - values.keys():
        raise ValueError("Undeclared native parameter")
    values.update(overrides)
    return IndicatorConfig(name, tuple(values.items()), route, gap_policy)


def supported(name: str, route: Route) -> bool:
    """Inspect the compiled method owner, excluding inherited unimplemented base handlers."""
    method = getattr(getattr(native, name), "handle_bar" if route == "bar" else "handle_quote_tick")
    return not str(method.__doc__).startswith("Indicator.")


def instantiate(config: IndicatorConfig) -> native.Indicator:
    """Translate only declared native enums and identifiers before invoking the pinned class."""
    values = dict(config.parameters)
    for key in ("ma_type", "ma_type_atr", "cmo_ma_type"):
        if key in values:
            values[key] = native.MovingAverageType[values[key]]
    if "price_type" in values:
        values["price_type"] = PriceType[values["price_type"]]
    if "instrument_id" in values:
        values["instrument_id"] = InstrumentId.from_str(values["instrument_id"])
    if "weights" in values:
        values["weights"] = list(values["weights"])
    return getattr(native, config.name)(**values)


def inventory() -> dict[str, object]:
    """Fail closed on a changed native catalogue and explain every non-indicator exclusion."""
    concrete, excluded = set(), {}
    for name in dir(native):
        value = getattr(native, name)
        if name.startswith("_") or not isinstance(value, type):
            continue
        if name in ("Indicator", "MovingAverage"):
            excluded[name] = "Abstract indicator base; no complete calculation"
        elif issubclass(value, native.Indicator):
            concrete.add(name)
        elif issubclass(value, Enum) or name == "StochasticsDMethod":
            excluded[name] = "Configuration or categorical-output enum, not an indicator"
        elif name == "MovingAverageFactory":
            excluded[name] = "Factory; every concrete moving-average class is included separately"
        elif name == "FuzzyCandle":
            excluded[name] = "Value record exported through FuzzyCandlesticks categorical output"
        else:
            raise ValueError(f"Unclassified pinned public class: {name}")
    if concrete != REGISTRY.keys():
        raise ValueError(f"Native indicator inventory changed: {concrete.symmetric_difference(REGISTRY)}")
    return {"concrete": sorted(concrete), "excluded": excluded,
            "unsupported_routes": {
                name: {route: ("Native base handler raises NotImplementedError; requires genuine BBO"
                               if name == "SpreadAnalyzer" else
                               "Native base handler raises NotImplementedError; this class requires OHLC/volume bars")
                       for route in ("bar", "quote") if not supported(name, route)} for name in sorted(concrete)},
            "routes": {name: [route for route in ("bar", "quote") if supported(name, route)]
                       for name in sorted(concrete)}}
