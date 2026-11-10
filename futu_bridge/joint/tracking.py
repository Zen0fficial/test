"""Local SQLite-backed MLflow integration for Qlib's native experiment recorder."""

from __future__ import annotations

import hashlib
from pathlib import Path

EXPERIMENT = "hk00700-comparison"


def tracking_uri(root: Path) -> str:
    """Return an absolute SQLite URI; never inherit an external tracking server."""
    return f"sqlite:///{(root.resolve() / 'mlflow.db').as_posix()}"


def prepare_tracking(root: Path) -> dict[str, object]:
    """Create local run metadata and artifact storage for Qlib and MLflow together."""
    from mlflow.tracking import MlflowClient

    uri = tracking_uri(root)
    client = MlflowClient(tracking_uri=uri)
    client.create_experiment(EXPERIMENT, artifact_location=(root.resolve() / "mlartifacts").as_uri())
    return {"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": uri, "default_exp_name": EXPERIMENT}}


def verify_tracking(root: Path) -> dict[str, object]:
    """Read both finished Qlib runs through MLflow and verify model artifact bytes."""
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri(root))
    experiment = client.get_experiment_by_name(EXPERIMENT)
    if experiment is None:
        raise ValueError("Missing MLflow experiment")
    runs = client.search_runs([experiment.experiment_id])
    expected = {"qlib_lgb_7", "qlib_lgb_15"}
    if {run.info.run_name for run in runs} != expected or len(runs) != 2:
        raise ValueError("Missing or duplicated Qlib model runs")
    evidence: list[dict[str, object]] = []
    for run in runs:
        if run.info.status != "FINISHED" or not run.data.metrics:
            raise ValueError("MLflow run did not finish with training metrics")
        filename = f"{run.info.run_name}.txt"
        restored = Path(client.download_artifacts(run.info.run_id, filename))
        original_hash = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        if hashlib.sha256(restored.read_bytes()).hexdigest() != original_hash:
            raise ValueError("MLflow model artifact did not round-trip")
        evidence.append({"name": run.info.run_name, "run_id": run.info.run_id, "status": run.info.status,
                         "metric_count": len(run.data.metrics), "model_sha256": original_hash})
    return {"passed": True, "backend": "sqlite", "tracking_uri": tracking_uri(root), "runs": evidence}
