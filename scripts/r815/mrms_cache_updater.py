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
import sqlite3
import logging
import tempfile
import datetime
import multiprocessing
from pathlib import Path
from osgeo import gdal

gdal.UseExceptions()

# ── Config ────────────────────────────────────────────────────────────────────
LDM_BASE    = Path('/LDM/radar/mrms')
CACHE_DIR   = Path('/var/www/mapserver/cache')
LOG_FILE    = '/var/log/mrms_cache_updater.log'
RETAIN_HOURS = 36       # rolling window to keep -- incident documentation needs 24-36h
MAX_DAYS_BACK = 2       # how many days back to search
TIF_CHILD_TIMEOUT = 60  # seconds -- composite's 7000x3500 grid, generous margin
MAPCACHE_DIMS_DB = Path('/var/cache/mapcache/dims.sqlite')

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


def _tif_worker(grib2_path: str, tif_path: str, result_path: str):
    """
    Runs in an isolated subprocess -- see translate_to_tiled_tif() docstring
    for why. Writes 'OK' or an error message to result_path so the parent
    can tell success from a crash without relying on exit code alone (a
    GDAL-level segfault gives no useful message).
    """
    try:
        from osgeo import gdal as _gdal
        _gdal.UseExceptions()

        grib2_path = Path(grib2_path)
        tif_path = Path(tif_path)
        tmp = tif_path.with_suffix('.tmp.tif')

        src_ds = _gdal.Open(str(grib2_path))
        if src_ds is None:
            raise RuntimeError('gdal.Open returned None')
        is_float = _gdal.GetDataTypeName(src_ds.GetRasterBand(1).DataType).startswith('Float')
        src_ds = None
        predictor = 3 if is_float else 2

        ds = _gdal.Translate(
            str(tmp), str(grib2_path),
            format='GTiff',
            creationOptions=[
                'TILED=YES', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256',
                'COMPRESS=DEFLATE', f'PREDICTOR={predictor}',
            ],
        )
        if ds is None:
            raise RuntimeError('gdal.Translate returned None')
        ds = None
        ds = _gdal.Open(str(tmp), _gdal.GA_Update)
        ds.BuildOverviews('NEAREST', [2, 4, 8, 16])
        ds = None
        tmp.replace(tif_path)
        mt = grib2_path.stat().st_mtime
        os.utime(tif_path, (mt, mt))

        with open(result_path, 'w') as f:
            f.write('OK')
    except Exception as e:
        try:
            Path(tif_path).with_suffix('.tmp.tif').unlink(missing_ok=True)
        except Exception:
            pass
        with open(result_path, 'w') as f:
            f.write(f'ERROR: {e}')


def translate_to_tiled_tif(grib2_path: Path, tif_path: Path) -> bool:
    """
    Convert a gunzipped grib2 into an internally-tiled GeoTIFF with
    embedded overviews. GRIB2's compression doesn't support efficient
    windowed reads -- MapServer was decoding the full multi-million-pixel
    grid on every single WMS tile request (measured ~3-10s for one 256x256
    tile on composite's 7000x3500 grid). A tiled GeoTIFF fixes that at the
    source; MapCache fixes the "don't re-render the same tile twice" half.

    Pixel values are copied verbatim (no rescaling) so existing mapfile
    CLASS/EXPRESSION thresholds keep working unchanged. NEAREST resampling
    for overviews -- this is classified/thresholded data (dBZ, mm, s^-1),
    not photographic imagery, so averaging across class/no-data boundaries
    would invent physically meaningless values.

    DEFLATE's PREDICTOR must match the source data type: 2 (horizontal
    differencing) for integer types, 3 (floating point predictor) for
    float types. Using the wrong one is a real, if intermittent, libtiff
    corruption bug -- confirmed in production: PREDICTOR=2 against
    composite's Float64 grids produced garbage on ~1 in 200 frames
    (ZIPDecode/TIFFReadEncodedTile errors on read-back, and one outright
    "Maximum TIFF file size exceeded" from a pathological expansion).

    The actual Translate/BuildOverviews calls run in a subprocess, not
    in-process. GDAL's Python bindings were observed accumulating corrupted
    internal state across many sequential Translate/BuildOverviews calls in
    one long-running process -- a file that failed during a real backfill
    run converted cleanly moments later in isolation. Same class of bug
    already fixed this way in satellite_cache_updater.py's convert_one().
    """
    result_path = str(tif_path) + '.result'
    proc = multiprocessing.Process(
        target=_tif_worker, args=(str(grib2_path), str(tif_path), result_path)
    )
    proc.start()
    proc.join(timeout=TIF_CHILD_TIMEOUT)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        Path(result_path).unlink(missing_ok=True)
        log.error(f'translate_to_tiled_tif {grib2_path.name}: timed out after {TIF_CHILD_TIMEOUT}s')
        return False

    if not Path(result_path).exists():
        log.error(f'translate_to_tiled_tif {grib2_path.name}: child crashed (exitcode={proc.exitcode})')
        return False

    msg = Path(result_path).read_text()
    Path(result_path).unlink(missing_ok=True)
    if msg == 'OK':
        return True
    log.error(f'translate_to_tiled_tif {grib2_path.name}: {msg}')
    return False


