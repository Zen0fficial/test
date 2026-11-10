"""Convert real captured Futu depth snapshots into native Nautilus L2 snapshots.

The source is an observed, gap-possible desktop stream. CLEAR plus ADD messages
represent each supplied snapshot; they are not inferred exchange order events.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import HKD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import AccountType, BookAction, BookType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from .session import write_report

IDENTITY = InstrumentId.from_str("00700.HKDEPTH")


def snapshots(raw: Path) -> tuple[list[OrderBookDeltas], list[dict[str, object]]]:
    """Validate and convert complete real ten-level snapshots with receipt ordering."""
    native, expected = [], []
    prior = 0
    for line in raw.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        data = record.get("body", {}).get("data", {})
        if record.get("type") != "orderbook" or data.get("book_kind") != "depth":
            continue
        if data.get("symbol") != "HK.00700" or len(data.get("bids", [])) != 10 or len(data.get("asks", [])) != 10:
            raise ValueError("Depth adapter requires complete HK.00700 ten-level snapshots")
        available = int(record["received_ns"])
        event = int(pd.Timestamp(data["observed_at"]).value)
        if available <= prior or event > available:
            raise ValueError("Depth availability must increase and follow provider observation")
        deltas = [OrderBookDelta(IDENTITY, BookAction.CLEAR, None, 32, 0, event, available)]
        sides = {}
        for name, side in (("bids", OrderSide.BUY), ("asks", OrderSide.SELL)):
            levels = []
            for position, row in enumerate(data[name], 1):
                price = Decimal(row["price_raw"]) / Decimal(1_000_000_000)
                size = Decimal(row["volume_raw"]) / 10 ** int(row["volume_precision"])
                if (row["level"] != position or not 200 < price <= 500 or price % Decimal(".2")
                        or size <= 0 or size != size.to_integral_value()
                        or price != Decimal(str(row["price"])) or size != Decimal(str(row["volume"]))):
                    raise ValueError("Depth raw/decoded fields, ticks or quantities disagree")
                levels.append([str(price), str(size)])
                flags = 160 if name == "asks" and position == 10 else 32
                deltas.append(OrderBookDelta(IDENTITY, BookAction.ADD,
                    BookOrder(side, Price.from_str(format(price, ".1f")), Quantity.from_int(int(size)), position),
                    flags, 0, event, available))
            values = [Decimal(x[0]) for x in levels]
            if values != sorted(set(values), reverse=name == "bids"):
                raise ValueError("Depth levels must be unique and ordered")
            sides[name] = levels
        if Decimal(sides["bids"][0][0]) >= Decimal(sides["asks"][0][0]):
            raise ValueError("Crossed or locked captured depth is unsupported")
        expected.append({"event_ns": event, "available_ns": available, **sides})
        native.append(OrderBookDeltas(IDENTITY, deltas))
        prior = available
    if not native:
        raise ValueError("No real complete depth snapshots are available")
    return native, expected


def levels(book: OrderBook) -> dict[str, list[list[str]]]:
    """Read all native price levels rather than only its best bid and ask."""
    return {name: [[str(level.price.as_decimal()), str(Decimal(str(level.size())))] for level in getter()]
            for name, getter in (("bids", book.bids), ("asks", book.asks))}


def delta_signature(snapshot: OrderBookDeltas) -> list[tuple[object, ...]]:
    """Compare numeric delta values; Parquet may normalize CLEAR's zero-price precision."""
    return [(str(d.instrument_id), d.action, d.order.side, d.order.price.as_decimal(), d.order.size.as_decimal(),
             d.order.order_id, d.flags, d.sequence, d.ts_event, d.ts_init) for d in snapshot.deltas]


def compare(actual: dict[str, object], expected: dict[str, object]) -> None:
    """Require equality of every observed native level, volume and availability timestamp."""
    for side in ("bids", "asks"):
        left = [[Decimal(p), Decimal(q)] for p, q in actual[side]]
        right = [[Decimal(p), Decimal(q)] for p, q in expected[side]]
        if left != right:
            raise ValueError("Native depth differs from captured price levels or volumes")
    for name in ("event_ns", "available_ns"):
        if actual[name] != expected[name]:
            raise ValueError("Native depth timestamp differs from source observation/receipt")


class DepthActor(Actor):
    """Observe the native data engine's managed order book without placing orders."""

    def __init__(self) -> None:
        """Initialize a dedicated native Actor and immutable snapshot output list."""
        super().__init__(ActorConfig())
        self.rows: list[dict[str, object]] = []

    def on_start(self) -> None:
        """Ask the native data engine to maintain a real captured L2 book."""
        self.subscribe_order_book_deltas(IDENTITY, BookType.L2_MBP, managed=True)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """Record the book only after the native cache has applied its snapshot."""
        self.rows.append({"event_ns": deltas.ts_event, "available_ns": self.clock.timestamp_ns(),
                          **levels(self.cache.order_book(IDENTITY))})


def run_depth(raw: Path, output: Path) -> dict[str, object]:
    """Persist and replay actual depth, verifying all snapshots before declaring coverage."""
    native, expected = snapshots(raw)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(raw, output / "provider_raw.jsonl")
    instrument = Equity(IDENTITY, Symbol("00700"), HKD, 1, Price.from_str(".2"), Quantity.from_int(100), 0, 0)
    catalog = ParquetDataCatalog((output / "catalog").resolve().as_posix())
    catalog.write_data([instrument])
    catalog.write_data(native)
    reloaded = catalog.order_book_deltas(instrument_ids=[str(IDENTITY)], batched=True)
    if [delta_signature(x) for x in reloaded] != [delta_signature(x) for x in native]:
        raise ValueError("Depth catalogue round trip lost native snapshot deltas")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(Venue("HKDEPTH"), oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=HKD, starting_balances=[Money(100000, HKD)], book_type=BookType.L2_MBP)
        engine.add_instrument(instrument)
        actor = DepthActor()
        engine.add_actor(actor)
        engine.add_data(reloaded)
        engine.run()
        if len(actor.rows) != len(expected) or engine.cache.orders():
            raise ValueError("Depth replay lost snapshots or unexpectedly submitted orders")
        for actual, source in zip(actor.rows, expected):
            compare(actual, source)
        write_report(output / "native_snapshots.json", {"rows": actor.rows})
        report = {"passed": True, "source_path": str(raw.resolve()),
                  "source_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                  "snapshots": len(native), "levels_per_snapshot": 20, "all_levels_verified": True,
                  "catalogue_roundtrip": True,
                  "timestamp_basis": "provider observation with local receipt availability",
                  "exchange_sequence_verified": False, "lossless": False, "real_orders_enabled": False,
                  "semantics": "Native CLEAR/ADD snapshot replacement, not exchange order-event reconstruction"}
        write_report(output / "report.json", report)
        return report
    finally:
        engine.dispose()
