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

Reads HRRR GRIB2 surface files from /LDM/models/hrrr/{date}/{cycle}z/
(NFS-shared, LDM-fed from data1). Pointed at
/var/www/cap_winds_app/model_data/ (fetch_models.sh's AWS S3 pull) from
2026-08-12 to 2026-09-20 because the LDM path wasn't reliably populated at
the time; re-verified 2026-09-20 that it now is (see render_hrrr_winds.py's
docstring for the same finding) and reverted, same reasoning: NFS-visible
from any host, not tied to r815's local S3 mirror.
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

Levels: SFC AGL1000 850 700 500, plus synthetic DLM (vector mean of
850/700/500 — matches render_wind_particles.py's ECMWF DLM). No 200 hPa:
HRRR's native isobaric levels top out at 250 hPa with no 200 hPa product,
so that combination is simply absent for source=hrrr (the API 404s it —
the frontend already handles a missing level/fhr combo).

AGL1000 (added 2026-10-11): same feature, same implementation approach as
render_hrrr_winds.py's Streamlines AGL1000 level (true 1,000 ft AGL,
terrain-relative — see that module's docstring for the full design
rationale). Under MODEL_SOURCE='hrrr' (the live default here too, same
WIND_MODEL_SOURCE=hrrr cron pin), it's a per-gridpoint vertical
interpolation to orog(point) + 304.8m from 10m/80m AGL + 1000/850/700/
500mb isobaric wind/height; under 'rrfs' it's RRFS's native 320m AGL
field. Computed on the native curvilinear grid *before* regrid_nearest()
— same as every other level here.

DLM was commented out 2026-09-19 (reasoning: this domain never reached the
Tropical Atlantic, so computing it was pure waste) and re-enabled
2026-10-08 at Gerry's direction for Hurricane Isaias in the Gulf of
Mexico, which — unlike the deep tropical Atlantic — sits well inside this
script's CONUS domain (LAT/LON_MIN/MAX below cover 20-53N, 130-60W).

FHR: 000-012 — short-range only, unlike the 120h ECMWF sources; this is
near-term convective-scale guidance, not synoptic-scale, so extending it
further out wouldn't add much even though /LDM/models/hrrr now reliably
has more (F018 always, F048 on synoptic cycles) -- deliberate choice, not
a data-availability limit anymore. Revisit if that judgment changes.

Usage:
  render_hrrr_particles.py              # auto-detect and render latest cycle
  render_hrrr_particles.py --force      # re-render even if output exists
  render_hrrr_particles.py --cycle 15z  # render a specific cycle

Parallelization: each forecast hour is rendered in its own thread (the KDTree
is built once beforehand and only queried, not mutated, by each thread — safe
to share). 13 FHRs run concurrently (I/O-bound, cfgrib reads dominate).

Model source (HRRR->RRFS migration, 2026-09-21): set via MODEL_SOURCE below
(or the WIND_MODEL_SOURCE env var) -- 'rrfs' is the default per the Phase 2
measurement's GO decision (see the plan file), 'hrrr' remains a one-line
fallback. RRFS's 2dfld product has NO isobaric-level winds at all (confirmed
2026-09-21) -- only SFC/AGL heights -- so the RRFS path pulls two files per
fhr via rrfs_wind_fetch.py: '2dfld' for the 10m SFC winds, 'prslev' for the
850/700/500mb isobaric winds this script needs. Their cfgrib dataset lists
are simply concatenated before extraction, so get_sfc_winds()/
get_pressure_winds() below work completely unchanged regardless of source.
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
HRRR_BASE     = '/LDM/models/hrrr'   # NFS-shared, LDM-fed -- see docstring history
OUTPUT_BASE   = '/LDM/models/wind_particles/hrrr'
MAX_CYCLES    = 3          # number of cycle dirs to keep on disk
MAX_WORKERS   = 6          # parallel FHR workers — I/O bound, limit NFS load
LOG_FILE      = '/home/ldm/var/logs/hrrr_particles_render.log'
MODEL_SOURCE  = os.environ.get('WIND_MODEL_SOURCE', 'rrfs')  # 'rrfs' (default) or 'hrrr'

