"""Reproducible actual-desktop push audit; no artificial market data is injected.

The delivery fault phase deliberately discards one real callback in Python. The
overflow phase limits the consumer queue to one event. These are local fault
tests, not evidence that an exchange event was lost. Native stock/option trades
usually require a regular US market session; --require-trades makes their absence
a failing audit rather than treating a quiet subscription as verified coverage.
"""

import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import time

from .client import FutuClient


def _observe(stream, seconds):
    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        event = stream.read(min(1, max(0.001, deadline - time.monotonic())))
        if event is not None:
            events.append({"type": event.type, "id": event.id, "data": event.result.data,
                           "source": event.result.source, "warnings": event.result.warnings})
    return {"events": events, "counts": dict(Counter(event["type"] for event in events)), "status": stream.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20, help="Observation seconds per phase (1..300)")
    parser.add_argument("--output", type=Path, default=Path("coverage_audit/push_test/library_push_audit.json"))
    parser.add_argument("--require-trades", action="store_true", help="Fail unless genuine new trade pushes arrive for stock, call, and put")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300")
    report = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "phases": {},
              "native_frames": [], "rpc_calls": [], "market_data_injected": False,
              "exchange_lossless_verified": False, "require_trades": args.require_trades}
    phase = {"name": "discovery"}
    started_ms = int(time.time() * 1000)
    try:
        with FutuClient.desktop(stream_mode="push") as client:
            provider = client.provider
            last = client.quotes("US.AAPL").data["quotes"][0]["last"]
            chain = client.option_chain("US.AAPL").data
            contracts = [item for item in chain["contracts"] if item.get("symbol")]
            options = [min((item for item in contracts if item["side"] == side),
                           key=lambda item: abs(item["strike"] - last)) for side in ("call", "put")]
            symbols = ["US.AAPL"] + [item["symbol"] for item in options]
            report.update(symbols=symbols, options=options, capabilities=client.capabilities())
            original_rpc, original_message = provider._rpc, provider._on_push_message

            def rpc(method, *values):
                report["rpc_calls"].append({"phase": phase["name"], "method": method})
                return original_rpc(method, *values)

            def capture(message, body, generation):
                envelope = message.get("payload", {})
                if message.get("type") == "send" and envelope.get("kind") == "native_push":
                    dropped = (phase["name"] == "local_delivery_fault" and envelope.get("event") == "data"
                               and envelope.get("capture_sequence") == 2)
                    report["native_frames"].append({"phase": phase["name"], **envelope,
                                                     "payload_bytes": list(body or b""),
                                                     "intentionally_dropped_by_audit": dropped})
                    if dropped:
                        return
                original_message(message, body, generation)

            provider._rpc, provider._on_push_message = rpc, capture
            phase["name"] = "initial_subscription"
            with client.stream(symbols, ["quotes", "trades", "orderbook"]) as stream:
                report["phases"][phase["name"]] = _observe(stream, args.seconds)
                phase["name"] = "resubscribe"
                stream.reconnect()
                report["phases"][phase["name"]] = _observe(stream, args.seconds)
                phase["name"] = "capture_detach_reattach"
                provider._session.detach()  # Only this instrumentation session; Futu stays open.
                report["detached_status"] = stream.status()
                stream.reconnect()
                report["phases"][phase["name"]] = _observe(stream, args.seconds)
                settle_keys = [{key: item[key] for key in ("stock_id", "bit", "prob")}
                               for item in stream.source["subscription"]["accepted"]]
            report["after_main_close"] = original_rpc("pushstatus")
            report["main_unsubscribe_settled"] = original_rpc("pushsettle", settle_keys, 2500)

            phase["name"] = "local_delivery_fault"
            with client.stream(symbols, ["quotes", "trades", "orderbook"]) as stream:
                report["phases"][phase["name"]] = _observe(stream, args.seconds)
            report["fault_unsubscribe_settled"] = original_rpc("pushsettle", settle_keys, 2500)

            phase["name"] = "queue_overflow"
            provider.push_queue_size = 1
            with client.stream(symbols, ["quotes", "trades", "orderbook"]) as stream:
                # A deliberately slow consumer, receiving real subscription images.
                time.sleep(min(args.seconds, 5))
                report["phases"][phase["name"]] = _observe(stream, 1)
            report["after_all_close"] = original_rpc("pushstatus")
            report["retained_handles_after_close"] = sorted(provider._owned_push_handles)

        all_events = [event for value in report["phases"].values() for event in value["events"]]
        per_symbol = {}
        for symbol in report["symbols"]:
            book = [event for event in all_events if event["type"] == "orderbook" and event["data"]["symbol"] == symbol]
            trades = [event for event in all_events if event["type"] == "trades" and event["data"]["symbol"] == symbol]
            new_rows = [row for event in trades for row in event["data"]["trades"]
                        if row.get("exchange_timestamp_ms", 0) >= started_ms]
            per_symbol[symbol] = {"book_events": len(book), "trade_events": len(trades),
                                  "new_trade_records": len(new_rows), "new_trade_push_verified": bool(new_rows),
                                  "book_push_verified": bool(book)}
        report["coverage"] = per_symbol
        report["polling_rpc_calls"] = [item for item in report["rpc_calls"] if item["method"] in
                                        ("requestbook", "requesttrades", "requestsnapshot", "readquotes", "readbook", "readtrades")]
        report["checks"] = {
            "native_books_all_symbols": all(item["book_push_verified"] for item in per_symbol.values()),
            "native_trade_activity_all_symbols": all(item["new_trade_push_verified"] for item in per_symbol.values()),
            "no_polling_calls_during_stream": not report["polling_rpc_calls"],
            "resubscribe_delivered_data": bool(report["phases"]["resubscribe"]["counts"].get("orderbook")),
            "capture_reattach_delivered_data": bool(report["phases"]["capture_detach_reattach"]["counts"].get("orderbook")),
            "detach_reported": report["detached_status"]["disconnects"] >= 1,
            "local_drop_detected": report["phases"]["local_delivery_fault"]["status"]["missing_capture_messages"] >= 1,
            "overflow_reported": report["phases"]["queue_overflow"]["status"]["queue_dropped_events"] >= 1,
            "native_handles_released": not report["after_all_close"]["subscriptions"] and not report["retained_handles_after_close"],
        }
        report["passed_available_checks"] = all(value for key, value in report["checks"].items()
                                                 if key != "native_trade_activity_all_symbols")
        report["open_market_trade_test_pending"] = not report["checks"]["native_trade_activity_all_symbols"]
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
        raise
    finally:
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("coverage", "checks", "error") if key in report}, indent=2), flush=True)
        print(str(args.output.resolve()), flush=True)
    return 0 if report["passed_available_checks"] and (not args.require_trades or not report["open_market_trade_test_pending"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
