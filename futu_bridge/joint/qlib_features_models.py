"""Native supervised models and chronological Qlib rolling tasks over real captures."""

from __future__ import annotations

import copy
import hashlib
import importlib
import pickle
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
from qlib.data.dataset.processor import Processor
from qlib.contrib.model.pytorch_nn import DNNModelPytorch

from .session import write_report
from .tracking import EXPERIMENT


class FiniteByDate(Processor):
    """Faithful vectorized ProcessInf: replace only infinities by the same-date finite mean.

    Native ProcessInf loops through every date/column. Grouped transformation
    retains its cross-sectional behavior and leaves existing missing values intact.
    """

    def __call__(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Transform a copied handler frame, retaining its index and column order."""
        values = frame.replace([np.inf, -np.inf], np.nan)
        means = values.groupby(level="datetime", sort=False).transform("mean")
        return frame.mask(np.isinf(frame), means).sort_index()


def plateau_scheduler(optimizer: object) -> object:
    """Preserve native scheduler parameters while avoiding Qlib's lexicographic Torch-version bug."""
    from torch.optim.lr_scheduler import ReduceLROnPlateau

    return ReduceLROnPlateau(optimizer, mode="min", factor=.5, patience=10, threshold=.0001,
                            threshold_mode="rel", cooldown=0, min_lr=.00001, eps=1e-8)


class CompatibleDNN(DNNModelPytorch):
    """Native DNN training with a compatible scheduler and explicit single-stock diagnostic.

    Upstream's IC diagnostic requires at least 50 stocks per date. Negative MSE
    is logged instead; native MSE training, early stopping and checkpoint selection
    remain unchanged. This diagnostic is never represented as cross-sectional IC.
    """

    def __init__(self, **kwargs: object) -> None:
        """Delegate training, architecture, losses and prediction to the unchanged upstream class."""
        super().__init__(scheduler=plateau_scheduler, **kwargs)

    def get_metric(self, prediction: object, target: object, index: pd.Index) -> object:
        """Return a defined negative-MSE diagnostic without inventing single-stock IC."""
        return -((prediction.reshape(-1) - target.reshape(-1)) ** 2).mean()


def model_configs() -> dict[str, dict[str, object]]:
    """Expose explicitly tested native model configurations rather than claiming every optional model."""
    configs = {f"linear_{method}": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                                  "kwargs": {"estimator": method, "fit_intercept": True,
                                             "alpha": .001 if method == "lasso" else 1. if method == "ridge" else 0.}}
               for method in ("ols", "nnls", "ridge", "lasso")}
    configs["lgb"] = {"class": "LGBModel", "module_path": "qlib.contrib.model.gbdt", "kwargs": {
        "num_boost_round": 30, "early_stopping_rounds": 5, "num_leaves": 7, "min_data_in_leaf": 40,
        "num_threads": 1, "seed": 7, "deterministic": True, "force_col_wise": True}}
    common = {"d_feat": 6, "n_epochs": 2, "batch_size": 64, "early_stop": 2, "GPU": -1, "seed": 7}
    for name, klass in (("lstm", "LSTM"), ("gru", "GRU"), ("alstm", "ALSTM")):
        configs[name] = {"class": klass, "module_path": "qlib.contrib.model.pytorch_" + name,
                         "kwargs": {**common, "hidden_size": 16, "num_layers": 1}}
    configs["tcn"] = {"class": "TCN", "module_path": "qlib.contrib.model.pytorch_tcn", "kwargs": {
        **common, "n_chans": 16, "num_layers": 2, "kernel_size": 3, "dropout": 0.}}
    configs["transformer"] = {"class": "TransformerModel", "module_path": "qlib.contrib.model.pytorch_transformer",
                              "kwargs": {**common, "d_model": 16, "nhead": 2, "num_layers": 1, "n_jobs": 1}}
    configs["dnn"] = {"class": "CompatibleDNN", "module_path": "futu_bridge.joint.qlib_features_models", "kwargs": {
        "max_steps": 12, "batch_size": 64, "eval_steps": 4, "early_stop_rounds": 4, "GPU": -1, "seed": 7,
        "optimizer": "adam", "pt_model_kwargs": {"input_dim": 158, "layers": (32,)}}}
    return configs


