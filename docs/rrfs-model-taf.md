# RRFS Model-Derived TAFs

Model-guidance TAFs generated from RRFS (primary) / HRRR (fallback) forecast
data, for stations that don't have a real NWS TAF, or where model-driven
short-range guidance is independently useful. **These are not official
NWS/USAF products** — model guidance only, always labeled as such in the
output.

## Background

Built 2026-09-21/22 for Gerry's laptop-side Mission Execution Forecast
(MEF) tool, against the spec in
`~gerry/CAP/RPP/MEF_dev/RRFS_Model_TAF_Spec.20260920_2300-C.md`. The MEF
tool itself lives on Gerry's laptop, not in this app — this app's job is
purely the server-side generation and text/JSON API the laptop tool
scrapes.

Two generators exist, built a day apart, kept as separate products rather
than merged:

- **`scripts/mission_taf_extract.py`** (2026-09-21) — a fixed 9-airport
  cluster around Columbus, IN (KBAK, KOVO, KMQJ, KSER, KBFR, KGEZ, KI34,
  KHLB, KCEV — the same set EWMC's station-plot MEF-priority tier uses),
  fixed 06/14/22Z-style target times, single-layer sky condition, RRFS
  only. Served as plain-text/JSON bulletins by `mission_taf_api.py`
  (`/api/mission-taf/text`, `/api/mission-taf/json`).
- **`scripts/rrfs_model_taf.py`** (2026-09-22) — the general version: any
  station (given lat/lon/elevation), multi-layer clouds, BECMG trend
  groups, altimeter, RRFS-primary with an automatic HRRR fallback. Served
  by `model_taf_api.py` (`/api/modeltaf/...`). This is what the MEF spec
  actually asked for; the KBAK-cluster script predates it and was kept
  because it already worked and nothing depends on retiring it.

Neither is linked from the WxCOP homepage or any template — both exist
purely to be scraped by the laptop MEF tool.

## How it works

### Data source and fallback

RRFS has no local LDM/NOAAPort feed into this app (confirmed 2026-09-19 —
unlike HRRR, which arrives locally via `/LDM/models/hrrr/`). Every RRFS
field used here comes from a targeted NOMADS byte-range fetch
(`scripts/rrfs_wind_fetch.py`, shared with the wind-particle/wind-barb
render scripts), caching only the curated (var, level) pairs actually
needed rather than downloading full ~150-300MB files. Cache lives under
`/tmp/rrfs_wind_cache`, keyed by (product, date, cycle, forecast hour,
field-subset tag) so different callers wanting different field subsets of
the same source file don't collide.

If no RRFS cycle covers the requested forecast window (network issue,
NOMADS outage, or the far end of a long window not posted yet), generation
falls back to HRRR, read directly from the local LDM mirror — no fetch
needed there since it's already local.

RRFS's `para` (experimental) feed posts on a **strict 3-hourly cadence**
(00/03/06/09/12/15/18/21Z), confirmed via a full 24-hour check on
2026-09-23 — not hourly like HRRR. Non-synoptic cycles (03/09/15/21Z) only
extend to F018; synoptic cycles (00/06/12/18Z) extend further, enough to
cover the MEF's 30-hour window. `find_cycle_covering()` in
`rrfs_model_taf.py` handles this: checks local cache first (never re-hits
NOMADS for data already on disk), falls back to a 24-hour window if the
full 30 isn't posted yet, and only then drops to HRRR.

### Ceiling field — a real bug, fixed 2026-09-23

RRFS's `2dfld` product carries **two distinct ceiling fields** in the same
file: `GRIB_ELEMENT=CEIL` and `GRIB_ELEMENT=HGT` at
`typeOfLevel='cloud ceiling'` (geopotential-height-based). This code
originally fetched `CEIL` — confirmed to be a genuine artifact: at KBAK, a
2026-09-23 00z run alternated 1795→**3468**→1798→**3346**ft hour to hour
while the low-cloud-cover fraction (`LCDC`) sat at a rock-steady 100% the
entire time, which is physically impossible under a real unbroken overcast
layer. `HGT:cloud ceiling`, same file, same hours: 1650→1644→1644→1854ft —
smooth, consistent with the steady cover fraction. Independently
cross-checked the next morning against an outside GALWEM read (Gerry has
access, this app doesn't) — close agreement after the fix.

Fixed in `rrfs_model_taf.py`, `mission_taf_extract.py`, and (same bug,
same pattern) `ingest_nam_oconus.py` (Flight Ops Impacts' AK/HI/PR
stoplight ingest — unrelated consumer, same underlying field). Since
`GRIB_ELEMENT=HGT` is ambiguous by itself (RRFS's `2dfld` has several
distinct HGT fields — cloud base, ceiling, top, surface, tropopause...),
each script's `load_rrfs_bands()` disambiguates via `GRIB_SHORT_NAME`
(`'0-CEIL'`/`'0-CBL'`/`'0-CTL'`), mapped back to the same `'ceil'` output
key each module already used, so nothing downstream needed to change.

### Multi-layer cloud logic

RRFS gives per-band cloud cover fractions (`LCDC`/`MCDC`/`HCDC` — low/mid/
high, standardized altitude bands, not model-diagnosed heights) plus the
one real diagnosed ceiling height above. `derive_sky()` converts each
band's fraction to an okta-derived amount (FEW/SCT/BKN/OVC), assigns the
real diagnosed height to whichever band is actually ceiling-defining, and
falls back to a band midpoint placeholder for non-ceiling layers — honest
about which numbers are real model output and which are a representative
placeholder. Placeholder heights above 10,000ft, and any diagnosed ceiling
above 25,000ft, are suppressed to CLR (per Gerry: TAFs don't report
precise heights that high, and a 3km grid cell's value up there isn't
trustworthy anyway).

### TAF text assembly

Targets USAF FM51/AFMAN 15-124 grammar. Output is designed to round-trip
through `~gerry/CAP/RPP/MEF_dev/taf_usaf.py`'s parser — the ceiling this
module reports in JSON must equal what that parser computes back out of
the generated TAF text (that module's own acceptance test, spec section
7.8). BECMG groups require a minimum 3-hour gap between transitions
(avoids an unreadable TAF full of 1-hour BECMGs); TEMPO vs BECMG is
decided by whether a change reverts at least halfway back toward baseline
within about an hour (a relative-magnitude test, not an absolute
threshold — needed after a real single-hour ceiling spike wasn't being
caught as TEMPO under the simpler original design).

### Performance

RRFS decode (`rasterio` + GRIB_ELEMENT tag matching) has a one-time
5-30s per-process warm-up, then ~0.5s per subsequent forecast hour in the
same process — a `ProcessPoolExecutor` (lazily created once per WSGI
worker, reused across requests) fetches/decodes all requested hours in
parallel, so the warm-up cost is paid once per worker process, not once
per request.

## What it's used for

- **`mission_taf_api.py`** (`/api/mission-taf/text`, `/api/mission-taf/json`)
  — the fixed KBAK-cluster bulletin, scraped by Gerry's laptop MEF tool.
  On-demand only, no cron, no DB writes.
- **`model_taf_api.py`** (`/api/modeltaf/<icao>`, `/<icao>.txt`, `/batch`,
  and an async `/batch/start` + `/batch/status/<job_id>` +
  `/batch/result/<job_id>` job-queue pattern for longer multi-station/
  multi-hour requests, since Apache/mod_wsgi has a real ~60s request
  timeout) — the general any-station version. `max_fhour` defaults to 18
  for synchronous single-station requests, 30 for the async batch path
  (matching real MEF mission duration, standard TAF validity convention).

Both are deliberately request-driven only — no cron populates the
database with generated TAFs, nothing is stored; each request regenerates
from whatever's in the RRFS/HRRR cache at request time.

### Cache priming

`scripts/prime_rrfs_cache.py` (cron, hourly, `www-data` on r815) proactively
pre-warms the same RRFS cache these generators read from, so a real request
doesn't pay live NOMADS fetch latency. Added 2026-09-23 once this became
routine daily use rather than ad-hoc testing. Uses the exact same fetch
path/cache tag (`'modeltaf'`) `rrfs_model_taf.fetch_fhour()` uses, so a
real request afterward is a guaranteed cache hit, not a separate cache.
See `docs/` header comment in that script, and memory
`project_rrfs_cache_priming` for the operational detail (cadence choice,
a self-healing retry bug found before deploy, etc).

## Known limitations

- Not an official NWS/USAF product — model guidance only, clearly labeled
  in every output.
- Single ceiling-layer fidelity limit in `mission_taf_extract.py`
  specifically (the older, KBAK-cluster script) — `rrfs_model_taf.py`'s
  multi-layer logic is the fix for this, but the two scripts remain
  separate products rather than one replacing the other.
- RRFS's 3-hourly (not hourly) posting cadence means "latest data" can be
  up to ~3 hours stale, depending on how recently a cycle posted. Worth
  re-checking if/when RRFS moves off its current HPC "open space"
  allocation to production — Gerry's expectation is that a production
  switchover would likely bring a true hourly cadence.
- 3km RRFS grid resolution vs. the requested point is a real, accepted
  simplification (no downscaling/interpolation correction) — matches how
  every other RRFS-based feature in this app treats grid-point vs.
  station-location precision.
