"""Invariant I9: the em dash character (U+2014) appears nowhere in the
repository's source, prose, prompt, or fixture files.

The scan walks the repo for *.py, *.md, *.json, and *.toml files, skipping
only generated or third-party trees (.venv, .git, log, runs, results,
node_modules). The character is referenced here by escape only, so this
file passes its own scan.
"""

from __future__ import annotations

from pathlib import Path

from balancecheck.config import REPO_ROOT

EM_DASH = "\u2014"
EXTENSIONS = {".py", ".md", ".json", ".toml"}
EXCLUDED_DIRS = {".venv", ".git", "log", "runs", "results", "node_modules"}


def scannable_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in EXTENSIONS:
            continue
        relative_parts = path.relative_to(root).parts[:-1]
        if any(part in EXCLUDED_DIRS for part in relative_parts):
            continue
        files.append(path)
    return files


def test_scan_finds_the_source_tree() -> None:
    """Guard against a silently empty scan: the repo's own files must be in
    the walked set, and excluded trees must not be."""
    files = scannable_files(REPO_ROOT)
    names = {p.relative_to(REPO_ROOT).as_posix() for p in files}
    assert "balancecheck/spine/report.py" in names
    assert "balancecheck/contracts/models.py" in names
    assert "tests/test_no_emdash.py" in names
    assert not any(n.startswith((".venv/", "log/", "runs/", "results/")) for n in names)


def test_no_emdash_anywhere() -> None:
    offenders: list[str] = []
    for path in scannable_files(REPO_ROOT):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if EM_DASH in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, (
        "em dash (U+2014) found in: " + ", ".join(offenders) + "; use commas, colons, or parentheses"
    )
