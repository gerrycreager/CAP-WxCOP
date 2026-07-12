#!/usr/bin/env python3
"""
tpw_cache_updater.py — GOES ABI L2 Total Precipitable Water cache for MapServer WMS.

Runs every 2 minutes via systemd timer. Source files arrive over the HDS/WMO
bulletin feed (pqact_hds_tpw.conf) as a NetCDF4/HDF5 L2 TPW product wrapped
behind a WMO abbreviated-bulletin text header -- NOT raw GRIB2 despite the
".grib2" extension pqact writes them with (a pre-existing naming artifact in
the pqact rule, harmless since this script identifies the payload by content,
not extension). The WMO header text has to be stripped (search for \x89HDF
magic) before any HDF5/NetCDF4 reader can open the file.

The TPW variable itself is a standard CF-scaled integer grid (units: mm,
"lwe_thickness_of_atmosphere_mass_content_of_water_vapor", valid from
~300 hPa to the surface per the file's own summary attribute) -- decode
relies on netCDF4's own auto scale/mask (var[:] already comes back in mm),
then reprojects from the native GOES fixed-grid (geostationary) projection to
EPSG:4326, same shape of problem as satellite_cache_updater.py's L1b
radiance conversion.

IMPORTANT: which WMO header (IXTO89 vs IXTO99) carries which satellite/scene
combination is NOT stable -- observed firsthand in production: at one point
IXTO99 carried GOES-19/East Mesoscale-1, later the same header carried
GOES-18/West Full Disk, and both headers were also seen carrying a CONUS-
sector scene (ABI-L2-TPWC) that isn't in the original pqact catalog comment
at all. This matches real NOAA operational behavior (GOES East/West roles
and mesoscale slot assignments get reassigned, and NOAA apparently
interleaves more scene types under these headers than initially observed).
So this script does NOT trust the WMO header for product routing -- every
file is opened and routed by its own scene_id/orbital_slot attributes into
one of the fixed output buckets (KNOWN_PRODUCTS below), regardless of which
header it arrived under. New scene_id values show up as a loud per-file
error (and a permanent bad-file skip) rather than silently mis-routing --
see SCENE_TO_BUCKET.

Each file's conversion runs in its own subprocess -- same rationale as
satellite_cache_updater.py: a malformed HDF5 payload can segfault the
library outright rather than raising a catchable Python exception.

Author: CAP WxCOP
"""

import os
import re
import json
import sqlite3
import logging
import datetime
import multiprocessing
from pathlib import Path

import numpy as np

# ── Config ───────────────────────────────────────────────────────────────────
LDM_DIR       = Path('/LDM/hds/tpw')
CACHE_DIR     = Path('/var/www/mapserver/cache')
LOG_FILE      = '/var/log/tpw_cache_updater.log'
BAD_FILE_CACHE       = CACHE_DIR / '.tpw_bad_files.json'
PROCESSED_FILE_CACHE = CACHE_DIR / '.tpw_processed_files.json'
RETAIN_HOURS  = 36  # incident documentation needs 24-36h, matches MRMS/satellite
CHILD_TIMEOUT = 90
MAPCACHE_DIMS_DB = Path('/var/cache/mapcache/dims.sqlite')

# Raw filename: {IXT header}_{WMO station}_{HHMMSS}.{receipt YYYYMMDDHHMMSS}.grib2
# The receipt timestamp is when pqact wrote the file, not the actual scene
# time -- good enough as a coarse recency filter, not for naming output frames
# (the scene's own time_coverage_start, read inside the worker, is used for that).
RECEIPT_TS_RE = re.compile(r'\.(\d{14})\.grib2$')

# Both currently-active WMO headers carry TPW; content (not header) decides
# which of these buckets a given file lands in -- see module docstring.
KNOWN_PRODUCTS = [
    'tpw_conus_west', 'tpw_conus_east',
    'tpw_full_west',  'tpw_full_east',
    'tpw_meso_west',  'tpw_meso_east',
]

SCENE_TO_BUCKET = {'CONUS': 'conus', 'Full Disk': 'full', 'Mesoscale': 'meso'}
SLOT_TO_SIDE = {'GOES-West': 'west', 'GOES-East': 'east'}

