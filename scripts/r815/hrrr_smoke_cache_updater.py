#!/usr/bin/env python3
"""
hrrr_smoke_cache_updater.py — HRRR near-surface smoke (MASSDEN) cache for
MapServer WMS, feeding the Cadet Weather COP smoke/PM2.5 contour layer.

Source: direct HTTPS range-fetch from NOAA's public AWS Open Data HRRR
bucket (noaa-hrrr-bdp-pds) -- NOT the LDM feed this app's other HRRR
fields come from. Confirmed empirically: the LDM relay currently in use
(idd.aos.wisc.edu) does not carry MASSDEN/COLMD for HRRR -- a live
notifyme scan of its HRRR product stream matched exactly the ~79 fields
already landing locally with no smoke fields among them. Not a local
pqact filtering issue; the upstream relay itself doesn't forward it.
(User is separately looking into whether a different relay would.) This
script is the interim -- possibly permanent -- path around that.

MASSDEN (near-surface smoke, 8m AGL, kg/m3) is individually byte-range
fetchable via the .idx sidecar NOAA publishes alongside each GRIB2 file --
pulls ~850KB instead of the full multi-GB wrfsfc file. GDAL auto-detects
HRRR's native Lambert Conformal projection straight from the GRIB2
metadata (verified against a live file), so no manual reprojection step
is needed -- same "copy pixel values verbatim" approach as
mrms_cache_updater.py; the mapfile's CLASS/EXPRESSION thresholds are
defined directly in the native kg/m3 units, no rescaling.

Runs hourly via cron, matching HRRR's own cycle cadence. F00's smoke
field isn't a cold-started guess -- it's informed by RAVE, which fuses
GOES ABI + VIIRS satellite fire-radiative-power detections into hourly
emissions feeding each new HRRR cycle (see cadet_wx_api.py's
air_quality_color() docstring for the parallel reasoning re: AirNow).
Fetches F00-F03 of the latest available cycle for light animation.

Each conversion runs in its own subprocess -- same crash-isolation
rationale as tpw_cache_updater.py/mrms_cache_updater.py: a malformed
GRIB2 payload or GDAL-level fault shouldn't be able to take down the
whole cron run.
"""
import os
import re
import sys
import json
import sqlite3
import logging
import datetime
import multiprocessing
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
CACHE_DIR       = Path('/var/www/mapserver/cache/hrrr_smoke')
PRODUCT         = 'hrrr_smoke'
LOG_FILE        = '/var/log/hrrr_smoke_cache_updater.log'
RETAIN_HOURS    = 6              # a few recent frames for light animation, not a full archive
FORECAST_HOURS  = [0, 1, 2, 3]   # near-term "what's happening now/soon", not a multi-day archive
CHILD_TIMEOUT   = 90
CYCLE_LOOKBACK  = 6              # how many hours back to search for the latest published cycle

S3_BASE = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
MAPCACHE_DIMS_DB = Path('/var/cache/mapcache/dims.sqlite')

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('hrrr_smoke_cache')


# ── Latest-cycle discovery ────────────────────────────────────────────────────
def find_latest_cycle() -> tuple[str, str] | tuple[None, None]:
    """HEAD-check F00's .idx for the current UTC hour, stepping back until one
    exists. Operational HRRR typically finishes within ~50-55min of cycle
    time, so recent hours may not be published yet -- that's expected, not
    an error."""
    now = datetime.datetime.now(datetime.UTC)
    for back in range(CYCLE_LOOKBACK):
        dt = now - datetime.timedelta(hours=back)
        date_str = dt.strftime('%Y%m%d')
        cyc_str  = dt.strftime('%H')
        url = f'{S3_BASE}/hrrr.{date_str}/conus/hrrr.t{cyc_str}z.wrfsfcf00.grib2.idx'
        try:
            req = urllib.request.Request(url, method='HEAD')
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return date_str, cyc_str
        except urllib.error.HTTPError:
            continue
        except Exception as e:
            log.warning(f'HEAD check failed for {url}: {e}')
            continue
    return None, None


