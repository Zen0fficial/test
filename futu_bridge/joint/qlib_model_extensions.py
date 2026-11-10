"""Explicit native model configurations and dataset contracts for additional Qlib learners.

Budgets here are new integration acceptance configurations, not changes to the
existing model suite or claims of optimized predictive performance. Native model
implementations and losses are retained; flat, sequence and transport datasets
are supplied through their own native interfaces.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd

from .qlib_features import handler_config, segments


def configurations(output: Path, *, panel: bool = False) -> dict[str, dict[str, Any]]:
    """Return typed native configurations with fixed CPU, seed and explicit bounded budgets."""
    common = {"n_epochs": 2, "early_stop": 2, "GPU": -1, "seed": 7}
    recurrent = {**common, "d_feat": 6, "hidden_size": 16, "num_layers": 1, "batch_size": 64,
                 "dropout": 0., "n_jobs": 0}
    entries: dict[str, tuple[str, dict[str, Any], str]] = {}
    for module, name in (("pytorch_lstm_ts", "LSTM"), ("pytorch_gru_ts", "GRU"),
                         ("pytorch_alstm_ts", "ALSTM")):
        entries[module] = (name, recurrent, "ts")
    entries["pytorch_general_nn"] = ("GeneralPTNN", {**common, "batch_size": 64, "n_jobs": 0,
        "pt_model_kwargs": {"d_feat": 6, "hidden_size": 16, "num_layers": 1, "dropout": 0.}}, "ts")
    transformer = {**common, "d_feat": 6, "d_model": 16, "nhead": 2, "num_layers": 1,
                   "batch_size": 64, "dropout": 0., "n_jobs": 0}
    entries["pytorch_transformer_ts"] = ("TransformerModel", transformer, "ts")
    entries["pytorch_localformer_ts"] = ("LocalformerModel", transformer, "ts")
    entries["pytorch_localformer"] = ("LocalformerModel", transformer, "flat360")
    entries["pytorch_tcn_ts"] = ("TCN", {**common, "d_feat": 6, "n_chans": 16, "kernel_size": 3,
                                        "num_layers": 2, "batch_size": 64, "n_jobs": 0}, "ts")
    entries["pytorch_adarnn"] = ("ADARNN", {**common, "d_feat": 6, "hidden_size": 16, "num_layers": 1,
        "batch_size": 64, "pre_epoch": 1, "len_seq": 60, "n_splits": 2}, "flat360")
    entries["pytorch_krnn"] = ("KRNN", {**common, "fea_dim": 6, "cnn_dim": 16, "rnn_dim": 16,
        "rnn_dups": 2, "rnn_layers": 1, "batch_size": 64}, "flat360")
    entries["pytorch_sandwich"] = ("Sandwich", {**common, "fea_dim": 6, "cnn_dim_1": 16, "cnn_dim_2": 8,
        "rnn_dim_1": 16, "rnn_dim_2": 8, "rnn_dups": 2, "rnn_layers": 1, "batch_size": 64}, "flat360")
    entries["pytorch_sfm"] = ("SFM", {**common, "d_feat": 6, "hidden_size": 16, "freq_dim": 4,
                                      "batch_size": 64, "eval_steps": 1}, "flat360")
    entries["pytorch_tcts"] = ("TCTS", {**common, "d_feat": 6, "hidden_size": 16, "num_layers": 1,
        "input_dim": 360, "output_dim": 3, "batch_size": 64, "steps": 1,
        "fore_lr": .001, "weight_lr": .001}, "multi_horizon")
    entries["pytorch_tabnet"] = ("TabnetModel", {**common, "d_feat": 158, "out_dim": 16, "n_d": 8,
        "n_a": 8, "n_shared": 1, "n_ind": 1, "n_steps": 2, "batch_size": 64, "vbs": 16,
        "pretrain": True, "pretrain_n_epochs": 2, "pretrain_file": str(output / "tabnet_pretrain.bin")}, "tabnet")
    entries["double_ensemble"] = ("DEnsembleModel", {"num_models": 2, "epochs": 20,
        "bins_sr": 5, "bins_fs": 5, "num_leaves": 7, "min_data_in_leaf": 20,
        "num_threads": 1, "seed": 7, "verbosity": -1, "decay": .9}, "flat158")
    if panel:
        entries = {"pytorch_gats": ("GATs", {**common, "d_feat": 6, "hidden_size": 64,
                                              "num_layers": 2}, "flat360"),
                   "pytorch_gats_ts": ("GATs", {**common, "d_feat": 6, "hidden_size": 64,
                                                 "num_layers": 2, "n_jobs": 0}, "ts"),
                   "pytorch_igmtf": ("IGMTF", {**common, "d_feat": 6, "hidden_size": 64,
                                                "num_layers": 2, "metric": "ic"}, "flat360"),
                   "pytorch_add": ("ADD", {**common, "d_feat": 6, "hidden_size": 16,
                                            "num_layers": 1, "batch_size": 128}, "flat360"),
                   "highfreq_gdbt_model": ("HFLGBModel", {"num_leaves": 7, "min_data_in_leaf": 20,
                        "num_threads": 1, "seed": 7, "verbosity": -1}, "flat158"),
                   "pytorch_tra": ("TRAModel", {"model_config": {"input_size": 6, "hidden_size": 16,
                        "num_layers": 1, "dropout": 0.}, "tra_config": {"num_states": 2, "hidden_size": 8},
                        "n_epochs": 2, "early_stop": 2, "seed": 7, "transport_method": "router",
                        "memory_mode": "sample", "eval_test": False, "logdir": None}, "transport")}
    result = {module: {"model": {"class": name, "module_path": "qlib.contrib.model." + module,
                                  "kwargs": deepcopy(kwargs)}, "dataset_kind": kind}
              for module, (name, kwargs, kind) in entries.items()}
    if "pytorch_tcn_ts" in result:
        result["pytorch_tcn_ts"]["model"].update(
            {"class": "CompatibleTSTCN", "module_path": "futu_bridge.joint.qlib_model_extensions_compat"})
        result["pytorch_tcn_ts"]["adaptation"] = "Match native training's time/feature transpose during prediction"
    for module, name, reason in (
            ("pytorch_igmtf", "CompatibleIGMTF",
             "Retain native hidden tensors in a one-dimensional NumPy object array"),
            ("pytorch_tra", "CompatibleTRA",
             "Restore optional diagnostic writer as closed after native deserialization")):
        if module in result:
            result[module]["model"].update({"class": name,
                                           "module_path": "futu_bridge.joint.qlib_model_extensions_compat"})
            result[module]["adaptation"] = reason
    return result


def six_features(instruments: list[str], dates: pd.DatetimeIndex,
                 split: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """Configure six causal current-bar fields for native sequential samplers."""
    fields = ["$open/$close", "$high/$close", "$low/$close", "$close/Ref($close,1)-1",
              "$volume/Mean($volume,20)-1", "$vwap/$close"]
    return {"class": "DataHandlerLP", "module_path": "qlib.data.dataset.handler", "kwargs": {
        "instruments": instruments, "start_time": str(dates[0]), "end_time": str(dates[-1]),
        "data_loader": {"class": "QlibDataLoader", "module_path": "qlib.data.dataset.loader", "kwargs": {
            "config": {"feature": (fields, ["OPEN", "HIGH", "LOW", "RET", "VOL", "VWAP"]),
                       "label": (["Ref($close,-1)/$close-1"], ["LABEL0"])}}},
        "infer_processors": [{"class": "FiniteByDate", "module_path": "futu_bridge.joint.qlib_features_models"},
            {"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature",
                "fit_start_time": split["train"][0], "fit_end_time": split["train"][1]}},
            {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}}],
        "learn_processors": [{"class": "DropnaLabel"}]}}


def dataset_config(kind: str, frame: pd.DataFrame, instruments: list[str]) -> dict[str, Any]:
    """Provide native flat, TSDataSampler, multi-label and transport-memory dataset contracts."""
    split = segments(frame)
    if kind == "multi_horizon":
        dates = frame.index
        first, second = int(len(dates) * .6), int(len(dates) * .8)
        split = {"train": (str(dates[60]), str(dates[first - 6])),
                 "valid": (str(dates[first]), str(dates[second - 6])),
                 "test": (str(dates[second]), str(dates[-6]))}
    if kind in ("ts", "transport"):
        handler = six_features(instruments, frame.index, split)
    else:
        handler = handler_config("Alpha158" if kind in ("flat158", "tabnet") else "Alpha360", frame, split)
        handler["kwargs"]["instruments"] = instruments
        if kind == "multi_horizon":
            handler["kwargs"]["label"] = ([f"Ref($close,-{horizon})/$close-1" for horizon in (1, 2, 5)],
                                            [f"LABEL{horizon}" for horizon in (1, 2, 5)])
        if kind == "tabnet":
            training = frame.loc[split["train"][0]:split["train"][1]].index
            split_at = int(len(training) * .8)
            split["pretrain"] = (str(training[0]), str(training[split_at - 1]))
            split["pretrain_validation"] = (str(training[split_at]), str(training[-1]))
            handler["kwargs"]["fit_end_time"] = str(training[split_at - 1])
    settings = {"handler": handler, "segments": split}
    if kind == "ts":
        return {"class": "TSDatasetH", "module_path": "qlib.data.dataset", "kwargs": {**settings, "step_len": 20}}
    if kind == "transport":
        return {"class": "MTSDatasetH", "module_path": "qlib.contrib.data.dataset", "kwargs": {
            **settings, "seq_len": 20, "horizon": 1, "num_states": 2, "memory_mode": "sample",
            "batch_size": 64, "input_size": 6}}
    return {"class": "DatasetH", "module_path": "qlib.data.dataset", "kwargs": settings}
