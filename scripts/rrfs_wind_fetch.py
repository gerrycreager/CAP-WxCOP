#!/var/www/cap_winds_app/venv/bin/python3
"""
rrfs_wind_fetch.py — shared RRFS byte-range fetch-and-cache for the wind
render scripts (render_hrrr_winds.py, render_hrrr_particles.py), part of
the HRRR->RRFS migration (Gerry's deadline: 2026-10-16 — see
/root/.claude/plans/rosy-wobbling-music.md, "Phase 2: Wind Particles").

Generalizes the fetch-and-cache-per-(date,cycle,fhr) pattern already proven
in rrfs_sounding_extract.py (prslev) to arbitrary (var, level) selections
and to the 2dfld product, confirmed working 2026-09-21. Same NOMADS
byte-range .idx technique, same per-key lock (guards the exact race
hrrr_sounding_extract.py hit in production before that fix).

Two products the wind scripts need:
  - '2dfld' : surface/AGL winds only (10m UGRD/VGRD) — confirmed 2026-09-21
    this product has NO isobaric levels at all (only SFC/AGL heights and
    fixed MSL bands).
  - 'prslev': isobaric winds (UGRD/VGRD at any of the 37 100-1000mb levels)
    — same product Soundings already uses, confirmed present there.

Real measured costs (2026-09-21, rrfs.20260920/09z/f000): 2dfld 10m fetch
0.94s; prslev 4-level fetch 2.29s. cfgrib decode has a one-time ~5-30s
per-PROCESS warm-up (confirmed genuine library warm-up via cache-drop
testing, not OS file-cache), then ~0.5s per subsequent forecast hour within
the same process — the render scripts loop over 13-19 fhrs in one process,
so they pay the warm-up once per run, not once per fhr. Realistic per-run
totals: particles (13 fhrs) ~48s, winds (19 fhrs) ~78s, both trivially
inside the hourly cron window.

Cache is deliberately short-lived (6h, not Soundings' 48h) — these are
re-rendered every hour by cron, no reason to keep stale cycles around.

Domain-aware as of 2026-09-21 (Phase 3, ingest_model_site_wx.py's RRFS
repoint for CONUS+AK+HI+PR cadet sites): RRFS posts separate files per
domain -- 'conus'/'ak' at 3km, 'hi'/'pr' at 2.5km, confirmed live
(rrfs.tCCz.2dfld.{3km,2p5km}.fFFF.{conus,ak,hi,pr}.grib2). Field structure
confirmed byte-identical across all four domains via a real pygrib decode
check. `domain`/`resolution` default to 'conus'/'3km' so the existing
wind-particle callers (Phase 2) need no changes.
"""
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger('rrfs_wind_fetch')

SOURCE_BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/para'
CACHE_DIR = Path('/tmp/rrfs_wind_cache')
CACHE_RETENTION_HOURS = 6

_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


def _lock_for(key):
    with _fetch_locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[key] = lock
        return lock


def _parse_idx(idx_text):
    """Skips any line that isn't well-formed numeric .idx content, rather
    than raising -- found live 2026-09-22 (rrfs_model_taf.py, under heavier
    concurrent NOMADS load than any prior caller of this module): a `.idx`
    URL can return HTTP 200 with an HTML rate-limit/error page body instead
    of real .idx text, and parts[0]/parts[1] then aren't integers at all.
    A malformed response naturally yields zero entries this way, and
    fetch_and_cache()'s existing `if not entries: return None` already
    treats that as "not ready" -- exactly the right fallback behavior,
    no different than a 404."""
    entries = []
    for line in idx_text.strip().splitlines():
        parts = line.split(':')
        if len(parts) < 5:
            continue
        try:
            msgnum, offset = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        var, level = parts[3], parts[4]
        entries.append((msgnum, offset, var, level))
    return entries


def _cache_path(product, date, cycle, fhr, tag, domain='conus', resolution='3km'):
    return CACHE_DIR / date / f'{cycle}z' / f'rrfs.t{cycle}z.{product}.{resolution}.f{fhr:03d}.{domain}.{tag}.subset.grib2'


