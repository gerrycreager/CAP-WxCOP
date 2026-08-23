#!/var/www/cap_winds_app/venv/bin/python3
"""
render_hrrr_particles.py — Pre-render HRRR wind vectors as leaflet-velocity /
wind-js grid JSON, for the animated wind-particle layer (same format/consumer
as render_wind_particles.py's ECMWF IFS/AIFS output — see wind_particles_api.py).
Feeds the Enhanced Weather Map's animated wind layer only. Separate from, and
does not replace, render_hrrr_winds.py (the static wind-barb GeoJSON renderer
that still feeds Flight Ops Impacts via hrrr_winds_api.py) — the Enhanced
Weather Map's own static HRRR wind-barb layer was removed and replaced by
this animated one; Flight Ops Impacts' independent copy was left as-is.

Runs hourly via cron on r815. Reads HRRR GRIB2 surface files from
/var/www/cap_winds_app/model_data/hrrr.{date}/{cycle}z/ (fetch_models.sh's
AWS S3 pull — NOT /LDM/models/hrrr, which nothing actually writes to).
Writes to /LDM/models/wind_particles/hrrr/{date}/{cycle}z/, alongside the
ecmwf-ifs/ecmwf-aifs dirs, so wind_particles_api.py serves it identically —
just add 'hrrr' to that module's VALID_SOURCES.

Regridding: HRRR's native grid is Lambert Conformal (curvilinear — 2D lat/lon
arrays), but wind-js requires a regular lat/lon grid. Nearest-neighbor
regridding onto a 0.25deg regular grid (matching GFS/ECMWF's resolution) via
a KDTree built once per cycle and reused across all FHR/level renders — HRRR's
~3km native spacing is far finer than the 0.25deg (~25km) target, so nearest-
neighbor is a fine approximation (no visible benefit from linear/cubic here)
and is orders of magnitude cheaper than re-triangulating per level/FHR. This
approximation is part of why HRRR is labeled "best effort" in the UI, not a
precise decision-support product.

Levels: SFC 850 700 500, plus synthetic DLM (vector mean of 850/700/500 —
matches render_wind_particles.py's ECMWF DLM exactly, for cross-source
comparability). No 200 hPa: HRRR's native isobaric levels top out at 250 hPa
with no 200 hPa product, so that combination is simply absent for source=hrrr
(the API 404s it — the frontend already handles a missing level/fhr combo).

FHR: 000-012 (fetch_models.sh's S3 pull only goes to F12) — short-range only,
unlike the 120h ECMWF sources; this is near-term convective-scale guidance,
not synoptic-scale, so extending it further out wouldn't mean anything even
if the source data went further.

Usage:
  render_hrrr_particles.py              # auto-detect and render latest cycle
  render_hrrr_particles.py --force      # re-render even if output exists
  render_hrrr_particles.py --cycle 15z  # render a specific cycle

Parallelization: each forecast hour is rendered in its own thread (the KDTree
is built once beforehand and only queried, not mutated, by each thread — safe
to share). 13 FHRs run concurrently (I/O-bound, cfgrib reads dominate).
"""
import os
import sys
import glob
import json
import shutil
import logging
import argparse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────
HRRR_BASE   = '/var/www/cap_winds_app/model_data'   # fetch_models.sh (AWS S3), not LDM
OUTPUT_BASE = '/LDM/models/wind_particles/hrrr'
MAX_CYCLES  = 3          # number of cycle dirs to keep on disk
MAX_WORKERS = 6          # parallel FHR workers — I/O bound, limit NFS load
LOG_FILE    = '/home/ldm/var/logs/hrrr_particles_render.log'

# Same CONUS domain the old streamline layer used, regridded to 0.25deg —
# matches GFS/ECMWF's wind-particle resolution for visual consistency.
LAT_MIN, LAT_MAX = 20.0, 53.0
LON_MIN, LON_MAX = -130.0, -60.0
GRID_RES = 0.25

DIRECT_LEVELS  = ['SFC', '850', '700', '500']
DLM_COMPONENTS = ['850', '700', '500']
ALL_LEVELS     = DIRECT_LEVELS + ['DLM']
LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    '850': '850 hPa',
    '700': '700 hPa',
    '500': '500 hPa',
    'DLM': 'Deep-Layer Mean (850-700-500 hPa steering flow)',
}

