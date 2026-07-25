from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HORUS_", env_file=".env", extra="ignore")

    # Host port 5435: coexists with SENTINEL (5432), ARGUS (5433) and PHAROS (5434).
    database_url: str = "postgresql+psycopg://horus:horus@localhost:5435/horus"

    http_timeout_seconds: float = 30.0

    # --- Collection: adsb.lol v2 API (free, keyless, readsb aircraft schema) -----------
    # One point query returns every tracked aircraft within `adsb_radius_nm` of the
    # centre — including the GNSS integrity fields (nic / nac_p / sil) the flagship
    # detector needs. Default centre is Singapore; 250 nm is the API's radius cap and
    # covers the Malacca approaches, the Singapore Strait and the Riau archipelago.
    adsb_api_url: str = "https://api.adsb.lol/v2"
    adsb_center_lat: float = 1.35
    adsb_center_lon: float = 103.82
    adsb_radius_nm: int = 250
    adsb_poll_seconds: float = 30.0
    adsb_source_label: str = "adsb-lol"
    # Drop stale plots: a position older than this (readsb `seen_pos`) at sample time is
    # a coasted estimate, not a fresh report.
    adsb_max_seen_pos_seconds: float = 60.0

    # --- Track building ----------------------------------------------------------------
    # A new track (flight segment) starts when an aircraft is silent longer than this.
    track_gap_minutes: float = 15.0
    track_min_points: int = 10
    # Fixed-length resample size for the anomaly models' feature/sequence descriptors.
    track_resample_points: int = 64

    # --- GNSS-interference detector (the flagship) --------------------------------------
    # NIC (Navigation Integrity Category) is broadcast by the aircraft itself and collapses
    # when its own GNSS solution degrades — the well-established jamming proxy (GPSJam-style).
    # Healthy en-route traffic reports NIC >= 7; NIC <= gnss_bad_nic_max counts as degraded.
    gnss_good_nic_min: int = 7
    gnss_bad_nic_max: int = 5
    # Spatio-temporal aggregation: fraction of degraded aircraft per grid cell per window.
    gnss_cell_deg: float = 0.5
    gnss_window_minutes: float = 10.0
    # Cells observing fewer aircraft than this are unscoreable (small-sample honesty),
    # and an incident needs at least this fraction of them degraded.
    gnss_min_aircraft: int = 4
    gnss_bad_fraction_threshold: float = 0.5
    # Hard integrity loss: measured over real interference (Baltic, 2026-07-23), degradation
    # is near-binary rather than gradual — 18 of 20 degraded aircraft sat at exactly NIC 0,
    # and NACp collapsed with it (15 at NACp 0), while healthy traffic never reported NACp
    # below 8. So NIC 0 corroborated by NACp 0 is a far sharper signature than the ≤5 band,
    # and it is tracked as its own tier rather than folded into the same count.
    gnss_hard_loss_nic: int = 0
    gnss_hard_loss_nac_p: int = 0
    # Multi-resolution scoring: how many times the cell may be doubled when the fine grid
    # holds too few aircraft. A fixed size is always wrong somewhere — at 0.5° over
    # Singapore 83% of cells were unscoreable — so sky that fails the minimum falls through
    # to a coarser level, and the level used is recorded on the incident. 0 disables.
    gnss_coarsen_levels: int = 2

    # --- Dark-aircraft (transponder gap) detector ---------------------------------------
    # The coverage confound is *worse* than AIS: low-altitude traffic drops out of
    # terrestrial ADS-B reception routinely, so gaps only count while the aircraft was
    # last seen at/above the altitude floor, and must reappear displaced.
    gap_min_minutes: float = 10.0
    gap_min_displacement_km: float = 50.0
    gap_min_altitude_ft: float = 10_000.0
    # An aircraft DESCENDING when it goes silent has a benign explanation: it was landing,
    # and will reappear hours later on its next departure, displaced — which reads exactly
    # like going dark. Measured on 11.7 h of real Singapore traffic: 25 of 34 gap calls were
    # descending faster than this at the moment of silence, i.e. approach-and-turnaround, not
    # evasion. A genuine dark event is an aircraft in CRUISE that stops transmitting, so a
    # descent steeper than this disqualifies the call. (The maritime sibling learned the same
    # lesson as anchored vessels being called ship-to-ship transfers.)
    gap_max_descent_fpm: float = -300.0

    # --- Low-level watch-box incursion detector -----------------------------------------
    # A genuine low-level cross-border profile is LOW — not an airliner passing under the gap
    # detector's 10,000 ft floor on approach. Measured over real Singapore traffic (2026-07-25):
    # the Riau box overlaps Changi/Batam terminal airspace, so the borrowed 10k floor caught
    # the entire arrival stream — A380s, 777s, 787s on scheduled callsigns (ANA, BAW, Citilink).
    # A dedicated, much lower floor is the first fix.
    incursion_max_altitude_ft: float = 5_000.0
    # Split an aircraft's in-box samples into contiguous visits at this silence, so "dwell" is
    # a real continuous presence — not a span across a whole day of repeated transits (one
    # airliner otherwise showed a 34-hour "dwell").
    incursion_visit_gap_minutes: float = 15.0
    # An aircraft climbing/descending through the box is transitioning to/from an airport (an
    # approach or departure), not sustaining a low-level presence — the same insight as the
    # dark-aircraft descent exclusion. Require the visit to be roughly level. Missing vertical
    # rate fails OPEN (treated as level) so a genuine low-flyer isn't dropped on absent data.
    incursion_max_level_rate_fpm: float = 500.0

    # --- Spoof / kinematic-impossibility detector ---------------------------------------
    # Faster than anything in civil traffic; an implied speed above this between fixes
    # means the fixes can't both be real (teleporting identity / bad data / spoof).
    spoof_max_speed_kt: float = 1_400.0

    # --- Flagship anomaly model (served) ------------------------------------------------
    # Optional SHA-256 pin for the frozen GRU artifact `/score-track` loads. When set, a
    # mismatch is reported and scoring is refused so a served model cannot silently drift
    # from the benchmarked freeze (the maritime sibling's discipline). Empty = no pin.
    anomaly_artifact_sha256: str = ""
    # Bound the stateless inference route's input so a pasted track can't balloon a request.
    score_track_max_points: int = 4_000

    # --- API hardening for public deployment (safe local-dev defaults) ------------------
    api_allowed_origins: str = ""
    api_max_request_chars: int = 100_000
    api_rate_limit_requests: int = 30
    api_rate_limit_window_seconds: float = 60.0
    api_max_concurrent_inference: int = 2
    # Behind a proxy (Render terminates TLS) the client IP is in X-Forwarded-For, not the
    # socket peer. Trust it there so rate limiting is per real client; NEVER trust it when
    # directly exposed, since a client could then spoof its own rate-limit bucket.
    api_trust_forwarded_header: bool = False

    # --- Demo / snapshot mode (the deployed free container) -----------------------------
    # The public demo ships a BAKED synthetic seed, not a live feed. In demo mode the
    # time-relative views stop pretending to be live: /gnss-coverage ignores its rolling
    # window (a fixed snapshot has no "last N hours"), and the UI shows a snapshot banner.
    # This is the honest framing — a snapshot is labelled a snapshot, never dressed as live.
    demo_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
