#!/var/www/cap_winds_app/venv/bin/python3
"""
hrrr_sounding_extract.py — on-demand HRRR forecast sounding extraction.

Given (date, cycle, fhr, lat, lon), returns a decoded vertical profile in
the same shape as observations.sounding.profile, for Skew-T/hodograph
rendering. Called synchronously from soundings_api.py's forecast route --
acceptable since this is a rare, single-point, human-driven action, not a
bulk job.

Why S3 and not the local LDM mirror: confirmed 2026-09-19 that
/LDM/models/hrrr only carries the wrfsfc (surface package) product, capped
at 1000-500mb -- the upstream relay (idd.aos.wisc.edu) doesn't carry HRRR's
pressure-level (wrfprs) product family at all under this subscription (zero
matches across ~2h of live notifyme monitoring). NOAA's public S3 archive
does carry the full wrfprs product; this pulls a curated byte-range subset
from there, same technique as scripts/fetch_hrrr_extended.py.

Why fetch-and-cache-per-cycle, not fetch-fresh-per-request: GRIB2 messages
aren't byte-addressable *within* a field -- extracting one point still means
downloading and decoding the whole message, so a naive per-click S3 fetch
would pay the same ~125MB transfer on every single request. Instead, the
first request for a given (date, cycle, fhr) fetches and caches the curated
subset locally; every subsequent point clicked in that same cycle/hour, by
anyone, is served from the cache. Deliberately NOT written under
/LDM/models/ (that's the shared, retention-managed HRRR mirror) -- this is
a short-lived, request-driven cache, pruned after CACHE_RETENTION_HOURS.

Scope: 100-1000mb only (37 of HRRR's 39 wrfprs levels), matching the
TTAA/TTBB observed-sounding scope -- CAP aircraft don't operate anywhere
near 100mb, no need to pull/cache the 50-75mb levels above it.
"""
import os
import re
import glob
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger('hrrr_sounding_extract')

S3_BASE = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
CACHE_DIR = Path('/tmp/hrrr_soundings')
CACHE_RETENTION_HOURS = 48

# One lock per (date, cycle, fhr) so two concurrent requests for the same
# forecast hour (e.g. a double-click, or a stray retry) coordinate instead
# of racing on the same tmp file -- this raced and threw a FileNotFoundError
# on the tmp->final rename in production on 2026-09-19 before this fix.
_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


def _lock_for(date, cycle, fhr):
    key = (date, cycle, fhr)
    with _fetch_locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[key] = lock
        return lock

WANTED_VARS = {'HGT', 'TMP', 'DPT', 'UGRD', 'VGRD'}
MIN_MB, MAX_MB = 100, 1000


def _parse_idx(idx_text):
    entries = []
    for line in idx_text.strip().splitlines():
        parts = line.split(':')
        if len(parts) < 5:
            continue
        msgnum, offset = int(parts[0]), int(parts[1])
        var, level = parts[3], parts[4]
        entries.append((msgnum, offset, var, level))
    return entries


def _level_mb(level_str):
    if not level_str.endswith(' mb'):
        return None
    try:
        return int(level_str.split()[0])
    except ValueError:
        return None


def _cache_path(date, cycle, fhr):
    return CACHE_DIR / date / f'{cycle}z' / f'hrrr.t{cycle}z.wrfprsf{fhr:02d}.subset.grib2'


def fetch_and_cache(date, cycle, fhr):
    """Ensure the curated wrfprs subset for this (date, cycle, fhr) exists
    locally, fetching it from S3 if not already cached. Returns the local
    path, or None if the source isn't available (not posted yet).

    Locked per (date, cycle, fhr): if two requests for the same forecast
    hour land at once, the second waits for the first's fetch instead of
    racing it on the same tmp file (or double-fetching all 185 messages)."""
    out_path = _cache_path(date, cycle, fhr)
    if out_path.exists():
        return out_path

    with _lock_for(date, cycle, fhr):
        if out_path.exists():  # someone else finished it while we waited
            return out_path

        key = f'hrrr.{date}/conus/hrrr.t{cycle}z.wrfprsf{fhr:02d}.grib2'
        idx_url = f'{S3_BASE}/{key}.idx'
        grib_url = f'{S3_BASE}/{key}'

        with requests.Session() as session:
            r = session.get(idx_url, timeout=20)
            if r.status_code == 404:
                log.warning('wrfprs not posted yet: %s', key)
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
                mb = _level_mb(level)
                if var in WANTED_VARS and mb is not None and MIN_MB <= mb <= MAX_MB:
                    end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_size - 1
                    wanted.append((offset, end))

            if not wanted:
                return None

            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix('.tmp')
            # Reuse one HTTP connection (keep-alive) across all 185 range
            # requests instead of a fresh TCP+TLS handshake per request --
            # that per-request setup cost was a meaningful chunk of the
            # ~2-3min cold-fetch latency observed in production.
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


