#!/var/www/cap_winds_app/venv/bin/python3
"""
render_hrrr_winds.py — Pre-render HRRR wind vectors to static GeoJSON files.
Feeds the Flight Ops Impacts wind-barb layer (hrrr_winds_api.py, /api/winds/hrrr)
-- NOT the Enhanced Weather Map's animated wind particles, which is a separate
source (render_hrrr_particles.py) as of 2026-08-12.

Reads HRRR GRIB2 from /LDM/models/hrrr/ (NFS-shared, LDM-fed from data1).
Writes compact GeoJSON to /LDM/models/hrrr_winds/{date}/{cycle}z/

History: pointed at /var/www/cap_winds_app/model_data/ (fetch_models.sh's
AWS S3 pull) from 2026-08-12 to 2026-09-20, because /LDM/models/hrrr wasn't
reliably populated at the time. Re-verified 2026-09-20 that the LDM path is
now complete and fresh for every cycle (F000-F018, F000-F048 for the four
synoptic cycles, since fetch_hrrr_extended.py filled the F019+ gap) -- so
this reverts to the LDM path, which is NFS-visible from any host (unlike
the r815-local S3 mirror) and gives more forecast hours than the S3 pull's
F012 cap.

Output files:
  winds_{LEVEL}_f{FHR}.json   per level/fhr
  index.json                  manifest of available files + metadata
  latest -> symlink to newest cycle dir

Levels: SFC AGL1000 925 850 700 600 500
FHR:    000-018

AGL1000 (added 2026-10-11, Gerry's request): the standard CAP search/
aerial-photography mission altitude, 1,000 ft AGL (304.8m). This is a
genuinely AGL (terrain-relative) level, not an MSL band like 925-500mb
above -- "1,000 ft above the ground" means a different MSL height at a
sea-level airport than at a mile-high one, so a flat MSL approximation
would be wrong by exactly the amount that matters most (mountainous
terrain). Two implementations, chosen by MODEL_SOURCE:
  - RRFS ('rrfs'): native field -- 2dfld's 320m AGL level (~1,050 ft, the
    closest native RRFS height to 1,000 ft) via get_fixed_agl_winds().
  - HRRR ('hrrr', the live default as of 2026-10-11 -- see Model source
    note below): HRRR's wrfsfc file has no AGL field above 80m, so this
    is a real vertical interpolation, not a lookup: get_agl1000_winds_hrrr()
    builds a per-gridpoint height profile from the fields HRRR does carry
    (10m/80m AGL wind, orog for surface elevation, and 1000/850/700/500mb
    isobaric wind + geopotential height -- 925mb is skipped, it has no
    matching 'gh' field in this product) and linearly interpolates each
    point's U/V to target height = orog(point) + 304.8m, clamping to the
    nearest level rather than extrapolating where the target falls outside
    the profile (e.g. very low-lying terrain below the 10m anchor, or
    terrain so high the 500mb level is still below the target).

Usage:
  render_hrrr_winds.py              # auto-detect and render latest cycle
  render_hrrr_winds.py --force      # re-render even if output exists
  render_hrrr_winds.py --cycle 15z  # render a specific cycle

Parallelization: each forecast hour is rendered in its own thread.
19 FHRs run concurrently (I/O-bound, cfgrib reads dominate).

Model source: set via MODEL_SOURCE below (or the WIND_MODEL_SOURCE env
var). The code default here is 'rrfs' (left over from the 2026-09-21
HRRR->RRFS migration's Phase 2 GO decision), but production's actual
cron invocation pins WIND_MODEL_SOURCE=hrrr (confirmed live, both this
script and render_hrrr_particles.py) -- per Gerry 2026-10-11, RRFS v1
failed NWS operational testing and HRRR is staying the live source at
least until RRFS v2 (MPAS-based) is available, ~2027. Don't read the
'rrfs' default below as the live behavior; check the actual cron env var
if that matters. RRFS's 2dfld product has NO isobaric-level winds at all
(confirmed 2026-09-21) -- only SFC/AGL heights -- so the RRFS path pulls
two files per fhr via rrfs_wind_fetch.py: '2dfld' for the 10m SFC winds
(and, as of 2026-10-11, the 320m AGL winds AGL1000 uses), 'prslev' for
the 925/850/700/500mb isobaric winds this script needs (600mb stays a
synthetic log-pressure interpolation between prslev's 700/500,
unchanged). Their cfgrib dataset lists are simply concatenated before
extraction, so get_sfc_winds()/get_pressure_winds() below work completely
unchanged regardless of source.
"""
import os
import sys
import glob
import json
import math
import shutil
import logging
import argparse
import warnings
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────
HRRR_BASE     = '/LDM/models/hrrr'   # NFS-shared, LDM-fed -- see docstring history
OUTPUT_BASE   = '/LDM/models/hrrr_winds'
GRID_STEP     = 14        # subsample step — ~42km, ~700 pts CONUS
MAX_CYCLES    = 3         # number of cycle dirs to keep on disk
MAX_WORKERS   = 6         # parallel FHR workers — I/O bound, limit NFS load
LOG_FILE      = '/home/ldm/var/logs/hrrr_winds_render.log'
MODEL_SOURCE  = os.environ.get('WIND_MODEL_SOURCE', 'rrfs')  # 'rrfs' (default) or 'hrrr'

