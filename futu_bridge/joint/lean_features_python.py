"""Native LEAN Python algorithm reusing the tested C# framework in the shared environment.

Loaded only by LEAN's embedded Python.NET host, not by the provider service.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import clr

clr.AddReference("QuantConnect.Algorithm.CSharp")
from QuantConnect.Algorithm.CSharp import LocalHKFeaturesAlgorithm


class LocalHKSharedPythonAlgorithm(LocalHKFeaturesAlgorithm):
    """Keep native model execution fast while allowing Python research and shared package use."""

    def Initialize(self) -> None:
        """Initialize the native offline pipeline and prove shared-package origins."""
        super().Initialize()
        self.shared_modules = {}
        for name in ("numpy", "pandas", "qlib", "nautilus_trader", "mlflow"):
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            if not path.is_relative_to(Path(sys.prefix).resolve()):
                raise ValueError("LEAN Python imported a module outside the configured shared environment")
            self.shared_modules[name] = {"file": str(path), "version": getattr(module, "__version__", None)}

    def OnData(self, data: object) -> None:
        """Delegate native data/indicator/history work without creating a second numerical implementation."""
        super().OnData(data)

    def OnOrderEvent(self, event: object) -> None:
        """Keep native accounting and its complete simulated-fill journal."""
        super().OnOrderEvent(event)

    def OnEndOfAlgorithm(self) -> None:
        """Attach shared-Python runtime evidence to the native framework result."""
        super().OnEndOfAlgorithm()
        path = Path(self.GetParameter("verification-output"))
        result = json.loads(path.read_text(encoding="utf-8"))
        result["python_runtime"] = {"prefix": sys.prefix, "version": sys.version,
                                    "modules": self.shared_modules, "native_python_algorithm": True}
        path.write_text(json.dumps(result), encoding="utf-8")
