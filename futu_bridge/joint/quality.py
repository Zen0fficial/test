"""Dependency-free structural coding gate for all joint framework modules.

This checks syntax, documentation, annotations, explicit exception handling,
line length and the read-only boundary. It is not a substitute for semantic
type checking or behavioral tests, which are described in the coding guide.
"""

from __future__ import annotations

import ast
from pathlib import Path


def check(root: Path) -> list[str]:
    """Return all standards violations without importing or executing modules."""
    failures: list[str] = []
    forbidden = {"submit_order", "MarketOrder", "LimitOrder", "SetHoldings", "Liquidate"}
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if not ast.get_docstring(tree):
            failures.append(f"{path.name}: missing module documentation")
        for line, text in enumerate(source.splitlines(), 1):
            if len(text) > 119 or text.rstrip() != text or "\t" in text:
                failures.append(f"{path.name}:{line}: line length/whitespace")
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', 1)}"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not ast.get_docstring(node) or node.returns is None:
                    failures.append(f"{where}: require docstring and return annotation")
                arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                arguments += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
                if any(a.annotation is None for a in arguments if a.arg not in ("self", "cls")):
                    failures.append(f"{where}: unannotated argument")
            if isinstance(node, ast.ClassDef) and not ast.get_docstring(node):
                failures.append(f"{where}: undocumented class")
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                failures.append(f"{where}: bare exception handler")
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                failures.append(f"{where}: wildcard import")
            if isinstance(node, ast.Assert):
                failures.append(f"{where}: production validation must survive python -O")
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in forbidden:
                    failures.append(f"{where}: execution is outside the data-only contract")
                if any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value
                       for k in node.keywords):
                    failures.append(f"{where}: shell execution is forbidden")
        compile(tree, str(path), "exec")
    return failures


def main() -> None:
    """Fail the local quality gate on any new-framework structural violation."""
    root = Path(__file__).parent
    failures = check(root)
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Structural coding gate passed for {len(list(root.glob('*.py')))} modules")


if __name__ == "__main__":
    main()
