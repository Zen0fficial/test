"""Durable consumer of the fixed local Futu SSE provider, with explicit continuity limits."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import time

from futu_bridge.converters.live_source import LocalSSE

from .journal import CaptureJournal
from .replay import replay
from .snapshots import SnapshotStore, dataset_name


def capture_live(store: SnapshotStore, output: Path, *, seconds: int, symbol: str = "HK.00700",
                 base_url: str = "http://127.0.0.1:8765", disconnect_after: int = 0,
                 dataset: str = "futu_observations") -> dict[str, object]:
    """Capture a bounded live observation interval without constructing any trading client.

    Forced reconnects deliberately create an unknown exchange gap. A successful
    durable capture does not certify a full session or lossless exchange replay.
    """
    if seconds <= 0:
        raise ValueError("Live acceptance requires a positive bounded duration")
    dataset_name(dataset)
    output.mkdir(parents=True, exist_ok=False)
    source = LocalSSE(base_url, symbol, output / "transport", disconnect_after=disconnect_after)
    fresh, received, max_queue = 0, 0, 0
    started = time.monotonic()
    source.start()
    try:
        with CaptureJournal(output / "journal") as journal:
            while time.monotonic() - started < seconds:
                try:
                    message = source.events.get(timeout=0.2)
                except queue.Empty:
                    journal.flush_due()
                    continue
                journal.append(message)
                received += 1
                max_queue = max(max_queue, source.events.qsize())
                data = message["body"].get("data", {})
                if (message["type"] in ("quotes", "trades", "orderbook")
                        and not data.get("initial_snapshot") and not data.get("backfill")):
                    fresh += 1
            source.close()
            while not source.events.empty():
                journal.append(source.events.get_nowait())
                received += 1
            journal.flush()
            status = journal.status()
            publication = replay(store, journal, dataset=dataset)
    finally:
        source.close()
    transport = source.status()
    passed = fresh > 0 and not transport["counts"].get("queue_overflows", 0)
    report = {"passed": passed, "dataset": dataset, "fresh_messages": fresh, "received_messages": received,
              "journal": status, "transport": transport, "snapshot": publication["snapshot"], "max_queue": max_queue,
              "real_orders_enabled": False, "full_session_verified": False,
              "exchange_lossless": False, "gap_recovery_available": False}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