# Same CONUS domain the old streamline layer used, regridded to 0.25deg —
# matches GFS/ECMWF's wind-particle resolution for visual consistency.
LAT_MIN, LAT_MAX = 20.0, 53.0
LON_MIN, LON_MAX = -130.0, -60.0
GRID_RES = 0.25

DIRECT_LEVELS  = ['SFC', 'AGL1000', '850', '700', '500']
DLM_COMPONENTS = ['850', '700', '500']
# DLM re-enabled 2026-10-08 (Hurricane Isaias, Gulf of Mexico) -- was
# commented out 2026-09-19 when this domain's only live source (CONUS
# HRRR/RRFS) didn't reach anywhere TC steering flow mattered. If dropped
# again, re-comment this line plus the DLM block in render_fhr() below and
# the matching entries in wind_particles_api.py and
# enhanced_weather_map_complete.html.
ALL_LEVELS     = DIRECT_LEVELS + ['DLM']
LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    'AGL1000': '1,000 ft AGL (terrain-relative)',
    '850': '850 hPa',
    '700': '700 hPa',
    '500': '500 hPa',
    'DLM': 'Deep-Layer Mean (850-700-500 hPa steering flow)',
}

# AGL1000 (added 2026-10-11, same feature as render_hrrr_winds.py's
# Streamlines layer -- see that module's docstring for the full design
# rationale). HRRR ('hrrr', the live source here too -- MODEL_SOURCE below
# defaults to 'rrfs' but the actual cron pins WIND_MODEL_SOURCE=hrrr):
# vertically interpolated per-gridpoint to orog(point) + 304.8m (true
# 1,000 ft AGL) from a 6-level profile (10m/80m AGL wind + 1000/850/700/
# 500mb isobaric wind at that level's own geopotential height). RRFS
# ('rrfs'): native 2dfld 320m AGL field instead (~1,050 ft, the closest
# native RRFS height) -- kept working even though RRFS is parked (per
# Gerry 2026-10-11: failed NWS operational testing, HRRR stays live until
# RRFS v2/MPAS, ~2027) in case that changes.
AGL1000_HEIGHT_M       = 320                          # RRFS native-field path only
AGL1000_TARGET_M       = 1000 * 0.3048                # 304.8 -- true target, HRRR path
AGL1000_ISOBARIC_MB_GH = [1000, 850, 700, 500]  # mb levels HRRR's wrfsfc carries a 'gh' field for

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


def get_fixed_agl_winds(datasets, height_m, np, tol=1.0):
    """Extract U/V at a fixed non-10m height-above-ground level (e.g.
    RRFS's native 320m AGL field used for AGL1000's RRFS path). These
    levels' GRIB shortNames are plain 'u'/'v' (not '10u'/'10v'), matching
    get_pressure_winds()'s isobaric lookup pattern rather than
    get_sfc_winds()'s."""
    for ds in datasets:
        if 'heightAboveGround' not in ds.coords:
            continue
        if 'u' not in ds.data_vars or 'v' not in ds.data_vars:
            continue
        heights = np.atleast_1d(ds.coords['heightAboveGround'].values)
        is_multi = np.ndim(ds.coords['heightAboveGround'].values) > 0
        for i, h in enumerate(heights):
            if abs(float(h) - height_m) <= tol:
                lons = np.where(ds.longitude.values > 180,
                                ds.longitude.values - 360,
                                ds.longitude.values)
                u = ds['u'].values[i] if is_multi else ds['u'].values
                v = ds['v'].values[i] if is_multi else ds['v'].values
                return u, v, ds.latitude.values, lons
    raise ValueError(f'No {height_m}m AGL wind dataset found')


