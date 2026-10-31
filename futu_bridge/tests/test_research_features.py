import json
import tempfile
import unittest
from pathlib import Path

from futu_bridge.option_features import option_snapshot_features
from futu_bridge.research_features import (
    aggregate_daily_features,
    earnings_transcript_events,
    futu_ai_summary_event,
    make_event,
    news_events,
    write_events_jsonl,
    write_feature_csv,
    merge_features_into_qlib_source,
)


class ResearchFeatureTests(unittest.TestCase):
    def test_news_headlines_keep_metadata_and_are_not_pit_safe_without_availability(self):
        events = news_events({
            "data": {"symbol": "US.AAPL", "items": [{
                "news_id": "n1", "headline": "Apple outlook improves", "published_at": "2026-10-23T13:00:00Z",
                "url": "https://example.test/n1", "publisher": "Example", "language": "en",
            }]},
            "source": {"provider": "fixture"}, "warnings": [],
        })
        self.assertEqual(events[0]["source_type"], "news_headline")
        self.assertFalse(events[0]["point_in_time_safe"])
        self.assertEqual(events[0]["publisher"], "Example")

    def test_transcript_paragraphs_preserve_ai_flag_and_fiscal_period(self):
        events = earnings_transcript_events({
            "data": {
                "symbol": "HK.00700", "fiscal_period": "2026Q2",
                "call": {"feed_id": "feed-1", "published_timestamp": 1790000000000},
                "transcript": {"language": "en", "paragraphs": ["Revenue outlook is strong"],
                               "machine_generated_or_translated": True, "issuer_verified": False},
            },
            "source": {"provider": "fixture"}, "warnings": [],
        })
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["ai_generated"])
        self.assertEqual(events[0]["metadata"]["fiscal_period"], "2026Q2")

    def test_available_at_basis_marks_safe_features_only_when_explicit(self):
        unsafe = make_event(instrument="US.AAPL", source_type="news_headline", source_id="n1",
                            text="risk may increase", event_time="2026-10-23T12:00:00Z")
        safe = make_event(instrument="US.AAPL", source_type="futu_ai_summary", source_id="s1",
                          text="guidance improves", event_time="2026-10-23T12:00:00Z",
                          available_at="2026-10-23T13:00:00Z", ai_generated=True)
        rows, manifest = aggregate_daily_features([unsafe, safe], date_basis="available_at")
        self.assertEqual(rows, [{
            "date": "2026-10-23", "symbol": "US.AAPL", "pit_safe": 1,
            "text_event_count": 1, "news_headline_count": 0, "earnings_transcript_count": 0,
            "report_pdf_page_count": 0, "futu_ai_summary_count": 1, "text_char_count": len("guidance improves"),
            "publisher_count": 0, "ai_generated_count": 1, "issuer_verified_count": 0,
            "uncertainty_term_count": 0, "guidance_term_count": 1, "catalyst_term_count": 0,
            "pressure_term_count": 0,
        }])
        self.assertEqual(manifest["skipped_events_without_date"], 1)
        self.assertFalse(manifest["point_in_time_safe"])

    def test_option_snapshot_counts_only_verified_labeled_contracts(self):
        features = option_snapshot_features("US.AAPL", {
            "data": {"expiry": "2026-11-20", "default_expiry_selected": False,
                     "metadata_count": 2, "contracts": [
                         {"symbol": "US.AAPL261120C100000", "side": "call", "strike": 100, "expiry": "2026-11-20"},
                         {"symbol": "US.AAPL261120P100000", "side": "put", "strike": 100, "expiry": "2026-11-20"},
                         {"symbol": None, "side": None, "strike": None, "expiry": "2026-11-20"},
                     ]},
            "source": {"provider": "fixture", "observed_at": "2026-10-31T12:00:00Z"}, "warnings": [],
        }, {
            "data": {"quotes": [
                {"symbol": "US.AAPL261120C100000", "last": 3.2},
                {"symbol": "US.AAPL261120P100000", "last": 2.8},
            ]},
            "source": {"provider": "fixture"}, "warnings": [],
        })
        self.assertEqual(features["option_contract_count"], 2)
        self.assertEqual(features["option_server_id_count"], 3)
        self.assertEqual(features["option_call_count"], 1)
        self.assertEqual(features["option_put_count"], 1)
        self.assertEqual(features["option_quote_count"], 2)
        self.assertEqual(features["option_pit_safe"], 0)

    def test_writers_are_atomic_and_serializable(self):
        event = futu_ai_summary_event(instrument="US.AAPL", source_id="s1", summary="Guidance improves",
                                     event_time="2026-10-23T00:00:00Z")
        rows, _ = aggregate_daily_features([event])
        with tempfile.TemporaryDirectory() as directory:
            events_path = write_events_jsonl([event], Path(directory) / "events.jsonl")
            features_path = write_feature_csv(rows, Path(directory) / "features.csv")
            self.assertEqual(json.loads(events_path.read_text(encoding="utf-8"))["source_type"], "futu_ai_summary")
            self.assertIn("guidance_term_count", features_path.read_text(encoding="utf-8"))

    def test_merge_adds_features_without_overwriting_price_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "AAPL.csv").write_text(
                "date,open,close,high,low,volume,factor\n2026-10-23,1,2,3,0,10,1\n",
                encoding="utf-8",
            )
            rows = [{"date": "2026-10-23", "symbol": "US.AAPL", "guidance_term_count": 3}]
            result = merge_features_into_qlib_source(source, rows)
            text = (source / "AAPL.csv").read_text(encoding="utf-8")
            self.assertEqual(result["updated"][0]["fields"], ["guidance_term_count"])
            self.assertIn("guidance_term_count", text)
            self.assertIn(",3\n", text)


if __name__ == "__main__":
    unittest.main()