# ── Logging ────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


# ── GRIB2 extraction ───────────────────────────────────────────────────────
# heightAboveGround/isobaricInhPa coords aren't reliably 0-d scalars -- some
# HRRR surface datasets bundle multiple heights (e.g. 10m + 80m) in one
# dataset, same issue render_wind_particles.py hit for GFS. np.atleast_1d
# handles both the scalar-coord and array-coord cases uniformly.
def get_sfc_winds(datasets, np):
    """Extract 10m U/V from open datasets list."""
    for ds in datasets:
        if 'heightAboveGround' not in ds.coords:
            continue
        heights = np.atleast_1d(ds.coords['heightAboveGround'].values)
        if not np.any(np.abs(heights - 10.0) <= 1):
            continue
        if 'u10' in ds.data_vars and 'v10' in ds.data_vars:
            lons = np.where(ds.longitude.values > 180,
                            ds.longitude.values - 360,
                            ds.longitude.values)
            return ds['u10'].values, ds['v10'].values, ds.latitude.values, lons
    raise ValueError('No 10m wind dataset found')


def get_pressure_winds(datasets, hpa, np):
    """Extract U/V at pressure level from open datasets list."""
    for ds in datasets:
        if 'isobaricInhPa' not in ds.coords:
            continue
        if 'u' not in ds.data_vars or 'v' not in ds.data_vars:
            continue
        levels = np.atleast_1d(ds.isobaricInhPa.values)
        is_multi = np.ndim(ds.isobaricInhPa.values) > 0
        for i, lv in enumerate(levels):
            if abs(float(lv) - float(hpa)) < 1:
                lons = np.where(ds.longitude.values > 180,
                                ds.longitude.values - 360,
                                ds.longitude.values)
                u = ds['u'].values[i] if is_multi else ds['u'].values
                v = ds['v'].values[i] if is_multi else ds['v'].values
                return u, v, ds.latitude.values, lons
    raise ValueError(f'No {hpa} hPa wind dataset found')


def extract_level(datasets, level_key, np):
    """Extract U/V for a named level key."""
    if level_key == 'SFC':
        return get_sfc_winds(datasets, np)
    return get_pressure_winds(datasets, int(level_key), np)


# ── Regridding: curvilinear (native Lambert Conformal) -> regular lat/lon ──
def build_target_grid(np):
    """Regular grid HRRR gets regridded onto. Descending lats (north to
    south), matching the GRIB scanMode convention build_windjs_grid() (and
    leaflet-velocity) expects."""
    lats = np.arange(LAT_MAX, LAT_MIN - GRID_RES / 2, -GRID_RES)
    lons = np.arange(LON_MIN, LON_MAX + GRID_RES / 2, GRID_RES)
    return lats, lons


def build_kdtree(lats_native, lons_native, np):
    """Built once per cycle from one grib file's native lat/lon (identical
    across all FHRs/levels for a fixed model domain) and reused for every
    regrid call — the expensive part (indexing ~1.9M native points) only
    needs to happen once, not per FHR/level."""
    from scipy.spatial import cKDTree
    pts = np.column_stack([lats_native.ravel(), lons_native.ravel()])
    return cKDTree(pts)


def regrid_nearest(u, v, tree, target_lats, target_lons, np, max_dist=0.15):
    """Nearest-neighbor regrid onto the target regular grid. Target points
    farther than max_dist (degrees) from any native point fall outside
    HRRR's actual (non-rectangular) coverage footprint -- zeroed rather than
    extrapolated from a distant edge point."""
    tlat_grid, tlon_grid = np.meshgrid(target_lats, target_lons, indexing='ij')
    query_pts = np.column_stack([tlat_grid.ravel(), tlon_grid.ravel()])
    dist, idx = tree.query(query_pts)
    u_flat = u.ravel()[idx]
    v_flat = v.ravel()[idx]
    outside = dist > max_dist
    u_flat[outside] = 0.0
    v_flat[outside] = 0.0
    ny, nx = len(target_lats), len(target_lons)
    return u_flat.reshape(ny, nx), v_flat.reshape(ny, nx)


