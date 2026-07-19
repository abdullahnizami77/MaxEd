# Judge calibration

| dimension | kappa | 95% CI (bootstrap) | PABAK | raw agreement |
|---|---|---|---|---|
| acceptable (accept/reject) | 0.746 | [0.347, 1.000] | 0.750 | 0.875 |
| tone (1-4, weighted) | 0.091 | [-0.306, 0.636] | n/a | 0.688 |
| clarity (1-4, weighted) | 0.267 | [-0.065, 0.649] | n/a | 0.562 |

By draft class: clean subset kappa 0.600 (n=8, raw 0.875); corrupted subset kappa 0.000 (n=8, raw 0.875). The corrupted-subset kappa is 0 by the constant-rater degeneracy (every corrupted draft is labelled not-acceptable), which is exactly why raw agreement and PABAK are reported alongside kappa.

What this measures: the acceptable dimension is the holistic accept-or-reject call (tone and completeness together), NOT grounding, which is checked by code. Kappa on n=16 carries a wide CI (its upper bound touches 1.0 as a small-sample bootstrap boundary artifact) and is a calibration signal, not a certification.
