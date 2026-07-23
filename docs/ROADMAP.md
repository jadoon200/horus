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

## Open (post-M9)

- **Multi-day collection.** The 12.5-minute window bounds false positives only. Jamming
  recall, GRU quality on real tracks, and diurnal behaviour all need days, not minutes.
- **Decide the flagship on real data.** The synthetic set has linear PCA beating the GRU
  (AUC 1.00 vs 0.976) because perfect circles are a linearly-separable caricature. That
  comparison is only meaningful on real pattern-of-life tracks.
- **The ARGUS-side consumer.** HORUS serves `/geoint/evidence` in ARGUS's `EvidenceItem`
  shape; the consuming bridge (parallel to `argus/bridge/pharos.py`) is not built yet.
