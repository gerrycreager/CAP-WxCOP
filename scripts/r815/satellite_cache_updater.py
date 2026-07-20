#!/usr/bin/env python3
"""
satellite_cache_updater.py — GOES ABI brightness-temperature cache for MapServer WMS.

Runs every 5 minutes via systemd timer. Reads raw ABI L1b radiance NetCDF from
the NFS-mounted LDM archive on data1 (/LDM/satellite/goes{18,19}/...), converts
radiance -> brightness temperature (Kelvin) via each file's own Planck
calibration coefficients, reprojects from the native GOES fixed-grid
(geostationary) projection to EPSG:4326, and writes single-band float32
GeoTIFFs into /var/www/mapserver/cache/{product}/ with a {product}_current.tif
symlink to the most recent frame.

File naming: {product}_{YYYYMMDD-HHMMSS}.tif

Each file's conversion runs in its own subprocess. GOES L1b files occasionally
land with corrupted internal HDF5 structure (observed in production) — this
can segfault the HDF5 library outright rather than raising a catchable Python
exception, so per-file subprocess isolation is not optional hardening here,
it's the only way one bad file doesn't take down the whole run.

Author: CAP WxCOP
"""

import os
import re
import sys
import json
import glob
import shutil
import sqlite3
import logging
import datetime
import multiprocessing
from pathlib import Path

import numpy as np

# ── Config ───────────────────────────────────────────────────────────────────
LDM_BASE      = Path('/LDM/satellite')
CACHE_DIR     = Path('/var/www/mapserver/cache')
LOG_FILE      = '/var/log/satellite_cache_updater.log'
BAD_FILE_CACHE = CACHE_DIR / '.satellite_bad_files.json'
RETAIN_HOURS  = 36  # incident documentation needs 24-36h
CHILD_TIMEOUT = 150  # seconds -- Full Disk C13 (5424x5424) reprojection can be slow
MAPCACHE_DIMS_DB = Path('/var/cache/mapcache/dims.sqlite')

# GOES filename timestamp: ..._sYYYYDDDHHMMSSs_e..._c...nc  (14 digits after 's')
TS_RE = re.compile(r'_s(\d{14})_')

PRODUCTS = {
    # ── CONUS sector, brightness temperature ──────────────────────────────
    'wv_conus_east': {'subdir': 'goes19/C08',      'glob': 'OR_ABI-L1b-RadC-M6C08_G19_*.nc'},
    'wv_conus_west': {'subdir': 'goes18/C08',      'glob': 'OR_ABI-L1b-RadC-M6C08_G18_*.nc'},
    'ir_conus_east': {'subdir': 'goes19/C13',      'glob': 'OR_ABI-L1b-RadC-M6C13_G19_*.nc'},
    'ir_conus_west': {'subdir': 'goes18/C13',      'glob': 'OR_ABI-L1b-RadC-M6C13_G18_*.nc'},
    # ── Full Disk, AK/HI/PR-USVI (Guam pending) ────────────────────────────
    'wv_full_east':  {'subdir': 'goes19/full/C08', 'glob': 'OR_ABI-L1b-RadF-M6C08_G19_*.nc'},
    'wv_full_west':  {'subdir': 'goes18/full/C08', 'glob': 'OR_ABI-L1b-RadF-M6C08_G18_*.nc'},
    'ir_full_east':  {'subdir': 'goes19/full/C13', 'glob': 'OR_ABI-L1b-RadF-M6C13_G19_*.nc'},
    'ir_full_west':  {'subdir': 'goes18/full/C13', 'glob': 'OR_ABI-L1b-RadF-M6C13_G18_*.nc'},
}

# East and West each have gaps the other satellite covers (most visibly:
# GOES-East's CONUS sector doesn't reach the far southwest/Baja area).
# East wins where both have data (better viewing geometry for the eastern
# 2/3 of CONUS); West fills whatever East's sector doesn't reach.
MOSAIC_PRODUCTS = {
    'wv_conus': {'east': 'wv_conus_east', 'west': 'wv_conus_west'},
    'ir_conus': {'east': 'ir_conus_east', 'west': 'ir_conus_west'},
}
MOSAIC_EXTENT = (-130.0, 20.0, -60.0, 55.0)  # left, bottom, right, top
# Matches CONUS_SAT_RES below -- was 0.025 (~2.7km), coarser than native ABI
# CONUS resolution (2km), which would have silently thrown away the
# per-satellite fix at mosaic time.
MOSAIC_RES    = 0.018  # degrees/pixel

