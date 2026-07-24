/**
 * The explainer view — a first-class part of the product (this portfolio ships no demo
 * videos or blog posts; the dashboard itself carries the "how it works" story).
 */
export default function HowItWorks() {
  return (
    <div className="how">
      <h2>What HORUS is</h2>
      <p>
        HORUS turns <b>free, public ADS-B broadcasts</b> into source-rated, human-review{' '}
        <b>air incidents</b> over the Singapore FIR neighbourhood — the air lane of a
        four-project intelligence portfolio (SENTINEL: cyber · ARGUS: all-source analysis ·
        PHAROS: maritime · HORUS: air). PHAROS watches the water; HORUS watches the sky above
        it — a joint air + sea picture over the world's busiest strait.
      </p>

      <h2>The pipeline</h2>
      <div className="pipeline">
        <div className="pipe-step">
          <b>1 · Collect</b>
          <span>
            Poll the keyless <code>adsb.lol</code> API (community ADS-B, readsb schema) every
            ~30 s over a 250 nm circle centred on Singapore. Stale coasted plots and TIS-B
            synthetic identities are dropped at parse time.
          </span>
        </div>
        <div className="pipe-arrow">→</div>
        <div className="pipe-step">
          <b>2 · Persist</b>
          <span>
            Idempotent, timezone-safe dedup on (aircraft, second). A collector run/outage
            ledger records <i>our own</i> downtime so it can never masquerade as a target
            going dark.
          </span>
        </div>
        <div className="pipe-arrow">→</div>
        <div className="pipe-step">
          <b>3 · Tracks</b>
          <span>
            Per-aircraft flight segments split at 15 min of silence, resampled to a fixed
            length, described as translation-invariant per-step deltas (dx, dy, step, turn,
            dalt) — models learn <i>shape</i>, not location.
          </span>
        </div>
        <div className="pipe-arrow">→</div>
        <div className="pipe-step">
          <b>4 · Detect</b>
          <span>Five detectors, each owning one threat signature (below).</span>
        </div>
        <div className="pipe-arrow">→</div>
        <div className="pipe-step">
          <b>5 · Fuse</b>
          <span>
            Composite air picture: per-aircraft rollups (which detectors agree, transparent
            risk arithmetic) + area-level GNSS incidents, served read-only.
          </span>
        </div>
      </div>

      <h2>The five detectors</h2>
      <div className="det-card flagship">
        <h4>GNSS interference — the flagship</h4>
        <p>
          Every ADS-B message carries the aircraft's own navigation-integrity figures:{' '}
          <code>NIC</code> (Navigation Integrity Category), <code>NACp</code> and{' '}
          <code>SIL</code>. When GNSS is jammed, these collapse — and they collapse{' '}
          <b>across many aircraft in the same place at the same time</b>, which no single
          faulty unit can mimic. HORUS grids the sky into 0.5° cells × 10-minute windows,
          takes each aircraft's <i>worst</i> NIC per cell-window, and opens an{' '}
          <b>area-level incident</b> when ≥50% of observed aircraft degrade to NIC ≤ 5
          (healthy cruise is 7–8+).
        </p>
        <p className="caveat">
          Small-sample honesty: a cell observing fewer than 4 aircraft is <b>unscoreable</b>{' '}
          and is skipped — counted and reported, never scored. A jamming map that colours
          empty cells is noise.
        </p>
        <p>
          <b>Multi-resolution.</b> One fixed cell size is always wrong somewhere: at 0.5°
          over Singapore, 83% of cells held too few aircraft to score — the detector was
          working but blind over most of the map. Sky that fails the minimum now falls
          through to a coarser cell, and the resolution that answered it is recorded on the
          incident, because a coarse cell is a weaker spatial claim than a fine one. That
          took the unscoreable share from <b>83.0% to 23.7%</b> without raising a single
          new incident over clean sky.
        </p>
        <p>
          <b>Two channels, not one.</b> Aircraft broadcast NACp as well as NIC. Measured over
          real interference, degradation is near-binary rather than gradual — 18 of 20
          degraded aircraft sat at exactly NIC 0, and NACp collapsed with them, while healthy
          traffic never reported NACp below 8. So NIC 0 <i>corroborated by</i> NACp 0 is
          tracked as its own "hard loss" tier; an aircraft that never broadcast NACp is
          judged on NIC alone rather than assumed degraded.
        </p>
      </div>
      <div className="det-card">
        <h4>Dark aircraft (transponder gap)</h4>
        <p>
          Silence ≥10 min from an aircraft last seen <b>at altitude</b> (≥10,000 ft — well
          inside receiver coverage), reappearing ≥50 km displaced, outside any recorded
          collector outage. Low-altitude dropouts are routine terrain-limited reception, so
          they never fire this detector — the coverage confound is handled, not hidden.
        </p>
        <p className="caveat">A gap may still be benign coverage loss; it is graded, not judged.</p>
      </div>
      <div className="det-card">
        <h4>Low-level watch-box incursion</h4>
        <p>
          Sustained low-altitude presence (≥3 samples below 10,000 ft) inside a curated
          border watch box. High-altitude overflight of the same box is ordinary airline
          traffic and is ignored.
        </p>
        <p className="caveat">
          Watch rings are coarse, illustrative rectangles — never authoritative airspace
          geometry, so an incident is framing for review, not a violation claim.
        </p>
      </div>
      <div className="det-card">
        <h4>Kinematic impossibility (spoof)</h4>
        <p>
          Implied speed between consecutive fixes above 1,400 kt, at least three times.
          Two positions that fast apart cannot both be genuine — the signature of two
          transmitters sharing one ICAO address, injected data, or feed corruption.
        </p>
      </div>
      <div className="det-card">
        <h4>Trajectory anomaly (GRU sequence autoencoder)</h4>
        <p>
          A compact recurrent autoencoder learns the population's pattern of life from{' '}
          <b>unlabeled</b> track sequences and scores each track by reconstruction error.
          Discipline: train/val split, train-only normalization, early stopping, and a
          persisted artifact so batch detection and inference always use identical weights.
          It is benchmarked against an Isolation Forest and a linear-PCA baseline under the
          same unsupervised setup.
        </p>
      </div>

      <h2>Ratings: score vs Admiralty grade</h2>
      <p>
        Every incident carries two separate numbers. The <b>score</b> (0–1) is the
        detector's confidence in the <i>pattern</i>. The <b>Admiralty grade</b> (A–F) rates
        the <i>evidence quality</i> — NATO's source-reliability convention applied to ADS-B:
        many independently-observed aircraft corroborating a cell grades B; a lone
        aircraft's own silence caps at C/D; low-altitude evidence drops a grade because
        reception there is weak. A strong pattern on weak evidence must look different from
        a strong pattern on strong evidence.
      </p>

      <h2>The composite risk sum</h2>
      <p>
        A rollup's risk = best incident score, +0.15 per additional agreeing detector,
        +0.10 if it touches a sensitive watch zone, capped at 1. Deliberately simple —
        click any rollup and the breakdown reconstructs the headline number exactly. A
        ranking a reviewer can't re-derive is a ranking they can't challenge.
      </p>

      <h2>Honest evaluation</h2>
      <div className="honesty">
        <p>
          <b>The synthetic gold set is a ceiling, not a capability claim.</b> Injected
          events are separable by construction, so recall/precision of 1.0 there proves the
          plumbing, nothing more. The informative synthetic results are the{' '}
          <i>confounder traps</i>: low-altitude coverage dropouts must not become "dark
          aircraft" (they don't), and a lone benign NIC dip must not become "jamming" (it
          doesn't).
        </p>
        <p>
          <b>The control pair.</b> A quiet sky proves nothing on its own: zero incidents over
          Singapore is equally consistent with "the detector works and there is no
          interference" and "the detector never fires". So the same detector, with identical
          parameters, is run over a region with ongoing GNSS interference and over a clean
          one. The Baltic lane raises incidents with cells reaching 100% of observed aircraft
          degraded; the Singapore lane raises none, and its NIC histogram contains only
          healthy values. That is a <i>contrast</i>, not a precision figure — the
          interference region has no per-cell ground-truth mask — and it is reported as such.
        </p>
        <p>
          <b>Recorded negative:</b> on the synthetic set the linear PCA baseline actually{' '}
          <i>beats</i> the flagship GRU (AUC 1.00 vs 0.98) — perfect circles are a
          linearly-separable caricature of anomaly. The maritime sibling PHAROS saw the
          mirror image (PCA looked fine on synthetic, collapsed to 0.27 AUC on real data).
          The GRU-vs-baselines question is decided on real collected tracks; the number
          lives in <code>docs/EVAL.md</code> and stays there even when it's unflattering.
        </p>
      </div>

      <h2>How the lanes join</h2>
      <p>
        <code>GET /geoint/evidence</code> serves every incident in the exact{' '}
        <code>EvidenceItem</code> shape the sibling ARGUS analyst consumes (doc_id · title ·
        source · Admiralty reliability · credibility · summary · resolvable URL) — the same
        read-only bridge pattern ARGUS already uses for SENTINEL's cyber campaigns and
        PHAROS's maritime incidents. One all-source analyst can therefore cite cyber,
        cognitive, maritime <i>and</i> air evidence in a single brief, each item carrying
        its own source rating.
      </p>

      <h2>Responsible use</h2>
      <p className="dim">
        Public, unauthenticated broadcasts only; aircraft-level, never individual persons;
        defensive and analytical only. Every incident is decision support for human review —
        never an automated verdict of hostile or illicit activity. Zero-cost rule: free data
        and free/local models only.
      </p>
    </div>
  )
}