# Fixed target extent for CONUS-bucket reprojection -- see the comment at its
# use site for why this can't be auto-detected. Matches satellite_cache_updater.py's
# MOSAIC_EXTENT for consistency. Native TPW CONUS resolution is ~10km (~0.09
# deg); 0.05 deg/pixel mildly oversamples for smoother tiles.
CONUS_EXTENT = (-130.0, 20.0, -60.0, 55.0)  # left, bottom, right, top
CONUS_RES    = 0.05  # degrees/pixel

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('tpw_cache')


def load_json_set(path: Path) -> set:
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_json_set(path: Path, values: set):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(sorted(values), f)
    tmp.replace(path)


def load_bad_files() -> dict:
    try:
        with open(BAD_FILE_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_bad_files(bad: dict, cutoff: datetime.datetime):
    pruned = {
        path: ts for path, ts in bad.items()
        if datetime.datetime.fromisoformat(ts) >= cutoff
    }
    tmp = BAD_FILE_CACHE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(pruned, f)
    tmp.replace(BAD_FILE_CACHE)


def receipt_timestamp(fname: str) -> datetime.datetime | None:
    m = RECEIPT_TS_RE.search(fname)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), '%Y%m%d%H%M%S')
    except ValueError:
        return None


def collect_source_files(cutoff: datetime.datetime) -> list[Path]:
    if not LDM_DIR.is_dir():
        return []
    results = []
    for f in LDM_DIR.glob('IXTO*.grib2'):
        ts = receipt_timestamp(f.name)
        if ts is None or ts < cutoff:
            continue
        results.append(f)
    results.sort()
    return results


# ── WMO-header strip + TPW decode + reprojection (child process) ─────────────

