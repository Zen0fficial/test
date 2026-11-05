"""Capture unchanged local HTTP responses once for reproducible platform tests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def capture(output, *, base_url, symbols, start, end, event_slug, bar_timestamp):
    """No platform dependencies, paid reads, LLM calls, or provider mutations.

    bar_timestamp is an explicit interpretation of Futu's native candle label,
    not a modification to its API. Unknown timestamp conventions must not be
    silently converted into a platform's start/end convention.
    """
    url = urllib.parse.urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password:
        raise ValueError("Capture requires a local loopback HTTP provider URL.")
    if bar_timestamp not in ("start", "end"):
        raise ValueError("Specify the provider bar timestamp convention: start or end.")
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first > last:
        raise ValueError("start must not follow end")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    records = []

    def get(name, route, **params):
        request_url = base_url.rstrip("/") + route
        if params:
            request_url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(request_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
        value = json.loads(raw)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError(f"Invalid local provider response: {name}")
        target = root / (name + ".json")
        target.write_bytes(raw)
        records.append({"name": name, "file": target.name, "sha256": sha256(target), "url": request_url})
        return value

    health = get("health", "/health")
    if health.get("provider_connected") is not True:
        raise ValueError("The local Futu provider is not connected.")
    get("quotes", "/v1/quotes", symbols=",".join(symbols))
    for index, symbol in enumerate(symbols):
        response = get(f"bars_{index}", "/v1/bars", symbol=symbol, timeframe="1m", start=start,
                       end=end, adjustment="none", session="regular")
        if not response["data"].get("bars"):
            raise ValueError(f"No real Futu bars for {symbol}")

    events = get("event", "/v1/polymarket/events", slug=event_slug)
    payload = events["data"]
    rows = payload if isinstance(payload, list) else payload.get("events", payload.get("data", []))
    event = next((e for e in rows if e.get("slug") == event_slug), None)
    if event is None:
        raise ValueError("Requested Polymarket event was not returned.")
    markets = event.get("markets", [])
    market = next((m for m in markets if m.get("clobTokenIds")), None)
    if market is None:
        raise ValueError("Event has no outcome token IDs.")
    def array(value):
        return json.loads(value) if isinstance(value, str) else value
    tokens, outcomes = array(market["clobTokenIds"]), array(market["outcomes"])
    if not tokens or len(tokens) != len(outcomes):
        raise ValueError("Outcome labels and token IDs do not match.")
    selection = []
    epoch = lambda day: int(dt.datetime.combine(day, dt.time(), dt.timezone.utc).timestamp())
    for index, (token, outcome) in enumerate(zip(tokens, outcomes)):
        name = f"probability_{index}"
        response = get(name, "/v1/polymarket/prices-history", token_id=token,
                       start=epoch(first), end=epoch(last + dt.timedelta(days=1)), bucket_seconds=60, limit=10000)
        selection.append({"name": name, "token_id": str(token), "outcome": outcome,
                          "market_id": str(market["id"]), "question": market["question"]})
    manifest = {"schema_version": 1, "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "start": start, "end": end, "symbols": symbols, "bar_timestamp": bar_timestamp,
                "timeframe": "1m", "event_slug": event_slug, "outcomes": selection, "records": records,
                "purpose": "converter/engine compatibility; not strategy profitability validation",
                "availability": "Retrospective historical prices; original receipt times are unknown. Current event metadata and quotes are not historical signals."}
    write_json(root / "manifest.json", manifest)
    return manifest


def load_capture(root):
    root = Path(root).resolve()
    manifest = read_json(root / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported capture schema")
    responses = {}
    for record in manifest["records"]:
        path = (root / record["file"]).resolve()
        if path.parent != root or sha256(path) != record["sha256"]:
            raise ValueError("Capture path/hash validation failed: " + record["name"])
        if record["name"] in responses:
            raise ValueError("Duplicate capture record")
        responses[record["name"]] = read_json(path)
    return manifest, responses


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--symbol", action="append", required=True, dest="symbols")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--event-slug", required=True)
    p.add_argument("--bar-timestamp", choices=("start", "end"), required=True)
    args = vars(p.parse_args())
    result = capture(**args)
    print(json.dumps({"output": str(Path(args["output"]).resolve()), "responses": len(result["records"])}))


if __name__ == "__main__":
    main()
