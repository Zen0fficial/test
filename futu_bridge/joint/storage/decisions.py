"""Version existing popularity/category/LLM decisions without invoking or changing the classifier."""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa

from .captures import timestamp
from .incremental import VersionContract

DECISION_SCHEMA = pa.schema([
    ("event_id", pa.string()), ("observed_ns", pa.int64()), ("judged_ns", pa.int64()),
    ("decision", pa.string()), ("category_decision", pa.string()), ("popularity_rank", pa.int64()),
    ("model", pa.string()), ("reasoning_effort", pa.string()), ("prompt_version", pa.string()),
    ("config_sha256", pa.string()), ("content_hash", pa.string()), ("judgment_cache_key", pa.string()),
    ("classifier_attempts", pa.int64()), ("payload_json", pa.large_string()), ("source_sha256", pa.string()),
], metadata={b"joint_schema": b"polymarket_equity_filter_decision_v1"})
DECISION_VERSIONS = VersionContract(("event_id", "config_sha256"), "observed_ns", ("payload_json",))


def decision_table(payload: bytes) -> pa.Table:
    """Preserve observed decisions, model identity and retry evidence as immutable analytical records."""
    value = json.loads(payload.decode("utf-8-sig"))
    data, source = value["data"], value["source"]
    if data.get("pipeline") != ["popularity", "polymarket_category", "deepseek"]:
        raise ValueError("Expected the existing three-stage equity relevance filter")
    observed = max(timestamp(source["observed_at"]), timestamp(data["generated_at"]))
    classifier, rows = data["classifier"], []
    for event in data["events"]:
        category = event.get("category", {}).get("decision")
        decision = event["decision"]
        if decision not in ("include", "exclude", "review") or (category == "exclude" and decision == "include"):
            raise ValueError("Invalid filter decision or bypassed exclusion gate")
        judged = timestamp(event["judged_at"]) if event.get("judged_at") else None
        rows.append({"event_id": str(event["event_id"]), "observed_ns": max(observed, judged or 0),
                     "judged_ns": judged, "decision": decision, "category_decision": category,
                     "popularity_rank": event.get("popularity_rank"), "model": classifier.get("model"),
                     "reasoning_effort": classifier.get("reasoning_effort"),
                     "prompt_version": classifier.get("prompt_version"), "config_sha256": data["config_sha256"],
                     "content_hash": event.get("content_hash"), "judgment_cache_key": event.get("judgment_cache_key"),
                     "classifier_attempts": event.get("classifier_attempts", 0),
                     "payload_json": json.dumps(event, ensure_ascii=False, allow_nan=False),
                     "source_sha256": hashlib.sha256(payload).hexdigest()})
    if not rows or len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("Empty or duplicate event judgments")
    return pa.Table.from_pylist(rows, schema=DECISION_SCHEMA)
