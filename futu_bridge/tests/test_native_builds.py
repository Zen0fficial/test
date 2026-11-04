from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from futu_bridge.native import NativeProvider
from futu_bridge.native_builds import SUPPORTED_BUILDS, select_build
from futu_bridge.provider import ProviderError
from futu_bridge.wire import parse_bars, parse_quote_snapshots


class NativeBuildTests(unittest.TestCase):
    def test_both_exact_build_pairs_are_supported(self):
        self.assertEqual({build.version for build in SUPPORTED_BUILDS}, {"16.33.17808", "16.37.27308"})
        for build in SUPPORTED_BUILDS:
            self.assertIs(select_build(build.qotbase_sha256, build.corebiz_sha256), build)

    def test_mixed_unknown_and_missing_modules_are_rejected(self):
        old, current = SUPPORTED_BUILDS
        for quote_hash, core_hash in (
            (old.qotbase_sha256, current.corebiz_sha256),
            (current.qotbase_sha256, old.corebiz_sha256),
            ("0" * 64, current.corebiz_sha256),
            (current.qotbase_sha256, None),
        ):
            with self.subTest(quote_hash=quote_hash, core_hash=core_hash):
                with self.assertRaises(ProviderError) as error:
                    select_build(quote_hash, core_hash)
                self.assertEqual(error.exception.code, "version_unsupported")

    def _provider_with_fixture_files(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        provider = NativeProvider.__new__(NativeProvider)
        attributes = ("module_path", "core_module_path", "news_module_path", "calendar_module_path")
        hashes = []
        for index, attribute in enumerate(attributes):
            path = folder / (attribute + ".dll")
            data = ("fixture-module-" + str(index)).encode()
            path.write_bytes(data)
            setattr(provider, attribute, path)
            hashes.append(hashlib.sha256(data).hexdigest())
        fixture = replace(SUPPORTED_BUILDS[-1], qotbase_sha256=hashes[0], corebiz_sha256=hashes[1],
                          ftnews_sha256=hashes[2], ftquote_sha256=hashes[3])
        return provider, fixture

    def test_mutated_core_is_rejected_before_native_attachment(self):
        provider, fixture = self._provider_with_fixture_files()
        with patch("futu_bridge.native_builds.SUPPORTED_BUILDS", (fixture,)):
            provider._verify_build_files()
            provider.core_module_path.write_bytes(b"changed")
            device = Mock()
            with self.assertRaises(ProviderError):
                provider._attach_capture(device)
            device.attach.assert_not_called()

    def test_reconnect_does_not_switch_to_another_supported_build(self):
        provider, fixture = self._provider_with_fixture_files()
        provider.build = SUPPORTED_BUILDS[0]
        with patch("futu_bridge.native_builds.SUPPORTED_BUILDS", (fixture,)):
            with self.assertRaises(ProviderError) as error:
                provider._verify_build_files()
        self.assertIn("changed before reconnect", error.exception.message)

    def test_optional_modules_must_belong_to_selected_build(self):
        provider, fixture = self._provider_with_fixture_files()
        with patch("futu_bridge.native_builds.SUPPORTED_BUILDS", (fixture,)):
            provider._verify_build_files()
            self.assertTrue(provider._news_module_valid)
            self.assertTrue(provider._calendar_module_valid)
            provider.news_module_path.write_bytes(b"different-news-module")
            provider.calendar_module_path.unlink()
            provider._verify_build_files()
            self.assertFalse(provider._news_module_valid)
            self.assertFalse(provider._calendar_module_valid)

    def test_provenance_uses_selected_build(self):
        provider = NativeProvider.__new__(NativeProvider)
        provider.build = SUPPORTED_BUILDS[-1]
        provider.module_sha256 = provider.build.qotbase_sha256
        provider.pid = 123
        self.assertEqual(provider._source("snapshot")["desktop_version"], "16.37.27308")

    def test_captured_1637_quotes_and_history_decode(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/native_1637_wire.json").read_text(encoding="utf-8"))
        quotes = parse_quote_snapshots(bytes(fixture["quotes"]))
        self.assertEqual([quote["stock_id"] for quote in quotes], ["205189", "54047868453564"])
        self.assertTrue(all(quote["exists"] for quote in quotes))
        self.assertEqual(quotes[1]["raw"]["PriceNominal"], "393800000000")
        history = parse_bars(bytes(fixture["history"]))
        self.assertEqual(history["result"], 0)
        self.assertEqual(history["stock_id"], "54047868453564")
        self.assertEqual([bar["date"] for bar in history["bars"]], ["2026-11-02", "2026-11-03", "2026-11-04"])
        self.assertEqual(history["bars"][-1]["close"], 393.8)


if __name__ == "__main__":
    unittest.main()
