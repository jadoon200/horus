.PHONY: env install lint typecheck test check up down migrate collect collector-install collector-stop process-live health prune tracks detect eval api ui

# One-time: create the conda env, then `conda activate horus`
env:
	conda create -y -n horus python=3.12

# Run inside the activated horus env. Torch powers the flagship GRU on every platform.
install:
	pip install -r requirements-dev.txt && pip install -e .

lint:
	ruff check . && ruff format --check .

typecheck:
	mypy

test:
	pytest -q

check: lint typecheck test

# Postgres via Docker Compose (host port 5435 — coexists with the siblings), then migrate.
up:
	docker compose up -d db && sleep 2 && alembic upgrade head

down:
	docker compose down

migrate:
	alembic upgrade head

# Poll the free adsb.lol feed over the configured centre/radius into the DB.
collect:
	python -m horus.ingest.collect

# Install / stop the continuous low-priority collector as a macOS launch agent.
collector-install:
	cp ops/com.horus.collector.plist ~/Library/LaunchAgents/
	launchctl load ~/Library/LaunchAgents/com.horus.collector.plist
	@echo "installed; check with: launchctl list | grep horus"

collector-stop:
	launchctl unload ~/Library/LaunchAgents/com.horus.collector.plist

# Incremental processing worker — keeps the air picture current without reprocessing
# the whole corpus each cycle. Run alongside the collector.
process-live:
	python -m scripts.process_live

# One-shot health glance at the live lane (read-only).
health:
	python -m scripts.collector_health

# Retention pass over the live database (delete, commit, then WAL-checkpoint).
prune:
	python -m scripts.prune_live

# Build per-aircraft flight segments (tracks) from positions.
tracks:
	python -m horus.tracks.build

# Run the detector ensemble → Incident rows.
detect:
	python -m horus.detect.run

# Score detectors/model on the synthetic gold set → docs/EVAL.md.
eval:
	python -m horus.eval.run

# Diurnal evaluation over the accumulated live lane (read-only) → docs/EVAL.md.
eval-multiday:
	python -m scripts.eval_multiday --write

# Read-only FastAPI + GeoJSON endpoints on :8000.
api:
	uvicorn horus.api.app:app --host 127.0.0.1 --port 8000

# React dashboard dev server (needs `make api` running).
ui:
	cd frontend && npm run dev
