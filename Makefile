# BALANCECHECK: every gate is a command, not a vibe.
#
# Stub mode is the default and needs zero credentials. Live mode reads
# BC_MODE=live, BC_BASE_URL, BC_MODEL from the environment (or a local
# .env you source yourself; nothing deployment-specific is committed).

PY := ./.venv/bin/python

.PHONY: demo fixtures stubs verify test pass1 poolA ingest pass2 pass2r errors bench report readme clean-log

fixtures:
	$(PY) -m balancecheck.substrate.foundry

stubs: fixtures
	$(PY) -m balancecheck.cli make-stubs

demo: stubs
	BC_MODE=stub $(PY) -m balancecheck.cli demo

test:
	$(PY) -m pytest -q

verify: test
	$(PY) -m balancecheck.cli verify-report-determinism

# --- the learning loop, live ---
poolA:
	$(PY) -m balancecheck.cli run-pool --pool A --pass-label poolA

review:
	$(PY) -m balancecheck.cli review --list

pass1:
	$(PY) -m balancecheck.cli run-pool --pool B --pass-label pass1

ingest:
	$(PY) -m balancecheck.cli ingest

pass2:
	$(PY) -m balancecheck.cli run-pool --pool B --pass-label pass2 --with-memory

pass2r:
	$(PY) -m balancecheck.cli run-pool --pool B --pass-label pass2R --with-random-memory

# --- evidence ---
errors:
	$(PY) -m balancecheck.cli errors

bench-select:
	$(PY) -m balancecheck.cli bench-select

bench-judge:
	$(PY) -m balancecheck.cli bench-judge

bench-agree:
	$(PY) -m balancecheck.cli bench-agree

bench-pairwise:
	$(PY) -m balancecheck.cli bench-pairwise

report:
	$(PY) -m balancecheck.cli report

readme: report
	$(PY) -m balancecheck.cli readme-inject