def get_byte_range(idx_text: str, field_name: str) -> tuple[int, int | None] | tuple[None, None]:
    """Parse a wgrib2-style .idx file for one field's byte range. Returns
    (start, end) with end=None meaning "to EOF" (field was the last message)."""
    lines = idx_text.strip().split('\n')
    for i, line in enumerate(lines):
        parts = line.split(':')
        if len(parts) < 4 or parts[3] != field_name:
            continue
        start = int(parts[1])
        end = None
        if i + 1 < len(lines):
            next_parts = lines[i + 1].split(':')
            if len(next_parts) > 1 and next_parts[1].isdigit():
                end = int(next_parts[1]) - 1
        return start, end
    return None, None


def fetch_massden(date_str: str, cyc_str: str, fhr: int) -> bytes | None:
    """Range-fetch just the MASSDEN message for one forecast hour. Returns
    None if this step isn't published yet (e.g. requesting ahead of what's
    actually available) -- not an error, callers should skip it quietly."""
    base = f'{S3_BASE}/hrrr.{date_str}/conus/hrrr.t{cyc_str}z.wrfsfcf{fhr:02d}.grib2'
    try:
        with urllib.request.urlopen(base + '.idx', timeout=20) as resp:
            idx_text = resp.read().decode('utf-8')
    except Exception as e:
        log.info(f'F{fhr:02d}: idx fetch failed ({e})')
        return None

    start, end = get_byte_range(idx_text, 'MASSDEN')
    if start is None:
        log.warning(f'F{fhr:02d}: MASSDEN not found in idx')
        return None

    range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
    try:
        req = urllib.request.Request(base, headers={'Range': range_hdr})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except Exception as e:
        log.warning(f'F{fhr:02d}: range fetch failed ({e})')
        return None


# ── GDAL conversion (child process) ───────────────────────────────────────────
def _convert_worker(grib2_bytes_path: str, tif_path: str, result_path: str):
    try:
        from osgeo import gdal as _gdal
        _gdal.UseExceptions()

        tmp = Path(tif_path).with_suffix('.tmp.tif')
        src_ds = _gdal.Open(grib2_bytes_path)
        if src_ds is None:
            raise RuntimeError('gdal.Open returned None')
        src_ds = None  # Float64 source -- predictor=3 always applies here

        ds = _gdal.Translate(
            str(tmp), grib2_bytes_path,
            format='GTiff',
            creationOptions=[
                'TILED=YES', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256',
                'COMPRESS=DEFLATE', 'PREDICTOR=3',
            ],
        )
        if ds is None:
            raise RuntimeError('gdal.Translate returned None')
        ds = None
        ds = _gdal.Open(str(tmp), _gdal.GA_Update)
        ds.BuildOverviews('NEAREST', [2, 4, 8, 16])
        ds = None
        tmp.replace(tif_path)

        with open(result_path, 'w') as f:
            f.write('OK')
    except Exception as e:
        try:
            Path(tif_path).with_suffix('.tmp.tif').unlink(missing_ok=True)
        except Exception:
            pass
        with open(result_path, 'w') as f:
            f.write(f'ERROR: {e}')


def convert_one(grib2_path: Path, tif_path: Path) -> tuple[bool, str]:
    result_path = str(tif_path) + '.result'
    proc = multiprocessing.Process(
        target=_convert_worker,
        args=(str(grib2_path), str(tif_path), result_path)
    )
    proc.start()
    proc.join(timeout=CHILD_TIMEOUT)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        Path(result_path).unlink(missing_ok=True)
        return False, f'timed out after {CHILD_TIMEOUT}s'

    if not Path(result_path).exists():
        return False, f'child crashed (exitcode={proc.exitcode})'

    msg = Path(result_path).read_text()
    Path(result_path).unlink(missing_ok=True)
    if msg == 'OK':
        return True, 'OK'
    return False, msg


