"""Namespaced Polymarket routes for the existing loopback HTTP service."""

import json
from urllib.parse import parse_qs

from .polymarket import ALIASES, BATCH_PATHS, as_provider_result, invalid
from .provider import ProviderError

PREFIX = "/v1/polymarket"


def query_values(text):
    try:
        values = parse_qs(text, keep_blank_values=True, max_num_fields=600)
    except ValueError as exc:
        raise invalid("Too many parameters.") from exc
    return {key: rows[0] if len(rows) == 1 else rows for key, rows in values.items()}


def _only(values, allowed):
    if set(values) - set(allowed):
        raise invalid("Unknown parameters: " + ", ".join(sorted(set(values) - set(allowed))))


def _route(resource):
    api, separator, path = resource.partition("/")
    if separator and api in ("gamma", "clob", "data", "goldsky"):
        if api == "data" and not path.startswith("v2/"):
            path = "v2/" + path
        if api == "goldsky" and not path.startswith("data/polymarket/"):
            path = "data/polymarket/" + path
        return api, "/" + path
    if resource in ALIASES:
        return ALIASES[resource]
    raise ProviderError("not_found", "Unknown Polymarket resource. See /v1/polymarket/catalog.", 404)


def handle_get(handler, split, request_id):
    from .server import result_body
    client = handler.server.polymarket
    if client is None:
        raise ProviderError("resource_disabled", "Polymarket is disabled for this server.", 503)
    resource = split.path.removeprefix(PREFIX).strip("/")
    params = query_values(split.query)
    if resource in ("", "catalog"):
        _only(params, ())
        handler.json_response(200, client.catalog())
        return
    if resource == "subgraphs":
        raise ProviderError("source_removed", "Goldsky subgraphs were removed. See /v1/polymarket/catalog.", 410)
    if resource == "onchain/catalog":
        from .polymarket_onchain import catalog
        _only(params, ())
        handler.json_response(200, catalog())
        return
    if resource == "equity-events/status":
        _only(params, ())
        handler.json_response(200, client.equity_filter().status())
        return
    if resource == "equity-events":
        _only(params, ("decision", "limit", "offset"))
        response = client.equity_events(**params)
        handler.json_response(200, result_body(as_provider_result(response), request_id))
        return
    if resource == "stream":
        _only(params, ("channel", "token_ids", "subscriptions", "event_slug", "market_slug", "symbols", "venue"))
        if "subscriptions" in params:
            try:
                params["subscriptions"] = json.loads(params["subscriptions"])
            except (ValueError, TypeError) as exc:
                raise invalid("subscriptions must be a JSON array.") from exc
        serve_stream(handler, client, params, request_id)
        return
    if resource.startswith("onchain/"):
        if "topics" in params:
            try:
                params["topics"] = json.loads(params["topics"])
            except (ValueError, TypeError) as exc:
                raise invalid("topics must be a JSON array.") from exc
        response = client.onchain(resource.split("/", 1)[1], **params)
    elif resource == "snapshot":
        _only(params, ("slug", "interval"))
        response = client.snapshot(params.get("slug"), history_interval=params.get("interval", "1d"))
    elif resource in BATCH_PATHS:
        _only(params, ("token_ids", "side"))
        if not params.get("token_ids"):
            raise invalid("token_ids is required.")
        response = client.batch(resource, params["token_ids"], side=params.get("side", "BUY"))
    elif resource in ALIASES:
        response = client.get(resource, **params)
    else:
        api, path = _route(resource)
        response = client.request(api, path, params=params)
    handler.json_response(200, result_body(as_provider_result(response), request_id))


def handle_post(handler, split, request_id):
    from .server import result_body
    client = handler.server.polymarket
    if client is None:
        raise ProviderError("resource_disabled", "Polymarket is disabled for this server.", 503)
    if handler.headers.get_content_type() != "application/json":
        raise ProviderError("unsupported_media_type", "Send application/json.", 415)
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or handler.headers.get("Transfer-Encoding"):
        raise invalid("A single Content-Length header is required.")
    try:
        length = int(lengths[0])
    except ValueError as exc:
        raise invalid("Invalid Content-Length.") from exc
    if not 0 < length <= 1024 * 1024:
        raise ProviderError("request_too_large", "JSON read requests must be 1 byte to 1 MiB.", 413)
    try:
        body = json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeError) as exc:
        raise invalid("Invalid JSON body.") from exc
    resource = split.path.removeprefix(PREFIX).strip("/")
    params = query_values(split.query)
    if resource in ("request", "stream", "equity-events/refresh") or resource.startswith("subgraphs/"):
        _only(params, ())
        if not isinstance(body, dict):
            raise invalid("Expected a JSON object.")
    if resource == "equity-events/refresh":
        _only(body, ("top_n", "analyze"))
        handler.json_response(202, client.equity_filter().start_refresh(**body))
        return
    if resource == "request":
        _only(body, ("api", "path", "params", "body"))
        response = client.request(body.get("api"), body.get("path"), params=body.get("params"), body=body.get("body"))
    elif resource == "stream":
        _only(body, ("channel", "token_ids", "subscriptions", "event_slug", "market_slug", "symbols", "venue"))
        serve_stream(handler, client, body, request_id)
        return
    elif resource.startswith("subgraphs/"):
        _only(body, ("query", "variables", "operationName"))
        response = client.subgraph(resource.split("/", 1)[1], body.get("query"),
                                   variables=body.get("variables"), operation_name=body.get("operationName"))
    else:
        api, path = _route(resource)
        response = client.request(api, path, params=params, body=body, method="POST")
    handler.json_response(200, result_body(as_provider_result(response), request_id))


def serve_stream(handler, client, options, request_id):
    from .server import encode, now
    stream = client.stream(**options)
    try:
        handler.server.register_subscription(stream)
    except Exception:
        stream.close()
        raise

    def emit(kind, value):
        handler.wfile.write(("event: " + kind + "\ndata: ").encode() + encode(value) + b"\n\n")
        handler.wfile.flush()

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.close_connection = True
        emit("ready", {"request_id": request_id, "source": stream.source, "served_at": now()})
        while True:
            try:
                event = stream.read(timeout=15)
            except StopIteration:
                emit("end", {"served_at": now(), "status": stream.status()})
                break
            if event is None:
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
            else:
                emit(event["type"], {**event, "request_id": request_id})
    except (OSError, TimeoutError):
        pass
    finally:
        handler.server.unregister_subscription(stream)
        stream.close()
