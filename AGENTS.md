# HORUS

Air-domain awareness & GNSS-interference monitoring: free keyless ADS-B (adsb.lol, readsb
schema incl. NIC/NACp/SIL integrity fields) → per-aircraft tracks → a detector ensemble
(GNSS-interference flagship + transponder gap + zone incursion + kinematic spoof + GRU
trajectory anomaly) → composite air picture. **The air lane of the portfolio** — sibling to
`../pharos` (maritime), `../argus` (all-source), `../sentinel` (cyber); oriented to
Singapore's **DIS** mission. Deadline context: DIS Work-Learn application ~10 Aug 2026.
Reuses PHAROS's architecture deliberately. Roadmap: `docs/ROADMAP.md`.

**Scope decision (2026-07-23):** no demo videos / blog posts — the dashboard carries the
"how it works" story (a first-class explainer view).

## Test / gate environment

The dedicated `horus` conda environment is installed at
`/Users/jayden/anaconda3/envs/horus` (Python 3.12). Activate it and run the self-contained
gate:

```
conda activate horus
make check
```

The current gate is ruff lint/format, strict mypy, and **99 tests**. Frontend changes also
run `npm run build && npm run lint` in `frontend/`; CI independently builds and boots the
exact deployment image.

## Commands

- `make check` — ruff lint + format check, mypy (strict), pytest. Run before every commit.
- `make up` / `make down` — Postgres via Docker Compose (host port **5435** — coexists with
  SENTINEL 5432 / ARGUS 5433 / PHAROS 5434), then Alembic migration.
- `make collect` — poll adsb.lol. `make tracks` / `make detect` / `make eval` /
  `make api` / `make ui` — build tracks, run detectors/eval, and serve the product.
- `HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.process_live --once` —
  one incremental live-processing cycle.
- `python -m scripts.correlate_air_sea` — joint HORUS × PHAROS review table.
- `alembic revision -m "..."` / `alembic upgrade head` — migrations in `migrations/versions/`.

## Layout (all built)

- ✅ `src/horus/config.py` — pydantic-settings; all config via `HORUS_*` env vars / `.env`.
- ✅ `src/horus/logging.py`, `src/horus/timeutil.py`, `src/horus/geo.py` — adapted from
  PHAROS (structlog console logger; UTC helpers; pure-numpy haversine / implied speed /
  point-in-polygon — **no PostGIS/shapely/pandas**).
- ✅ `src/horus/zones.py` — curated airspace zone registry (terminal/corridor/watch/border),
  coarse illustrative rings, NEVER authoritative airspace geometry; `zone_for`/`zones_containing`.
- ✅ `src/horus/db/` — models: Aircraft (icao24 PK), Position (incl. `nic`/`nac_p`/`sil` —
  the flagship signal), Track, Zone, Incident (icao24 **nullable**: jamming incidents are
  area-level), CollectorRun, CoverageOutage. `JsonType` = JSON w/ JSONB variant (SQLite
  tests). Migrations `0001_initial` + `0002_collection_boundaries`, with parity tests.
- ✅ `src/horus/ingest/` — keyless adsb.lol collection, defensive parsing, idempotent
  source/region-scoped persistence, synthetic gold, and opt-in OpenSky research adapter.
- ✅ `src/horus/tracks/` — segmentation (gap-split), resampling, kinematics, shape
  features + `[dx, dy, step_len, turn]`-style sequence descriptor (+ altitude channel).
- ✅ `src/horus/detect/` — `jamming.py` (flagship: NIC/NACp cell/window clustering,
  unscoreable-cell honesty), `gaps.py` (altitude floor + displacement; coverage confound),
  `incursion.py`, `spoof.py` (implied-speed impossibility), `seq_anomaly.py` (GRU AE vs
  IF/PCA baselines), `squawk.py`, `ensemble.py`, and incremental/full runners.
- ✅ `src/horus/eval/` + `scripts/eval_*.py` — synthetic ceiling, live/control,
  reliability, spoof-tail, OpenSky, and multi-cycle evaluation with recorded negatives.
- ✅ `src/horus/api/` — read-only hardened FastAPI + GeoJSON + `/geoint/evidence`-shaped
  export (ARGUS EvidenceItem fields) so the ARGUS bridge pattern consumes the air lane.
- ✅ `frontend/` — React + TS + Leaflet: live Air Picture, incident evidence drawer,
  model scoring interaction, and first-class **How it works** view.
- ✅ `src/horus/fusion.py` + `scripts/correlate_air_sea.py` — read-only joint Strait
  triage over HORUS/PHAROS evidence; co-location never becomes causation.

## Workflow

- Git identity: commits, PR bodies, and merge messages show **only @jaydenOoOo** — never add
  `Co-Authored-By` trailers, "Generated with Codex" footers, or any AI attribution.
- **Never commit `AGENTS.md` or `.Codex/`** — local-only (covered by the global git ignore).
- **Planning docs are local-only too.** Roadmap-style working notes (e.g. `.Codex/PLAN.md`)
  never get committed and never get their own branch — a branch exists for the *work*, named
  for the work (`feat/<topic>`), and the repo/history must carry no trace of AI planning
  artifacts. Public docs (`docs/ROADMAP.md`, `docs/EVAL.md`) are the only roadmap the repo
  shows, written as the project's own voice.
- **The living plan is `.Codex/PLAN.md`** — consult it at session start, tick items as they
  land, and keep it in sync with reality (it is the internal superset of `docs/ROADMAP.md`).
- `main` is deployable; work on `feat/<topic>` branches once the repo is on GitHub; frugal
  ubuntu-only CI when added (private-repo Actions minutes are finite).
- On every milestone completion, refresh README/ROADMAP (+ EVAL when it exists) and flip
  the ✅/⬜ markers. Delegate big doc passes to a small-model (Haiku) subagent.

## Conventions

- mypy strict everywhere; scoped ignore_missing_imports only for sklearn/torch — don't widen.
- Every ingester: httpx + tenacity retry, parse into ORM objects, idempotent upsert, unit
  test with a mocked payload (respx).
- **Zero-cost rule:** free data only; adsb.lol needs no key. No paid dependency ever required.
- **Honest-evaluation discipline:** synthetic gold numbers are a ceiling by construction;
  the number that counts comes from real collected data; negatives get recorded in EVAL.
- **Responsible use:** public broadcast data only; aircraft-level; human-review decision
  support, never automated verdicts; zone rings are illustrative, not authoritative.
- Large artifacts (data/, *.pt, *.npz) never go in git.
