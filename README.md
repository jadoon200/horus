# HORUS

**Air-domain awareness & GNSS-interference monitoring** — turns free ADS-B data into
source-rated, human-review **air incidents**: GNSS interference (jamming), dark aircraft
(transponder gaps), zone incursions, kinematic impossibilities, and trajectory anomalies —
fused into a composite air picture over the Singapore FIR neighbourhood.

> **The air lane of the portfolio.** Sibling to [SENTINEL](../sentinel) (cyber threat
> intelligence), [ARGUS](../argus) (all-source / information defence) and
> [PHAROS](../pharos) (maritime GEOINT). PHAROS watches the water; HORUS watches the sky
> above it — together a joint air + sea domain picture over the world's busiest strait.
> Named for the falcon-eyed sky god (a deliberate pair with PHAROS's Alexandrian lighthouse).

> **Status: core system built, gated, and exercised on real ADS-B.** Collection, track
> building, the five-detector battery (GNSS-interference flagship), the composite air
> picture, the honest eval harness, the read-only API + ARGUS evidence bridge, and the
> explainer-first React/Leaflet dashboard are all in place — 30 tests, mypy strict,
> browser-verified. A first live 12.5-minute Singapore window (54 aircraft, 796 reports)
> raised **zero false positives** and confirmed the NIC baseline on real traffic, while
> exposing the method's real bound: **83.3% of cells were too sparse to score**. Numbers,
> including what that window does *not* establish, are in [docs/EVAL.md](docs/EVAL.md);
> progress in [docs/ROADMAP.md](docs/ROADMAP.md).

## Why the air domain, why GNSS interference

GNSS jamming and spoofing have become one of the most visible gray-zone signatures in
open-source intelligence: aircraft broadcast their own navigation-integrity figures
(NIC / NACp / SIL) in every ADS-B message, and those figures collapse en masse when GNSS is
degraded over a region. That makes regional GNSS health *observable from entirely free,
public data* — the same discipline as the sibling projects: free data, honest evaluation,
human-review decision support, never automated verdicts.

## What it does (design)

Poll free ADS-B (adsb.lol, keyless, readsb schema) over the Singapore FIR → persist
positions incl. integrity fields → build per-aircraft flight segments → run a battery of
detectors, then fuse them into a composite per-aircraft/area air-threat picture:

1. **GNSS interference (the flagship)** — spatio-temporal clustering of NIC/NACp
   degradation across many aircraft in a grid cell/time window, with small-sample honesty
   (unscoreable cells stay unscored). Area-level incidents, GPSJam-style but
   incident-oriented.
2. **Dark aircraft / transponder gap** — silence at altitude with displaced reappearance;
   the coverage confound (low-altitude reception loss) is handled, not hidden.
3. **Zone incursion** — entry into coarse, curated watch boxes (never authoritative
   airspace geometry).
4. **Kinematic impossibility / spoof** — implied speeds no civil aircraft can fly.
5. **Trajectory anomaly** — a GRU sequence autoencoder over ordered track shape
   (the PHAROS flagship design, re-proven on air tracks against fair baselines).

## Data sources (all free)

[adsb.lol](https://api.adsb.lol/) (keyless community ADS-B API, readsb schema incl.
NIC/NACp/SIL) · a deterministic labelled synthetic generator (the offline gold set — a
ceiling, not a capability claim) · [OpenSky Network](https://opensky-network.org/)
(optional, later).

## Stack

Python 3.12 · SQLAlchemy 2.0 / Alembic · PostgreSQL (SQLite for tests — no PostGIS; spatial
math in pure numpy) · httpx + tenacity · scikit-learn · torch (GRU sequence autoencoder) ·
FastAPI · React + TypeScript + Leaflet (planned) · ruff + mypy (strict) + pytest gate.
Mirrors SENTINEL/ARGUS/PHAROS conventions so the four read as one body of work.

## Quickstart

```bash
make env && conda activate horus && make install   # one-time
make check                                          # ruff + mypy strict + pytest
make up                                             # Postgres (host port 5435) + migrations
```

## Deploy

A single free container (`Dockerfile.web` + `render.yaml`, the sibling pattern) builds the
dashboard, installs a slim API runtime, and bakes a **synthetic** demo seed — no real
aircraft identities ship in the image. It serves the read-only API + the SPA from one
service, no managed database. In demo mode the UI shows a "snapshot" banner and the coverage
map shows the whole baked picture (a fixed snapshot has no rolling window). Build and run:

```bash
docker build -f Dockerfile.web -t horus-web .
docker run -p 8000:8000 -e HORUS_DEMO_MODE=true horus-web
```

To publish: push to GitHub, then New → Blueprint on Render and point it at `render.yaml`.

## Responsible use

Public, unauthenticated ADS-B broadcasts only; aircraft-level (never individual persons);
defensive and analytical only. Incidents flag *patterns in public broadcast data* for human
review — they are decision support, never automated verdicts of hostile or illicit
activity. Zone rings are illustrative rectangles, not authoritative airspace boundaries.
