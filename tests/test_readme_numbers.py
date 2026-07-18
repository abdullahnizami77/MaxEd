"""Invariant I10: every number in the README is injected from results/.

The enforcement is a drift check: re-running the injection over the
committed results files must reproduce the committed README byte for byte.
A hand-edited number inside any injected block fails this test.
"""

from __future__ import annotations

import pytest

from balancecheck.config import REPO_ROOT

README = REPO_ROOT / "README.md"
RESULTS = REPO_ROOT / "results"


@pytest.mark.skipif(
    not README.exists() or "BC:BEGIN" not in README.read_text(encoding="utf-8"),
    reason="README injection blocks not present yet (pre-submission state)",
)
def test_readme_numbers_match_results():
    from balancecheck.spine.report import readme_inject

    committed = README.read_text(encoding="utf-8")
    regenerated = readme_inject(README, RESULTS)
    assert regenerated == committed, (
        "README drifted from results/: a number was edited by hand or results "
        "changed without re-running readme injection (make readme)"
    )
