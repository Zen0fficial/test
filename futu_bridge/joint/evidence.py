"""Bind successful local verification to source bytes and atomically published reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .session import write_report


def digest(path: Path) -> str:
    """Hash an evidence artifact without interpreting its contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(repo: Path) -> dict[str, str]:
    """Fingerprint joint code, converter code, fixed providers and the shared dependency lock."""
    paths = set((repo / "futu_bridge/joint").rglob("*.py"))
    paths.update((repo / "futu_bridge").glob("*.py"))
    paths.update((repo / "futu_bridge").glob("*.js"))
    paths.update((repo / "futu_bridge").glob("*.json"))
    for extension in ("*.py", "*.cs"):
        paths.update((repo / "futu_bridge/converters").rglob(extension))
    paths.update(p for p in (repo / "src").rglob("*") if p.is_file())
    for name in ("requirements/joint-py312-windows.lock", "pyproject.toml"):
        if (repo / name).is_file():
            paths.add(repo / name)
    result = {p.relative_to(repo).as_posix(): digest(p) for p in sorted(paths)}
    if not result:
        raise ValueError("Cannot certify an empty implementation manifest")
    return result


def publish(repo: Path, reports: dict[str, Path], before: dict[str, str]) -> None:
    """Publish only passing offline reports after checking code stayed fixed during verification."""
    if before != source_manifest(repo):
        raise ValueError("Implementation changed during feature verification; evidence was not published")
    index = repo / "output/joint/verified_features.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {}
    for platform, path in reports.items():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("passed") is not True or value.get("real_orders_enabled") is not False:
            raise ValueError("Cannot publish failed, incomplete or unsafe platform evidence")
        entries[platform] = {"report": str(path.resolve()), "sha256": digest(path), "sources": before}
    index.parent.mkdir(parents=True, exist_ok=True)
    write_report(index, entries)


def lookup(repo: Path, platform: str) -> tuple[Path | None, str]:
    """Resolve published evidence while distinguishing corruption, source drift and absent reports."""
    index = repo / "output/joint/verified_features.json"
    if not index.is_file():
        return None, "not_recorded"
    path = None
    try:
        entry = json.loads(index.read_text(encoding="utf-8")).get(platform)
        if entry is None:
            return None, "not_recorded"
        path = Path(entry["report"])
        if not path.is_file():
            return path, "missing_evidence"
        if digest(path) != entry["sha256"]:
            return path, "evidence_hash_mismatch"
        if entry["sources"] != source_manifest(repo):
            return path, "source_changed_since_verification"
        return path, "current_source_verified"
    except (KeyError, ValueError, OSError, TypeError):
        return path, "unreadable_evidence"