def get_orog(datasets, np):
    """Extract surface elevation (orography, MSL meters) -- the
    per-gridpoint terrain height AGL1000's HRRR path needs to turn
    '1,000 ft AGL' into an actual target MSL height at every point,
    instead of a flat band that would be wrong over mountainous terrain."""
    for ds in datasets:
        if 'orog' in ds.data_vars:
            return ds['orog'].values
    raise ValueError('No orography (surface elevation) field found')


def get_gh_profile(datasets, mbs, np):
    """Return {mb: gh_array} for whichever of `mbs` have a matching
    geopotential-height field in this file. HRRR's wrfsfc 'gh' isobaric
    dataset only carries 1000/850/700/500mb -- 925mb has u/v but no
    matching gh, so it's left out of AGL1000's HRRR profile entirely
    (there is no 925mb level in this script anyway)."""
    out = {}
    for ds in datasets:
        if 'isobaricInhPa' not in ds.coords or 'gh' not in ds.data_vars:
            continue
        levels = np.atleast_1d(ds.isobaricInhPa.values)
        is_multi = np.ndim(ds.isobaricInhPa.values) > 0
        for i, lv in enumerate(levels):
            mb = int(round(float(lv)))
            if mb in mbs:
                out[mb] = ds['gh'].values[i] if is_multi else ds['gh'].values
    return out


def interp_profile_to_target(heights_stack, u_stack, v_stack, target, np):
    """Per-gridpoint linear interpolation of U/V to `target` (MSL meters,
    same shape as one level's grid) from an arbitrary vertical profile
    stacked as (n_levels, ny, nx) arrays. Levels are sorted by height
    independently at every gridpoint (gh order can locally invert near
    steep terrain), so this isn't just "pick the two nearest rows."
    Clamps to the nearest level's wind when target falls outside the
    profile's range rather than extrapolating."""
    order    = np.argsort(heights_stack, axis=0)
    h_sorted = np.take_along_axis(heights_stack, order, axis=0)
    u_sorted = np.take_along_axis(u_stack, order, axis=0)
    v_sorted = np.take_along_axis(v_stack, order, axis=0)

    n = h_sorted.shape[0]
    ge      = h_sorted >= target[None, ...]
    idx_hi  = np.argmax(ge, axis=0)          # first level >= target (per point)
    idx_hi  = np.where(ge.any(axis=0), idx_hi, n - 1)   # target above every level -> clamp high
    idx_hi  = np.clip(idx_hi, 1, n - 1)
    idx_lo  = idx_hi - 1

    h_lo = np.take_along_axis(h_sorted, idx_lo[None, ...], axis=0)[0]
    h_hi = np.take_along_axis(h_sorted, idx_hi[None, ...], axis=0)[0]
    u_lo = np.take_along_axis(u_sorted, idx_lo[None, ...], axis=0)[0]
    u_hi = np.take_along_axis(u_sorted, idx_hi[None, ...], axis=0)[0]
    v_lo = np.take_along_axis(v_sorted, idx_lo[None, ...], axis=0)[0]
    v_hi = np.take_along_axis(v_sorted, idx_hi[None, ...], axis=0)[0]

    denom = h_hi - h_lo
    denom = np.where(denom == 0, 1.0, denom)
    w = np.clip((target - h_lo) / denom, 0.0, 1.0)   # clip handles both clamp cases
    return u_lo + w * (u_hi - u_lo), v_lo + w * (v_hi - v_lo)


