"""Exhaustive pinned Qlib learner-class inventory, separate from network building blocks.

This scans installed Python source without importing optional model modules or
claiming that importability establishes behavioral coverage. Missing integration
is distinguished from missing real data and from absent fixed-runtime packages.
"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from .session import write_report

VERIFIED = {"linear": "Native OLS/NNLS/Ridge/Lasso; Parquet Ridge differential verified",
            "gbdt": "Native LGBModel chronological feature acceptance",
            "pytorch_lstm": "Native LSTM with Alpha360",
            "pytorch_gru": "Native GRU with Alpha360",
            "pytorch_alstm": "Native ALSTM with Alpha360",
            "pytorch_tcn": "Native TCN with Alpha360",
            "pytorch_transformer": "Native TransformerModel with Alpha360",
            "pytorch_nn": "CompatibleDNN retains native algorithm; scheduler and single-stock diagnostic adapted"}
DATA_BLOCKERS = {
    "pytorch_hist": "Dated HK stock-to-concept and instrument-index mappings are absent; CSI300 mapping is invalid",
}
ADAPTERS = {
    "pytorch_tra": "MTSDatasetH transport-memory lifecycle and multi-state router need integration and testing",
    "pytorch_tcts": "Native multi-horizon labels can be derived from real bars; dataset adapter and tests remain",
    "pytorch_adarnn": "Temporal-domain splits and transfer-loss training remain unverified",
    "pytorch_tabnet": "Native pretraining/finetuning path and checkpoint validation remain unverified",
    "pytorch_general_nn": "Configured network class and TSDatasetH training lifecycle remain unverified",
    "double_ensemble": "Native sample-reweighting and feature-selection rounds remain unverified",
}


def verified_extensions(reports: list[Path]) -> dict[str, dict[str, Any]]:
    """Accept only passing native model evidence bound to the current adapter source bytes."""
    accepted = {}
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        current = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in report.get("source_hashes", {})}
        if not report.get("passed") or not current or current != report.get("source_hashes"):
            raise ValueError(f"Unverified or stale model-extension evidence: {path}")
        if not report.get("sources_unchanged_during_verification"):
            raise ValueError(f"Missing source stability guard: {path}")
        for module, model in report["models"].items():
            if not all(model.get(key) for key in ("passed", "reload_predictions_exact", "learned_state_changed")):
                raise ValueError(f"Incomplete native model evidence for {module}")
            accepted[module] = {"path": str(path.resolve()),
                                 "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "configuration": model["configuration"], "snapshot_id": report["snapshot_id"],
                                 "snapshot_sha256": report["snapshot_sha256"],
                                 "mlflow_run_id": model["mlflow_run_id"]}
    return accepted


def inventory(reports: list[Path] | None = None, *, baseline: Path | None = None,
              meta: Path | None = None) -> dict[str, Any]:
    """Enumerate every installed public Qlib model wrapper derived from Model or ModelFT."""
    import qlib

    if qlib.__version__ != "0.9.7":
        raise ValueError("Review the inventory classifications before using a different Qlib version")
    evidence = verified_extensions(reports or [])
    baseline_evidence = None
    if baseline is not None:
        baseline_data = json.loads(baseline.read_text(encoding="utf-8"))
        if not baseline_data.get("passed"):
            raise ValueError("The existing native learner acceptance report did not pass")
        baseline_evidence = {"path": str(baseline.resolve()),
                             "report_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()}
    meta_evidence: dict[str, Any] = {"status": "available_unverified",
        "reason": "Real 20-stock panel is available; require passing native meta acceptance evidence"}
    if meta is not None:
        meta_data = json.loads(meta.read_text(encoding="utf-8"))
        current = hashlib.sha256(Path(__file__).with_name("qlib_meta_parquet.py").read_bytes()).hexdigest()
        if not meta_data.get("passed") or current != meta_data.get("source_sha256"):
            raise ValueError("Unverified or stale native meta-learning evidence")
        meta_evidence = {"status": "verified_selected_configuration", "path": str(meta.resolve()),
                         "report_sha256": hashlib.sha256(meta.read_bytes()).hexdigest(),
                         "scope": "Native proxy Spearman matrix, IC loss, time weights and reweighted models"}
    root = Path(qlib.__file__).parent / "contrib" / "model"
    entries = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                   and any(isinstance(base, ast.Name) and base.id in ("Model", "ModelFT") for base in node.bases)]
        for node in classes:
            module = path.stem
            package = {"catboost_model": "catboost", "xgboost": "xgboost"}.get(module)
            missing = package is not None and importlib.util.find_spec(package) is None
            if missing:
                status, reason = "blocked_optional_dependency", f"{package} is absent from the fixed shared lock"
            elif module in DATA_BLOCKERS:
                status, reason = "blocked_validation_data", DATA_BLOCKERS[module]
            elif module in evidence:
                status = "verified_selected_configuration"
                reason = "Native fitting, finite aligned predictions and exact serialized reload verified"
            elif module in VERIFIED and baseline_evidence:
                status, reason = "verified_selected_configuration", VERIFIED[module]
            else:
                status = "available_unverified"
                reason = ("Joint native configuration exists; "
                          "current passing evidence was not supplied to this inventory")
            entries.append({"module": "qlib.contrib.model." + module, "class": node.name,
                            "source_line": node.lineno, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                            "status": status, "reason": reason, "optional_package": package,
                            "evidence": evidence.get(module, baseline_evidence if module in VERIFIED else None),
                            "evidence_scope": "Selected configurations, not optimized strategy quality"})
    return {"qlib_version": qlib.__version__, "scope": "Every Model/ModelFT learner in installed contrib/model",
            "model_classes": len(entries), "counts": dict(Counter(item["status"] for item in entries)),
            "entries": entries, "full_feature_coverage": False,
            "additional_families": {
                "MetaModelDS": meta_evidence,
                "EnhancedIndexing": "Dated exposures/covariance/specific-risk/benchmark weights absent"},
            "note": "Available-unverified is not an external blocker or a verified feature"}


def run(output: Path, reports: list[Path] | None = None, *, baseline: Path | None = None,
        meta: Path | None = None) -> dict[str, Any]:
    """Persist the actual pinned-source inventory without requiring optional dependency installation."""
    result = inventory(reports, baseline=baseline, meta=meta)
    write_report(output, result)
    return result