# ── Post-processing (prune / current symlink / mapcache sync) ───────────────
def sync_mapcache_dims(tif_files: list[Path]):
    if not MAPCACHE_DIMS_DB.exists():
        return
    ts_re = re.compile(r'_(\d{8}-\d{6})\.tif$')
    current_ts = {'current'}
    for f in tif_files:
        m = ts_re.search(f.name)
        if m:
            current_ts.add(m.group(1))
    try:
        conn = sqlite3.connect(str(MAPCACHE_DIMS_DB), timeout=5)
        placeholders = ','.join('?' * len(current_ts))
        conn.execute(
            f'DELETE FROM frames WHERE tileset = ? AND ts NOT IN ({placeholders})',
            [PRODUCT, *current_ts]
        )
        conn.executemany(
            'INSERT OR IGNORE INTO frames (tileset, ts) VALUES (?, ?)',
            [(PRODUCT, ts) for ts in current_ts]
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f'mapcache dims sync failed: {e}')


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info('=' * 60)
    log.info('HRRR smoke cache update started')

    date_str, cyc_str = find_latest_cycle()
    if date_str is None:
        log.error('No published HRRR cycle found in lookback window')
        sys.exit(1)
    log.info(f'Latest cycle: {date_str} {cyc_str}z')

    added = 0
    for fhr in FORECAST_HOURS:
        grib2_bytes = fetch_massden(date_str, cyc_str, fhr)
        if grib2_bytes is None:
            continue

        ts_str = (datetime.datetime.strptime(f'{date_str}{cyc_str}', '%Y%m%d%H')
                  + datetime.timedelta(hours=fhr)).strftime('%Y%m%d-%H%M%S')
        tif_path = CACHE_DIR / f'{PRODUCT}_{ts_str}.tif'
        if tif_path.exists():
            continue  # already have this frame

        grib2_path = CACHE_DIR / f'.tmp_{PRODUCT}_{ts_str}.grib2'
        grib2_path.write_bytes(grib2_bytes)
        try:
            ok, info = convert_one(grib2_path, tif_path)
            if ok:
                added += 1
                log.info(f'F{fhr:02d} ({ts_str}): converted OK')
            else:
                log.error(f'F{fhr:02d} ({ts_str}): {info}')
        finally:
            grib2_path.unlink(missing_ok=True)

    log.info(f'Added {added} new frames')

    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=RETAIN_HOURS)
    cached = sorted(CACHE_DIR.glob(f'{PRODUCT}_*.tif'))
    if cached:
        latest = cached[-1]
        current_link = CACHE_DIR / f'{PRODUCT}_current.tif'
        tmp_link = current_link.with_suffix('.tmp_link')
        try:
            tmp_link.symlink_to(latest)
            tmp_link.replace(current_link)
        except Exception as e:
            log.error(f'symlink update failed: {e}')
            tmp_link.unlink(missing_ok=True)

        ts_re = re.compile(rf'{re.escape(PRODUCT)}_(\d{{8}}-\d{{6}})\.tif$')
        pruned = 0
        for f in CACHE_DIR.glob(f'{PRODUCT}_*.tif'):
            m = ts_re.search(f.name)
            if not m:
                continue
            ts = datetime.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S')
            if ts < cutoff:
                try:
                    f.unlink()
                    pruned += 1
                except Exception as e:
                    log.warning(f'prune failed {f.name}: {e}')
        if pruned:
            log.info(f'Pruned {pruned} expired frames')

        sync_mapcache_dims(list(CACHE_DIR.glob(f'{PRODUCT}_*.tif')))
        remaining = len(list(CACHE_DIR.glob(f'{PRODUCT}_*.tif')))
        log.info(f'Cache has {remaining} frames, latest={latest.name}')
    else:
        log.warning('No cached frames available after this run')

    log.info('Done')
    log.info('=' * 60)


if __name__ == '__main__':
    main()
