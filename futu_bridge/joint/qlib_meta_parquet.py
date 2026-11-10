"""Native Qlib data-selection meta-learning over a real manually selected HK stock panel.

InternalData trains native proxy models and computes actual cross-sectional
Spearman performance. MetaDatasetDS masks overlapping and future information;
MetaModelDS keeps its native IC loss, weighting network and reweighted learner
interface. This is retrospective API/fidelity acceptance, not a universe backtest.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pickle
from typing import Any

import numpy as np
import pandas as pd
from qlib.contrib.meta.data_selection.dataset import InternalData

from .qlib_parquet import initialize, view
from .session import write_report
from .storage import SnapshotStore
from .tracking import prepare_tracking


class CompatibleInternalData(InternalData):
    """Retain native Spearman performance while explicitly preserving pandas group labels."""

    def _calc_perf(self, pred: pd.Series, label: pd.Series) -> pd.Series:
        """Preserve the datetime level removed by modern pandas when group_keys is false."""
        frame = pd.DataFrame({"pred": pred, "label": label})
        correlation = frame.groupby("datetime", group_keys=True).corr(method="spearman")
        return correlation.xs("pred", level=-1)["label"]


def template(instruments: list[str], dates: pd.DatetimeIndex) -> dict[str, Any]:
    """Use three causal native expressions and an explicitly regularized native Ridge proxy."""
    fields = ["$close/Ref($close,1)-1", "$close/Mean($close,5)-1", "$volume/(Mean($volume,5)+1)-1"]
    handler = {"class": "DataHandlerLP", "module_path": "qlib.data.dataset.handler", "kwargs": {
        "instruments": instruments, "start_time": str(dates[0]), "end_time": str(dates[-1]),
        "data_loader": {"class": "QlibDataLoader", "module_path": "qlib.data.dataset.loader", "kwargs": {
            "config": {"feature": (fields, ["RET1", "MA5", "VOL5"]),
                       "label": (["Ref($close,-1)/$close-1"], ["LABEL0"])}}},
        "infer_processors": [{"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}}],
        "learn_processors": [{"class": "DropnaLabel"}]}}
    return {"model": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                       "kwargs": {"estimator": "ridge", "alpha": 1., "fit_intercept": False}},
            "dataset": {"class": "DatasetH", "module_path": "qlib.data.dataset", "kwargs": {
                "handler": handler, "segments": {"train": (str(dates[60]), str(dates[300])),
                    "valid": (str(dates[302]), str(dates[400])), "test": (str(dates[420]), str(dates[620]))}}},
            "record": [{"class": "SignalRecord", "module_path": "qlib.workflow.record_temp"}]}


def independent_spearman(prediction: pd.Series, label: pd.Series) -> pd.Series:
    """Calculate rank correlation with centered ranks independently of native groupby.corr."""
    paired = pd.DataFrame({"pred": prediction, "label": label}).dropna()
    ranks = paired.groupby(level="datetime").rank(method="average")
    centered = ranks - ranks.groupby(level="datetime").transform("mean")
    numerator = (centered.pred * centered.label).groupby(level="datetime").sum()
    denominator = np.sqrt(centered.pred.pow(2).groupby(level="datetime").sum()
                          * centered.label.pow(2).groupby(level="datetime").sum())
    return numerator / denominator


def run(output: Path, repo: Path) -> dict[str, Any]:
    """Verify native proxy performance, causal meta inputs, learned weights and exact checkpoint inference."""
    import torch
    from joblib import parallel_backend
    from qlib.contrib.meta.data_selection.dataset import MetaDatasetDS
    from qlib.contrib.meta.data_selection.model import MetaModelDS
    from qlib.contrib.meta.data_selection.net import PredNet
    from qlib.model.trainer import TrainerR
    from qlib.utils import init_instance_by_config
    from qlib.workflow import R

    torch.set_num_threads(1)
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    panel_root = repo / "output/joint/plan_completion_20261110/panel"
    manifest = json.loads((panel_root / "report.json").read_text())
    store = SnapshotStore(panel_root / "store")
    snapshot = store.snapshot(manifest["snapshot"])
    source = view(str(store.root), snapshot.identity)
    instruments = sorted(set(source.frame.index.get_level_values("instrument")))
    dates = source.calendar
    if len(instruments) < 20 or len(dates) < 680:
        raise ValueError("Meta acceptance requires at least20 aligned stocks and680 observed sessions")
    initialize(store.root, snapshot.identity, cache=output / "cache", exp_manager=prepare_tracking(output))
    definition = template(instruments, dates)
    internal = CompatibleInternalData(deepcopy(definition), step=20, exp_name="native-meta-proxies")
    with parallel_backend("threading", n_jobs=2):
        internal.setup()
    performance = internal.data_ic_df
    performance.to_parquet(output / "native_performance.parquet")
    if performance.shape[1] < 10 or performance.notna().sum().min() < 100:
        raise ValueError("Native performance matrix lacks genuine cross-sectional evidence")
    label = init_instance_by_config(definition["dataset"]).handler.fetch(col_set="label").iloc[:, 0]
    proxies = R.list_recorders(experiment_name="native-meta-proxies")
    max_error = 0.
    for recorder in proxies.values():
        predictions = recorder.load_object("pred.pkl").iloc[:, 0]
        task = recorder.load_object("task")
        key = tuple(task["dataset"]["kwargs"]["segments"]["train"])
        expected = independent_spearman(predictions, label).reindex(performance.index)
        actual = performance[key]
        if not np.allclose(expected, actual, equal_nan=True, rtol=0, atol=1e-12):
            raise ValueError("Native InternalData Spearman matrix differs from independent centered-rank correlation")
        max_error = max(max_error, float((expected - actual).abs().max()))
        recorder.set_tags(joint_snapshot_id=snapshot.identity, joint_snapshot_sha256=snapshot.sha256)
    tasks = []
    for boundary in (280, 340, 400, 460, 520, 580, 640):
        task = deepcopy(definition)
        task["dataset"]["kwargs"]["segments"] = {
            "train": (str(dates[60]), str(dates[boundary - 2])),
            "test": (str(dates[boundary]), str(dates[boundary + 19]))}
        tasks.append(task)
    meta = MetaDatasetDS(task_tpl=tasks, step=20, trunc_days=2, exp_name=internal,
                         segments=.6, hist_step_n=3, fill_method="max")
    for task in meta.meta_task_l:
        end = pd.Timestamp(task.task["dataset"]["kwargs"]["segments"]["train"][1])
        if task.meta_info.index.max() > end or any(pd.Timestamp(column[1]) > end for column in task.meta_info.columns):
            raise ValueError("Native meta features include future performance or future-trained proxy models")
        for start, stop in task.meta_info.columns:
            overlap = task.meta_info[(start, stop)].loc[start:stop]
            if not overlap.isna().all():
                raise ValueError("Native meta features failed to mask their proxy training overlap")
    with torch.random.fork_rng():
        torch.manual_seed(43)
        initial = PredNet(step=20, hist_step_n=3, clip_weight=2., clip_method="tanh", alpha=1.)
        initial_weights = {name: value.detach().clone() for name, value in initial.named_parameters()}
    model = MetaModelDS(step=20, hist_step_n=3, criterion="ic_loss", max_epoch=3,
                        loss_skip_thresh=10, alpha=1., seed=43)
    with R.start(experiment_name="native-meta-training", recorder_name="MetaModelDS"):
        model.fit(meta)
        recorder = R.get_recorder()
        recorder.set_tags(joint_snapshot_id=snapshot.identity, joint_snapshot_sha256=snapshot.sha256,
                          manual_basket=True, real_orders_enabled=False)
        changes = [float((value.detach() - initial_weights[name]).abs().max())
                   for name, value in model.tn.named_parameters()]
        if not max(changes) > 0 or not all(torch.isfinite(value).all() for value in model.tn.parameters()):
            raise ValueError("Native meta IC training did not update finite weighting-network parameters")
        model_path = output / "meta_model.pkl"
        model_path.write_bytes(pickle.dumps(model))
        restored = pickle.loads(model_path.read_bytes())
        selected, reloaded = model.inference(meta), restored.inference(meta)
        for first, second in zip(selected, reloaded):
            pd.testing.assert_series_equal(first["reweighter"].time_weight, second["reweighter"].time_weight,
                                           check_exact=True)
            if not np.isfinite(first["reweighter"].time_weight).all():
                raise ValueError("Native inferred sample weights are not finite")
        recorder.log_artifact(str(model_path))
        training_run = recorder.id
    trainer = TrainerR(experiment_name="native-meta-reweighted")
    records = trainer.train(selected)
    trainer.end_train(records)
    reweighted_rows = 0
    for task, record in zip(selected, records):
        prediction = record.load_object("pred.pkl").iloc[:, 0]
        dataset = init_instance_by_config(task["dataset"])
        restored = record.load_object("params.pkl").predict(dataset)
        pd.testing.assert_series_equal(prediction, restored, check_exact=True, check_names=False)
        if record.status != "FINISHED" or not np.isfinite(prediction).all():
            raise ValueError("Reweighted native learner did not finish finite held-out inference")
        reweighted_rows += len(prediction)
    if source_hash != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("Meta adapter source changed during acceptance")
    report = {"passed": True, "snapshot_id": snapshot.identity, "snapshot_sha256": snapshot.sha256,
              "instruments": instruments, "real_rows": snapshot.rows, "native_proxy_models": len(proxies),
              "performance_shape": list(performance.shape),
              "performance_nonmissing": int(performance.notna().sum().sum()),
              "independent_spearman_max_error": max_error, "native_meta_tasks": len(meta.meta_task_l),
              "meta_train_tasks": len(meta.prepare_tasks("train")), "meta_test_tasks": len(meta.prepare_tasks("test")),
              "overlap_and_future_mask_verified": True, "criterion": "native_ic_loss", "loss_skip_thresh": 10,
              "meta_epochs": 3, "maximum_parameter_change": max(changes), "reload_weights_exact": True,
              "reweighted_native_models": len(records), "reweighted_prediction_rows": reweighted_rows,
              "mlflow_training_run": training_run, "real_orders_enabled": False, "providers_modified": False,
              "source_sha256": source_hash, "sources_unchanged_during_verification": True,
              "limitations": ["Retrospective manually selected20-stock basket; not a historical universe",
                              "New bounded conformance configuration, not optimized predictive performance",
                              "Native IC minimum-stock parameter is10 for this20-stock acceptance basket",
                              "Historical publication-time availability and index/risk inputs remain unavailable"]}
    write_report(output / "report.json", report)
    return report


def main() -> None:
    """Run genuine cross-sectional native meta-learning without modifying packages or providers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    args = parser.parse_args()
    run(args.output, args.repo)


if __name__ == "__main__":
    main()
