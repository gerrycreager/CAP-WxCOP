#!/var/www/cap_winds_app/venv/bin/python3
"""
rrfs_sounding_extract.py — on-demand RRFS forecast sounding extraction.
RRFS-side counterpart to hrrr_sounding_extract.py, used as the default
sounding source per the HRRR->RRFS migration (Gerry's deadline: all HRRR
usage replaced by 2026-10-16 -- see memory: project_noaaport_sbn_href_retirement,
and /root/.claude/plans/rosy-wobbling-music.md for the full migration plan).
soundings_api.py keeps hrrr_sounding_extract.py available as an explicit
`?source=hrrr` fallback -- see that file/plan for why.

Same profile shape, same fetch-and-cache-per-cycle design, same byte-range
.idx technique as hrrr_sounding_extract.py -- extract_profile() needed ZERO
changes from the HRRR version (confirmed 2026-09-20 against a real pull):
same cfgrib variable names (gh/t/dpt/u/v, isobaricInhPa coord) work
unchanged for RRFS's prslev product.

Source: NOMADS para (pre-operational) feed, the only live real-time source
as of 2026-09-20:
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/para/rrfs.YYYYMMDD/CC/
    rrfs.tCCz.prslev.3km.fFFF.conus.grib2  (+ .idx)
No S3/NODD Big Data Program bucket exists yet for RRFS/REFS (checked
2026-09-19/20, ~10 plausible bucket names, all 404) -- SOURCE_BASE below is
the one line to change if/when one appears (likely named noaa-rrfs-bdp-pds
if it follows HRRR's convention), worth re-checking around/after 2026-10-14.

Confirmed field mapping: prslev has HGT/TMP/DPT/UGRD/VGRD at 25mb steps,
exactly 37 levels in the 100-1000mb scope this feature cares about -- same
level count as HRRR's wrfprs curated subset.

KNOWN, real, unresolved latency cost (2026-09-20 measurement, KBAK point,
cache-hit i.e. zero network involved): decode-only time was 207.1s vs
HRRR's ~35-45s baseline -- ~5x slower, worse than the ~2.35x file-size
ratio (292MB curated subset vs HRRR's 124.6MB) alone would predict, so
there's real per-message/dataset-construction overhead beyond raw data
volume. This is inherent to RRFS's GRIB2 structure, not fixable by a
faster transport -- a cold request (NOMADS fetch + decode) can realistically
take 3-5+ minutes. The async job-queue design in soundings_api.py already
absorbs this with no timeout risk; just don't be surprised by the wait.

NOTE: NOMADS's Akamai edge sends a malformed Content-Length header that
breaks curl's HTTP/2 parsing (had to use --http1.1 for manual curl checks).
Confirmed NOT an issue for this module's actual `requests`-based fetches --
requests negotiates HTTP/1.1 by default (unlike curl, which prefers
HTTP/2), and the real end-to-end test here worked with no special handling.
"""
import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger('rrfs_sounding_extract')

# The one line to change once an S3/NODD bucket exists -- see module docstring.
SOURCE_BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/para'
CACHE_DIR = Path('/tmp/rrfs_soundings')
CACHE_RETENTION_HOURS = 48

WANTED_VARS = {'HGT', 'TMP', 'DPT', 'UGRD', 'VGRD'}
MIN_MB, MAX_MB = 100, 1000