AGL1000_HEIGHT_M       = 320      # nearest native RRFS AGL field to 1,000 ft -- RRFS path only
AGL1000_TARGET_FT      = 1000
AGL1000_TARGET_M       = AGL1000_TARGET_FT * 0.3048   # 304.8 -- true target, HRRR path
AGL1000_ISOBARIC_MB_GH = [1000, 850, 700, 500]  # mb levels HRRR's wrfsfc carries a 'gh' field for

# AGL1000 now works under both sources (RRFS: native field; HRRR: vertical
# interpolation -- see module docstring), so it's unconditionally in LEVELS.
LEVELS = ['SFC', 'AGL1000', '925', '850', '700', '600', '500']
LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    'AGL1000': '1,000 ft AGL (terrain-relative)',
    '925': '~3,000 ft MSL',
    '850': '~6,000 ft MSL',
    '700': '~10,000 ft MSL',
    '600': '~12,000 ft MSL',
    '500': '~18,000 ft MSL',
}

# Log-pressure weights for 600 mb interpolation (~12,000 ft)
W700_600 = (math.log(700) - math.log(600)) / (math.log(700) - math.log(500))
W500_600 = 1.0 - W700_600

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


def get_fixed_agl_winds(datasets, height_m, np, tol=1.0):
    """Extract U/V at a fixed non-10m height-above-ground level (e.g.
    RRFS's native 320m AGL field used for AGL1000). Unlike the 10m SFC
    case (shortName '10u'/'10v' -> data_vars 'u10'/'v10'), these levels'
    GRIB shortNames are plain 'u'/'v', matching get_pressure_winds()'s
    isobaric lookup pattern -- same reason that function doesn't reuse
    get_sfc_winds() for the 80m level either."""
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


def get_orog(datasets, np):
    """Extract surface elevation (orography, MSL meters) -- the
    per-gridpoint terrain height AGL1000's HRRR path needs to turn
    '1,000 ft AGL' into an actual target MSL height at every point,
    instead of a single flat band that would be wrong by exactly the
    amount that matters most over mountainous terrain."""
    for ds in datasets:
        if 'orog' in ds.data_vars:
            return ds['orog'].values
    raise ValueError('No orography (surface elevation) field found')


