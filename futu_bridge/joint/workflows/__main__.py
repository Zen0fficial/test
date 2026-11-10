"""Inspect and reconcile trusted local task pools without a MongoDB service."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any


def inspect(root: Path, pool: str | None = None) -> dict[str, Any]:
    """Read task state without decoding pickle payloads, claiming tasks or creating a database."""
    path = root.resolve() / "tasks.sqlite"
    if not path.is_file():
        raise FileNotFoundError("No existing task database at the requested root")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as database:
        database.row_factory = sqlite3.Row
        database.execute("BEGIN")
        schema = database.execute("PRAGMA user_version").fetchone()[0]
        if schema != 3:
            raise ValueError("Inspection requires current task schema3; migrate through TaskManager first")
        clause, arguments = (" WHERE pool=?", (pool,)) if pool is not None else ("", ())
        rows = database.execute("SELECT id,pool,status,priority,owner,fence,attempts,failures,max_attempts,"
                                "lease_clock,lease_ns,result_sha,error FROM tasks" + clause +
                                " ORDER BY pool,created_ns,id", arguments).fetchall()
        outbox = database.execute("SELECT o.task_id,o.fence,o.state,o.error FROM outbox o "
                                  "JOIN tasks t ON t.id=o.task_id" +
                                  (" WHERE t.pool=?" if pool is not None else "") +
                                  " ORDER BY t.pool,o.task_id,o.fence", arguments).fetchall()
    return {"root": str(root.resolve()), "schema": schema, "tasks": [dict(row) for row in rows],
            "outbox": [dict(row) for row in outbox], "real_orders_enabled": False,
            "lease_clock": "monotonic_ns: not a UTC timestamp"}


def main(argv: list[str] | None = None) -> None:
    """Expose inspection, reconciliation and explicit quiescent backup/restore of trusted task roots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pool")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("command", choices=("inspect", "export", "reconcile", "backup", "restore", "verify-backup"))
    args = parser.parse_args(argv)
    if args.command in ("backup", "restore", "verify-backup"):
        from .backup import backup_tasks, restore_tasks, verify_backup

        if args.pool is not None:
            raise ValueError("Backup and restore apply to the entire task root; omit --pool")
        if args.command != "verify-backup" and args.destination is None:
            raise ValueError("Backup/restore requires a new --destination outside its source root")
        operation = {"backup": backup_tasks, "restore": restore_tasks, "verify-backup": verify_backup}[args.command]
        result = operation(args.root) if args.command == "verify-backup" else operation(args.root, args.destination)
    else:
        before = inspect(args.root, args.pool)
        if args.command == "inspect":
            result = before
        else:
            from .manager import TaskManager

            if args.pool is None or not before["tasks"]:
                raise ValueError("Export/reconcile requires an existing nonempty --pool")
            manager = TaskManager(args.pool, args.root)
            result = ({"snapshot": manager.export_history()} if args.command == "export" else manager.reconcile())
            if result.get("pending_errors"):
                print(json.dumps(result, indent=2))
                raise SystemExit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