def _latest_available_rrfs_cycle(now=None, fhr=0):
    """Mirrors soundings_api.py's _latest_available_hrrr_cycle() -- RRFS
    posts hourly like HRRR, same ~1-1.5hr posting lag assumption applies.
    RRFS extends past F18 only on the four synoptic cycles (00/06/12/18Z),
    same as HRRR, but out to F84 (not HRRR's F48) -- pick the most recent
    synoptic cycle old enough to have that hour posted when the caller
    wants an extended-range hour."""
    now = now or datetime.now(timezone.utc)
    lag = timedelta(hours=2)
    if fhr <= 18:
        cycle_dt = (now - lag).replace(minute=0, second=0, microsecond=0)
        return cycle_dt.strftime('%Y%m%d'), cycle_dt.strftime('%H')

    candidate = now - lag
    synoptic_hours = [0, 6, 12, 18]
    eligible = [h for h in synoptic_hours if h <= candidate.hour]
    if eligible:
        cycle_dt = candidate.replace(hour=max(eligible), minute=0, second=0, microsecond=0)
    else:
        cycle_dt = (candidate - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return cycle_dt.strftime('%Y%m%d'), cycle_dt.strftime('%H')


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
    return CACHE_DIR / date / f'{cycle}z' / f'rrfs.t{cycle}z.prslev.3km.f{fhr:03d}.subset.grib2'


def fetch_and_cache(date, cycle, fhr):
    """Same fetch-and-cache-per-cycle design as hrrr_sounding_extract.py's
    fetch_and_cache(), pointed at RRFS's prslev product on NOMADS instead
    of HRRR's wrfprs on S3. Returns the local path, or None if not posted."""
    out_path = _cache_path(date, cycle, fhr)
    if out_path.exists():
        return out_path

    with _lock_for(date, cycle, fhr):
        if out_path.exists():
            return out_path

        key = f'rrfs.{date}/{cycle}/rrfs.t{cycle}z.prslev.3km.f{fhr:03d}.conus.grib2'
        idx_url = f'{SOURCE_BASE}/{key}.idx'
        grib_url = f'{SOURCE_BASE}/{key}'

        with requests.Session() as session:
            r = session.get(idx_url, timeout=20)
            if r.status_code == 404:
                log.warning('RRFS prslev not posted yet: %s', key)
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
    import numpy as np
    dist2 = (lats - target_lat) ** 2 + (lons - target_lon) ** 2
    return np.unravel_index(np.argmin(dist2), dist2.shape)


def extract_profile(grib_path, lat, lon):
    """Same decode logic as hrrr_sounding_extract.py's extract_profile() --
    same profile shape, same cfgrib short-name assumptions (gh/t/dpt/u/v).
    Confirmed 2026-09-20 against a real RRFS pull: needed zero changes from
    the HRRR version. Note the source file's decode is genuinely slower
    (~5x HRRR's baseline, see module docstring) -- not a bug in this
    function, a real cost of RRFS's GRIB2 structure."""
    import cfgrib
    import numpy as np

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
                temp_c -= 273.15
            dewpoint_c = lv.get('dewpoint_c')
            if dewpoint_c is not None and dewpoint_c > 100:
                dewpoint_c -= 273.15
            u, v = lv.get('_u'), lv.get('_v')
            wind_dir = wind_speed = None
            if u is not None and v is not None:
                wind_dir = round((np.degrees(np.arctan2(-u, -v)) + 360) % 360)
                wind_speed = round(np.hypot(u, v) * 1.94384)
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


def get_rrfs_sounding(lat, lon, date, cycle, fhr):
    """Top-level entry point, same shape as hrrr_sounding_extract.py's
    get_hrrr_sounding() -- default sounding source per the HRRR->RRFS
    migration."""
    grib_path = fetch_and_cache(date, cycle, int(fhr))
    if grib_path is None:
        return None
    prune_cache()
    return extract_profile(grib_path, lat, lon)


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(description='Extract an on-demand RRFS sounding')
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--date', type=str, required=True, help='YYYYMMDD')
    ap.add_argument('--cycle', type=str, required=True, help='HH (00/06/12/18)')
    ap.add_argument('--fhr', type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    t0 = time.time()
    profile = get_rrfs_sounding(args.lat, args.lon, args.date, args.cycle, args.fhr)
    t1 = time.time()
    print(f'--- took {t1-t0:.1f}s ---', flush=True)
    print(json.dumps(profile, indent=2))