def score_predictions(prediction: pd.Series, labels: pd.Series) -> dict[str, object]:
    """Require finite aligned outputs and score descriptive MSE and directional accuracy."""
    if not prediction.index.equals(labels.index) or not np.isfinite(prediction).all():
        raise ValueError("Native model predictions are nonfinite or misaligned")
    return {"rows": len(labels), "mse": float(np.mean((prediction - labels) ** 2)),
            "direction_accuracy": float(np.mean((prediction > 0) == (labels > 0)))}


def neural_weights(model: object) -> str | None:
    """Hash actual learned tensor values when a native model contains a Torch module."""
    import torch

    modules = [value for value in vars(model).values() if isinstance(value, torch.nn.Module)]
    if not modules:
        return None
    digest = hashlib.sha256()
    for module in modules:
        for name, tensor in module.state_dict().items():
            digest.update(name.encode())
            digest.update(tensor.detach().cpu().numpy().tobytes())
    return digest.hexdigest()


def train_models(datasets: dict[str, object], output: Path) -> dict[str, object]:
    """Fit actual native models, select only on validation and verify serialized prediction identity."""
    import torch
    from qlib.utils import init_instance_by_config
    from qlib.workflow import R

    torch.set_num_threads(1)
    entries, predictions = {}, {}
    for name, config in model_configs().items():
        family = "Alpha360" if name in ("lstm", "gru", "alstm", "tcn", "transformer") else "Alpha158"
        dataset = datasets[family]
        model = init_instance_by_config(config)
        before = pickle.dumps(model)
        initial_weights = neural_weights(model)
        started = perf_counter()
        with R.start(experiment_name=EXPERIMENT, recorder_name="feature_" + name):
            if name == "lgb":
                model.fit(dataset, verbose_eval=False)
            elif name in ("lstm", "gru", "alstm", "tcn", "transformer", "dnn"):
                model.fit(dataset, save_path=str(output / f"{name}_native_weights.bin"))
            else:
                model.fit(dataset)
            elapsed = perf_counter() - started
            predictions[name] = {part: model.predict(dataset, segment=part) for part in ("valid", "test")}
            metrics = {part: score_predictions(predictions[name][part], dataset.prepare(
                part, col_set="label", data_key="learn").iloc[:, 0]) for part in ("valid", "test")}
            artifact = pickle.dumps(model)
            if artifact == before:
                raise ValueError("Native model did not acquire learned state")
            if initial_weights and initial_weights == neural_weights(model):
                raise ValueError("Native neural model did not update actual weights")
            path = output / f"{name}_model.pkl"
            path.write_bytes(artifact)
            restored = pickle.loads(path.read_bytes())
            after = restored.predict(dataset, segment="test")
            if not np.array_equal(after.to_numpy(), predictions[name]["test"].to_numpy()):
                raise ValueError("Native trained model changed predictions after reload")
            predictions[name]["test"].to_csv(output / f"{name}_test_predictions.csv")
            R.log_metrics(validation_mse=metrics["valid"]["mse"], test_mse=metrics["test"]["mse"])
            R.get_recorder().log_artifact(str(path))
        entries[name] = {"passed": True, "handler": family, "configuration": config,
                         "metrics": metrics, "fit_seconds": elapsed, "reload_predictions_exact": True,
                         "neural_weights_changed": True if initial_weights else None}
        if name == "dnn":
            entries[name]["converter_adaptations"] = ["Torch scheduler logging argument compatibility",
                                                      "Undefined single-stock IC diagnostic replaced by negative MSE"]
        print("Qlib native model", name, "passed", flush=True)
    selected = min(entries, key=lambda name: (entries[name]["metrics"]["valid"]["mse"], name))
    # Record the validation decision explicitly; later test metrics never determine selection.
    write_report(output / "model_selection.json", {"selected": selected, "criterion": "validation MSE only",
                                                  "test_reused_historical": True})
    labels = datasets["Alpha158"].prepare("train", col_set="label", data_key="learn").iloc[:, 0]
    test = datasets["Alpha158"].prepare("test", col_set="label", data_key="learn").iloc[:, 0]
    baseline = pd.Series(float(labels.mean()), index=test.index)
    missing = {}
    for package in ("xgboost", "catboost", "optuna"):
        try:
            importlib.import_module(package)
            missing[package] = "installed but not part of this verified model selection"
        except ModuleNotFoundError:
            missing[package] = "absent from fixed environment; no replacement falsely labeled native"
    return {"passed": True, "configs_trained": len(entries), "entries": entries, "selected": selected,
            "constant_training_mean_test": score_predictions(baseline, test), "optional_dependencies": missing,
            "limitation": "Small native training conformance budget, not optimized predictive performance"}


