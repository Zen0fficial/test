"""Minimal tensor-container and deserialization compatibility for Qlib0.9.7 learners.

Native training transposes sampler axes, but native predict omits that transpose.
Only inference's input permutation changes. Convolutions, parameter objects,
optimizer, losses and trainer remain unchanged. The adapted predict method is
from Microsoft Qlib0.9.7 pytorch_tcn_ts.py; see QLIB_NOTICE.txt for the MIT notice.
IGMTF retains the same computed hidden values in a one-dimensional tensor object
array. TRA restores only its optional diagnostic writer after native unpickling.
"""

from __future__ import annotations

from typing import Any

import torch
import numpy as np
import pandas as pd
from torch.utils.data import DataLoader
from qlib.contrib.model.pytorch_tcn_ts import TCN
from qlib.contrib.model.pytorch_igmtf import IGMTF
from qlib.contrib.model.pytorch_tra import TRAModel
from qlib.data.dataset.handler import DataHandlerLP


class CompatibleTSTCN(TCN):
    """Retain native TCN training and repair inference's missing time/feature transpose."""

    def predict(self, dataset: Any) -> pd.Series:
        """Match native training's input layout while retaining upstream sampler and scoring behavior."""
        if not self.fitted:
            raise ValueError("model is not fitted yet")
        samples = dataset.prepare("test", col_set=["feature", "label"], data_key=DataHandlerLP.DK_I)
        samples.config(fillna_type="ffill+bfill")
        loader = DataLoader(samples, batch_size=self.batch_size, num_workers=self.n_jobs)
        self.TCN_model.eval()
        predictions = []
        for data in loader:
            feature = data[:, :, :-1].transpose(1, 2).to(self.device)
            with torch.no_grad():
                predictions.append(self.TCN_model(feature.float()).detach().cpu().numpy())
        return pd.Series(np.concatenate(predictions), index=samples.get_index())


class CompatibleIGMTF(IGMTF):
    """Preserve native hidden states as a one-dimensional tensor object array on modern NumPy."""

    def get_train_hidden(self, x_train: pd.DataFrame) -> tuple[np.ndarray, torch.Tensor]:
        """Undo NumPy's eager equal-shaped tensor expansion without changing values or native aggregation."""
        native, daily = super().get_train_hidden(x_train)
        values = np.empty(len(native), dtype=object)
        for index, item in enumerate(native):
            values[index] = item if isinstance(item, torch.Tensor) else torch.from_numpy(
                np.asarray(item, dtype=np.float32))
        return values, daily


class CompatibleTRA(TRAModel):
    """Restore the nonpersistent diagnostic writer after native Qlib model deserialization."""

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore all serialized native model state and leave the optional live writer closed."""
        super().__setstate__(state)
        self._writer = None