# calculate_default_transform()'s auto resolution estimate for a geos->4326
# warp comes out coarser than the source's true native sampling for any
# CONUS/OCONUS sector (i.e. anything off the sub-satellite point) -- measured
# ~3.2-4.1km/pixel actual output vs 2.0km/pixel native ABI CONUS resolution.
# Force it explicitly for CONUS scenes instead of trusting the auto-estimate.
# 0.018 deg ~= 2.0km at CONUS latitudes (~38N in the lat direction; finer
# than 2km in the lon direction, so this errs toward not losing resolution
# anywhere in the sector rather than an exact match everywhere).
CONUS_SAT_RES = 0.018  # degrees/pixel

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('satellite_cache')


def load_bad_files() -> dict:
    """
    Known-corrupted source files, keyed by path, value = ISO timestamp when
    recorded. Without this, every run re-spawns a subprocess to re-fail on
    the same already-known-bad files for as long as they sit in the 24h
    retention window -- observed in production to meaningfully delay fresh
    data on products with a heavy corrupted backlog.
    """
    try:
        with open(BAD_FILE_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_bad_files(bad: dict, cutoff: datetime.datetime):
    # Prune entries old enough to have aged out of the retention window anyway
    pruned = {
        path: ts for path, ts in bad.items()
        if datetime.datetime.fromisoformat(ts) >= cutoff
    }
    tmp = BAD_FILE_CACHE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(pruned, f)
    tmp.replace(BAD_FILE_CACHE)


def parse_timestamp(fname: str) -> datetime.datetime | None:
    m = TS_RE.search(fname)
    if not m:
        return None
    try:
        # first 13 digits are YYYYDDDHHMMSS; the 14th is tenths-of-a-second, dropped
        return datetime.datetime.strptime(m.group(1)[:13], '%Y%j%H%M%S')
    except ValueError:
        return None


def collect_source_files(cfg: dict, cutoff: datetime.datetime) -> list[tuple[datetime.datetime, Path]]:
    subdir = LDM_BASE / cfg['subdir']
    if not subdir.is_dir():
        return []
    results = []
    for f in subdir.glob(cfg['glob']):
        ts = parse_timestamp(f.name)
        if ts is None or ts < cutoff:
            continue
        results.append((ts, f))
    results.sort(key=lambda x: x[0])
    return results


# ── Radiance -> brightness temperature + reprojection (child process) ────────

def _convert_worker(src_path: str, dest_path: str, result_path: str):
    """
    Runs in an isolated subprocess. Writes 'OK' or an error message to
    result_path so the parent can tell success from a crash without relying
    on the child's exit code alone (a segfault gives no useful message).
    """
    try:
        import netCDF4 as nc
        import rasterio
        from rasterio.crs import CRS
        from rasterio.transform import Affine
        from rasterio.warp import calculate_default_transform, reproject, Resampling

        ds = nc.Dataset(src_path)
        rad = ds.variables['Rad'][:].astype(np.float64)
        dqf = ds.variables['DQF'][:]

        fk1 = float(ds.variables['planck_fk1'][:])
        fk2 = float(ds.variables['planck_fk2'][:])
        bc1 = float(ds.variables['planck_bc1'][:])
        bc2 = float(ds.variables['planck_bc2'][:])

        with np.errstate(divide='ignore', invalid='ignore'):
            bt = (fk2 / np.log((fk1 / rad) + 1) - bc1) / bc2
        bt = np.ma.masked_where((dqf != 0) | np.ma.getmaskarray(rad), bt)
        bt_filled = bt.filled(np.nan).astype(np.float32)

        gp = ds.variables['goes_imager_projection']
        h = gp.perspective_point_height
        a = gp.semi_major_axis
        b = gp.semi_minor_axis
        lon_0 = gp.longitude_of_projection_origin
        sweep = gp.sweep_angle_axis

        src_crs = CRS.from_proj4(
            f"+proj=geos +h={h} +a={a} +b={b} +lon_0={lon_0} +sweep={sweep} +units=m +no_defs"
        )

        x = ds.variables['x'][:].astype(np.float64) * h
        y = ds.variables['y'][:].astype(np.float64) * h
        x_res = (x[-1] - x[0]) / (len(x) - 1)
        y_res = (y[-1] - y[0]) / (len(y) - 1)
        src_transform = Affine(x_res, 0, x[0] - x_res / 2, 0, y_res, y[0] - y_res / 2)

        dst_crs = CRS.from_epsg(4326)
        # calculate_default_transform()'s auto resolution estimate comes out
        # coarser than native for an oblique geos->4326 warp -- force it for
        # CONUS scenes (see CONUS_SAT_RES). Full Disk/Mesoscale left on the
        # auto-estimate for now (coarser, but not the operationally-relevant
        # sector, and 4-9x the pixel count is a real cost not worth paying
        # there yet).
        #
        # Also can't trust calculate_default_transform's auto-detected
        # bounding box for CONUS either -- GOES-West's oblique view of CONUS
        # distorts badly enough near its limb that the auto bbox comes out
        # ~99 degrees of longitude (west blew up to 19999px wide before this
        # was caught), vastly larger than the real CONUS footprint. Use the
        # same fixed target extent as the East+West mosaic instead (also
        # makes the mosaic's regrid step a near-trivial resample now that
        # both inputs already share its grid/extent).
        scene_id = getattr(ds, 'scene_id', '')
        if scene_id == 'CONUS':
            import rasterio.transform
            left, bottom, right, top = MOSAIC_EXTENT
            width  = int(round((right - left) / CONUS_SAT_RES))
            height = int(round((top - bottom) / CONUS_SAT_RES))
            dst_transform = rasterio.transform.from_bounds(left, bottom, right, top, width, height)
        else:
            dst_transform, width, height = calculate_default_transform(
                src_crs, dst_crs, bt_filled.shape[1], bt_filled.shape[0],
                left=x.min(), right=x.max(), bottom=y.min(), top=y.max(),
            )

        dst = np.full((height, width), np.nan, dtype=np.float32)
        reproject(
            source=bt_filled, destination=dst,
            src_transform=src_transform, src_crs=src_crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

        tmp_path = dest_path + '.tmp'
        with rasterio.open(
            tmp_path, 'w', driver='GTiff',
            height=height, width=width, count=1, dtype='float32',
            crs=dst_crs, transform=dst_transform, nodata=np.nan,
            compress='deflate',
        ) as out:
            out.write(dst, 1)
        os.replace(tmp_path, dest_path)

        with open(result_path, 'w') as f:
            f.write('OK')
    except Exception as e:
        with open(result_path, 'w') as f:
            f.write(f'ERROR: {e}')


def convert_one(src: Path, dest: Path) -> tuple[bool, str]:
    """Isolate the actual conversion in a subprocess -- see module docstring."""
    result_path = str(dest) + '.result'
    proc = multiprocessing.Process(target=_convert_worker, args=(str(src), str(dest), result_path))
    proc.start()
    proc.join(timeout=CHILD_TIMEOUT)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        Path(result_path).unlink(missing_ok=True)
        return False, f'timed out after {CHILD_TIMEOUT}s'

    if not Path(result_path).exists():
        # process died without writing a result -- segfault or similar
        return False, f'child crashed (exitcode={proc.exitcode})'

    msg = Path(result_path).read_text()
    Path(result_path).unlink(missing_ok=True)
    if msg == 'OK':
        return True, ''
    return False, msg


# ── Per-product update ────────────────────────────────────────────────────────

def sync_mapcache_dims(product: str, tif_files: list[Path]):
    """
    Keep MapCache's sqlite dimension table (dims.sqlite, tileset=product)
    in sync with what's actually on disk -- see mrms_cache_updater.py's
    copy of this function for the full rationale.
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


def update_product(product: str, cfg: dict, cutoff: datetime.datetime, bad_files: dict):
    prod_dir = CACHE_DIR / product
    prod_dir.mkdir(parents=True, exist_ok=True)

    sources = collect_source_files(cfg, cutoff)
    if not sources:
        log.warning(f'{product}: no source files found since {cutoff:%Y%m%d-%H%M%S}')
        return

    added, skipped_bad = 0, 0
    for ts, src in sources:
        ts_str = ts.strftime('%Y%m%d-%H%M%S')
        dest = prod_dir / f'{product}_{ts_str}.tif'
        if dest.exists():
            continue
        src_key = str(src)
        if src_key in bad_files:
            skipped_bad += 1
            continue
        ok, err = convert_one(src, dest)
        if not ok:
            log.error(f'{product}: {src.name}: {err}')
            # Timeouts can be transient (load, slow reprojection) -- only
            # blacklist genuine crashes/read errors, worth retrying a timeout
            if 'timed out' not in err:
                bad_files[src_key] = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()
            continue
        added += 1

    if added:
        log.info(f'{product}: added {added} new frames')
    if skipped_bad:
        log.info(f'{product}: skipped {skipped_bad} known-bad source files')

    cached = sorted(prod_dir.glob(f'{product}_*.tif'))
    if not cached:
        log.warning(f'{product}: cache empty after update')
        return

    latest = cached[-1]
    current_link = CACHE_DIR / f'{product}_current.tif'
    tmp_link = current_link.with_suffix('.tmp_link')
    try:
        tmp_link.symlink_to(latest)
        tmp_link.replace(current_link)
    except Exception as e:
        log.error(f'{product}: symlink update failed: {e}')
        tmp_link.unlink(missing_ok=True)

    pruned = 0
    for f in prod_dir.glob(f'{product}_*.tif'):
        ts = parse_timestamp_output(f.name, product)
        if ts and ts < cutoff:
            try:
                f.unlink()
                pruned += 1
            except Exception as e:
                log.warning(f'prune failed {f.name}: {e}')
    if pruned:
        log.info(f'{product}: pruned {pruned} expired frames')

    sync_mapcache_dims(product, list(prod_dir.glob(f'{product}_*.tif')))

    remaining = len(list(prod_dir.glob(f'{product}_*.tif')))
    log.info(f'{product}: cache has {remaining} frames, latest={latest.name}')


def parse_timestamp_output(fname: str, product: str) -> datetime.datetime | None:
    m = re.search(rf'{re.escape(product)}_(\d{{8}}-\d{{6}})\.tif$', fname)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S')
    except ValueError:
        return None


# ── East+West CONUS mosaic ────────────────────────────────────────────────────
# Operates on our own already-converted, already-validated GeoTIFFs -- not raw
# untrusted L1b HDF5 -- so no subprocess isolation needed here, just a
# per-frame try/except so one bad merge doesn't take down the rest.

def mosaic_grid():
    import rasterio.transform
    left, bottom, right, top = MOSAIC_EXTENT
    width  = int(round((right - left) / MOSAIC_RES))
    height = int(round((top - bottom) / MOSAIC_RES))
    transform = rasterio.transform.from_bounds(left, bottom, right, top, width, height)
    return transform, width, height


def regrid(src_path: Path, dst_transform, dst_width: int, dst_height: int):
    """Resample an existing EPSG:4326 GeoTIFF onto the shared mosaic grid."""
    import rasterio
    from rasterio.warp import reproject, Resampling
    with rasterio.open(src_path) as src:
        dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1), destination=dst,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=dst_transform, dst_crs='EPSG:4326',
            src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return dst


def five_min_bucket(ts: datetime.datetime) -> datetime.datetime:
    return ts.replace(second=0, microsecond=0, minute=ts.minute - (ts.minute % 5))


def mosaic_conus_product(mosaic_name: str, east_product: str, west_product: str, cutoff: datetime.datetime):
    import rasterio

    east_dir = CACHE_DIR / east_product
    west_dir = CACHE_DIR / west_product
    out_dir  = CACHE_DIR / mosaic_name
    out_dir.mkdir(parents=True, exist_ok=True)

    transform, width, height = mosaic_grid()

    east_files, west_files = {}, {}
    for f in east_dir.glob(f'{east_product}_*.tif'):
        ts = parse_timestamp_output(f.name, east_product)
        if ts and ts >= cutoff:
            east_files[five_min_bucket(ts)] = f
    for f in west_dir.glob(f'{west_product}_*.tif'):
        ts = parse_timestamp_output(f.name, west_product)
        if ts and ts >= cutoff:
            west_files[five_min_bucket(ts)] = f

    added = 0
    for bucket in sorted(set(east_files) | set(west_files)):
        ts_str = bucket.strftime('%Y%m%d-%H%M%S')
        dest = out_dir / f'{mosaic_name}_{ts_str}.tif'
        if dest.exists():
            continue

        e, w = east_files.get(bucket), west_files.get(bucket)
        try:
            merged = np.full((height, width), np.nan, dtype=np.float32)
            if w is not None:
                merged = regrid(w, transform, width, height)
            if e is not None:
                east_grid = regrid(e, transform, width, height)
                merged = np.where(~np.isnan(east_grid), east_grid, merged)

            tmp = dest.with_suffix('.tif.tmp')
            with rasterio.open(
                tmp, 'w', driver='GTiff',
                height=height, width=width, count=1, dtype='float32',
                crs='EPSG:4326', transform=transform, nodata=np.nan,
                compress='deflate',
            ) as out:
                out.write(merged, 1)
            os.replace(tmp, dest)
            added += 1
        except Exception as e2:
            log.error(f'{mosaic_name}: mosaic failed for {ts_str}: {e2}')

    if added:
        log.info(f'{mosaic_name}: added {added} new mosaic frames')

    cached = sorted(out_dir.glob(f'{mosaic_name}_*.tif'))
    if not cached:
        return

    latest = cached[-1]
    current_link = CACHE_DIR / f'{mosaic_name}_current.tif'
    tmp_link = current_link.with_suffix('.tmp_link')
    try:
        tmp_link.symlink_to(latest)
        tmp_link.replace(current_link)
    except Exception as e2:
        log.error(f'{mosaic_name}: symlink update failed: {e2}')
        tmp_link.unlink(missing_ok=True)

    pruned = 0
    for f in out_dir.glob(f'{mosaic_name}_*.tif'):
        ts = parse_timestamp_output(f.name, mosaic_name)
        if ts and ts < cutoff:
            try:
                f.unlink()
                pruned += 1
            except Exception as e2:
                log.warning(f'prune failed {f.name}: {e2}')
    if pruned:
        log.info(f'{mosaic_name}: pruned {pruned} expired frames')

    sync_mapcache_dims(mosaic_name, list(out_dir.glob(f'{mosaic_name}_*.tif')))

    remaining = len(list(out_dir.glob(f'{mosaic_name}_*.tif')))
    log.info(f'{mosaic_name}: cache has {remaining} frames, latest={latest.name}')


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=RETAIN_HOURS)
    bad_files = load_bad_files()

    def run_product(product):
        try:
            update_product(product, PRODUCTS[product], cutoff, bad_files)
        except Exception as e:
            log.error(f'{product}: unhandled exception: {e}', exc_info=True)
        # Save after every product, not just at the end -- a long-running
        # product (heavy backlog) risks TimeoutStartSec killing the whole
        # run before it ever reaches a final save, losing everything learned
        save_bad_files(bad_files, cutoff)

    # CONUS east/west first: the mosaics below (what EWMC actually displays
    # by default) depend only on these two, and they're fast (~seconds each
    # in steady state). Full-disk products are the slow ones -- Full Disk
    # C13 reprojection alone can eat most of TimeoutStartSec when backlogged
    # -- so run them *after* the mosaic instead of before it. Previously the
    # mosaic ran last and got starved out of the run's time budget on every
    # cycle a full-disk product ran long, silently leaving the mosaic stale
    # for 2+ days despite the raw east/west products staying current.
    conus_ew = ['wv_conus_east', 'wv_conus_west', 'ir_conus_east', 'ir_conus_west']
    for product in conus_ew:
        run_product(product)

    for mosaic_name, cfg in MOSAIC_PRODUCTS.items():
        try:
            mosaic_conus_product(mosaic_name, cfg['east'], cfg['west'], cutoff)
        except Exception as e:
            log.error(f'{mosaic_name}: unhandled exception: {e}', exc_info=True)

    for product in PRODUCTS:
        if product not in conus_ew:
            run_product(product)


if __name__ == '__main__':
    main()