def get_gh_profile(datasets, mbs, np):
    """Return {mb: gh_array} for whichever of `mbs` have a matching
    geopotential-height field in this file. HRRR's wrfsfc 'gh' isobaric
    dataset only carries 1000/850/700/500mb -- 925mb has u/v but no
    matching gh, so it's deliberately left out of AGL1000's HRRR profile
    (see AGL1000_ISOBARIC_MB_GH)."""
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
    steep terrain -- e.g. the 1000mb level can sit below a 10m/80m AGL
    anchor over high ground), so this isn't just "pick the two nearest
    rows." Clamps to the nearest level's wind when target falls outside
    the profile's range rather than extrapolating."""
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
    """AGL1000 for MODEL_SOURCE='hrrr': HRRR's wrfsfc file has no native
    AGL field above 80m, so this vertically interpolates U/V to
    orog(point) + 304.8m (true 1,000 ft AGL, terrain-relative) from a
    6-level profile: 10m/80m AGL wind (height = orog + 10/80) and
    1000/850/700/500mb isobaric wind at that level's own 'gh' height.
    See module docstring and interp_profile_to_target()."""
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
    elif level_key == '600':
        u700, v700, lats, lons = get_pressure_winds(datasets, 700, np)
        u500, v500, _,    _    = get_pressure_winds(datasets, 500, np)
        u = u700 * W700_600 + u500 * W500_600
        v = v700 * W700_600 + v500 * W500_600
        return u, v, lats, lons
    else:
        return get_pressure_winds(datasets, int(level_key), np)


def build_geojson(u, v, lats, lons):
    """Subsample grid and build compact GeoJSON."""
    import math as _math
    features = []
    for i in range(0, u.shape[0], GRID_STEP):
        for j in range(0, u.shape[1], GRID_STEP):
            uv = float(u[i, j])
            vv = float(v[i, j])
            lat = float(lats[i, j])
            lon = float(lons[i, j])
            if not (_math.isfinite(uv) and _math.isfinite(vv)):
                continue
            if not (20 <= lat <= 53 and -130 <= lon <= -60):
                continue
            spd = _math.sqrt(uv**2 + vv**2) * 1.94384
            dir = (_math.degrees(_math.atan2(-uv, -vv)) + 360) % 360
            features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [round(lon, 3), round(lat, 3)]
                },
                'properties': {
                    'u':   round(uv, 2),
                    'v':   round(vv, 2),
                    'spd': round(spd, 1),
                    'dir': round(dir, 0),
                }
            })
    return {'type': 'FeatureCollection', 'features': features}


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
    """Find the most recent complete HRRR cycle (F000+F018 present)."""
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
            f018 = find_hrrr_file(date, f'{cyc}z', 18)
            if f000 and f018:
                return date, f'{cyc}z'
    return None, None


# ── RRFS file fetch (mixed-source: 2dfld for SFC, prslev for isobaric) ─────
RRFS_ISOBARIC_MB = [925, 850, 700, 500]   # 600 stays a synthetic interpolation, unchanged


