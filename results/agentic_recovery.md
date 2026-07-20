# Agentic recovery ablation

Identical correctable first drafts were revised once by the prompt
backend (one LLM call carrying the gate's correction) and once by the
bounded agentic backend (up to three LLM calls and six read-only tool
calls); the same extract, check, and decide pipeline judged both. The
agent never approves its own work and HUMAN_GATE remains the only
successful terminal state.

| case | failure | prompt arm | agentic arm | agent LLM calls | tool calls | forced final |
|---|---|---|---|---|---|---|
| swap_amount-S-A-01 | swap_amount | recovered | recovered | 2 | 3 | no |
| break_sum-S-A-01 | break_sum | recovered | recovered | 2 | 2 | no |
| swap_amount-S-A-02 | swap_amount | recovered | recovered | 3 | 4 | yes |
| break_sum-S-A-02 | break_sum | recovered | recovered | 2 | 3 | no |
| swap_amount-S-A-03 | swap_amount | recovered | recovered | 2 | 3 | no |
| break_sum-S-A-03 | break_sum | recovered | recovered | 2 | 2 | no |
| swap_amount-S-A-04 | swap_amount | recovered | recovered | 3 | 4 | yes |
| break_sum-S-A-04 | break_sum | recovered | recovered | 2 | 3 | no |
| swap_amount-S-A-05 | swap_amount | recovered | recovered | 2 | 3 | no |
| break_sum-S-A-05 | break_sum | recovered | recovered | 2 | 2 | no |
| credit_omitted-S-A-04 | credit_omitted | recovered | recovered | 2 | 3 | no |
| unapplied_omitted-S-A-02 | unapplied_omitted | recovered | recovered | 2 | 3 | no |
| full_amounts-S-A-03 | itemization_full_amounts | recovered | recovered | 2 | 2 | no |

Recovery rate: prompt 13/13 (100%), agentic 13/13 (100%).
Agentic cost: 28 agent LLM calls and 37 tool calls across 13 recoveries (2.1 calls and 2.8 tools per recovery on average).
Forced-final rate: 2/13 (15%). A forced final means the model used both tool rounds without returning an acceptable final draft, so the guaranteed third call was made to extract it. It does NOT by itself mean the evidence was missing (see the next line).
Coverage-forced rate: 0/13 (0%). This counts recoveries where the model failed to gather a REQUIRED tool result and the orchestrator executed it deterministically; it is the honest measure of how much of the agency is the model's own. Of the forced finals, 2 had gathered all required evidence themselves (coverage-forced zero) and simply used the full round budget before finalizing.
Cost framing: the prompt arm spends 1 LLM call per recovery; the agentic arm spent 2.1 on average plus tools. The fast path (a clean first draft) uses one call and zero tools in both modes.