# ── wind-js / leaflet-velocity format (mirrors render_wind_particles.py) ──
def build_windjs_grid(u, v, lats, lons, ref_time, fhr):
    ny, nx = u.shape
    dy = abs(float(lats[1] - lats[0])) if ny > 1 else GRID_RES
    dx = abs(float(lons[1] - lons[0])) if nx > 1 else GRID_RES
    la1, la2 = float(lats[0]), float(lats[-1])
    lo1, lo2 = float(lons[0]), float(lons[-1])

    def header(param_number):
        return {
            'parameterCategory': 2,   # momentum
            'parameterNumber':   param_number,  # 2=U-component, 3=V-component
            'la1': la1, 'lo1': lo1, 'la2': la2, 'lo2': lo2,
            'dx': dx, 'dy': dy, 'nx': nx, 'ny': ny,
            'scanMode': 0,
            'refTime': ref_time,
            'forecastTime': fhr,
        }

    u_flat = [round(float(x), 2) if _finite(x) else 0.0 for x in u.flatten()]
    v_flat = [round(float(x), 2) if _finite(x) else 0.0 for x in v.flatten()]
    return [
        {'header': header(2), 'data': u_flat},
        {'header': header(3), 'data': v_flat},
    ]


def _finite(x):
    import math
    return math.isfinite(x)


def write_index_atomic(index, out_dir):
    """Atomic temp+rename so a concurrent reader never sees a partial file."""
    index_file = out_dir / 'index.json'
    tmp = out_dir / 'index.json.tmp'
    with open(tmp, 'w') as f:
        json.dump(index, f, indent=2)
    tmp.rename(index_file)


# ── File discovery ─────────────────────────────────────────────────────────
def find_hrrr_file(date, cycle, fhr):
    """Return path to HRRR wrfsfc file or None."""
    fhr_str = f'{int(fhr):02d}'   # S3 HRRR files use 2-digit FHR (wrfsfcf00, not wrfsfcf000)
    cyc_str = cycle.replace('z', '')
    pattern = os.path.join(HRRR_BASE, f'hrrr.{date}', f'{cyc_str}z',
                           f'hrrr.t{cyc_str}z.wrfsfcf{fhr_str}.grib2')
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def find_latest_cycle():
    """Find the most recent complete HRRR cycle (F000+F012 present -- F012 is
    as far as fetch_models.sh's S3 pull goes; F018 never exists from this
    source, so requiring it (the original check) meant this never matched)."""
    today     = datetime.now(timezone.utc).strftime('%Y%m%d')
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
    for date in [today, yesterday]:
        date_dir = os.path.join(HRRR_BASE, f'hrrr.{date}')
        if not os.path.isdir(date_dir):
            continue
        cycles = sorted(os.listdir(date_dir), reverse=True)
        for cycle_dir in cycles:
            if not cycle_dir.endswith('z'):
                continue
            cyc = cycle_dir[:-1]  # strip trailing z
            f000 = find_hrrr_file(date, f'{cyc}z', 0)
            f012 = find_hrrr_file(date, f'{cyc}z', 12)
            if f000 and f012:
                return date, f'{cyc}z'
    return None, None


