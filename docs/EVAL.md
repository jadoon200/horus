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
| Cells observed | 359 | 65 |
| Cells unscoreable | 91 (25.3%) | 29 (44.6%) |
| **Incidents raised** | **15** | **0** |
| Highest cell degraded-fraction | 1.00 | 0.00 |

NIC distribution, Baltic: `{0: 340, 1: 1, 2: 2, 3: 26, 4: 4, 5: 19, 6: 44, 7: 86, 8: 1747,
9: 145, 10: 35, 11: 160}` — against Singapore: `{0: 1, 7: 2, 8: 569}`.

**What this establishes.** The detector separates the two skies cleanly under identical
settings, with the strongest Baltic cells reaching 1.00 (every observed aircraft in the
cell degraded). Singapore's higher unscoreable share (44.6% vs 25.3%) reflects its ~5x
thinner traffic in the window; the contrast in *incidents* survives it, because the cells
that do qualify are judged under the same rule on both sides.

*(Correction: this table's Singapore cell counts were first published as 167 observed /
44 unscoreable. Those figures were computed over the lane's whole database rather than the
overlap window — the detector run was not bounded by the same interval as the report
statistics. The evaluation script now bounds both to the identical window; the restated
figures are 65 / 29, and the incident counts were unaffected.)*

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

## Dark-aircraft false positives on 11.7 h of real traffic (2026-07-24)

The first 12.5-minute window raised zero gap calls, which said nothing — it was too short
for any aircraft to go silent. An overnight run finally exposed the detector's real
behaviour, and the raw number was bad: **85 dark-aircraft calls** over 11.7 hours of
Singapore traffic. Three separate causes, each diagnosed from the data and each fixed:

| Stage | Calls | What was removed |
|---|---:|---|
| Raw | 85 | — |
| Collector-outage ledger | 34 | **51** silences caused by *our own host sleeping* |
| Descent exclusion | 9 | **25** aircraft that were landing, not disappearing |
| Coverage-exit exclusion | **4** | **5** that had time to leave the 250 nm circle and return |

**Our own downtime was the single largest cause.** The host slept 8 times overnight
(209 min ledgered). During a sleep every aircraft stops reporting and reappears displaced —
the dark signature exactly — so without the ledger, 60% of all calls would have been
artefacts of the collector rather than observations of the sky.

**Descent.** 25 of the remaining 34 were descending when they went silent. An aircraft on
approach lands, sits, and departs hours later, reappearing far away: benign, and now
excluded on vertical rate. A genuine dark event is an aircraft in *cruise* that stops
transmitting.

**The collection boundary.** The collector watches a finite 250 nm circle. An aircraft that
departs, crosses the boundary and returns hours later also reappears displaced after a long
silence. A conservative geometric test — could it have reached the boundary and come back at
its own last known ground speed? — removes those. This is the air-domain analogue of the
maritime sibling's coverage model: never claim what coverage can explain.

The four survivors are the plausible ones, e.g. `781d9e`: silent 10.7 min at 36,000 ft in
**level** flight at 458 kt, reappearing 134 km displaced. One survivor (`8a0ab8`) broadcast
no ground speed, so the coverage test could not run on it — a limitation, recorded rather
than hidden.

**The exclusions cost no true positives:** the synthetic gold set still returns gap recall
1.0 and precision 1.0 against its injected dark aircraft, and the low-altitude coverage-
dropout trap still stays quiet.

This mirrors the maritime sibling's first real-data run almost exactly (2,999 → 51 calls
after three domain-correct fixes), and for the same underlying reason: a detector tuned on
generated data meets failure modes that only real operations contain.

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

### Frozen model record — SG-AIR-v1 (2026-07-25)

The selected 8-unit GRU was retrained on the subsequently accumulated Singapore corpus and
frozen as a local inference artifact:

| Field | Value |
|---|---:|
| Real Singapore tracks | 725 |
| Hidden units | 8 |
| Training seed | 7 |
| Frozen population threshold (p99 train error) | 2.877444 |
| Artifact SHA-256 | `c3e218d0ebbf4529f4c594112c775a5fce561b4cfc30acafaf49cf106581ed53` |

