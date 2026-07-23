# HORUS evaluation

Every claim here is reported on the number that survives scrutiny; the
synthetic block below is explicitly a ceiling. The real-data section comes
first because it is the only part that constitutes evidence about capability.

## Real ADS-B — first live Singapore window (2026-07-23)

A 12.5-minute keyless `adsb.lol` collection over the Singapore FIR
(250 nm radius, 30 s polling), processed through the full pipeline.

| Quantity | Value |
|---|---:|
| Collection window | 14:19–14:32 UTC (12.5 min) |
| Positions persisted | 796 |
| Distinct aircraft | 54 |
| Tracks built | 37 |
| Grid cells observed | 74 |
| Cells **unscoreable** (too few aircraft) | 59 (79.7%) |
| Incidents raised (all four deterministic detectors) | **0** |

**The NIC baseline is confirmed on real traffic.** Every one of the 796 real
reports carried a GNSS integrity figure, distributed NIC 8 (733) and NIC 7
(63) — zero degraded. That is the expected picture for a normal day with no
regional interference, and it independently validates the configured
thresholds: healthy traffic really does sit at NIC 7–8, so the
`gnss_bad_nic_max = 5` cutoff is not arbitrary.

**Zero false positives on real traffic — but read it narrowly.** No dark
aircraft, spoof, or incursion was raised from genuine ADS-B, which is the
non-trivial half: real feeds carry receiver dropouts, coasted plots and
identity churn that naive thresholds turn into incidents (the sibling PHAROS
project's first real maritime run produced 2,999 false positives before three
domain fixes cut them ~98%). HORUS's parse-time and altitude-floor guards
appear to hold. **The jamming detector, however, was never challenged**: with
zero degraded aircraft there was nothing to detect, so this window is evidence
about false positives only and says nothing about jamming recall.

**The dominant real limitation: 79.7% of cells are unscoreable.** At this
traffic density most 0.5° cells simply do not contain the 4 aircraft the
detector requires, so they are skipped rather than scored. This is the
small-sample honesty rule doing exactly its job, and it bounds where the
method works: GNSS-interference detection is viable over busy airways and
terminal areas, and is *structurally* blind over empty sky. A longer window
raises aircraft-per-cell and shrinks this fraction; it does not remove the
constraint.

**What this window does not establish:** jamming recall (no event occurred),
trajectory-anomaly quality on real tracks (37 tracks over 12.5 minutes is too
few and too short to train the GRU honestly), or any behaviour across a
diurnal cycle. Those need a multi-day collection and are not claimed here.

Reproduce:

```bash
HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m horus.ingest.collect --cycles 24 --region sg-live
```

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
