"""The I1 lint: no float literal in any module under balancecheck/substrate/
or balancecheck/checks/. The AST scan is a lint, not the proof (the runtime
boundary checks in money.py are the proof); it iterates whatever .py files
exist under the two packages, so checks/ is covered automatically as soon as
that package gains modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import balancecheck

SCANNED_PACKAGES = ("substrate", "checks")


def _scanned_files() -> list[Path]:
    pkg_root = Path(balancecheck.__file__).resolve().parent
    files: list[Path] = []
    for sub in SCANNED_PACKAGES:
        directory = pkg_root / sub
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def test_scan_covers_the_money_substrate() -> None:
    names = {path.name for path in _scanned_files()}
    assert "money.py" in names
    assert "derive.py" in names
    assert "foundry.py" in names


def test_no_float_literal_in_money_bearing_modules() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path}:{node.lineno}: {node.value!r}")
    assert offenders == [], "float literals in money-bearing modules: " + "; ".join(offenders)