def fetch_and_cache(product, date, cycle, fhr, wanted_var_levels, tag, domain='conus', resolution='3km'):
    """Fetch a curated byte-range subset of an RRFS product for one
    forecast hour, matching exactly the (var, level-string) pairs in
    wanted_var_levels -- a set of (var, level) tuples exactly as they
    appear in the .idx file, e.g. ('UGRD', '10 m above ground') or
    ('UGRD', '850 mb'). `tag` distinguishes cache files for different
    wanted-set selections of the same (product, date, cycle, fhr) (e.g.
    'sfc10m' vs 'iso4lvl') so two scripts wanting different subsets of the
    same source file don't collide on one cache entry. `domain`/`resolution`
    select which RRFS domain file to fetch ('conus'/'ak' at 3km, 'hi'/'pr'
    at 2.5km -- pass resolution='2p5km' for those two). Returns the local
    path, or None if not posted yet or nothing matched."""
    key = (product, date, cycle, fhr, tag, domain, resolution)
    out_path = _cache_path(product, date, cycle, fhr, tag, domain, resolution)
    if out_path.exists():
        return out_path

    with _lock_for(key):
        if out_path.exists():
            return out_path

        grib_key = f'rrfs.{date}/{cycle}/rrfs.t{cycle}z.{product}.{resolution}.f{fhr:03d}.{domain}.grib2'
        idx_url = f'{SOURCE_BASE}/{grib_key}.idx'
        grib_url = f'{SOURCE_BASE}/{grib_key}'

        with requests.Session() as session:
            r = session.get(idx_url, timeout=20)
            if r.status_code == 404:
                log.warning('%s not posted yet: %s', product, grib_key)
                return None
            r.raise_for_status()
            entries = _parse_idx(r.text)
            if not entries:
                return None

            head = session.head(grib_url, timeout=20)
            head.raise_for_status()
            total_size = int(head.headers['Content-Length'])

            entries.sort(key=lambda e: e[1])
            wanted = []
            for i, (msgnum, offset, var, level) in enumerate(entries):
                if (var, level) in wanted_var_levels:
                    end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_size - 1
                    wanted.append((offset, end))

            if not wanted:
                log.warning('none of %s matched in %s', wanted_var_levels, grib_key)
                return None

            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix('.tmp')
            with open(tmp_path, 'wb') as out:
                for start, end in wanted:
                    resp = session.get(grib_url, headers={'Range': f'bytes={start}-{end}'}, timeout=30)
                    if resp.status_code not in (200, 206):
                        tmp_path.unlink(missing_ok=True)
                        raise RuntimeError(f'range fetch failed: HTTP {resp.status_code}')
                    out.write(resp.content)
            tmp_path.rename(out_path)
        log.info('Cached %s (%d fields, %d bytes)', out_path, len(wanted), out_path.stat().st_size)
        return out_path


def prune_cache(now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=CACHE_RETENTION_HOURS)
    if not CACHE_DIR.is_dir():
        return
    for f in CACHE_DIR.glob('*/*/*.grib2'):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
                f.unlink()
        except OSError:
            pass


def latest_available_cycle(now=None, lag_hours=2.5):
    """RRFS posting lag has been observed to run a bit past the ~1-1.5hr
    assumption baked into the Soundings cycle-picker (a 02z cycle wasn't
    posted yet at 04:06 UTC, ~2hr4min later) -- use a more conservative
    2.5hr default here since these are unattended cron jobs with no user
    watching a spinner, better to wait a bit longer for a cycle that's
    actually there than to fail a scheduled render.

    Kept as a pure guess (no network call) for callers that just want a
    starting point -- see find_ready_cycle() for the version that actually
    verifies the guess against NOMADS and steps backward if needed."""
    now = now or datetime.now(timezone.utc)
    cycle_dt = (now - timedelta(hours=lag_hours)).replace(minute=0, second=0, microsecond=0)
    return cycle_dt.strftime('%Y%m%d'), cycle_dt.strftime('%H')


def _cycle_has_f000(product, date, cycle, domain='conus', resolution='3km'):
    """Cheap readiness probe: does this cycle's F000 .idx exist yet?"""
    url = f'{SOURCE_BASE}/rrfs.{date}/{cycle}/rrfs.t{cycle}z.{product}.{resolution}.f000.{domain}.grib2.idx'
    try:
        r = requests.head(url, timeout=10)
        if r.status_code == 404:
            r = requests.get(url, timeout=10, stream=True)  # NOMADS's HEAD isn't always reliable for .idx
            r.close()
        return r.status_code == 200
    except requests.RequestException:
        return False


def find_ready_cycle(product='2dfld', now=None, start_lag_hours=2.5, max_lookback_hours=6,
                      domain='conus', resolution='3km'):
    """Real-world finding (2026-09-20): the para feed's posting isn't
    perfectly predictable by a fixed lag margin alone -- a guessed cycle can
    be not-yet-posted, or (less commonly) skipped/pruned entirely (a cycle
    older than an available one still 404ing). Start from the lag-margin
    guess and step backward hour by hour, actually checking NOMADS, until a
    cycle with F000 posted is found. Returns (date, cycle_num) or
    (None, None) if nothing in the lookback window is ready -- callers
    should treat that as "skip this run" (matching find_latest_cycle()'s
    existing HRRR-path contract), not raise."""
    now = now or datetime.now(timezone.utc)
    cursor = (now - timedelta(hours=start_lag_hours)).replace(minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=max_lookback_hours)
    while cursor >= oldest:
        date, cyc = cursor.strftime('%Y%m%d'), cursor.strftime('%H')
        if _cycle_has_f000(product, date, cyc, domain, resolution):
            return date, cyc
        log.warning('%s cycle %s %sz not ready, trying previous hour', product, date, cyc)
        cursor -= timedelta(hours=1)
    return None, None
