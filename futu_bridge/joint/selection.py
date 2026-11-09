"""Deterministic capability selection; no silent downgrade after data errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    """Supported data-only operations; execution is deliberately absent."""

    REPLAY = "replay"
    LIVE = "live"
    FEATURES = "features"
    STORAGE = "storage"


@dataclass(frozen=True, slots=True)
class Selection:
    """Auditable backend choice and its suitability rationale."""

    backend: str
    reason: str
    fallback: bool


def select(capability: Capability, available: set[str], requested: str = "auto") -> Selection:
    """Choose by documented fidelity/capability policy, not unproven speed claims.

    An explicit unavailable backend raises. Automatic fallback is allowed only
    for unavailable runtimes, before an operation begins. Runtime/data failures
    must propagate; they never trigger a semantic change mid-run.
    """
    policies = {
        Capability.REPLAY: ("nautilus", "lean"),
        Capability.LIVE: ("lean", "nautilus"),
        Capability.FEATURES: ("qlib", "reference"),
        Capability.STORAGE: ("qlib",),
    }
    preferences = policies[capability]
    candidates = preferences if requested == "auto" else (requested,)
    for name in candidates:
        if name in preferences and name in available:
            return Selection(name, f"{capability.value}: policy {' > '.join(preferences)}; available runtime selected",
                             name != preferences[0])
    raise RuntimeError(f"No eligible {capability.value} backend for {requested}")
