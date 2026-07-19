# Before/after: first-pass drafts, code-computed

Scores are read on first-pass drafts (stage first_pass), before any
revision, because the revise loop scrubs numeric errors and a
final-stage read would mask learning.

## Per pass

| pass | scenarios | grounding | completeness | revisions (total/scenarios) | terminal actions |
|---|---|---|---|---|---|
| pass1 | 6 | 36/36 (100%) | 12/12 (100%) | 0/6 | human_gate: 5, abstain: 1 |
| pass2 | 6 | 58/58 (100%) | 12/12 (100%) | 0/6 | human_gate: 5, abstain: 1 |
| pass2R | 6 | 56/56 (100%) | 12/12 (100%) | 0/6 | human_gate: 5, abstain: 1 |

## Paired per scenario: pass1 vs pass2

| scenario | grounding pass1 -> pass2 | delta (pp) | completeness pass1 -> pass2 | delta (pp) | revisions pass1 -> pass2 | delta |
|---|---|---|---|---|---|---|
| S-B-01 | 6/6 (100%) -> 10/10 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-02 | 8/8 (100%) -> 13/13 (100%) | 0 | 3/3 (100%) -> 3/3 (100%) | 0 | 0 -> 0 | 0 |
| S-B-03 | 6/6 (100%) -> 12/12 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-04 | 10/10 (100%) -> 13/13 (100%) | 0 | 3/3 (100%) -> 3/3 (100%) | 0 | 0 -> 0 | 0 |
| S-B-05 | 6/6 (100%) -> 10/10 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-06 | 0/0 (n/a) -> 0/0 (n/a) | n/a | 0/0 (n/a) -> 0/0 (n/a) | n/a | 0 -> 0 | 0 |

n = 6 paired scenarios. n is too small for a significance claim; deltas are directional.

## Directional: the behaviours the Pool A edits targeted

Each behaviour is a code-derived check over the first-pass draft
(the client name, the issue and due dates of every open invoice, an
in-place explanation of unapplied items, a reconciliation invite), not
a fixed golden phrase. A behaviour that cannot apply to a scenario (no
unapplied item) is excluded from that scenario's denominator. The pass2R
column is the random-memory ablation: relevant retrieval versus arbitrary
examples, the comparison that isolates the retrieval function.

| behaviour | pass1 | pass2 | pass2R |
|---|---|---|---|
| greets the client by name | 0/5 | 5/5 | 5/5 |
| itemizes open invoices with issue and due dates | 0/5 | 5/5 | 5/5 |
| explains unapplied cash or credit in place | 0/2 | 2/2 | 1/2 |
| invites reconciliation in the closing | 4/5 | 5/5 | 5/5 |

| measure | pass1 | pass2 | pass2R |
|---|---|---|---|
| mean first-pass draft length (chars) | 429 | 637 | 599 |
