# Injected errors

## Tier 0 (clean controls: caught means falsely flagged)

| error_id | scenario | expected kind | expected action | decision | reason | caught | notes |
|---|---|---|---|---|---|---|---|
| E-CLEAN-S-A-01 | S-A-01 | none | human_gate | human_gate | all checks passed | no |  |
| E-CLEAN-S-A-02 | S-A-02 | none | human_gate | human_gate | all checks passed | no |  |
| E-CLEAN-S-A-03 | S-A-03 | none | human_gate | human_gate | all checks passed | no |  |
| E-CLEAN-S-A-04 | S-A-04 | none | human_gate | human_gate | all checks passed | no |  |
| E-CLEAN-S-A-05 | S-A-05 | none | human_gate | human_gate | all checks passed | no |  |

Tier 0: caught 0/5 (0%).

## Tier 1

| error_id | scenario | expected kind | expected action | decision | reason | caught | notes |
|---|---|---|---|---|---|---|---|
| E-T1-01-S-A-01 | S-A-01 | c_amt_or_sum_fail | revise_or_escalate | revise | correctable code-check failure | yes |  |
| E-T1-01-S-A-02 | S-A-02 | c_amt_or_sum_fail | revise_or_escalate | revise | correctable code-check failure | yes |  |
| E-T1-02-S-A-01 | S-A-01 | c_exist_fail | escalate | escalate | fabricated document reference | yes |  |
| E-T1-02-S-A-02 | S-A-02 | c_exist_fail | escalate | escalate | fabricated document reference | yes |  |
| E-T1-03-S-A-01 | S-A-01 | c_sum_fail | revise_or_escalate | revise | correctable code-check failure | yes |  |
| E-T1-03-S-A-02 | S-A-02 | c_sum_fail | revise_or_escalate | revise | correctable code-check failure | yes |  |

Tier 1: caught 6/6 (100%).

## Tier 2

| error_id | scenario | expected kind | expected action | decision | reason | caught | notes |
|---|---|---|---|---|---|---|---|
| T2-SUM-IGNORES-UNAPPLIED | S-A-02 | c_sum_or_amt_fail | revise_or_escalate | revise | correctable code-check failure | yes |  |
| T2-FULL-ORIGINAL-AMOUNTS | S-A-03 | c_sum_or_amt_fail | revise_or_escalate | revise | itemized amounts contradict the computed totals | yes |  |
| T2-CREDITS-ASSUMED-APPLIED | S-A-04 | c_sum_or_amt_fail | revise_or_escalate | revise | draft is missing required content | yes |  |

Tier 2: caught 3/3 (100%).

## Tier 3

| error_id | scenario | expected kind | expected action | decision | reason | caught | notes |
|---|---|---|---|---|---|---|---|
| E-T3-01 | S-A-06 | ambiguous_allocation | abstain_or_escalate | abstain | ambiguous allocation | yes |  |

Tier 3: caught 1/1 (100%).

## Tier 4

| error_id | scenario | expected kind | expected action | decision | reason | caught | notes |
|---|---|---|---|---|---|---|---|
| E-T4-S-A-01 | S-A-01 | fuzzy_unsupported | revise_or_escalate | revise | unsupported fuzzy claim | yes |  |
| E-T4-S-A-03 | S-A-03 | fuzzy_unsupported | revise_or_escalate | revise | unsupported fuzzy claim | yes |  |

Tier 4: caught 2/2 (100%).

## Balanced accuracy per tier

Raw accuracy would reward a rubber stamp (most claims in real drafts are true); balanced accuracy averages the catch rate on corrupted rows with the pass rate on the clean controls.

Tier 1: balanced accuracy 100% (catch rate 6/6 (100%), clean-pass rate 5/5 (100%)).
Tier 2: balanced accuracy 100% (catch rate 3/3 (100%), clean-pass rate 5/5 (100%)).
Tier 3: balanced accuracy 100% (catch rate 1/1 (100%), clean-pass rate 5/5 (100%)).
Tier 4: balanced accuracy 100% (catch rate 2/2 (100%), clean-pass rate 5/5 (100%)).
