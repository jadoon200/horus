# HORUS evaluation

Every claim here is reported on the number that survives scrutiny; the
synthetic block below is explicitly a ceiling. Real-data results from the
live Singapore collection lane land in this file as they are measured.

<!-- AUTO-EVAL:BEGIN -->
## Synthetic gold-set results (auto-generated — a CEILING, not a capability claim)

Injected events are separable by construction; the informative read is the
*confounder* rows (did benign traps stay quiet?) and the baseline gaps.

- **GNSS interference (flagship):** 1/1 labelled events detected; 3 incident cell-windows, 0 outside the labelled area; 466/498 cells unscoreable (too few aircraft — skipped honestly, never scored).
- **gap:** recall 1.0, precision 1.0 (expected ['d00001', 'd00002'], detected ['d00001', 'd00002'], FP [])
- **incursion:** recall 1.0, precision 1.0 (expected ['e00001'], detected ['e00001'], FP [])
- **spoof:** recall 1.0, precision 1.0 (expected ['f00bad'], detected ['f00bad'], FP [])
- **Trajectory anomaly (44 tracks, 2 injected):** GRU AUC 0.9762 vs Isolation Forest 0.9762 vs linear PCA 1.0 (same unsupervised setup).
- **Confounders:** low-altitude dropouts flagged as gaps: none; lone NIC dip in a jamming incident: none.
<!-- AUTO-EVAL:END -->

## Recorded negatives & reading guide

- **The flagship does NOT win on the synthetic set — recorded, not hidden.** On 44
  gold tracks the linear PCA baseline scores AUC 1.0 while the GRU scores 0.976: two
  perfectly-circular injected orbits are a *linear-separable caricature* of anomaly, so
  the cheapest method wins. This mirrors the maritime lesson exactly (PHAROS's PCA
  looked fine on synthetic and collapsed to 0.27 on real data): the synthetic set can
  only prove the plumbing works, never that the model is better. The GRU-vs-baselines
  question is decided on real collected tracks, not here.
- **`tests/test_seq_anomaly.py` pins the opposite PCA failure mode** (a dominant
  outlier drags the principal components toward itself and gets reconstructed *well*).
  Both behaviours are real; which one you see depends on the population — one more
  reason a linear baseline is not decision-grade.
- **Unscoreable-cell accounting is a feature.** 466/498 GNSS cells were skipped for
  having fewer than the minimum aircraft. A jamming map that scores empty cells is
  noise; the honest map says "no data" where there is no data.
- **What would change these numbers:** real ADS-B has heterogeneous NIC baselines by
  airframe/equipage, coverage seams, and MLAT-derived positions — false-positive
  pressure the gold set cannot simulate. The live Singapore collection window is the
  actual test.