def sync_mapcache_dims(product: str, tif_files: list[Path]):
    """
    Keep MapCache's sqlite dimension table (dims.sqlite, tileset=product)
    in sync with what's actually on disk. Without this, historical WMS
    requests (dim_ts=...) would validate against a snapshot frozen at
    whenever the table was last populated, rejecting every new frame as
    soon as the cache-updater ages the old ones out.
    """
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
            [product, *current_ts]
        )
        conn.executemany(
            'INSERT OR IGNORE INTO frames (tileset, ts) VALUES (?, ?)',
            [(product, ts) for ts in current_ts]
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f'{product}: mapcache dims sync failed: {e}')


def update_product(product: str, cfg: dict, cutoff: datetime.datetime):
    prod_dir = CACHE_DIR / product
    prod_dir.mkdir(parents=True, exist_ok=True)

    # ── Collect source files newer than cutoff ────────────────────────────────
    sources = collect_source_files(cfg, cutoff)
    if not sources:
        log.warning(f'{product}: no source files found since {cutoff:%Y%m%d-%H%M%S}')
        return

    # ── Gunzip any files not yet in cache, then convert to tiled GeoTIFF ──────
    added, tif_added = 0, 0
    for ts, src in sources:
        ts_str = ts.strftime('%Y%m%d-%H%M%S')
        dest = prod_dir / f'{product}_{ts_str}.grib2'
        tif_dest = prod_dir / f'{product}_{ts_str}.tif'
        if not dest.exists():
            if not gunzip_atomic(src, dest):
                continue
            # For MESH, verify extent after gunzip
            if cfg['conus'] == 'extent':
                if not verify_conus_extent(dest):
                    log.debug(f'{product}: {dest.name} is not CONUS, removing')
                    dest.unlink(missing_ok=True)
                    continue
            added += 1
        # grib2 is the archival copy (kept for incident-documentation
        # fidelity); the tif is what MapServer actually serves from
        if dest.exists() and not tif_dest.exists():
            if translate_to_tiled_tif(dest, tif_dest):
                tif_added += 1

    if added:
        log.info(f'{product}: added {added} new frames')
    if tif_added:
        log.info(f'{product}: converted {tif_added} frames to tiled GeoTIFF')

    # ── Update _current symlinks to most recent file ──────────────────────────
    cached = sorted(prod_dir.glob(f'{product}_*.grib2'))
    if not cached:
        log.warning(f'{product}: cache empty after update')
        return

    latest = cached[-1]
    for ext, cache_glob in (('grib2', prod_dir.glob(f'{product}_*.grib2')),
                             ('tif',   prod_dir.glob(f'{product}_*.tif'))):
        matching = sorted(cache_glob)
        if not matching:
            continue
        latest_ext = matching[-1]
        current_link = CACHE_DIR / f'{product}_current.{ext}'
        tmp_link = current_link.with_suffix('.tmp_link')
        try:
            tmp_link.symlink_to(latest_ext)
            tmp_link.replace(current_link)
        except Exception as e:
            log.error(f'{product}: {ext} symlink update failed: {e}')
            try:
                tmp_link.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Prune files older than cutoff ─────────────────────────────────────────
    pruned = 0
    for pattern, suffix_re in ((f'{product}_*.grib2', r'(\d{8}-\d{6})\.grib2$'),
                                (f'{product}_*.tif',   r'(\d{8}-\d{6})\.tif$')):
        for f in prod_dir.glob(pattern):
            ts = parse_timestamp(f.name, suffix_re)
            if ts and ts < cutoff:
                try:
                    f.unlink()
                    pruned += 1
                except Exception as e:
                    log.warning(f'prune failed {f.name}: {e}')
    if pruned:
        log.info(f'{product}: pruned {pruned} expired frames')

    sync_mapcache_dims(product, list(prod_dir.glob(f'{product}_*.tif')))

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