def get_agl1000_winds_hrrr(datasets, np):
    """AGL1000 for MODEL_SOURCE='hrrr': vertically interpolates U/V to
    orog(point) + 304.8m (true 1,000 ft AGL, terrain-relative) from a
    6-level profile: 10m/80m AGL wind (height = orog + 10/80) and
    1000/850/700/500mb isobaric wind at that level's own 'gh' height.
    Same approach as render_hrrr_winds.py's AGL1000 -- see that module's
    docstring for the full rationale."""
    orog = get_orog(datasets, np)
    u10, v10, lats, lons = get_sfc_winds(datasets, np)
    u80, v80, _, _        = get_fixed_agl_winds(datasets, 80.0, np)

    gh = get_gh_profile(datasets, AGL1000_ISOBARIC_MB_GH, np)
    missing = [mb for mb in AGL1000_ISOBARIC_MB_GH if mb not in gh]
    if missing:
        raise ValueError(f'AGL1000 (HRRR): missing gh field(s) for {missing} mb')

    heights = [orog + 10.0, orog + 80.0]
    us      = [u10, u80]
    vs      = [v10, v80]
    for mb in AGL1000_ISOBARIC_MB_GH:
        u_mb, v_mb, _, _ = get_pressure_winds(datasets, mb, np)
        heights.append(gh[mb])
        us.append(u_mb)
        vs.append(v_mb)

    target = orog + AGL1000_TARGET_M
    u_t, v_t = interp_profile_to_target(np.stack(heights), np.stack(us), np.stack(vs), target, np)
    return u_t, v_t, lats, lons


def extract_level(datasets, level_key, np, model_source=None):
    """Extract U/V for a named level key."""
    if level_key == 'SFC':
        return get_sfc_winds(datasets, np)
    elif level_key == 'AGL1000':
        if model_source == 'rrfs':
            return get_fixed_agl_winds(datasets, AGL1000_HEIGHT_M, np)
        return get_agl1000_winds_hrrr(datasets, np)
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
    fhr_str = f'{int(fhr):03d}'   # LDM-fed files use 3-digit FHR (wrfsfcf000)
    cyc_str = cycle.replace('z', '')
    pattern = os.path.join(HRRR_BASE, f'hrrr.{date}', f'{cyc_str}z',
                           f'hrrr.t{cyc_str}z.wrfsfcf{fhr_str}.grib2')
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def find_latest_cycle():
    """Find the most recent complete HRRR cycle (F000+F012 present -- F012 is
    as far as this script's deliberately-short render range goes; see the
    FHR note in the module docstring)."""
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


# ── RRFS file fetch (mixed-source: 2dfld for SFC, prslev for isobaric) ─────
# Explicit literal, not derived from DIRECT_LEVELS -- that list now also
# contains 'AGL1000', which isn't an mb-level string.
RRFS_ISOBARIC_MB = [850, 700, 500]


