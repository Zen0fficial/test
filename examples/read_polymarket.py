"""Run with: python examples/read_polymarket.py

No API key or Futu session is needed. Use a market's clobTokenIds (or V2
positionIds) for CLOB and price history, and conditionId for market analytics.
"""

import json

from futu_bridge import PolymarketClient
from futu_bridge.polymarket import page_items


with PolymarketClient() as client:
    page = client.markets(closed=False, order="volume24hr", ascending=False, limit=5)
    for market in page_items(page.data):
        print(market["slug"], market["question"])

    market = page_items(page.data)[0]
    print(json.dumps(client.snapshot(market["slug"]).to_dict(), ensure_ascii=False, indent=2))

    # Complete, bounded export with upstream cursors and a completeness manifest:
    # from futu_bridge.polymarket_cli import export_pages
    # export_pages(client, "output/polymarket/markets.jsonl",
    #              resource="markets", params={"closed": False, "limit": 100}, max_pages=10)

    # WebSocket dependencies: pip install -e ".[polymarket]"
    # token_ids = json.loads(market["clobTokenIds"])
    # with client.stream(token_ids=token_ids) as stream:
    #     print(stream.read(timeout=15))
