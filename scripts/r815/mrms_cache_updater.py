#!/usr/bin/env python3
"""
mrms_cache_updater.py — Rolling 3-hour grib2 cache for MapServer MRMS WMS.

Runs every 2 minutes via systemd timer. Reads from NFS-mounted LDM archive
on data1 (/LDM/radar/mrms/YYYY/MM/DD/{subdir}/), gunzips all CONUS files
from the last 3 hours into /var/www/mapserver/cache/{product}/ and maintains
a {product}_current.grib2 symlink to the most recent file.

File naming: {product}_{YYYYMMDD-HHMMSS}.grib2
  e.g. composite_20260406-200438.grib2

CONUS identification:
  Composite : extent check — origin lon < 0 (Western Hemisphere)
  AzShear   : extent check — origin lon < 0 (Western Hemisphere)
  MESH      : extent check — origin lon < 0 (Western Hemisphere)

Author: CAP WxCOP
"""

import os
import re
import sys
import glob
import gzip
import shutil
import logging
import tempfile
import datetime
from pathlib import Path
from osgeo import gdal

gdal.UseExceptions()

# ── Config ────────────────────────────────────────────────────────────────────
LDM_BASE    = Path('/LDM/radar/mrms')
CACHE_DIR   = Path('/var/www/mapserver/cache')
LOG_FILE    = '/var/log/mrms_cache_updater.log'
RETAIN_HOURS = 24       # rolling window to keep
MAX_DAYS_BACK = 2       # how many days back to search