def _nearest_index(lats, lons, target_lat, target_lon):
    """Single nearest-neighbor lookup on the native curvilinear grid. A
    plain vectorized argmin, not a KDTree: a KDTree's build cost only pays
    off across many repeated queries against the same tree (that's what
    render_hrrr_particles.py needs, regridding the whole CONUS domain) --
    for a single on-demand point, one O(n) pass is simpler and faster than
    building a tree just to run it once."""
    import numpy as np
    dist2 = (lats - target_lat) ** 2 + (lons - target_lon) ** 2
    return np.unravel_index(np.argmin(dist2), dist2.shape)


def extract_profile(grib_path, lat, lon):
    """Extract a single-column profile from a cached wrfprs subset file.
    Returns a list of {pressure_mb, height_m, temp_c, dewpoint_c,
    wind_dir_deg, wind_speed_kt} dicts, sorted surface -> 100mb (matching
    observations.sounding.profile's shape), or None on failure."""
    import cfgrib
    import numpy as np

    # Tried letting cfgrib persist a .idx sidecar here (this cache dir is
    # private/single-writer, unlike the shared LDM path render_hrrr_winds.py
    # avoids indexing) hoping repeat opens would be faster -- measured the
    # opposite: 93s vs 35s, apparently because persisting an index changes
    # how cfgrib groups messages into datasets (many more pairwise xr.merge()
    # calls, visible as a wall of FutureWarnings). Reverted; this stays a
    # real, unsolved latency concern (~30-45s per extraction even without a
    # fetch) -- see the Soundings plan's verification notes.
    datasets = cfgrib.open_datasets(str(grib_path), backend_kwargs={'indexpath': ''})
    try:
        by_level = {}
        idx = None
        for ds in datasets:
            if 'isobaricInhPa' not in ds.coords:
                continue
            levels = np.atleast_1d(ds.isobaricInhPa.values)
            is_multi = np.ndim(ds.isobaricInhPa.values) > 0
            lats = ds.latitude.values
            lons = np.where(ds.longitude.values > 180, ds.longitude.values - 360, ds.longitude.values)
            if idx is None:
                idx = _nearest_index(lats, lons, lat, lon)
            for i, lv in enumerate(levels):
                mb = int(round(float(lv)))
                entry = by_level.setdefault(mb, {})
                for var_name, key in (('gh', 'height_m'), ('t', 'temp_c'), ('dpt', 'dewpoint_c'),
                                       ('u', '_u'), ('v', '_v')):
                    if var_name not in ds.data_vars:
                        continue
                    arr = ds[var_name].values
                    val = arr[i][idx] if is_multi else arr[idx]
                    entry[key] = float(val)
        if not by_level:
            return None

        profile = []
        for mb in sorted(by_level.keys(), reverse=True):
            lv = by_level[mb]
            temp_c = lv.get('temp_c')
            if temp_c is not None and temp_c > 100:
                temp_c -= 273.15  # GRIB stores Kelvin
            dewpoint_c = lv.get('dewpoint_c')
            if dewpoint_c is not None and dewpoint_c > 100:
                dewpoint_c -= 273.15
            u, v = lv.get('_u'), lv.get('_v')
            wind_dir = wind_speed = None
            if u is not None and v is not None:
                wind_dir = round((np.degrees(np.arctan2(-u, -v)) + 360) % 360)
                wind_speed = round(np.hypot(u, v) * 1.94384)  # m/s -> kt
            profile.append({
                'pressure_mb': mb,
                'height_m': lv.get('height_m'),
                'temp_c': round(temp_c, 1) if temp_c is not None else None,
                'dewpoint_c': round(dewpoint_c, 1) if dewpoint_c is not None else None,
                'wind_dir_deg': wind_dir,
                'wind_speed_kt': wind_speed,
            })
        return profile
    finally:
        for ds in datasets:
            try:
                ds.close()
            except Exception:
                pass


def get_hrrr_sounding(lat, lon, date, cycle, fhr):
    """Top-level entry point: fetch/cache the subset if needed, extract the
    nearest-point profile. Returns the profile list, or None if the source
    data isn't available."""
    grib_path = fetch_and_cache(date, cycle, int(fhr))
    if grib_path is None:
        return None
    prune_cache()
    return extract_profile(grib_path, lat, lon)


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(description='Extract an on-demand HRRR sounding')
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--date', type=str, required=True, help='YYYYMMDD')
    ap.add_argument('--cycle', type=str, required=True, help='HH (00/06/12/18)')
    ap.add_argument('--fhr', type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    profile = get_hrrr_sounding(args.lat, args.lon, args.date, args.cycle, args.fhr)
    print(json.dumps(profile, indent=2))
