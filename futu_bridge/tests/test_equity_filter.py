import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from futu_bridge.client import MarketDataResponse
from futu_bridge.deepseek_extractor import DeepSeekExtractionError
from futu_bridge.equity_filter import (DeepSeekEventJudge, EquityEventFilter, category_gate,
                                      event_record, key_configured, load_config, validate_judgments)
from futu_bridge.polymarket import PolymarketClient
from futu_bridge.provider import ProviderError
from futu_bridge.tests.test_deepseek_extractor import FakeOpener
from futu_bridge.tests import test_polymarket as http_helpers


def event(id="1", title="Will the Fed cut interest rates?", volume=100, tags=None, **extra):
    return {"id": id, "title": title, "description": "The decision is announced by the Federal Reserve.",
            "active": True, "closed": False, "volume24hr": volume, "volume1wk": volume * 7,
            "liquidity": 5000, "tags": tags if tags is not None else [{"slug": "economy"}],
            "markets": [{"id": "m" + id, "question": title, "outcomes": '["Yes","No"]',
                         "outcomePrices": '["0","1"]', "clobTokenIds": '["123","456"]', "closed": False}], **extra}


class FixtureClient:
    def __init__(self, rows, *, sports=None, sports_error=False):
        self.rows = rows
        self.sports = sports if sports is not None else [{"primaryTagId": 99, "series": "nba-series"}]
        self.sports_error = sports_error
        self.discovery_error = False
        self.calls = []

    def iter_pages(self, resource, params, max_pages):
        self.calls.append((resource, params, max_pages))
        if self.discovery_error:
            raise ProviderError("offline", "Discovery unavailable.", 502)
        yield MarketDataResponse({"events": copy.deepcopy(self.rows)}, {"pagination": {"complete": True}, "observed_at": "2026-11-04T00:00:00Z"})

    def get(self, resource):
        self.calls.append((resource,))
        if self.sports_error:
            raise ProviderError("offline", "Taxonomy unavailable.", 502)
        return MarketDataResponse(self.sports, {"provider": "fixture"})


def judgment(row, **overrides):
    return {"event_id": row["event_id"], "decision": "include", "importance": 90,
            "affected_assets": ["SPY"], "sectors": ["broad_market"],
            "impact_mechanism": "Changes in interest rates can change equity discount rates.",
            "confidence": "high", "evidence_quotes": [row["title"]], **overrides}


class FixtureJudge:
    def __init__(self, **overrides):
        self.calls = []
        self.overrides = overrides

    def classify(self, rows):
        self.calls.append(copy.deepcopy(rows))
        return {r["event_id"]: judgment(r, **self.overrides) for r in rows}, {"usage": {"prompt_tokens": 50, "completion_tokens": 50}}


class EquityFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict("os.environ", {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = load_config()

    def engine(self, rows, judge=None, **options):
        result = EquityEventFilter(FixtureClient(rows, **options), cache_dir=self.temp.name, judge=judge)
        result.config["retry_base_delay_seconds"] = 0
        self.addCleanup(result.close)
        return result

    def test_popularity_precedes_categories_and_keeps_liquidity_separate(self):
        rows = [event("2", volume=90, liquidity=0), event("3", volume=80),
                event("1", title="NBA: Lakers win?", volume=100, tags=[{"slug": "sports"}])]
        f = self.engine(rows)
        report = f.refresh(top_n=2)
        self.assertEqual([r["event_id"] for r in report["events"]], ["1", "2"])
        self.assertEqual([r["decision"] for r in report["events"]], ["exclude", "review"])
        self.assertEqual(report["events"][1]["reason"], "awaiting_api_key")
        self.assertIn("low_or_unknown_liquidity", report["events"][1]["quality_flags"])
        self.assertEqual(f.client.calls[0][1]["order"], "volume24hr")
        self.assertEqual(f.client.calls[1], ("sports",))

    def test_eligibility_dedup_and_missing_popularity(self):
        f = self.engine([event("1"), event("1"), event("2", closed=True), event("3", archived=True),
                         event("4", active=False), event("5", volume="NaN")])
        report = f.refresh(top_n=10)
        self.assertEqual([r["event_id"] for r in report["events"]], ["1", "5"])
        self.assertEqual(report["discovery"]["duplicates_skipped"], 1)
        self.assertIn("missing_24h_volume", report["events"][1]["quality_flags"])

    def test_sports_guards_and_generic_all_tag(self):
        sports = [{"primaryTagId": 99, "series": "league"}]
        cases = [event(title="NBA: Mavericks win?", tags=[{"slug": "all"}]),
                 event(tags=[{"id": 99}]), event(series=[{"id": "league"}]),
                 event(markets=[{"sportsMarketType": "moneyline"}]), event(category="Sports")]
        for row in cases:
            with self.subTest(row=row):
                self.assertEqual(category_gate(row, sports, self.config)["reason"], "sports_contest")
        self.assertEqual(category_gate(event(tags=[{"slug": "all"}]), sports, self.config)["decision"], "review")

    def test_business_context_is_not_a_sporting_contest(self):
        row = event(title="Will DraftKings face new regulation?", tags=[{"slug": "sports"}, {"slug": "business"}])
        self.assertEqual(category_gate(row, [], self.config)["decision"], "pass")
        row["markets"][0]["sportsMarketType"] = "moneyline"
        self.assertEqual(category_gate(row, [], self.config)["decision"], "exclude")

    def test_entertainment_and_unknown_categories(self):
        self.assertEqual(category_gate(event(tags=[{"slug": "movies"}]), [], self.config)["decision"], "exclude")
        self.assertEqual(category_gate(event(tags=[]), [], self.config)["decision"], "review")

    def test_commercial_events_survive_entertainment_tags(self):
        cases = [("GTA 6 launch postponed again?", ["pop-culture", "tech", "video-games"]),
                 ('New "Stranger Things" episode released by...?', ["movies", "netflix", "pop-culture"]),
                 ("Where will 2026 rank among the hottest years?", ["pop-culture", "weather"]),
                 ("Who will announce Presidential run?", ["celebrities", "elections", "politics"])]
        for title, labels in cases:
            with self.subTest(title=title):
                row = event(title=title, tags=[{"slug":v} for v in labels])
                self.assertNotEqual(category_gate(row, [], self.config)["decision"], "exclude")
        trivia = event(title="Elon Musk # tweets this week?", tags=[{"slug":"pop-culture"}, {"slug":"politics"}])
        self.assertEqual(category_gate(trivia, [], self.config)["decision"], "exclude")

    def test_zero_probability_and_nonbinary_outcomes_are_preserved(self):
        row = event()
        row["markets"][0].update(outcomes='["A","B","C"]', outcomePrices='["0","0.4","0.6"]', clobTokenIds='["1","2","3"]')
        record = event_record(row, 1, self.config)
        self.assertEqual([o["implied_probability_pct"] for o in record["markets"][0]["outcomes"]], [0, 40, 60])
        self.assertIsNone(record["judgment"])

    def test_placeholder_key_never_calls_deepseek(self):
        with patch("futu_bridge.equity_filter.DeepSeekEventJudge") as factory:
            report = self.engine([event()]).refresh()
        factory.assert_not_called()
        self.assertFalse(key_configured())
        self.assertEqual(report["classifier"]["api_calls"], 0)
        self.assertEqual(report["counts"], {"exclude": 0, "include": 0, "review": 1})
        self.assertNotIn("YOUR_DEEPSEEK_API_KEY", json.dumps(report))

    def test_llm_receives_only_category_survivors(self):
        judge = FixtureJudge()
        f = self.engine([event("1"), event("2", title="NFL winner", tags=[{"slug": "sports"}])], judge)
        report = f.refresh()
        self.assertEqual([[r["event_id"] for r in rows] for rows in judge.calls], [["1"]])
        self.assertEqual(report["counts"]["include"], 1)
        self.assertEqual(f.results().data["selected_token_ids"], ["123", "456"])

    def test_cache_reuses_judgment_when_only_prices_and_volume_change(self):
        judge = FixtureJudge()
        f = self.engine([event()], judge)
        f.refresh()
        f.client.rows[0]["volume24hr"] = 999
        f.client.rows[0]["markets"][0]["outcomePrices"] = '["0.2","0.8"]'
        report = f.refresh()
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(report["classifier"]["cache_hits"], 1)
        self.assertEqual(report["events"][0]["markets"][0]["outcomes"][0]["price"], 0.2)
        f.client.rows[0]["description"] = "The decision date has changed."
        f.refresh()
        self.assertEqual(len(judge.calls), 2)

    def test_cached_low_confidence_stays_review(self):
        f = self.engine([event()], FixtureJudge(confidence="low"))
        self.assertEqual(f.refresh()["events"][0]["decision"], "review")
        self.assertEqual(f.refresh()["classifier"]["cache_hits"], 1)

    def test_invalid_evidence_does_not_auto_include(self):
        report = self.engine([event()], FixtureJudge(evidence_quotes=["Fabricated claim"])).refresh()
        self.assertEqual(report["counts"]["include"], 0)
        self.assertEqual(report["events"][0]["reason"], "llm_failed")

    def test_strict_schema_rejects_hallucinated_assets_and_bad_types(self):
        row = event_record(event(), 1, self.config)
        for change in ({"affected_assets": ["FAKE"]}, {"importance": True}, {"importance": 101},
                       {"decision": []}, {"confidence": {}}, {"evidence_quotes": [""]}):
            with self.subTest(change=change), self.assertRaises(DeepSeekExtractionError):
                validate_judgments({"items": [judgment(row, **change)]}, [row], self.config)
        for items in ([], [judgment(row), judgment(row)]):
            with self.assertRaises(DeepSeekExtractionError):
                validate_judgments({"items": items}, [row], self.config)

    def test_llm_limit_leaves_visible_review_records(self):
        judge = FixtureJudge()
        f = self.engine([event("1"), event("2")], judge)
        f.config["max_llm_events"] = 1
        report = f.refresh()
        self.assertEqual(report["events"][1]["reason"], "llm_run_limit")

    def test_unknown_taxonomy_never_becomes_unqualified_inclusion(self):
        f = self.engine([event(tags=[])], FixtureJudge(), sports_error=True)
        report = f.refresh()
        self.assertEqual(report["events"][0]["reason"], "taxonomy_unverified")
        self.assertEqual(report["counts"]["include"], 0)

    def test_transient_failure_does_not_discard_later_batches(self):
        judge = FixtureJudge()
        original = judge.classify
        attempts = []
        def classify(rows):
            attempts.append(rows)
            if len(attempts) == 1:
                raise DeepSeekExtractionError("DeepSeek API request failed: RemoteDisconnected.")
            return original(rows)
        judge.classify = classify
        f = self.engine([event("1"), event("2")], judge)
        f.config["batch_size"] = 1
        f.config["retry_attempts"] = 1
        report = f.refresh()
        self.assertEqual([r["reason"] for r in report["events"]], ["llm_failed", "llm_judgment"])
        self.assertEqual(report["classifier"]["state"], "partial_failure")
        self.assertEqual(report["classifier"]["api_calls"], 2)

    def test_repeated_transport_failure_and_auth_errors_stop_requests(self):
        for message, calls in (("DeepSeek API request failed: RemoteDisconnected.", 3),
                               ("DeepSeek API returned HTTP 401.", 1),
                               ("DeepSeek API returned HTTP 429.", 1)):
            with self.subTest(message=message):
                judge = FixtureJudge()
                with patch.object(judge, "classify", side_effect=DeepSeekExtractionError(message)) as request:
                    f = self.engine([event(str(i)) for i in range(5)], judge)
                    f.config["batch_size"] = 1
                    f.config["retry_attempts"] = 1
                    report = f.refresh()
                self.assertEqual(request.call_count, calls)
                self.assertEqual(report["classifier"]["state"], "unavailable")
                self.assertEqual(report["counts"]["include"], 0)
                self.assertEqual(report["events"][-1]["reason"], "llm_unavailable")

    def test_no_llm_mode_ignores_existing_judgments(self):
        f = self.engine([event()], FixtureJudge())
        f.refresh()
        report = f.refresh(analyze=False)
        self.assertEqual(report["events"][0]["reason"], "llm_disabled")
        self.assertEqual(report["classifier"]["api_calls"], 0)

    def test_failed_refresh_preserves_last_successful_report(self):
        f = self.engine([event()])
        f.refresh()
        path = Path(self.temp.name)/"latest.json"
        before = path.read_bytes()
        f.client.discovery_error = True
        with self.assertRaises(ProviderError):
            f.refresh()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(f.status()["state"], "failed")

    def test_get_never_runs_discovery_or_llm(self):
        f = self.engine([event()])
        with self.assertRaises(ProviderError):
            f.results()
        self.assertFalse(f.client.calls)
        f.refresh()
        before = len(f.client.calls)
        self.assertEqual(len(f.results(decision="review").data["events"]), 1)
        self.assertEqual(len(f.client.calls), before)

    def test_shared_deepseek_transport_and_json_contract(self):
        row = event_record(event(), 1, self.config)
        row["category"] = category_gate(event(), [], self.config)
        payload = {"items": [judgment(row)]}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}).encode()
        opener = FakeOpener(body)
        judge = DeepSeekEventJudge(self.config, api_key="fixture-private-key", opener=opener)
        result, metadata = judge.classify([row])
        self.assertEqual(result["1"]["decision"], "include")
        sent = json.loads(opener.request.data)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["model"], "deepseek-v4-pro")
        self.assertEqual(sent["reasoning_effort"], "max")
        self.assertEqual(sent["thinking"], {"type": "enabled"})
        self.assertEqual(sent["max_tokens"], 65536)
        self.assertIn("untrusted", sent["messages"][0]["content"])
        self.assertNotIn("fixture-private-key", json.dumps(result))
        self.assertNotIn("fixture-private-key", opener.request.data.decode())

    def test_failed_batch_retries_without_lowering_reasoning(self):
        for message in ("DeepSeek API request failed: RemoteDisconnected.",
                        "DeepSeek API returned HTTP 429.",
                        "Classifier evidence is absent from the event text."):
            with self.subTest(message=message):
                judge = FixtureJudge()
                f = self.engine([event("retry-" + str(len(message)))], judge)
                original = judge.classify
                calls = []
                def classify(rows):
                    calls.append(rows)
                    if len(calls) == 1:
                        raise DeepSeekExtractionError(message)
                    return original(rows)
                judge.classify = classify
                report = f.refresh()
                self.assertEqual(report["counts"]["include"], 1)
                self.assertEqual(report["classifier"]["api_calls"], 2)
                self.assertEqual(report["classifier"]["retries"], 1)
                self.assertEqual(report["classifier"]["failed_events"], 0)
                self.assertEqual(report["classifier"]["reasoning_effort"], "max")
                self.assertEqual(len(report["classifier"]["attempt_errors"]), 1)

    def test_retry_exhaustion_and_auth_failure_are_bounded(self):
        for message, calls in (("DeepSeek API request failed: TimeoutError.", 3),
                               ("DeepSeek API returned HTTP 401.", 1),
                               ("DeepSeek API returned HTTP 402.", 1)):
            with self.subTest(message=message):
                judge = FixtureJudge()
                with patch.object(judge, "classify", side_effect=DeepSeekExtractionError(message)) as request:
                    report = self.engine([event("exhaust-" + str(len(message)))], judge).refresh()
                self.assertEqual(request.call_count, calls)
                self.assertEqual(report["counts"]["review"], 1)
                self.assertEqual(report["events"][0]["classifier_attempts"], calls)

    def test_weaker_judgments_are_not_reused_for_pro_max(self):
        judge = FixtureJudge()
        f = self.engine([event()], judge)
        f.config.update(model="deepseek-flash", reasoning_effort="low")
        f.refresh()
        f.config.update(model="deepseek-v4-pro", reasoning_effort="max")
        with self.assertRaises(ProviderError) as caught:
            f.results()
        self.assertEqual(caught.exception.code, "filter_config_changed")
        report = f.refresh()
        self.assertEqual(len(judge.calls), 2)
        self.assertEqual(report["classifier"]["cache_hits"], 0)

    def test_retry_after_is_respected(self):
        judge = FixtureJudge()
        f = self.engine([event()], judge)
        result = {"1": judgment(event_record(event(), 1, self.config))}
        error = DeepSeekExtractionError("DeepSeek API returned HTTP 429.", retry_after=7)
        with patch.object(judge, "classify", side_effect=[error, (result, {})]), patch.object(f._stop, "wait", return_value=False) as wait:
            report = f.refresh()
        wait.assert_called_once_with(7)
        self.assertEqual(report["classifier"]["retries"], 1)

    def test_refresh_job_cannot_duplicate_concurrent_work(self):
        f = self.engine([event()])
        entered, release = threading.Event(), threading.Event()
        original = f._discover
        def slow(top_n):
            entered.set()
            release.wait(2)
            return original(top_n)
        with patch.object(f, "_discover", side_effect=slow):
            f.start_refresh()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ProviderError) as caught:
                f.start_refresh()
            self.assertEqual(caught.exception.code, "filter_busy")
            release.set()
            f._thread.join(timeout=3)
            self.assertEqual(f.status()["state"], "completed")


