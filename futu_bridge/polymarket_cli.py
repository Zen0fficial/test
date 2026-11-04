"""Fetch, export, and stream public Polymarket data without a wallet."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .polymarket import PolymarketClient, json_bytes, page_items, utcnow
from .provider import ProviderError


def export_pages(client, output, *, resource=None, api=None, path=None, params=None,
                 max_pages=10, overwrite=False):
    """Write lossless page envelopes as JSONL with a resumable completeness manifest.

    A failure leaves .partial and a failed manifest. Only successful bounded walks
    promote the partial file. complete means the requested upstream scan ended,
    not that the upstream has every historical observation.
    """
    output = Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    manifest_path = output.with_name(output.name + ".manifest.json")
    if not overwrite and any(p.exists() for p in (output, partial, manifest_path)):
        raise FileExistsError("Export already exists; choose another path or use --overwrite.")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"provider": "polymarket", "format": "one complete response envelope per JSONL line",
                "started_at": utcnow(), "resource": resource, "api": api, "path": path,
                "params": params or {}, "max_pages": max_pages, "pages": 0, "records": 0,
                "complete": False, "limited": False, "status": "running", "next_params": params or {}}

    def checkpoint():
        temp = manifest_path.with_name(manifest_path.name + ".tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, manifest_path)

    try:
        with partial.open("wb") as handle:
            for response in client.iter_pages(resource, api=api, path=path, params=params, max_pages=max_pages):
                handle.write(json_bytes(response.to_dict()) + b"\n")
                handle.flush()
                pagination = response.source["pagination"]
                manifest.update({key: pagination[key] for key in ("complete", "limited", "next_params")})
                manifest["pages"] += 1
                rows = page_items(response.data)
                manifest["records"] += len(rows) if rows is not None else 1
                manifest["updated_at"] = utcnow()
                checkpoint()
        os.replace(partial, output)
        manifest.update(status="complete" if manifest["complete"] else "limited", output=str(output), finished_at=utcnow())
        checkpoint()
        return manifest
    except BaseException as exc:
        manifest.update(status="failed", complete=False, error_type=type(exc).__name__, partial_file=str(partial), finished_at=utcnow())
        checkpoint()
        raise


def _params(args):
    try:
        value = json.loads(Path(args.params_file).read_text(encoding="utf-8-sig") if args.params_file else args.params)
    except (ValueError, OSError) as exc:
        raise ValueError("params must be a JSON object, supplied inline or in --params-file") from exc
    if not isinstance(value, dict):
        raise ValueError("params must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog", help="List every supported public REST endpoint and its parameters")
    for command in ("get", "export"):
        cmd = sub.add_parser(command)
        selection = cmd.add_mutually_exclusive_group(required=True)
        selection.add_argument("--resource", help="Convenience resource, e.g. markets, trades, prices-history")
        selection.add_argument("--api", choices=("gamma", "clob", "data"))
        cmd.add_argument("--path", help="Raw upstream path, e.g. /v2/positions")
        source = cmd.add_mutually_exclusive_group()
        source.add_argument("--params", default="{}", help="JSON object of upstream query parameters")
        source.add_argument("--params-file", type=Path)
        if command == "get":
            cmd.add_argument("--body-file", type=Path, help="JSON body for a catalogued read-only POST")
        else:
            cmd.add_argument("--output", type=Path, required=True)
            limit = cmd.add_mutually_exclusive_group()
            limit.add_argument("--max-pages", type=int, default=10)
            limit.add_argument("--all", action="store_true", help="Explicitly scan until upstream pagination ends")
            cmd.add_argument("--overwrite", action="store_true")
    snap = sub.add_parser("snapshot", help="Collect a market's metadata, books, history, trades and analytics")
    snap.add_argument("slug")
    snap.add_argument("--interval", default="1d")
    stream = sub.add_parser("stream", help="Stream raw upstream events as JSONL")
    stream.add_argument("--channel", choices=("market", "activity", "rtds", "sports"), default="market")
    stream.add_argument("--token-id", action="append", dest="token_ids")
    stream.add_argument("--subscriptions-file", type=Path)
    stream.add_argument("--event-slug")
    stream.add_argument("--market-slug")
    stream.add_argument("--seconds", type=float, default=30)
    stream.add_argument("--output", type=Path, help="New JSONL file; otherwise stdout")
    chain = sub.add_parser("onchain", help="Free Polygon RPC reads for current Polymarket contracts")
    chain.add_argument("resource", choices=("catalog", "status", "logs", "balance", "receipt"))
    source = chain.add_mutually_exclusive_group()
    source.add_argument("--params", default="{}")
    source.add_argument("--params-file", type=Path)
    equity = sub.add_parser("equity-events", help="Popularity/category/DeepSeek filter for US equities")
    equity.add_argument("--refresh", action="store_true", help="Fetch candidates and classify; otherwise read the saved report")
    equity.add_argument("--top-n", type=int)
    equity.add_argument("--no-llm", action="store_true", help="Run free discovery and category filtering only")
    equity.add_argument("--decision", choices=("include", "exclude", "review", "all"), default="include")
    equity.add_argument("--limit", type=int, default=50)
    equity.add_argument("--offset", type=int, default=0)
    equity.add_argument("--config", type=Path, help="JSON configuration overrides")
    equity.add_argument("--cache-dir", type=Path)
    args = parser.parse_args(argv)

    def emit(value, file=None):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=file or sys.stdout, flush=True)

    try:
        with PolymarketClient(timeout=args.timeout) as client:
            if args.command == "catalog":
                emit(client.catalog())
            elif args.command == "equity-events":
                from .equity_filter import EquityEventFilter
                with_filter = EquityEventFilter(client, config_path=args.config, cache_dir=args.cache_dir)
                try:
                    if args.refresh:
                        with_filter.refresh(top_n=args.top_n, analyze=not args.no_llm)
                    elif args.top_n is not None or args.no_llm:
                        parser.error("--top-n and --no-llm require --refresh")
                    emit(with_filter.results(decision=args.decision, limit=args.limit, offset=args.offset).to_dict())
                finally:
                    with_filter.close()
            elif args.command in ("get", "export"):
                params = _params(args)
                if args.api and not args.path:
                    parser.error("--api requires --path")
                if args.command == "export":
                    emit(export_pages(client, args.output, resource=args.resource, api=args.api, path=args.path,
                                      params=params, max_pages=None if args.all else args.max_pages, overwrite=args.overwrite))
                elif args.resource:
                    if args.body_file:
                        parser.error("--body-file requires --api and --path")
                    emit(client.get(args.resource, **params).to_dict())
                else:
                    body = json.loads(args.body_file.read_text(encoding="utf-8-sig")) if args.body_file else None
                    emit(client.request(args.api, args.path, params=params, body=body).to_dict())
            elif args.command == "snapshot":
                emit(client.snapshot(args.slug, history_interval=args.interval).to_dict())
            elif args.command == "onchain":
                if args.resource == "catalog":
                    from .polymarket_onchain import catalog
                    emit(catalog())
                else:
                    emit(client.onchain(args.resource, **_params(args)).to_dict())
            elif args.command == "stream":
                if not 0 < args.seconds <= 86400:
                    parser.error("--seconds must be positive and at most 86400")
                subscriptions = json.loads(args.subscriptions_file.read_text(encoding="utf-8-sig")) if args.subscriptions_file else None
                with client.stream(args.channel, token_ids=args.token_ids, subscriptions=subscriptions,
                                   event_slug=args.event_slug, market_slug=args.market_slug) as feed:
                    output = args.output.open("x", encoding="utf-8") if args.output else None
                    try:
                        deadline = time.monotonic() + args.seconds
                        while time.monotonic() < deadline:
                            try:
                                event = feed.read(timeout=min(1, max(0.001, deadline - time.monotonic())))
                            except StopIteration:
                                break
                            if event is not None:
                                emit(event, output)
                    finally:
                        if output:
                            output.close()
    except ProviderError as exc:
        emit({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        emit({"error": {"code": "invalid_input", "message": str(exc)}}, sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
