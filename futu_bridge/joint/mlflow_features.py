"""Native local MLflow registry, promotion, tracking HTTP and inference verification.

All services bind loopback, use the existing interpreter and terminate after
verification. Model promotion selects on validation only; this is infrastructure
conformance over real data, not evidence of a profitable trading strategy.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .paper_common import capture
from .session import write_report


def request(url: str, payload: dict[str, object] | None = None) -> object:
    """Use bounded local HTTP requests, returning decoded JSON or plain text."""
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("Feature verification allows loopback services only")
    body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
    with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10) as response:
        raw = response.read().decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


@contextmanager
def local_service(arguments: list[str], log_path: Path, env: dict[str, str]) -> Iterator[str]:
    """Start one bounded native MLflow service and clean up its own process tree."""
    import psutil

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "mlflow", *arguments,
                                    "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
                                   stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        parent = psutil.Process(process.pid)
        try:
            deadline = time.monotonic() + 55
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Local MLflow process exited: see {log_path}")
                try:
                    request(url + ("/ping" if arguments[0] == "models" else "/health"))
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Local MLflow readiness timed out: see {log_path}")
                    time.sleep(.2)
            yield url
        finally:
            try:
                owned = parent.children(recursive=True) + [parent]
            except psutil.NoSuchProcess:
                owned = []
            for child in owned:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(owned, timeout=5)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.wait(timeout=10)


def dataset(response: Path) -> tuple[pd.DataFrame, dict[str, list[str]], dict[str, object]]:
    """Build completed-minute causal features and within-session next-close labels."""
    data = capture(response)
    pieces = []
    for _, frame in data.minutes.groupby(data.minutes.index.date):
        # Reset at lunch as well as overnight; never bridge a missing interval.
        groups = frame.index.to_series().diff().ne(pd.Timedelta(minutes=1)).cumsum()
        for _, segment in frame.groupby(groups.to_numpy()):
            rows = pd.DataFrame({"return1": segment.close.pct_change(fill_method=None),
                                 "mean5": segment.close / segment.close.rolling(5).mean() - 1,
                                 "range": (segment.high - segment.low) / segment.close,
                                 "target": segment.close.shift(-1) / segment.close - 1})
            pieces.append(rows.dropna())
    result = pd.concat(pieces).sort_index()
    if not np.isfinite(result.to_numpy()).all() or result.index.has_duplicates:
        raise ValueError("Model registry dataset must be unique and finite")
    return result, data.partitions, data.provenance


def promote(client: object, name: str, candidates: list[dict[str, object]]) -> str:
    """Choose a verified candidate using only validation loss and a stable tie break."""
    if not candidates or any(not c.get("reload_verified") or not np.isfinite(c["validation_mse"])
                             for c in candidates):
        raise ValueError("Promotion requires finite validation scores and verified reloads")
    chosen = min(candidates, key=lambda c: (c["validation_mse"], int(c["version"])))
    client.set_registered_model_alias(name, "champion", chosen["version"])
    if client.get_model_version_by_alias(name, "champion").version != chosen["version"]:
        raise ValueError("Registry alias did not resolve to the selected version")
    return chosen["version"]


def exercise(response: Path, output: Path) -> dict[str, object]:
    """Train, register, promote, reload, serve and independently check two real-data models."""
    import mlflow
    from mlflow.models import infer_signature
    from mlflow.tracking import MlflowClient
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    rows, partitions, provenance = dataset(response)
    split = {name: rows.loc[rows.index.strftime("%Y-%m-%d").isin(days)] for name, days in partitions.items()}
    validation = "validation" if "validation" in split else "valid"
    train, valid, test = split["train"], split[validation], split["test"]
    if not train.index.max() < valid.index.min() < test.index.min() or min(len(v) for v in split.values()) < 20:
        raise ValueError("Chronological nonempty model training/validation/test partitions are required")
    columns = ["return1", "mean5", "range"]
    env = dict(os.environ, MLFLOW_DISABLE_AGENT_HINT="1", MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING="false",
               PYTHONIOENCODING="utf-8")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    store = f"sqlite:///{(output / 'mlflow.db').as_posix()}"
    arguments = ["server", "--backend-store-uri", store, "--default-artifact-root",
                 (output / "artifacts").as_uri(), "--no-serve-artifacts"]
    original_uri = mlflow.get_tracking_uri()
    print_setting = os.environ.get("MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT")
    os.environ["MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT"] = "true"
    candidates, predictions = [], {}
    try:
        with local_service(arguments, output / "tracking-server.log", env) as tracking:
            mlflow.set_tracking_uri(tracking)
            mlflow.set_experiment("joint-feature-model-registry")
            client = MlflowClient(tracking_uri=tracking)
            name = "HK00700-research-return"
            for alpha in (1., 10.):
                model = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
                model.fit(train[columns], train.target)
                before = model.predict(test[columns])
                # Direct standardized linear algebra is independent of sklearn.predict.
                scaler, ridge = model.steps[0][1], model.steps[1][1]
                standardized = (test[columns].to_numpy() - scaler.mean_) / scaler.scale_
                independent = standardized @ ridge.coef_ + ridge.intercept_
                if not np.allclose(before, independent, atol=1e-14, rtol=0):
                    raise ValueError("Independent regression inference mismatch")
                valid_mse = float(np.mean((model.predict(valid[columns]) - valid.target.to_numpy()) ** 2))
                with mlflow.start_run(run_name=f"ridge-{alpha:g}") as active:
                    mlflow.log_params({"alpha": alpha, "source_sha256": provenance["response_sha256"],
                                       "real_orders_enabled": False, "selection_partition": "validation"})
                    logged_data = mlflow.data.from_pandas(train.reset_index(drop=True),
                                      source=response.as_uri(), targets="target", name="real-hk-train")
                    mlflow.log_input(logged_data, context="training")
                    mlflow.log_metric("validation_mse", valid_mse)
                    info = mlflow.sklearn.log_model(model, name="model", registered_model_name=name,
                        signature=infer_signature(train[columns], model.predict(train[columns])),
                        input_example=train[columns].iloc[:2], pip_requirements=[
                            f"scikit-learn=={__import__('sklearn').__version__}", f"numpy=={np.__version__}",
                            f"pandas=={pd.__version__}"])
                    restored = mlflow.pyfunc.load_model(info.model_uri).predict(test[columns])
                    if not np.array_equal(before, restored):
                        raise ValueError("Native MLflow model reload changed predictions")
                    lineage = client.get_run(active.info.run_id).inputs.dataset_inputs
                    if len(lineage) != 1 or lineage[0].dataset.name != "real-hk-train":
                        raise ValueError("Native dataset lineage was not retained")
                    version = str(info.registered_model_version)
                    candidates.append({"version": version, "alpha": alpha, "validation_mse": valid_mse,
                                       "reload_verified": True, "run_id": active.info.run_id,
                                       "model_uri": info.model_uri, "dataset_digest": lineage[0].dataset.digest})
                    predictions[version] = before
            selected = promote(client, name, candidates)
            uri = f"models:/{name}@champion"
            restored = mlflow.pyfunc.load_model(uri).predict(test[columns])
            if not np.array_equal(restored, predictions[selected]):
                raise ValueError("Alias-based inference differs from the selected native model")
            alternate = next(c["version"] for c in candidates if c["version"] != selected)
            client.set_registered_model_alias(name, "champion", alternate)
            if client.get_model_version_by_alias(name, "champion").version != alternate:
                raise ValueError("Model alias rollback failed")
            client.set_registered_model_alias(name, "champion", selected)
            serve_env = dict(env, MLFLOW_TRACKING_URI=tracking)
            with local_service(["models", "serve", "--model-uri", uri, "--env-manager", "local"],
                               output / "inference-server.log", serve_env) as inference:
                sample = test[columns].iloc[:20]
                body = {"dataframe_split": {"columns": columns, "data": sample.to_numpy().tolist()}}
                served = request(inference + "/invocations", body)
                if not np.allclose(served["predictions"], predictions[selected][:20], atol=1e-14, rtol=0):
                    raise ValueError("Native HTTP inference changed predictions")
                try:
                    request(inference + "/invocations", {"dataframe_records": [{"unknown": 1.}]})
                except HTTPError as error:
                    if error.code != 400:
                        raise
                else:
                    raise ValueError("Serving accepted a request missing the required feature schema")
            runs = client.search_runs([client.get_experiment_by_name("joint-feature-model-registry").experiment_id])
            if len(runs) != 2 or any(r.info.status != "FINISHED" for r in runs):
                raise ValueError("Model registry runs did not finish cleanly")
        return {"passed": True, "source": provenance, "rows": {k: len(v) for k, v in split.items()},
                "candidates": candidates, "selected_version": selected, "tracking_http_verified": True,
                "registry_alias_verified": True, "rollback_verified": True, "dataset_lineage_verified": True,
                "http_predictions_verified": 20, "schema_rejection_verified": True,
                "holdout_mse": float(np.mean((predictions[selected] - test.target.to_numpy()) ** 2)),
                "services_stopped": True, "shared_python": sys.executable, "real_orders_enabled": False,
                "limitations": ["Local loopback lifecycle; no authenticated multi-user production deployment",
                                "Two Ridge research models, not all MLflow flavors or profitable-strategy evidence"]}
    finally:
        mlflow.set_tracking_uri(original_uri)
        if print_setting is None:
            os.environ.pop("MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT", None)
        else:
            os.environ["MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT"] = print_setting


def run(response: Path, output: Path, repo: Path) -> dict[str, object]:
    """Publish success only after native services and model lifecycle checks complete."""
    response, output = response.resolve(), output.resolve()
    if output == response.parent or response.parent in output.parents:
        raise ValueError("MLflow verification output must be outside the source capture")
    output.mkdir(parents=True, exist_ok=False)
    write_report(output / "status.json", {"state": "running"})
    try:
        report = exercise(response, output)
        write_report(output / "report.json", report)
        write_report(output / "status.json", {"state": "completed"})
        return report
    except Exception:
        write_report(output / "status.json", {"state": "failed"})
        raise


def main() -> None:
    """Run this isolated worker using the existing shared Python environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.response, args.output, Path.cwd()), indent=2))


if __name__ == "__main__":
    main()