def _convert_worker(src_path: str, cache_dir: str, result_path: str):
    """
    Runs in an isolated subprocess. Writes 'OK:{product}' or an error message
    to result_path -- see satellite_cache_updater.py's identical rationale
    for why this needs subprocess isolation rather than a try/except.

    tmp_nc is anchored directly under cache_dir (writable by www-data), NOT
    next to src_path -- the raw LDM source directory is owned by ldm and not
    writable by the www-data user this runs as. Which product bucket the
    output belongs to isn't known until the file's own metadata is read, so
    it can't be decided by the parent ahead of time either.
    """
    tmp_nc = os.path.join(cache_dir, Path(src_path).name + '.strip.nc')
    try:
        import netCDF4 as nc
        import rasterio
        from rasterio.crs import CRS
        from rasterio.transform import Affine
        from rasterio.warp import calculate_default_transform, reproject, Resampling

        raw = open(src_path, 'rb').read()
        idx = raw.find(b'\x89HDF')
        if idx < 0:
            raise ValueError('no HDF5 magic found in payload')
        with open(tmp_nc, 'wb') as f:
            f.write(raw[idx:])

        ds = nc.Dataset(tmp_nc)

        scene_bucket = SCENE_TO_BUCKET.get(ds.scene_id)
        side = SLOT_TO_SIDE.get(ds.orbital_slot)
        if scene_bucket is None or side is None:
            raise ValueError(f'unrecognized scene_id={ds.scene_id!r} orbital_slot={ds.orbital_slot!r}')
        product = f'tpw_{scene_bucket}_{side}'
        prod_dir = os.path.join(cache_dir, product)
        os.makedirs(prod_dir, exist_ok=True)

        # Scene time from the product's own metadata -- the WMO bulletin/
        # filename timestamp is only when this landed on our feed
        t_start = ds.time_coverage_start
        scene_dt = datetime.datetime.strptime(t_start[:19], '%Y-%m-%dT%H:%M:%S')

        # netCDF4 auto-applies scale_factor/add_offset and auto-masks
        # _FillValue/valid_range for us (default auto_mask+auto_scale) --
        # var[:] already comes back in mm as a masked array, no manual
        # unpacking needed (re-applying scale_factor here would silently
        # double-scale the data -- caught that the hard way).
        var = ds.variables['TPW']
        tpw = np.ma.filled(var[:].astype(np.float64), np.nan).astype(np.float32)

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
        if scene_bucket == 'conus':
            # GOES-West's CONUS sector is oblique enough that its true
            # footprint's NW corner sits near +176E while its SW corner
            # sits near -161W -- i.e. it straddles the antimeridian.
            # calculate_default_transform()'s auto-extent doesn't handle
            # that (observed firsthand: it produced a near-global-width
            # raster that was >95% empty). Both sides' CONUS sectors are
            # meteorologically useful only within the actual CONUS box
            # anyway, so just target a fixed extent instead of guessing.
            import rasterio.transform
            left, bottom, right, top = CONUS_EXTENT
            width  = int(round((right - left) / CONUS_RES))
            height = int(round((top - bottom) / CONUS_RES))
            dst_transform = rasterio.transform.from_bounds(left, bottom, right, top, width, height)
        else:
            dst_transform, width, height = calculate_default_transform(
                src_crs, dst_crs, tpw.shape[1], tpw.shape[0],
                left=x.min(), right=x.max(), bottom=y.min(), top=y.max(),
            )

        dst = np.full((height, width), np.nan, dtype=np.float32)
        reproject(
            source=tpw, destination=dst,
            src_transform=src_transform, src_crs=src_crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

        ts_str = scene_dt.strftime('%Y%m%d-%H%M%S')
        dest_path = os.path.join(prod_dir, f'{product}_{ts_str}.tif')
        tmp_tif = dest_path + '.tmp'
        with rasterio.open(
            tmp_tif, 'w', driver='GTiff',
            height=height, width=width, count=1, dtype='float32',
            crs=dst_crs, transform=dst_transform, nodata=np.nan,
            compress='deflate',
        ) as out:
            out.write(dst, 1)
        os.replace(tmp_tif, dest_path)

        with open(result_path, 'w') as f:
            f.write(f'OK:{product}')
    except Exception as e:
        with open(result_path, 'w') as f:
            f.write(f'ERROR: {e}')
    finally:
        try:
            os.unlink(tmp_nc)
        except OSError:
            pass


def convert_one(src: Path) -> tuple[bool, str]:
    """Returns (ok, product_or_error_message)."""
    result_path = str(CACHE_DIR / (src.name + '.result'))
    proc = multiprocessing.Process(
        target=_convert_worker,
        args=(str(src), str(CACHE_DIR), result_path)
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
    if msg.startswith('OK:'):
        return True, msg[3:]
    return False, msg


# ── Per-product post-processing (prune / current symlink / mapcache sync) ────

def sync_mapcache_dims(product: str, tif_files: list[Path]):
    """Keep MapCache's sqlite dimension table in sync -- see mrms_cache_updater.py."""
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


def parse_timestamp_output(fname: str, product: str) -> datetime.datetime | None:
    m = re.search(rf'{re.escape(product)}_(\d{{8}}-\d{{6}})\.tif$', fname)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S')
    except ValueError:
        return None


def finalize_product(product: str, cutoff: datetime.datetime):
    prod_dir = CACHE_DIR / product
    cached = sorted(prod_dir.glob(f'{product}_*.tif')) if prod_dir.is_dir() else []
    if not cached:
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


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=RETAIN_HOURS)
    bad_files = load_bad_files()
    processed = load_json_set(PROCESSED_FILE_CACHE)

    # Raw source files are scoured off disk after 1 day (see scour.conf) --
    # drop any processed/bad-file entries pointing at files that no longer
    # exist so these sets don't grow forever
    processed = {p for p in processed if os.path.exists(p)}

    sources = collect_source_files(cutoff)
    added_by_product = {}
    skipped_bad = skipped_done = 0

    for src in sources:
        src_key = str(src)
        if src_key in processed:
            skipped_done += 1
            continue
        if src_key in bad_files:
            skipped_bad += 1
            continue
        ok, info = convert_one(src)
        if not ok:
            log.error(f'{src.name}: {info}')
            if 'timed out' not in info:
                bad_files[src_key] = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()
            continue
        processed.add(src_key)
        added_by_product[info] = added_by_product.get(info, 0) + 1

    for product, n in added_by_product.items():
        log.info(f'{product}: added {n} new frames')
    if skipped_bad:
        log.info(f'skipped {skipped_bad} known-bad source files')
    if not sources:
        log.warning(f'no source files found since {cutoff:%Y%m%d-%H%M%S}')

    save_bad_files(bad_files, cutoff)
    save_json_set(PROCESSED_FILE_CACHE, processed)

    for product in KNOWN_PRODUCTS:
        try:
            finalize_product(product, cutoff)
        except Exception as e:
            log.error(f'{product}: unhandled exception during finalize: {e}', exc_info=True)


if __name__ == '__main__':
    main()
