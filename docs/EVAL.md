# HORUS evaluation

Every claim here is reported on the number that survives scrutiny; the
synthetic block below is explicitly a ceiling. The real-data section comes
first because it is the only part that constitutes evidence about capability.

## Controlled evaluation — known-interference sky vs clean sky (2026-07-23)

A quiet sky proves nothing on its own. Zero incidents over Singapore is equally consistent
with *"the detector works and there is no interference"* and *"the detector never fires"*,
and nothing in a single-region window distinguishes them. The answer is a control pair.

Two lanes were collected **simultaneously** through the same code path with identical
detector parameters, differing only in where the collection circle was centred: the Baltic
(250 nm around 54.9 N, 20.5 E — a region with widely reported, ongoing GNSS interference)
as the positive control, and the Singapore FIR as the negative control. Both lanes are
restricted below to the wall-clock window in which both were collecting, so traffic
density, time of day and detector configuration are held constant.

| Measure | Baltic (positive control) | Singapore (negative control) |
|---|---:|---:|
| Collection span (min) | 13.8 | 13.7 |
| Reports carrying NIC | 2,609 | 572 |
| Distinct aircraft | 159 | 39 |
| Reports degraded (NIC ≤ 5) | 392 (15.0%) | 1 (0.2%) |
| Reports in hard loss (NIC 0 **and** NACp 0) | 322 | 0 |
| Cells observed | 359 | 167 |
| Cells unscoreable | 91 (25.3%) | 44 (26.3%) |
| **Incidents raised** | **15** | **0** |
| Highest cell degraded-fraction | 1.00 | 0.00 |

NIC distribution, Baltic: `{0: 340, 1: 1, 2: 2, 3: 26, 4: 4, 5: 19, 6: 44, 7: 86, 8: 1747,
9: 145, 10: 35, 11: 160}` — against Singapore: `{0: 1, 7: 2, 8: 569}`.

**What this establishes.** The detector separates the two skies cleanly under identical
settings, with the strongest Baltic cells reaching 1.00 (every observed aircraft in the
cell degraded). The unscoreable fractions are within a point of each other, so the contrast
is not an artefact of one lane being better covered than the other.

**The negative control's single degraded report is the most informative number here.**
Singapore recorded exactly one NIC-0 report in the window and the detector raised **zero**
incidents from it. That is the lone-dip rule working on real data rather than on an
injected synthetic trap: one aircraft's integrity dropping is an avionics event, and only
a corroborated cluster is treated as an area signal.

**What this does not establish.** This is a *contrast, not precision or recall.* The Baltic
region has no per-cell ground-truth mask — nobody publishes which 0.5° cell was jammed in
which 10-minute window — so no true/false positive count is available and none is claimed.
It bounds the detector's behaviour at the two ends (fires on known interference, silent on
clean sky) and nothing between them.

Reproduce:

```bash
HORUS_DATABASE_URL=sqlite:///data/baltic-control.db python -m horus.ingest.collect \
    --cycles 26 --region baltic-control --lat 54.9 --lon 20.5
python -m scripts.eval_control \
    --positive data/baltic-control.db --negative data/sg-live.db --overlap
```

Interference is not static — a future run over the same box may find a clean sky, which is
a property of the world rather than a regression in the detector.

## Flagship anomaly model, decided on real tracks (2026-07-24)

The synthetic gold set could not settle this and said so: there, two perfectly circular
injected orbits are a linearly-separable caricature of "anomaly" and **linear PCA beat the
GRU** (AUC 1.00 vs 0.976). That negative is recorded below and was never explained away —
it was left open until real pattern-of-life data existed to decide it.

571 real Singapore tracks (523 aircraft) now exist, with 100 real Baltic tracks as an
unseen-region transfer set. Real tracks carry **no anomaly labels**, so there is no AUC to
compute here and none is claimed. Three things *are* measurable without labels, over 5 seeds:

| Model | Rank stability (Spearman) | Top-10 overlap | Separation (p90/median) | Transfer stability |
|---|---:|---:|---:|---:|
| GRU sequence autoencoder | 0.9818 | 0.89 | **4.43** | 0.9017 |
| Isolation Forest | 0.9721 | 0.62 | **1.21** | 0.9718 |
| linear PCA | 1.0000 | 1.00 | 3.99 | 1.0000 |

**Isolation Forest is eliminated.** A separation of 1.21 means its top-decile score is
barely distinguishable from its median — it ranks tracks but cannot discriminate between
them, and its top-10 set reshuffles across seeds (overlap 0.62). Neither its stability nor
its transfer number rescues a model that does not separate.

**The synthetic negative does not reproduce.** On real tracks the GRU separates outliers
further than PCA (4.43 vs 3.99) rather than losing to it. The two rank tracks similarly
(Spearman 0.884), so this is a modest edge, not a rout.