# ── Per-FHR render (called in thread) ──────────────────────────────────────
def render_fhr(fhr, date, cycle, out_dir, force, tree, target_lats, target_lons):
    """
    Render all levels (direct + synthetic DLM) for one forecast hour.
    Returns (fhr, index_entry_or_None, rendered_count, error_count).
    Called concurrently — must be thread-safe (tree/target grid are
    read-only here, safe to share across threads).
    """
    import cfgrib
    import numpy as np

    grib_file = find_hrrr_file(date, cycle, fhr)
    if not grib_file:
        log.warning(f'  F{fhr:03d}: GRIB2 file not found, skipping')
        return fhr, None, 0, 1

    try:
        # indexpath='' -- model_data/ is www-data-owned (fetch_models.sh),
        # ldm can't write a .idx sidecar there; skip the persistent index
        # rather than let cfgrib fail-and-retry in-memory on every open.
        datasets = cfgrib.open_datasets(grib_file, backend_kwargs={'indexpath': ''})
    except Exception as e:
        log.error(f'  F{fhr:03d}: open_datasets failed: {e}')
        return fhr, None, 0, 1

    ref_time = f'{date}T{cycle.replace("z", "")}:00:00Z'
    rendered  = 0
    errors    = 0
    dlm_parts = {}   # level -> (u_regrid, v_regrid) for DLM averaging

    for level in DIRECT_LEVELS:
        out_file = out_dir / f'particles_{level}_f{fhr:03d}.json'
        try:
            u, v, lats, lons = extract_level(datasets, level, np)
            u_r, v_r = regrid_nearest(u, v, tree, target_lats, target_lons, np)
            if level in DLM_COMPONENTS:
                dlm_parts[level] = (u_r, v_r)

            if out_file.exists() and not force:
                continue
            grid = build_windjs_grid(u_r, v_r, target_lats, target_lons, ref_time, fhr)
            tmp = out_file.with_suffix(f'.{fhr:03d}_{level}.tmp')
            with open(tmp, 'w') as f:
                json.dump(grid, f, separators=(',', ':'))
            tmp.rename(out_file)
            rendered += 1
            log.info(f'  F{fhr:03d} {level}: {u_r.shape} grid -> {out_file.name}')
        except Exception as e:
            log.error(f'  F{fhr:03d} {level}: {e}')
            errors += 1

    if len(dlm_parts) == len(DLM_COMPONENTS):
        try:
            out_file = out_dir / f'particles_DLM_f{fhr:03d}.json'
            if force or not out_file.exists():
                u_stack = np.stack([dlm_parts[lv][0] for lv in DLM_COMPONENTS])
                v_stack = np.stack([dlm_parts[lv][1] for lv in DLM_COMPONENTS])
                u_dlm = u_stack.mean(axis=0)
                v_dlm = v_stack.mean(axis=0)
                grid = build_windjs_grid(u_dlm, v_dlm, target_lats, target_lons, ref_time, fhr)
                tmp = out_file.with_suffix(f'.{fhr:03d}_DLM.tmp')
                with open(tmp, 'w') as f:
                    json.dump(grid, f, separators=(',', ':'))
                tmp.rename(out_file)
                rendered += 1
                log.info(f'  F{fhr:03d} DLM: vector mean of {DLM_COMPONENTS} -> {out_file.name}')
        except Exception as e:
            log.error(f'  F{fhr:03d} DLM: {e}')
            errors += 1
    elif dlm_parts:
        log.warning(f'  F{fhr:03d}: DLM skipped, only got {list(dlm_parts.keys())}')

    for ds in datasets:
        try:
            ds.close()
        except Exception:
            pass

    # Present-on-disk, not just-rendered-this-run -- see render_wind_particles.py's
    # identical comment: using `rendered` here would drop already-valid fhrs
    # from the index on every run that finds them already cached.
    all_present = all((out_dir / f'particles_{lv}_f{fhr:03d}.json').exists() for lv in ALL_LEVELS)
    index_entry = None
    if all_present:
        index_entry = {
            'fhr':   fhr,
            'files': {lv: f'particles_{lv}_f{fhr:03d}.json' for lv in ALL_LEVELS}
        }
    return fhr, index_entry, rendered, errors