class EquityHTTPTests(unittest.TestCase):
    setUp = http_helpers.PolymarketHTTPTests.setUp
    tearDown = http_helpers.PolymarketHTTPTests.tearDown
    request = http_helpers.PolymarketHTTPTests.request

    def test_refresh_and_read_only_results(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"DEEPSEEK_API_KEY": "YOUR_DEEPSEEK_API_KEY"}):
            self.server.polymarket._equity = EquityEventFilter(FixtureClient([event()]), cache_dir=temp)
            status, result = self.request("/v1/polymarket/equity-events")
            self.assertEqual(status, 503)
            status, job = self.request("/v1/polymarket/equity-events/refresh", {"top_n": 10})
            self.assertEqual(status, 202)
            self.server.polymarket._equity._thread.join(timeout=3)
            status, result = self.request("/v1/polymarket/equity-events?decision=review&limit=10")
            self.assertEqual(status, 200)
            self.assertEqual(result["data"]["counts"]["include"], 0)
            self.assertEqual(result["data"]["events"][0]["reason"], "awaiting_api_key")
            status, result = self.request("/v1/polymarket/equity-events/status")
            self.assertEqual(result["state"], "completed")
            status, _ = self.request("/v1/polymarket/equity-events?decision=include&decision=review")
            self.assertEqual(status, 400)
            status, _ = self.request("/v1/polymarket/equity-events/refresh", {"analyze": "false"})
            self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