def find_rrfs_files(date, cycle_num, fhr):
    """Fetch-and-cache this fhr's 2dfld (SFC 10m) and prslev (isobaric)
    curated subsets via rrfs_wind_fetch.py. Returns (sfc_path, prslev_path);
    either may be None if that product/fhr isn't posted yet."""
    import rrfs_wind_fetch as rwf

    sfc_wanted = {('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
                  ('UGRD', f'{AGL1000_HEIGHT_M} m above ground'),
                  ('VGRD', f'{AGL1000_HEIGHT_M} m above ground')}
    sfc_path = rwf.fetch_and_cache('2dfld', date, cycle_num, fhr, sfc_wanted, tag='sfc10m320m')

    iso_wanted = set()
    for mb in RRFS_ISOBARIC_MB:
        iso_wanted.add(('UGRD', f'{mb} mb'))
        iso_wanted.add(('VGRD', f'{mb} mb'))
    prslev_path = rwf.fetch_and_cache('prslev', date, cycle_num, fhr, iso_wanted, tag='iso4lvl')

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
        log.warning(f'  F{fhr:03d}: RRFS prslev not available, isobaric levels will be missing')
    return datasets


# ── Per-FHR render (called in thread) ─────────────────────────────────────
def render_fhr(fhr, date, cycle, out_dir, force):
    """
    Render all levels for one forecast hour.
    Returns (fhr, index_entry_or_None, rendered_count, error_count).
    Called concurrently — must be thread-safe (no shared mutable state).
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
            # writers: pqact, fetch_hrrr_extended.py, this script, potentially
            # others), so skip persisting a .idx sidecar there rather than risk
            # concurrent-write conflicts; in-memory indexing per open is fine.
            datasets = cfgrib.open_datasets(grib_file, backend_kwargs={'indexpath': ''})
        except Exception as e:
            log.error(f'  F{fhr:03d}: open_datasets failed: {e}')
            return fhr, None, 0, 1

    rendered = 0
    errors   = 0
    fhr_ok   = True

    for level in LEVELS:
        out_file = out_dir / f'winds_{level}_f{fhr:03d}.json'
        if out_file.exists() and not force:
            continue

        try:
            u, v, lats, lons = extract_level(datasets, level, np, model_source=MODEL_SOURCE)
            geojson = build_geojson(u, v, lats, lons)
            geojson['metadata'] = {
                'model':    MODEL_SOURCE.upper(),
                'cycle':    f'{date} {cycle}',
                'fhr':      fhr,
                'level':    level,
                'label':    LEVEL_LABELS[level],
                'n_pts':    len(geojson['features']),
                'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            # Atomic write via temp file — safe across threads (unique per fhr+level)
            tmp = out_file.with_suffix(f'.{fhr:03d}_{level}.tmp')
            with open(tmp, 'w') as f:
                json.dump(geojson, f, separators=(',', ':'))
            tmp.rename(out_file)
            rendered += 1
            log.info(f'  F{fhr:03d} {level}: {len(geojson["features"])} pts → {out_file.name}')
        except Exception as e:
            log.error(f'  F{fhr:03d} {level}: {e}')
            errors += 1
            fhr_ok = False

    # Close datasets explicitly to free file handles
    for ds in datasets:
        try:
            ds.close()
        except Exception:
            pass

    index_entry = None
    if fhr_ok and rendered > 0:
        index_entry = {
            'fhr':   fhr,
            'files': {lv: f'winds_{lv}_f{fhr:03d}.json' for lv in LEVELS}
        }

    return fhr, index_entry, rendered, errors


# ── Rendering ──────────────────────────────────────────────────────────────
def render_cycle(date, cycle, force=False):
    """Render all levels and forecast hours for one HRRR cycle in parallel."""
    out_dir = Path(OUTPUT_BASE) / date / cycle
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f'Rendering cycle {date} {cycle} → {out_dir}')
    log.info(f'Parallel workers: {MAX_WORKERS}')

    total_rendered = 0
    total_skipped  = 0
    total_errors   = 0
    index_entries  = {}   # fhr → entry (dict for thread-safe insertion by key)

    fhrs = list(range(0, 19))   # LDM-fed path reliably has F000-F018

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(render_fhr, fhr, date, cycle, out_dir, force): fhr
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

    # Build index.json with entries sorted by FHR
    index = {
        'model':    MODEL_SOURCE.upper(),
        'cycle':    f'{date} {cycle}',
        'date':     date,
        'levels':   [{'key': k, 'label': LEVEL_LABELS[k]} for k in LEVELS],
        'fhrs':     [index_entries[k] for k in sorted(index_entries.keys())],
        'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    with open(out_dir / 'index.json', 'w') as f:
        json.dump(index, f, indent=2)

    if MODEL_SOURCE == 'rrfs':
        import rrfs_wind_fetch as rwf
        rwf.prune_cache()

    # Update 'latest' symlink atomically
    latest_link = Path(OUTPUT_BASE) / 'latest'
    tmp_link    = Path(OUTPUT_BASE) / 'latest.tmp'
    if tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(out_dir)
    tmp_link.rename(latest_link)
    log.info(f'latest → {out_dir}')

    log.info(f'Cycle {date} {cycle}: rendered={total_rendered} '
             f'skipped={total_skipped} errors={total_errors}')
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
    parser = argparse.ArgumentParser(description='Pre-render HRRR wind GeoJSON')
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
    log.info(f'{MODEL_SOURCE.upper()} wind render started')

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
