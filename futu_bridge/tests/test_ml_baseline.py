"""Data validation and temporal leakage tests; no orders or return simulation."""

import copy
import datetime as dt
import importlib.util
import unittest

from futu_bridge.ml.baseline import (
    FEATURES, chronological_split, features, fit, portable_model, predict_portable,
    samples, validate_response,
)

HAS_ML = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pandas", "sklearn"))


def frame():
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(19)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .02, 700)))
    return pd.DataFrame({"open": close * .999, "high": close * 1.03, "low": close * .97,
                         "close": close, "volume": rng.integers(1000, 2000, 700)},
                        index=pd.bdate_range("2020-01-01", periods=700, name="date"))


def response():
    return {"source": {"provider": "futu-desktop-session"}, "data": {
        "symbol": "HK.00700", "timeframe": "1d", "adjustment": "none", "session": "provider_default",
        "complete_requested_range": True, "truncated": False, "count": 1,
        "bars": [{"date": "2020-01-02", "timestamp": "2020-01-01T16:00:00Z", "symbol": "HK.00700",
                  "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}]}}


@unittest.skipUnless(HAS_ML, "Install the optional ml dependencies")
class BaselineTests(unittest.TestCase):
    def test_future_values_cannot_change_past_features(self):
        import pandas as pd
        original = frame()
        changed = original.copy()
        changed.iloc[400:] *= 3
        pd.testing.assert_frame_equal(features(original).iloc[:400], features(changed).iloc[:400])

    def test_latest_unknown_target_is_excluded_and_labels_shift_one_day(self):
        original = frame()
        data = samples(original)
        self.assertNotIn(original.index[-1], data.index)
        for day, row in data.iterrows():
            i = original.index.get_loc(day)
            self.assertEqual(row["label_date"], original.index[i + 1])
            self.assertEqual(row["label"], float(original["close"].iloc[i + 1] > original["close"].iloc[i]))

    def test_ties_are_not_up(self):
        original = frame()
        original.loc[original.index[101], "close"] = original["close"].iloc[100]
        self.assertEqual(samples(original).loc[original.index[100], "label"], 0)

    def test_partition_labels_do_not_reach_next_partition(self):
        split = chronological_split(samples(frame()))
        self.assertLess(split["train"]["label_date"].max(), split["validation"].index.min())
        self.assertLess(split["validation"]["label_date"].max(), split["test"].index.min())
        for a, b in (("train", "validation"), ("validation", "test")):
            self.assertTrue(set(split[a].index).isdisjoint(split[b].index))

    def test_scaler_fits_training_only_and_portable_predictions_match(self):
        import numpy as np
        split = chronological_split(samples(frame()))
        model = fit(split["train"])
        artifact = portable_model(model, split["train"])
        np.testing.assert_allclose(artifact["mean"], split["train"].loc[:, list(FEATURES)].mean())
        expected = model.predict_proba(split["test"].loc[:, list(FEATURES)])[:, 1]
        actual = [predict_portable(artifact, row) for _, row in split["test"].iterrows()]
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)
        self.assertFalse(artifact["orders_enabled"])

    def test_unknown_schema_and_nonfinite_inference_fail(self):
        split = chronological_split(samples(frame()))
        artifact = portable_model(fit(split["train"]), split["train"])
        row = split["test"].iloc[0].copy()
        row[FEATURES[0]] = float("nan")
        with self.assertRaises(ValueError): predict_portable(artifact, row)
        artifact["features"] = list(reversed(FEATURES))
        with self.assertRaises(ValueError): predict_portable(artifact, split["test"].iloc[0])

    def test_small_training_set_is_rejected(self):
        with self.assertRaises(ValueError): chronological_split(samples(frame()).iloc[:200])

    def test_daily_timestamp_label_is_interpreted_in_hong_kong(self):
        result = validate_response(response(), "2020-01-01", "2020-01-02", today=dt.date(2020, 1, 3))
        self.assertEqual(str(result.index[0].date()), "2020-01-02")

    def test_current_day_and_invalid_provider_data_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_response(response(), "2020-01-01", "2020-01-02", today=dt.date(2020, 1, 2))
        for case in ("source", "incomplete", "duplicate", "nan", "ohlc", "timezone", "symbol"):
            with self.subTest(case=case):
                value = copy.deepcopy(response())
                if case == "source": value["source"]["provider"] = "sample"
                if case == "incomplete": value["data"]["complete_requested_range"] = False
                if case == "duplicate":
                    value["data"]["bars"] *= 2; value["data"]["count"] = 2
                if case == "nan": value["data"]["bars"][0]["close"] = float("nan")
                if case == "ohlc": value["data"]["bars"][0]["high"] = 1
                if case == "timezone": value["data"]["bars"][0]["timestamp"] = "2020-01-02T00:00:00"
                if case == "symbol": value["data"]["symbol"] = "US.AAPL"
                with self.assertRaises(ValueError): validate_response(value, "2020-01-01", "2020-01-02")


if __name__ == "__main__": unittest.main()