The artifact stays local with the real-data lane. The public demo instead trains and bakes
a separate model from the deterministic **synthetic** seed during the container build, so
no real per-aircraft data or real-trained artifact ships. `GET /model` reports the served
artifact's SHA and pin status; `POST /score-track` applies that exact frozen scaler, network
and threshold to supplied points. If `HORUS_ANOMALY_ARTIFACT_SHA256` is configured and does
not match, scoring is refused rather than silently using a drifted model.

This makes inference reproducible for a given frozen artifact; it does **not** turn the
unsupervised score into a label. The response says only that a trajectory is unusual
relative to the training population and retains the human-review caveat.

Reproduce the real-data freeze locally:

```bash
HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.train_anomaly --region sg-live
```

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

## Diurnal behaviour over the continuous Singapore lane

A single continuous span of **19.0 h** (Singapore is UTC+8, so this crosses one local midnight). This shows the *shape* of the day — it is one partial cycle, not a multi-day average, and no stable diurnal law is claimed from n=1.

**Coverage honesty:** 8 ledgered outages totalling ~209 min (host sleep). Hours with material outage are flagged `*`; a low incident count there is our own silence, not a quiet sky.

| UTC hour | Reports | Aircraft | Unscoreable | jam | gap | incur | spoof | Outage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 14:00 | 796 | 54 | 15/96 (16%) | 0 | 0 | 0 | 0 | 28m `*` |
| 15:00 | 757 | 44 | 33/74 (45%) | 0 | 1 | 1 | 0 | 42m `*` |
| 16:00 | 2,747 | 93 | 81/242 (33%) | 0 | 0 | 2 | 0 | 0m |
| 17:00 | 1,677 | 68 | 66/187 (35%) | 0 | 0 | 3 | 0 | 0m |
| 18:00 | 1,686 | 63 | 96/203 (47%) | 0 | 0 | 4 | 0 | 0m |
| 19:00 | 1,855 | 59 | 49/196 (25%) | 1 | 0 | 0 | 0 | 0m |
| 20:00 | 1,276 | 60 | 61/175 (35%) | 0 | 1 | 0 | 0 | 0m |
| 21:00 | 2,875 | 81 | 65/239 (27%) | 0 | 0 | 2 | 0 | 0m |
| 22:00 | 3,008 | 102 | 64/240 (27%) | 0 | 1 | 6 | 0 | 0m |
| 23:00 | 4,209 | 133 | 81/334 (24%) | 0 | 1 | 5 | 0 | 0m |
| 00:00 | 513 | 69 | 31/95 (33%) | 0 | 0 | 2 | 0 | 85m `*` |
| 01:00 | 1,346 | 121 | 40/186 (22%) | 0 | 0 | 2 | 0 | 54m `*` |
| 02:00 | 5,516 | 196 | 78/353 (22%) | 1 | 0 | 13 | 0 | 0m |
| 03:00 | 4,952 | 170 | 86/336 (26%) | 0 | 0 | 12 | 0 | 0m |
| 04:00 | 4,899 | 169 | 79/313 (25%) | 0 | 3 | 10 | 0 | 0m |
| 05:00 | 5,224 | 188 | 75/336 (22%) | 0 | 3 | 4 | 0 | 0m |
| 06:00 | 6,657 | 213 | 78/384 (20%) | 0 | 0 | 7 | 0 | 0m |
| 07:00 | 6,387 | 209 | 85/346 (25%) | 0 | 1 | 7 | 0 | 0m |
| 08:00 | 53 | 53 | 10/27 (37%) | 0 | 0 | 0 | 0 | 0m |

**The prediction the data can check:** thinner traffic → more unscoreable sky. Busiest hour 06:00Z (6,657 reports) was 20% unscoreable; quietest 08:00Z (53 reports) was 37%. The trough is blinder, as expected.

**Detector mix over the span:** jamming 2, gap 11, incursion 80, spoof 0. The **incursion** detector dominates, which — as with the dark-aircraft story — reads as an over-firing class to triage next, not a genuine surge: routine low-level traffic near the border watch box is the likely explanation, and it is the maritime sibling's loitering-false-positive lesson repeating in the air. Recorded here as the next investigation, not silently tuned away.
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
