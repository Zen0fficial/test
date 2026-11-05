"""Simple HK.00700 daily direction model using the unchanged local provider."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
import warnings
from zoneinfo import ZoneInfo

from futu_bridge.converters.live_source import local_url

FEATURES = (
    "return_1", "return_5", "return_20", "close_to_ma_5", "close_to_ma_20",
    "volatility_5", "volatility_20", "intraday_return", "daily_range",
    "volume_to_ma_20", "opening_gap",
)
HK = ZoneInfo("Asia/Hong_Kong")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def validate_response(response, start, end, *, today=None):
    """Daily timestamps are date labels, not when OHLCV became available."""
    import math
    import pandas as pd

    today = today or dt.datetime.now(HK).date()
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first > last or last >= today:
        raise ValueError("Use an ordered range ending before today in Hong Kong")
    data, source = response["data"], response["source"]
    if source.get("provider") != "futu-desktop-session":
        raise ValueError("Require the existing native Futu provider")
    if (data.get("symbol") != "HK.00700" or data.get("timeframe") != "1d"
            or data.get("adjustment") != "none" or data.get("session") != "provider_default"):
        raise ValueError("Require raw HK.00700 daily bars with provider-default session")
    if data.get("complete_requested_range") is not True or data.get("truncated"):
        raise ValueError("Incomplete provider range")
    rows = data["bars"]
    if not rows or data.get("count") != len(rows):
        raise ValueError("Empty or inconsistent bar count")
    result, previous = [], None
    for row in rows:
        day = dt.date.fromisoformat(row["date"])
        stamp = dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if (not first <= day <= last or (previous is not None and day <= previous)
                or stamp.tzinfo is None or stamp.astimezone(HK).date() != day
                or row.get("symbol") != "HK.00700"):
            raise ValueError("Invalid date, timestamp, symbol, or duplicate/unsorted bar")
        previous = day
        if any(isinstance(row[k], bool) for k in ("open", "high", "low", "close", "volume")):
            raise ValueError("Boolean OHLCV value")
        values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
        if (not all(math.isfinite(x) for x in values.values()) or values["volume"] < 0
                or min(values[k] for k in ("open", "high", "low", "close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise ValueError("Invalid OHLCV value/range")
        result.append({"date": pd.Timestamp(day), **values})
    return pd.DataFrame(result).set_index("date")


def features(frame):
    """All windows are trailing, including the completed feature day only."""
    import numpy as np
    import pandas as pd

    close, volume = frame["close"], frame["volume"]
    ret = close.pct_change(fill_method=None)
    result = pd.DataFrame({
        "return_1": ret,
        "return_5": close.pct_change(5, fill_method=None),
        "return_20": close.pct_change(20, fill_method=None),
        "close_to_ma_5": close / close.rolling(5).mean() - 1,
        "close_to_ma_20": close / close.rolling(20).mean() - 1,
        "volatility_5": ret.rolling(5).std(ddof=0),
        "volatility_20": ret.rolling(20).std(ddof=0),
        "intraday_return": close / frame["open"] - 1,
        "daily_range": (frame["high"] - frame["low"]) / close,
        "volume_to_ma_20": volume / volume.rolling(20).mean() - 1,
        "opening_gap": frame["open"] / close.shift(1) - 1,
    }, index=frame.index)
    return result.loc[:, list(FEATURES)].replace([np.inf, -np.inf], np.nan)


def samples(frame):
    import numpy as np
    import pandas as pd

    result = features(frame)
    next_close = frame["close"].shift(-1)
    result["label"] = np.where(next_close.notna(), (next_close > frame["close"]).astype(float), np.nan)
    result["label_date"] = pd.Series(frame.index, index=frame.index).shift(-1)
    return result.dropna(subset=[*FEATURES, "label", "label_date"])


def chronological_split(data):
    """60/20/20 by time, purging labels that touch the next partition."""
    if len(data) < 500:
        raise ValueError("Need at least 500 labeled observations for this baseline")
    a, b = int(len(data) * .6), int(len(data) * .8)
    train, validation, test = data.iloc[:a], data.iloc[a:b], data.iloc[b:]
    train = train.loc[train["label_date"] < validation.index.min()]
    validation = validation.loc[validation["label_date"] < test.index.min()]
    if min(len(part) for part in (train, validation, test)) < 50 or train["label"].nunique() < 2:
        raise ValueError("Insufficient partition sizes or training class diversity")
    return {"train": train, "validation": validation, "test": test}


def fit(data):
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    # Fixed in advance. No hyperparameter/threshold search over later data.
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, solver="lbfgs", max_iter=2000, random_state=0))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(data.loc[:, list(FEATURES)], data["label"].astype(int))
    return model


def portable_model(model, data):
    scaler, classifier = model.steps[0][1], model.steps[1][1]
    return {"schema_version": 1, "symbol": "HK.00700", "timeframe": "1d", "adjustment": "none",
            "target": "next observed daily close > feature-day close; ties are not-up",
            "features": list(FEATURES), "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
            "coefficients": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
            "classes": classifier.classes_.tolist(), "threshold": .5,
            "fit_feature_start": str(data.index.min().date()), "fit_feature_end": str(data.index.max().date()),
            "fit_label_end": str(data["label_date"].max().date()), "fit_rows": len(data),
            "orders_enabled": False}


def predict_portable(model, row):
    """Portable JSON inference; no pickle, framework, or execution client."""
    import math
    if model.get("schema_version") != 1 or model.get("features") != list(FEATURES) or model.get("classes") != [0, 1]:
        raise ValueError("Unsupported model schema")
    if any(len(model[k]) != len(FEATURES) for k in ("mean", "scale", "coefficients")):
        raise ValueError("Invalid model dimensions")
    score = float(model["intercept"])
    for name, mean, scale, weight in zip(FEATURES, model["mean"], model["scale"], model["coefficients"]):
        values = [float(row[name]), float(mean), float(scale), float(weight)]
        if not all(math.isfinite(x) for x in values) or scale <= 0:
            raise ValueError("Invalid inference feature or scaling")
        score += (values[0] - mean) / scale * weight
    if not math.isfinite(score): raise ValueError("Invalid model score")
    return 1 / (1 + math.exp(-score)) if score >= 0 else math.exp(score) / (1 + math.exp(score))


def metrics(labels, probabilities):
    import numpy as np
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, log_loss, roc_auc_score

    y, p = np.asarray(labels, dtype=int), np.asarray(probabilities, dtype=float)
    return {"n": len(y), "actual_up_rate": float(y.mean()),
            "accuracy": float(accuracy_score(y, p >= .5)),
            "balanced_accuracy": float(balanced_accuracy_score(y, p >= .5)),
            "roc_auc": float(roc_auc_score(y, p)) if len(set(y)) == 2 else None,
            "log_loss": float(log_loss(y, p, labels=[0, 1])), "brier_score": float(brier_score_loss(y, p)),
            "predicted_up_rate": float((p >= .5).mean())}


def run(output, start="2020-01-01", end=None, base_url="http://127.0.0.1:8765", input_response=None):
    import numpy as np
    import pandas as pd
    import sklearn

    end = end or (dt.datetime.now(HK).date() - dt.timedelta(days=1)).isoformat()
    if dt.date.fromisoformat(end) >= dt.datetime.now(HK).date() or start > end:
        raise ValueError("Use a completed historical range")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    url = local_url(base_url) + "/v1/bars?" + urllib.parse.urlencode(
        {"symbol": "HK.00700", "timeframe": "1d", "start": start, "end": end, "adjustment": "none"})
    if input_response:
        raw = Path(input_response).read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=120) as response: raw = response.read()
    (root / "provider_response.json").write_bytes(raw)
    response = json.loads(raw)
    frame = validate_response(response, start, end)
    data = samples(frame)
    split = chronological_split(data)
    model = fit(split["train"])
    portable = portable_model(model, split["train"])
    write_json(root / "evaluation_model.json", portable)
    prior = float(split["train"]["label"].mean())
    evaluations, predictions = {}, []
    for name, part in split.items():
        probability = model.predict_proba(part.loc[:, list(FEATURES)])[:, 1]
        from_json = np.array([predict_portable(portable, row) for _, row in part.iterrows()])
        if not np.allclose(probability, from_json, rtol=0, atol=1e-12):
            raise ValueError("Portable inference differs from sklearn")
        baseline = np.full(len(part), prior)
        evaluations[name] = {"feature_start": str(part.index.min().date()), "feature_end": str(part.index.max().date()),
                             "label_end": str(part["label_date"].max().date()),
                             "model": metrics(part["label"], probability), "constant_training_prior": metrics(part["label"], baseline)}
        out = part.loc[:, ["label", "label_date"]].copy()
        out["partition"], out["probability_up"], out["baseline_probability_up"] = name, probability, prior
        predictions.append(out)
    pd.concat(predictions).to_csv(root / "predictions.csv", index_label="feature_date")
    data.to_csv(root / "features_and_labels.csv", index_label="feature_date")
    # A separate refit supports current inference. It never supplies holdout metrics.
    refit = portable_model(fit(data), data)
    write_json(root / "model.json", refit)
    latest = features(frame).iloc[-1]
    latest_probability = predict_portable(refit, latest)
    latest_result = {"symbol": "HK.00700", "feature_date": str(frame.index[-1].date()),
                     "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "probability_up": latest_probability, "target": refit["target"],
                     "model": "model.json; refitted on all labeled history after evaluation",
                     "label_known_through": refit["fit_label_end"], "orders_enabled": False,
                     "interpretation": "Experimental model score for manual review, not calibrated odds or a trade instruction"}
    write_json(root / "latest_prediction.json", latest_result)
    frame.to_csv(root / "daily_bars.csv", index_label="date")
    test = evaluations["test"]
    report = {"completed": True, "symbol": "HK.00700", "model": "StandardScaler + L2 logistic regression, fixed C=1, threshold=0.5",
              "target": refit["target"], "features": list(FEATURES), "raw_bars": len(frame), "labeled_rows": len(data),
              "purged_boundary_rows": len(data) - sum(len(x) for x in split.values()),
              "first_date": str(frame.index[0].date()), "last_date": str(frame.index[-1].date()),
              "evaluation": evaluations,
              "test_accuracy_difference_from_baseline": test["model"]["accuracy"] - test["constant_training_prior"]["accuracy"],
              "test_log_loss_improvement_over_baseline": test["constant_training_prior"]["log_loss"] - test["model"]["log_loss"],
              "latest_prediction": latest_result,
              "provenance": {"input_response": str(Path(input_response).resolve()) if input_response else None,
                             "request_url": None if input_response else url,
                             "response_sha256": hashlib.sha256(raw).hexdigest(), "source": response["source"],
                             "provider_warnings": response.get("warnings", [])},
              "versions": {"sklearn": sklearn.__version__, "pandas": pd.__version__, "numpy": np.__version__},
              "portable_inference_matches_sklearn": True, "providers_modified": False, "orders_enabled": False,
              "limitations": ["Single-symbol baseline; one historical holdout does not establish predictive skill",
                              "Raw closing-price direction, not dividend-adjusted total return",
                              "Provider does not certify every trading date; target uses the next observed daily bar",
                              "Daily source timestamp labels are not availability times; features require the completed day",
                              "Retrospective history may be revised; original point-in-time receipt is unknown",
                              "Evaluation model fits training data only; full-history refit has no separate holdout score",
                              "No trading, execution, P&L, or cost simulation"]}
    write_json(root / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", help="Default: yesterday in Hong Kong; current-day candles are rejected")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--input-response", help="Replay a saved provider JSON response without network access")
    args = vars(parser.parse_args())
    report = run(**args)
    print(json.dumps({key: report[key] for key in ("symbol", "raw_bars", "evaluation", "latest_prediction")}, indent=2))


if __name__ == "__main__": main()
