# Before/after: first-pass drafts, code-computed

Scores are read on first-pass drafts (stage first_pass), before any
revision, because the revise loop scrubs numeric errors and a
final-stage read would mask learning.

## Per pass

| pass | scenarios | grounding | completeness | mean revisions | terminal actions |
|---|---|---|---|---|---|
| pass1 | 6 | 36/36 (100%) | 12/15 (80%) | 0/6 | human_gate: 5, abstain: 1 |
| pass2 | 6 | 62/62 (100%) | 12/15 (80%) | 0/6 | human_gate: 5, abstain: 1 |
| pass2R | 6 | 55/55 (100%) | 12/15 (80%) | 0/6 | human_gate: 5, abstain: 1 |

## Paired per scenario: pass1 vs pass2

| scenario | grounding pass1 -> pass2 | delta (pp) | completeness pass1 -> pass2 | delta (pp) | revisions pass1 -> pass2 | delta |
|---|---|---|---|---|---|---|
| S-B-01 | 6/6 (100%) -> 11/11 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-02 | 8/8 (100%) -> 13/13 (100%) | 0 | 3/3 (100%) -> 3/3 (100%) | 0 | 0 -> 0 | 0 |
| S-B-03 | 6/6 (100%) -> 12/12 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-04 | 10/10 (100%) -> 13/13 (100%) | 0 | 3/3 (100%) -> 3/3 (100%) | 0 | 0 -> 0 | 0 |
| S-B-05 | 6/6 (100%) -> 13/13 (100%) | 0 | 2/2 (100%) -> 2/2 (100%) | 0 | 0 -> 0 | 0 |
| S-B-06 | 0/0 (n/a) -> 0/0 (n/a) | n/a | 0/3 (0%) -> 0/3 (0%) | 0 | 0 -> 0 | 0 |

n = 6 paired scenarios. n is too small for a significance claim; deltas are directional.

## Directional: the behaviors the Pool A edits targeted

Rates over first-pass Pool B drafts; each marker is a deterministic
string check named in the generator, not a judged quality.

| behavior | pass1 | pass2 |
|---|---|---|
| greets the client by name | 0/5 | 5/5 |
| itemizes with issue and due dates | 0/5 | 5/5 |
| explains unapplied items in place | 0/5 | 1/5 |
| invites reconciliation in the closing | 0/5 | 5/5 |
