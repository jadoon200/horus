# HORUS roadmap

Honest status ledger — ✅ built & gated, 🔨 in progress, ⬜ planned.

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Scaffold: config, DB schema + migration parity gate, geo primitives, airspace zone registry | ✅ |
| M1 | Collection: adsb.lol poller (keyless, integrity fields) + idempotent persist + collector/outage ledger | ✅ |
| M2 | Synthetic generator: deterministic labelled gold set (jamming cells, gaps, incursions, spoofs, anomalies + benign confounders) | ✅ |
| M3 | Track building: per-aircraft segmentation, resampling, kinematics, shape features + sequence descriptor | ✅ |
| M4 | Detector ensemble: GNSS-interference flagship + gap + incursion + spoof; composite rollup + Admiralty-style reliability grading | ✅ |
| M5 | Trajectory anomaly: GRU sequence autoencoder vs Isolation Forest / PCA baselines (fair, unsupervised setup) | ✅ (synthetic: PCA wins — recorded negative; real-data decision pending) |
| M6 | Honest eval harness → docs/EVAL.md (synthetic = ceiling; live-data validation is the number that counts; recorded negatives) | ✅ |
| M7 | Read-only hardened API + `/geoint/evidence`-shaped export for the ARGUS bridge | ✅ |
| M8 | React + Leaflet dashboard: Air Picture map, Incidents feed, and a first-class "How it works" explainer view | ✅ browser-verified |
| M9 | Live Singapore collection window + real-data validation write-up | ✅ first 12.5-min window measured (0 FPs, NIC baseline confirmed, 79.7% cells unscoreable); multi-day run still open |

## Design decisions already locked

- Free data only; keyless adsb.lol is the primary lane (zero-cost rule).
- NIC/NACp degradation is the jamming proxy; cells with too few aircraft are *unscoreable*,
  never silently scored (small-sample honesty).
- The coverage confound (low-altitude reception loss) is handled explicitly in the gap
  detector: altitude floor + displacement requirement + reliability grade.
- Zone rings are coarse and illustrative — never authoritative airspace geometry.
- Incidents are human-review decision support, never automated verdicts.
- Mirrors PHAROS architecture/conventions deliberately (tracks → detectors → composite →
  map), so the pair reads as one joint air + sea domain-awareness system.

## v2 — in progress

| # | Milestone | Status |
|---|-----------|--------|
| V1 | Collector hardened for continuous operation: downtime bridged into the coverage ledger (between runs *and* mid-run host sleep), retention with a pilot-start floor, health/prune tooling, low-priority launch agent | ✅ installed & running |
| V2 | Region-parameterised collection, so a second lane over a different part of the world runs through the same code path | ✅ |
| V3 | Two-channel hard-loss tier (NIC 0 corroborated by NACp 0) — measured over real interference, degradation is near-binary rather than gradual | ✅ |
| V4 | Multi-resolution scoring — sky too sparse for a fine cell falls through to a coarser one; unscoreable 83.0% → 23.7% with no new false positives | ✅ |
| V5 | Positive/negative control evaluation — the same detector over a known-interference sky and a clean one, identical parameters | ✅ 15 incidents vs 0, simultaneous windows |
| V6 | Multi-day Singapore window: diurnal unscoreable curve, per-detector false-positive rate | ✅ 18 h span: unscoreable tracks the traffic trough (20% peak → 35% quiet); surfaced incursion over-firing as the next FP triage |
| V7 | Decide the flagship anomaly model on real tracks | ✅ Isolation Forest eliminated (separation 1.21); GRU retained over PCA on separation 4.43 vs 3.99; the synthetic negative does not reproduce |
| V8 | Coverage/plausibility model for the transponder-gap detector | ✅ real-data FP triage: 85 → 4 calls (outage ledger, descent, boundary-exit), true positives unchanged |
| V9 | Incremental live processing, so the dashboard is current rather than batch-stale | ✅ warm cycle bounded by the refresh window, not the corpus (6 h → 0.67 s); parity with full rebuild tested |
| V10 | Live dashboard: interference heatmap with *unscoreable* as a first-class state, collector freshness in the masthead | ✅ browser-verified against the live lane |
| V11 | Single-container deploy (Dockerfile.web + render.yaml, slim API runtime, baked synthetic seed; demo/snapshot mode; keep-alive) | ✅ **live** on Render, browser-verified |
| V12 | Incursion false-positive triage (surfaced by the multi-day eval): dedicated low floor, contiguous visits, level-flight requirement | ✅ 153 → 17 real calls, gold-set unchanged; airway-overlap residual recorded |
| V13 | Freeze + serve the selected GRU: versioned real-data artifact record, SHA pin enforcement, `/model` provenance, stateless `/score-track`, explainer interaction | ✅ 725-track SG-AIR-v1 freeze; deploy image smoke-tested |
| V14 | Incident evidence drawer: cell → corroborating aircraft → aligned NIC/NACp history, plus aircraft-rollup drill-down | ✅ API-alignment test + frontend build/lint; browser verification pending final deploy pass |
| V15 | Emergency-squawk notable-event channel: visit-grouped 7500/7600/7700, repeated-sample floor, incremental parity | ✅ synthetic 7700 detected; 1200/2000 quiet; 0 events over 120,342 real SG reports |
| V16 | Deployment-image CI: build the exact Dockerfile, boot the baked demo, assert health/data/model/inference | ✅ independent smoke lane |

**Live:** https://horus-kc7w.onrender.com (Render free tier, baked synthetic snapshot; keep-alive pings `/health`).

The ARGUS-side consumer is **done** — ARGUS reads this project's `/geoint/evidence` and
cites air incidents alongside cyber and maritime ones.
