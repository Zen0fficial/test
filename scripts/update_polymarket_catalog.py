"""Refresh the public, read-only REST catalog from Polymarket's OpenAPI specs.

Development tool: requires PyYAML. Review the generated diff before releasing.
Runtime requests never download or execute a remote schema.
"""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

SPECS = {
    "gamma": "https://docs.polymarket.com/api-spec/gamma-openapi.yaml",
    "clob": "https://docs.polymarket.com/api-spec/clob-openapi.yaml",
    "data": "https://data-api.polymarket.com/v2/openapi.json",
}
# These POSTs only retrieve data. Never infer read-only semantics from a name.
READ_POSTS = {
    "gamma": {"/markets/information", "/markets/abridged"},
    "clob": {"/books", "/prices", "/midpoints", "/spreads", "/last-trades-prices",
             "/batch-prices-history", "/markets/live-activity"},
    "data": set(),
}


def build(specs):
    sources, endpoints = {}, []
    for api, raw in specs.items():
        spec = yaml.safe_load(raw)
        sources[api] = {"url": SPECS[api], "sha256": hashlib.sha256(raw).hexdigest()}

        def resolve(value):
            if "$ref" not in value:
                return value
            node = spec
            for part in value["$ref"].removeprefix("#/").split("/"):
                node = node[part]
            return node

        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                if method not in ("get", "post") or operation.get("security", spec.get("security")):
                    continue
                if method == "post" and path not in READ_POSTS[api]:
                    continue
                params = []
                for param in operations.get("parameters", []) + operation.get("parameters", []):
                    param = resolve(param)
                    params.append({key: param[key] for key in
                                   ("name", "in", "required", "description", "schema", "style", "explode")
                                   if key in param})
                # The published YAML lags the current protocol-version filter.
                # Documented in the official py-sdk 0.16.0 list_markets/list_events
                # methods and verified against both live keyset routes.
                if api == "gamma" and path in ("/markets", "/events", "/markets/keyset", "/events/keyset") and not any(p["name"] == "version" for p in params):
                    params.append({"name": "version", "in": "query", "schema": {"type": "array", "items": {"type": "string", "enum": ["v1", "v2"]}},
                                   "description": "Protocol version filter (official py-sdk 0.16.0); repeat to include v1 and v2."})
                if api == "gamma" and path in ("/markets", "/markets/keyset"):
                    supplements = {
                        "position_ids": {"type": "array", "items": {"type": "string"}},
                        "request_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                        "onchain_event_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                        "resolution_status": {"type": "string", "enum": ["inactive", "active", "resolved"]},
                        "market_maker_address": {"type": "array", "items": {"type": "string"}},
                        "rewards_min_size": {"type": "number"},
                    }
                    for name, schema in supplements.items():
                        if not any(p["name"] == name for p in params):
                            params.append({"name": name, "in": "query", "schema": schema,
                                           "description": "Current Gamma filter documented in official py-sdk 0.16.0."})
                endpoints.append({"api": api, "method": method.upper(), "path": path,
                                  "summary": operation.get("summary", ""), "parameters": params})
    return {"schema_version": 1, "sources": sources, "endpoints": endpoints}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-dir", type=Path, help="Directory with gamma.yaml, clob.yaml, data.yaml")
    args = parser.parse_args()
    specs = {}
    for api, url in SPECS.items():
        if args.spec_dir:
            specs[api] = (args.spec_dir / (api + ".yaml")).read_bytes()
        else:
            with urlopen(Request(url, headers={"User-Agent": "futu-desktop-bridge", "Accept": "application/json"}),
                         timeout=30) as response:
                specs[api] = response.read()
    path = Path(__file__).resolve().parents[1] / "futu_bridge" / "polymarket_endpoints.json"
    path.write_text(json.dumps(build(specs), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