def find_rrfs_files(date, cycle_num, fhr):
    """Fetch-and-cache this fhr's 2dfld (SFC 10m + AGL1000's 320m AGL) and
    prslev (isobaric) curated subsets via rrfs_wind_fetch.py. Returns
    (sfc_path, prslev_path); either may be None if that product/fhr isn't
    posted yet."""
    import rrfs_wind_fetch as rwf

    sfc_wanted = {('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
                  ('UGRD', f'{AGL1000_HEIGHT_M} m above ground'),
                  ('VGRD', f'{AGL1000_HEIGHT_M} m above ground')}
    sfc_path = rwf.fetch_and_cache('2dfld', date, cycle_num, fhr, sfc_wanted, tag='sfc10m320m')

    iso_wanted = set()
    for mb in RRFS_ISOBARIC_MB:
        iso_wanted.add(('UGRD', f'{mb} mb'))
        iso_wanted.add(('VGRD', f'{mb} mb'))
    prslev_path = rwf.fetch_and_cache('prslev', date, cycle_num, fhr, iso_wanted, tag='iso3lvl')

    return sfc_path, prslev_path


def find_latest_rrfs_cycle():
    import rrfs_wind_fetch as rwf
    date, cyc = rwf.find_ready_cycle(product='2dfld')
    if not date:
        return None, None
    return date, f'{cyc}z'


def open_rrfs_datasets(date, cycle_num, fhr):
    """Fetch both RRFS files for this fhr and return one concatenated
    cfgrib dataset list -- get_sfc_winds()/get_pressure_winds() search a
    plain list of datasets already, so this needs no changes there."""
    import cfgrib
    sfc_path, prslev_path = find_rrfs_files(date, cycle_num, fhr)
    if not sfc_path:
        return None
    datasets = list(cfgrib.open_datasets(str(sfc_path), backend_kwargs={'indexpath': ''}))
    if prslev_path:
        datasets += list(cfgrib.open_datasets(str(prslev_path), backend_kwargs={'indexpath': ''}))
    else:
        log.warning('  F%03d: RRFS prslev not available, isobaric levels will be missing', fhr)
    return datasets


# ── Per-FHR render (called in thread) ──────────────────────────────────────
def render_fhr(fhr, date, cycle, out_dir, force, tree, target_lats, target_lons):
    """
    Render all direct levels for one forecast hour (synthetic DLM commented
    out 2026-09-19 -- see module docstring).
    Returns (fhr, index_entry_or_None, rendered_count, error_count).
    Called concurrently — must be thread-safe (tree/target grid are
    read-only here, safe to share across threads).
    """
    import cfgrib
    import numpy as np

    if MODEL_SOURCE == 'rrfs':
        cycle_num = cycle.replace('z', '')
        try:
            datasets = open_rrfs_datasets(date, cycle_num, fhr)
        except Exception as e:
            log.error(f'  F{fhr:03d}: RRFS fetch/open failed: {e}')
            return fhr, None, 0, 1
        if not datasets:
            log.warning(f'  F{fhr:03d}: RRFS data not posted yet, skipping')
            return fhr, None, 0, 1
    else:
        grib_file = find_hrrr_file(date, cycle, fhr)
        if not grib_file:
            log.warning(f'  F{fhr:03d}: GRIB2 file not found, skipping')
            return fhr, None, 0, 1

        try:
            # indexpath='' -- /LDM/models/hrrr is shared (NFS + multiple readers/
            # writers), so skip persisting a .idx sidecar there rather than risk
            # concurrent-write conflicts; in-memory indexing per open is fine.
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
            u, v, lats, lons = extract_level(datasets, level, np, model_source=MODEL_SOURCE)
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

    # DLM (Deep-Layer Mean) synthetic level -- re-enabled 2026-10-08, see
    # module docstring.
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
    if MODEL_SOURCE == 'rrfs':
        cycle_num = cycle.replace('z', '')
        ds0 = open_rrfs_datasets(date, cycle_num, 0)
        if not ds0:
            log.error('RRFS F000 not available -- cannot build regrid tree')
            return 0, 1
    else:
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

    fhrs = list(range(0, 13))   # deliberately short-range -- see FHR note in module docstring

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
        'model':    MODEL_SOURCE.upper(),
        'cycle':    f'{date} {cycle}',
        'date':     date,
        'domain':   {'lat_min': LAT_MIN, 'lat_max': LAT_MAX, 'lon_min': LON_MIN, 'lon_max': LON_MAX},
        'levels':   [{'key': k, 'label': LEVEL_LABELS[k]} for k in ALL_LEVELS],
        'fhrs':     [index_entries[k] for k in sorted(index_entries.keys())],
        'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    write_index_atomic(index, out_dir)

    if MODEL_SOURCE == 'rrfs':
        import rrfs_wind_fetch as rwf
        rwf.prune_cache()

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
    log.info(f'{MODEL_SOURCE.upper()} wind-particle render started')

    if args.cycle:
        cycle = args.cycle if args.cycle.endswith('z') else args.cycle + 'z'
        date  = args.date or datetime.now(timezone.utc).strftime('%Y%m%d')
    elif MODEL_SOURCE == 'rrfs':
        date, cycle = find_latest_rrfs_cycle()
        if not date:
            log.error('No ready RRFS cycle found within lookback window')
            sys.exit(1)
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