**PCA's perfect stability is determinism, not quality.** It has no random seed, so 1.0000
and an overlap of 1.00 are properties of the algorithm. Read against separation, PCA remains
a genuinely competitive and far cheaper baseline — it is retained as the reference, not
dismissed.

**Verdict:** the GRU is retained as the flagship on separation and on the interpretability
check below, with PCA as a close, cheap alternative and Isolation Forest dropped. This is a
narrower claim than "the GRU is best": stability, transfer and separation are *necessary*
conditions for trusting a ranking, not sufficient ones.

### Interpretability spot-check (illustrative, not evidence)

The GRU's second-ranked anomaly over Singapore was `9MMFA`, a **P28A** — a Piper PA-28
single-engine piston — in a corpus otherwise composed of airliners (A20N, B763, A333, B77W),
having covered 40.7 km where the others covered 200–580 km. A light general-aviation
aircraft pottering among long-haul jets genuinely is the anomalous pattern of life in that
sky, and the model surfaced it with no labels and no type information — it sees only
translation-invariant per-step shape.

This is **one anecdote and is treated as one.** It shows the ranking is not arbitrary; it
does not establish precision. A blinded multi-track review is the honest version of this
check and is not yet done.

Reproduce:

```bash
python -m scripts.benchmark_anomaly --train data/sg-live.db --transfer data/baltic-control.db
```

## Multi-resolution scoring — the unscoreable-cell problem

The first Singapore window's dominant limitation was that **83% of cells held too few
aircraft to score**. The detector was not mis-thresholded; it was blind over most of the
map. Cells are now aggregated finest-first, and sky failing the aircraft minimum falls
through to a coarser level, with the level that answered it recorded on the incident.

Measured on the real Singapore window (135 cells):

| Coarsening levels | Cells unscoreable | Incidents |
|---|---:|---:|
| 0 (fixed 0.5°) | 112 (83.0%) | 0 |
| 1 | 72 (53.3%) | 0 |
| **2 (default)** | **32 (23.7%)** | **0** |
| 3 | 18 (13.3%) | 0 |

Scoreable map coverage rises from 17% to 76% while incidents stay at zero over clean sky,
so the added coverage costs nothing in false positives. A coarse cell is a weaker spatial
claim than a fine one, which is why the resolution travels with the incident rather than
being implied.

## Real ADS-B — first live Singapore window (2026-07-23)

A 12.5-minute keyless `adsb.lol` collection over the Singapore FIR
(250 nm radius, 30 s polling), processed through the full pipeline.

| Quantity | Value |
|---|---:|
| Collection window | 14:19–14:32 UTC (12.5 min) |
| Positions persisted | 796 |
| Distinct aircraft | 54 |
| Tracks built | 37 |
| Grid cells observed | 96 |
| Cells **unscoreable** (too few aircraft, fixed-resolution) | 80 (83.3%) |
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
appear to hold. **The jamming detector, however, was never challenged in this
window**: with zero degraded aircraft there was nothing to detect, so on its own
this window is evidence about false positives only. That gap is what the
controlled evaluation above exists to close.

**The dominant real limitation: 83.3% of cells are unscoreable.** At this
traffic density most 0.5° cells simply do not contain the 4 aircraft the
detector requires, so they are skipped rather than scored. This is the
small-sample honesty rule doing exactly its job, and it bounds where the
method works: GNSS-interference detection is viable over busy airways and
terminal areas, and is *structurally* blind over empty sky. A longer window
raises aircraft-per-cell and shrinks this fraction; it does not remove the
constraint. **Superseded in part:** multi-resolution scoring (above) cuts this
share to 23.7% on the same data by answering sparse sky at a coarser cell; the
83.3% figure is the fixed-resolution behaviour retained here for comparison.

(These cell counts were restated after the time-bucket anchoring fix: buckets are now
anchored to a fixed epoch rather than the corpus minimum, which re-cuts window boundaries.
The earlier figures for this same window were 59/74 = 79.7%. Detection outcomes on the
synthetic gold set were unchanged by the fix.)

**What this window does not establish:** trajectory-anomaly quality on real
tracks (37 tracks over 12.5 minutes is too few and too short to train the GRU
honestly), or any behaviour across a diurnal cycle. Those need a multi-day
collection and are not claimed here.

Reproduce:

```bash
HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m horus.ingest.collect --cycles 24 --region sg-live
```

<!-- AUTO-EVAL:BEGIN -->
## Synthetic gold-set results (auto-generated — a CEILING, not a capability claim)

Injected events are separable by construction; the informative read is the
*confounder* rows (did benign traps stay quiet?) and the baseline gaps.

- **GNSS interference (flagship):** 1/1 labelled events detected; 4 incident cell-windows, 0 outside the labelled area; 173/498 cells unscoreable (too few aircraft — skipped honestly, never scored).
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
