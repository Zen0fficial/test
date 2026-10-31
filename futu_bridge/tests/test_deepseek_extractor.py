import io
import json
import os
import unittest
from unittest.mock import patch

from futu_bridge.deepseek_extractor import DeepSeekExtractionError, DeepSeekExtractor, extract_jsonl
from futu_bridge.research_features import make_event


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body


class FakeOpener:
    def __init__(self, body):
        self.body = body
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return FakeResponse(self.body)


class DeepSeekExtractorTests(unittest.TestCase):
    def event(self):
        return make_event(
            instrument="US.AAPL", source_type="report_pdf_page", source_id="report",
            text="Revenue grew 10 percent. Management expects guidance to improve.",
            event_time="2026-10-23T00:00:00Z", available_at="2026-10-23T10:00:00Z",
        )

    def test_requires_environment_key_without_logging_or_persisting_it(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DeepSeekExtractionError):
                DeepSeekExtractor()

    def test_json_mode_request_and_claim_quote_validation(self):
        event = self.event()
        item_id = __import__("futu_bridge.deepseek_extractor", fromlist=["_item_id"])._item_id(event, 0, event["text"])
        payload = {"items": [{
            "item_id": item_id,
            "summary": "Revenue improved.",
            "topics": ["results"],
            "claims": [{
                "claim_type": "financial_result", "statement": "Revenue grew 10 percent.",
                "direction": "positive", "metric": "revenue", "value": 10, "unit": "percent",
                "period": "2026", "evidence_quote": "Revenue grew 10 percent.", "confidence": "high",
            }],
            "risk_factors": [],
        }]}
        body = json.dumps({"id": "resp-1", "model": "deepseek-flash",
                           "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 20, "completion_tokens": 30}}).encode()
        opener = FakeOpener(body)
        extractor = DeepSeekExtractor(api_key="in-memory-test-key", opener=opener)
        output = extractor.extract_events([event])
        self.assertEqual(output[0]["claims"][0]["value"], 10)
        self.assertFalse(output[0]["point_in_time_safe"])
        sent = json.loads(opener.request.data.decode())
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertNotIn("in-memory-test-key", opener.request.data.decode())

    def test_invalid_quote_is_dropped_without_failing_whole_batch(self):
        event = self.event()
        item_id = __import__("futu_bridge.deepseek_extractor", fromlist=["_item_id"])._item_id(event, 0, event["text"])
        payload = {"items": [{"item_id": item_id, "summary": "x", "topics": [], "claims": [{
            "claim_type": "risk", "statement": "not in source", "direction": "negative",
            "metric": None, "value": None, "unit": None, "period": None,
            "evidence_quote": "not in source", "confidence": "low",
        }], "risk_factors": []}]}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}).encode()
        output = DeepSeekExtractor(api_key="test", opener=FakeOpener(body)).extract_events([event])
        self.assertEqual(output[0]["claims"], [])

    def test_jsonl_runner_writes_only_structured_results(self):
        event = self.event()
        item_id = __import__("futu_bridge.deepseek_extractor", fromlist=["_item_id"])._item_id(event, 0, event["text"])
        payload = {"items": [{"item_id": item_id, "summary": "x", "topics": [], "claims": [], "risk_factors": []}]}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}).encode()
        with __import__("tempfile").TemporaryDirectory() as directory:
            source = os.path.join(directory, "events.jsonl")
            output = os.path.join(directory, "claims.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
            with patch("futu_bridge.deepseek_extractor.build_opener", return_value=FakeOpener(body)):
                result = extract_jsonl(source, output, api_key="test")
            self.assertEqual(result["output_items"], 1)
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(json.loads(handle.read())["summary"], "x")


if __name__ == "__main__":
    unittest.main()