def rolling_workflow(frame: pd.DataFrame, output: Path) -> dict[str, object]:
    """Train native generated rolling tasks and verify each stored model's predictions and date boundaries."""
    from qlib.model.trainer import TrainerR
    from qlib.utils import init_instance_by_config
    from qlib.workflow.task.gen import RollingGen
    from qlib.workflow.online.utils import OnlineToolR
    from .qlib_features import handler_config

    dates = frame.index
    initial = {"train": (str(dates[60]), str(dates[-303])),
               "valid": (str(dates[-301]), str(dates[-183])),
               "test": (str(dates[-181]), str(dates[-2]))}
    template = {"model": model_configs()["linear_ridge"], "dataset": {"class": "DatasetH",
        "module_path": "qlib.data.dataset", "kwargs": {"handler": handler_config("Alpha158", frame, initial),
                                                       "segments": initial}},
        "record": [{"class": "SignalRecord", "module_path": "qlib.workflow.record_temp"}]}
    generated = RollingGen(step=60, rtype=RollingGen.ROLL_EX, trunc_days=1).generate(template)
    # Native RollingGen changes segments; explicitly refit normalization on each expanding train fold.
    tasks = copy.deepcopy(generated)
    for task in tasks:
        seg = task["dataset"]["kwargs"]["segments"]
        settings = task["dataset"]["kwargs"]["handler"]["kwargs"]
        settings.update(fit_start_time=seg["train"][0], fit_end_time=seg["train"][1])
        if not seg["train"][1] < seg["valid"][0] < seg["valid"][1] < seg["test"][0]:
            raise ValueError("Rolling task leaks chronological partitions")
    trainer = TrainerR(experiment_name=EXPERIMENT, default_rec_name="native_rolling_ridge")
    records = trainer.train(tasks)
    trainer.end_train(records)
    evidence = []
    for task, recorder in zip(tasks, records):
        prediction = recorder.load_object("pred.pkl").iloc[:, 0]
        model = recorder.load_object("params.pkl")
        dataset = init_instance_by_config(task["dataset"])
        restored = model.predict(dataset)
        if recorder.status != "FINISHED" or not np.array_equal(prediction.to_numpy(), restored.to_numpy()):
            raise ValueError("Native rolling recorder reload failed")
        if not prediction.index.equals(restored.index):
            raise ValueError("Rolling prediction indices changed")
        part = task["dataset"]["kwargs"]["segments"]
        dates_used = prediction.index.get_level_values("datetime")
        if dates_used.min() <= pd.Timestamp(part["valid"][1]) or dates_used.max() > frame.index[-2]:
            raise ValueError("Rolling predictions overlap fitting dates or an unavailable label")
        evidence.append({"recorder_id": recorder.id, "rows": len(prediction),
                         "segments": {k: [str(x) for x in v] for k, v in part.items()},
                         "reload_predictions_exact": True})
    if len(evidence) < 2:
        raise ValueError("Rolling verification requires multiple genuine retraining folds")
    tool = OnlineToolR(EXPERIMENT)
    transition = []
    for chosen in (records[0], records[-1], records[0], records[-1]):
        tool.reset_online_tag(chosen)
        active = [record.id for record in tool.online_models()]
        if active != [chosen.id]:
            raise ValueError("Native online-recorder promotion/rollback did not isolate the selected model")
        transition.append(chosen.id)
    final = records[-1]
    old_prediction = final.load_object("pred.pkl")
    tool.update_online_pred(to_date=str(frame.index[-1]), from_date=str(frame.index[-1]))
    updated = final.load_object("pred.pkl")
    if len(updated) != len(old_prediction) + 1 or not updated.loc[old_prediction.index].equals(old_prediction):
        raise ValueError("Native online prediction update changed old predictions or omitted the new observation")
    final_dataset = init_instance_by_config(tasks[-1]["dataset"])
    final_dataset.config(segments={"test": (frame.index[-1], frame.index[-1])})
    expected_new = final.load_object("params.pkl").predict(final_dataset)
    if not np.array_equal(updated.iloc[-1].to_numpy(), expected_new.to_numpy()):
        raise ValueError("Native PredUpdater differs from direct loaded model inference")
    return {"passed": True, "native_generator": "RollingGen", "native_trainer": "TrainerR",
            "tasks": evidence, "online_recorder_transitions": transition, "native_prediction_updates": 1,
            "old_predictions_preserved": True, "distributed": False, "continuous_online_service": False}