PRODUCTS = {
    # ── CONUS products ────────────────────────────────────────────────────────
    'composite': {
        'subdir'  : 'Composite',
        'glob'    : 'MRMS_MergedReflectivityQComposite_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': None,
        'conus'   : 'extent',
    },
    'azshear_low': {
        'subdir'  : 'AzShear',
        'glob'    : 'MRMS_MergedAzShear_0-2kmAGL_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': None,
        'conus'   : 'extent',
    },
    'mesh': {
        'subdir'  : 'Severe',
        'glob'    : 'MRMS_MESH_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': None,
        'conus'   : 'extent',
    },
    'lightning': {
        'subdir'  : 'Lightning',
        'glob'    : 'MRMS_LightningProbabilityNext60minGrid_scale_1_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': None,
        'conus'   : 'extent',
    },
    # ── OCONUS composite sectors ─────────────────────────────────────────────
    # Filed by data2 pqact_mrms_oconus.conf into sector subdirectories
    # No size/extent filter needed — each sector directory is sector-specific
    'composite_alaska': {
        'subdir'  : 'Composite/ALASKA',
        'glob'    : 'MRMS_MergedReflectivityQComposite_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': 50_000,    # AK composite ~234KB compressed
        'conus'   : 'size',
    },
    'composite_hawaii': {
        'subdir'  : 'Composite/HAWAII',
        'glob'    : 'MRMS_MergedReflectivityQComposite_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': 10_000,    # HI composite ~63KB compressed
        'conus'   : 'size',
    },
    'composite_carib': {
        'subdir'  : 'Composite/CARIB',
        'glob'    : 'MRMS_MergedReflectivityQComposite_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': 10_000,    # CARIB composite ~23KB compressed
        'conus'   : 'size',
    },
    'composite_guam': {
        'subdir'  : 'Composite/GUAM',
        'glob'    : 'MRMS_MergedReflectivityQComposite_00.50_*.grib2.gz',
        'ts_re'   : r'(\d{8}-\d{6})\.grib2\.gz$',
        'min_size': 10_000,    # GUAM composite ~424KB compressed
        'conus'   : 'size',
    },
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('mrms_cache')


def date_dirs():
    today = datetime.date.today()
    for i in range(MAX_DAYS_BACK + 1):
        yield LDM_BASE / (today - datetime.timedelta(days=i)).strftime('%Y/%m/%d')


def parse_timestamp(fname: str, ts_re: str) -> datetime.datetime | None:
    m = re.search(ts_re, fname)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S')
    except ValueError:
        return None


def is_conus_extent(gz_path: Path) -> bool:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.grib2', delete=False) as tmp:
            tmp_path = tmp.name
            with gzip.open(gz_path, 'rb') as gz:
                shutil.copyfileobj(gz, tmp)
        ds = gdal.Open(tmp_path)
        if ds is None:
            return False
        origin_lon = ds.GetGeoTransform()[0]
        ds = None
        return origin_lon < 0
    except Exception as e:
        log.warning(f'extent check failed {gz_path.name}: {e}')
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def collect_source_files(cfg: dict, cutoff: datetime.datetime) -> list[tuple[datetime.datetime, Path]]:
    """
    Return list of (timestamp, path) for all CONUS files newer than cutoff,
    sorted oldest-first.
    """
    results = []
    seen_ts = set()

    for day_dir in date_dirs():
        subdir = day_dir / cfg['subdir']
        if not subdir.is_dir():
            continue
        for f in subdir.glob(cfg['glob']):
            ts = parse_timestamp(f.name, cfg['ts_re'])
            if ts is None or ts < cutoff:
                continue
            if ts in seen_ts:
                continue
            # CONUS filter
            if cfg['conus'] == 'size':
                if f.stat().st_size < cfg['min_size']:
                    continue
            # extent check deferred — done during gunzip to avoid double-read
            results.append((ts, f))
            seen_ts.add(ts)

    results.sort(key=lambda x: x[0])
    return results


def gunzip_atomic(src: Path, dest: Path) -> bool:
    tmp = dest.with_suffix('.tmp')
    try:
        with gzip.open(src, 'rb') as gz_in, open(tmp, 'wb') as f_out:
            shutil.copyfileobj(gz_in, f_out)
        tmp.replace(dest)
        # Preserve source mtime
        mt = src.stat().st_mtime
        os.utime(dest, (mt, mt))
        return True
    except Exception as e:
        log.error(f'gunzip_atomic {src.name}: {e}')
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def verify_conus_extent(path: Path) -> bool:
    """Check already-gunzipped file for CONUS extent."""
    try:
        ds = gdal.Open(str(path))
        if ds is None:
            return False
        origin_lon = ds.GetGeoTransform()[0]
        ds = None
        return origin_lon < 0
    except Exception:
        return False


def update_product(product: str, cfg: dict, cutoff: datetime.datetime):
    prod_dir = CACHE_DIR / product
    prod_dir.mkdir(parents=True, exist_ok=True)

    # ── Collect source files newer than cutoff ────────────────────────────────
    sources = collect_source_files(cfg, cutoff)
    if not sources:
        log.warning(f'{product}: no source files found since {cutoff:%Y%m%d-%H%M%S}')
        return

    # ── Gunzip any files not yet in cache ─────────────────────────────────────
    added = 0
    for ts, src in sources:
        ts_str = ts.strftime('%Y%m%d-%H%M%S')
        dest = prod_dir / f'{product}_{ts_str}.grib2'
        if dest.exists():
            continue
        if not gunzip_atomic(src, dest):
            continue
        # For MESH, verify extent after gunzip
        if cfg['conus'] == 'extent':
            if not verify_conus_extent(dest):
                log.debug(f'{product}: {dest.name} is not CONUS, removing')
                dest.unlink(missing_ok=True)
                continue
        added += 1

    if added:
        log.info(f'{product}: added {added} new frames')

    # ── Update _current symlink to most recent file ───────────────────────────
    cached = sorted(prod_dir.glob(f'{product}_*.grib2'))
    if not cached:
        log.warning(f'{product}: cache empty after update')
        return

    latest = cached[-1]
    current_link = CACHE_DIR / f'{product}_current.grib2'
    # Atomic symlink update
    tmp_link = current_link.with_suffix('.tmp_link')
    try:
        tmp_link.symlink_to(latest)
        tmp_link.replace(current_link)
    except Exception as e:
        log.error(f'{product}: symlink update failed: {e}')
        try:
            tmp_link.unlink(missing_ok=True)
        except Exception:
            pass

    # ── Prune files older than cutoff ─────────────────────────────────────────
    pruned = 0
    for f in prod_dir.glob(f'{product}_*.grib2'):
        ts = parse_timestamp(f.name, r'(\d{8}-\d{6})\.grib2$')
        if ts and ts < cutoff:
            try:
                f.unlink()
                pruned += 1
            except Exception as e:
                log.warning(f'prune failed {f.name}: {e}')
    if pruned:
        log.info(f'{product}: pruned {pruned} expired frames')

    # Report cache state
    remaining = len(list(prod_dir.glob(f'{product}_*.grib2')))
    log.info(f'{product}: cache has {remaining} frames, latest={latest.name}')


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=RETAIN_HOURS)

    for product, cfg in PRODUCTS.items():
        try:
            update_product(product, cfg, cutoff)
        except Exception as e:
            log.error(f'{product}: unhandled exception: {e}', exc_info=True)


if __name__ == '__main__':
    main()