# ── Rendering ──────────────────────────────────────────────────────────────
def render_cycle(date, cycle, force=False):
    """Render all levels and forecast hours for one HRRR cycle in parallel."""
    import cfgrib
    import numpy as np

    out_dir = Path(OUTPUT_BASE) / date / cycle
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f'Rendering cycle {date} {cycle} -> {out_dir}')
    log.info(f'Parallel workers: {MAX_WORKERS}')

    target_lats, target_lons = build_target_grid(np)

    # Build the KDTree once from F000's native grid -- identical across all
    # FHRs/levels for this fixed model domain, so every thread below just
    # queries this, never rebuilds it.
    f000 = find_hrrr_file(date, cycle, 0)
    if not f000:
        log.error('F000 file not found -- cannot build regrid tree')
        return 0, 1
    ds0 = cfgrib.open_datasets(f000, backend_kwargs={'indexpath': ''})
    try:
        _, _, lats_native, lons_native = get_sfc_winds(ds0, np)
    finally:
        for ds in ds0:
            try:
                ds.close()
            except Exception:
                pass
    tree = build_kdtree(lats_native, lons_native, np)
    log.info(f'Regrid tree built: {lats_native.size} native points -> '
             f'{len(target_lats)}x{len(target_lons)} target grid')

    total_rendered = 0
    total_errors   = 0
    index_entries  = {}

    fhrs = list(range(0, 13))   # fetch_models.sh only pulls F00-F12 from S3

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(render_fhr, fhr, date, cycle, out_dir, force,
                             tree, target_lats, target_lons): fhr
            for fhr in fhrs
        }
        for future in as_completed(futures):
            fhr = futures[future]
            try:
                fhr_result, idx_entry, rendered, errors = future.result()
                total_rendered += rendered
                total_errors   += errors
                if idx_entry:
                    index_entries[fhr_result] = idx_entry
            except Exception as e:
                log.error(f'F{fhr:03d} worker exception: {e}')
                total_errors += 1

    index = {
        'model':    'HRRR',
        'cycle':    f'{date} {cycle}',
        'date':     date,
        'domain':   {'lat_min': LAT_MIN, 'lat_max': LAT_MAX, 'lon_min': LON_MIN, 'lon_max': LON_MAX},
        'levels':   [{'key': k, 'label': LEVEL_LABELS[k]} for k in ALL_LEVELS],
        'fhrs':     [index_entries[k] for k in sorted(index_entries.keys())],
        'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    write_index_atomic(index, out_dir)

    latest_link = Path(OUTPUT_BASE) / 'latest'
    tmp_link    = Path(OUTPUT_BASE) / 'latest.tmp'
    if tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(out_dir)
    tmp_link.rename(latest_link)
    log.info(f'latest -> {out_dir}')

    log.info(f'Cycle {date} {cycle}: rendered={total_rendered} errors={total_errors}')
    return total_rendered, total_errors


def scour_old_cycles():
    """Remove cycle dirs older than MAX_CYCLES."""
    base = Path(OUTPUT_BASE)
    if not base.exists():
        return
    dirs = sorted([
        d for d in base.glob('*/*z')
        if d.is_dir() and d.name.endswith('z')
    ])
    to_remove = dirs[:-MAX_CYCLES] if len(dirs) > MAX_CYCLES else []
    for d in to_remove:
        log.info(f'Scouring old cycle: {d}')
        shutil.rmtree(d, ignore_errors=True)
        try:
            d.parent.rmdir()
        except OSError:
            pass


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    global MAX_WORKERS
    parser = argparse.ArgumentParser(description='Pre-render HRRR wind-js grids for animated wind')
    parser.add_argument('--force',  action='store_true',
                        help='Re-render even if output files exist')
    parser.add_argument('--cycle',  type=str, default=None,
                        help='Specific cycle to render, e.g. 15z')
    parser.add_argument('--date',   type=str, default=None,
                        help='Date for specific cycle, e.g. 20260519 (default: today)')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS,
                        help=f'Parallel workers (default: {MAX_WORKERS})')
    args = parser.parse_args()
    MAX_WORKERS = args.workers

    log.info('=' * 60)
    log.info('HRRR wind render started')

    if args.cycle:
        cycle = args.cycle if args.cycle.endswith('z') else args.cycle + 'z'
        date  = args.date or datetime.now(timezone.utc).strftime('%Y%m%d')
    else:
        date, cycle = find_latest_cycle()
        if not date:
            log.error('No complete HRRR cycle found')
            sys.exit(1)

    log.info(f'Rendering cycle: {date} {cycle}')
    rendered, errors = render_cycle(date, cycle, force=args.force)
    scour_old_cycles()

    log.info(f'Done: {rendered} files rendered, {errors} errors')
    log.info('=' * 60)
    sys.exit(0 if errors == 0 else 1)


if __name__ == '__main__':
    main()
