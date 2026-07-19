# Decision journal

Contemporaneous notes, appended as the work happens. Newest at the bottom.

## Day 1, morning: plan and foundation

- Assumed the brief's "one or two synthetic clients with a 20-40 transaction
  ledger" is best served by 12 small scenario snapshots (two clients, six
  deliberate structures each, pools A and B) rather than one big ledger: the
  learning loop needs a held-out pool, and the error taxonomy needs each
  structure isolated. Stated as a re-slice in the README.
- Endpoint probe before any code: the served model leaks its thinking
  process into content by default. chat_template_kwargs enable_thinking
  false fixes it, and vLLM honors response_format json_schema, so structured
  judge and verifier calls are schema-constrained at the decoder. That
  single probe retired the plan's biggest small-model risk (malformed JSON)
  before a line of the judge existed.
- Dead end avoided narrowly: the first sketch of whole_percent tried to be
  clever with a single expression and was wrong at the half boundary;
  rewrote it as plain divmod plus an explicit half-even branch. Lesson:
  money code wants boring shapes.
- Wrote the contracts, money, derive, event spine, trace hook, and model
  client by hand before delegating anything: the parallel builders code
  against frozen interfaces, which is what keeps seven concurrent authors
  from drifting.
- The net-balance semantics decision (open invoices minus unapplied cash
  minus unapplied credits) is the load-bearing domain call; it makes the
  Tier 2 naive misreading ("sum the unpaid invoices") a computable FAIL
  rather than a judgment call.

## Day 1, afternoon: parallel build

- Fanned out seven builders (foundry, drafting, checks and gate, memory,
  deterministic bench, judge bench, report) with disjoint file ownership and
  per-builder hermetic tests; integration and the runner stayed with the
  integrator. AI tooling note: the fan-out only became safe after the
  interfaces were frozen by hand; an earlier attempt to let builders define
  their own contracts is exactly how interface drift happens.
- Wrote the runner, CLI, golden-draft renderer, and Makefile while builders
  ran. The golden draft (correct-by-construction from ledger truth) turned
  out to be the keystone piece: it is at once the stub response, the Tier 1
  corruption substrate, and the checks-versus-oracle cross-battery input.

## Day 2, morning: adversarial review and hardening

- Six execution-based reviewers attacked the built system. The statistics,
  arithmetic, and decision table held; the extraction seam did not: 18 of
  23 crafted wrong drafts reached the human gate. The lesson that stings
  usefully: the layer everyone writes last (parsing prose) is the layer
  everything else trusts first.
- Fixed by making unattributed amounts context-strict, broadening document
  reference detection, adding paid-paraphrases, and catching marker-free
  money shapes. Escape rate re-measured at zero with no true sentence
  wrongly condemned; the whole corpus is now a regression test.
- Dead end worth recording: a background fix workflow died with the session
  overnight and left nothing recoverable; redoing the fixes inline took an
  hour. Lesson: durable state belongs in files and commits, not in process
  memory, which is also the thesis of this system's event log.

## Day 2, afternoon: the live runs

- The live error run exposed my own harness bug: a credit-memo misreading
  instruction was pointed at a scenario with no credit memos, proving
  nothing. Each naive instruction now targets the structure it attacks.
- The subtler live finding: the drafter transcribes correct totals from the
  ledger block even under naive instructions, so wrongness shows up as
  framing (full amounts presented as owed, credits silently omitted), not
  as bad numbers. Added two aggregate gate rows: itemization consistency
  and required-content completeness. Claim checks see claims; only the gate
  can see what is missing.
- My own verifier rejected my own Pool A edits twice: a decomposition
  sentence (originally X, paid Y, leaving Z) tripped the open-amount rule,
  and the word "confirm" tripped the fuzzy lexicon. Fixed the first in the
  checker (the phrasing is exactly what a good partial-payment reply should
  say), reworded the second. A system that makes its own author revise is
  working.
- The learning loop closed visibly: pass2 drafts on the held-out pool
  greeted the client by name, itemized with dates, and explained unapplied
  cash, none of which pass1 did, all while staying fully verified. Verified
  claim density rose from 36 to 62 across the same six scenarios.
- Judge calibration matched the pre-registered shape (high agreement on
  accept-or-reject, noise on tone), and both accept-call disagreements were
  judge errors, which is the argument for numbers-by-code stated as data.
- AI tools note: parallel builder agents against frozen contracts produced
  zero interface drift across seven modules; the same approach without
  frozen contracts failed in an earlier project. The freeze is the feature.
