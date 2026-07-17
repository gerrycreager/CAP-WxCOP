#!/var/www/cap_winds_app/venv/bin/python3
"""
render_wind_particles.py — Pre-render GFS wind grids for animated (Windy-style)
particle visualization via leaflet-velocity.

Runs a few times per day via cron on r815 (GFS updates 4x/day: 00/06/12/18Z).
Reads the GLOBAL GFS 0.25deg GRIB2 from /LDM/models/gfs/0p25/ -- NOT the
CONUS-cropped variant, since this needs to see the full Atlantic basin for
tropical cyclone work (genesis off Africa through Caribbean/Gulf track).
Writes leaflet-velocity/wind-js format grid JSON to
/LDM/models/wind_particles/gfs/{date}/{cycle}z/

Unlike HRRR (native Lambert Conformal, curvilinear grid, needs 2D lat/lon
indexing -- see render_hrrr_winds.py), GFS's 0.25deg grid is already a
regular lat-lon grid (confirmed via cfgrib: 1D latitude/longitude coordinate
arrays, 721 x 1440). No regridding/interpolation needed -- this script is a
direct reshape+crop+flatten, not an interpolation.

Output files:
  particles_{LEVEL}_f{FHR}.json   per level/fhr, leaflet-velocity format
                                   (2-element array: U-component, V-component)
  index.json                      manifest of available files + metadata
  latest -> symlink to newest cycle dir

Levels: SFC 850 700 500 200, plus synthetic DLM (Deep-Layer Mean: vector
mean of the 850/700/500 U and V components -- the standard TC steering-flow
proxy. Vector mean, not a separate speed/direction mean, since direction
wraps around and averaging it directly would be wrong.)

Domain: Atlantic basin, 0-60N, 100W-0W -- covers Cabo Verde genesis region
through Caribbean/Gulf/US East Coast track. Not global -- keeps output size
and render time reasonable.

Usage:
  render_wind_particles.py              # auto-detect and render latest cycle
  render_wind_particles.py --force      # re-render even if output exists
  render_wind_particles.py --cycle 12z  # render a specific cycle
"""
import os
import sys
import glob
import json
import shutil
import logging
import argparse
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────
GFS_BASE    = '/LDM/models/gfs/0p25'
OUTPUT_BASE = '/LDM/models/wind_particles/gfs'
MAX_CYCLES  = 3          # number of cycle dirs to keep on disk
LOG_FILE    = '/home/ldm/var/logs/wind_particles_render.log'

# Atlantic basin -- Cabo Verde genesis region through Caribbean/Gulf/US East Coast
LAT_MIN, LAT_MAX = 0.0, 60.0
LON_MIN, LON_MAX = -100.0, 0.0     # -180..180 convention; converted to GFS's 0..360 at read time

DIRECT_LEVELS = ['SFC', '850', '700', '500', '200']
LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    '850': '~5,000 ft MSL',
    '700': '~10,000 ft MSL',
    '500': '~18,000 ft MSL',
    '200': '~39,000 ft MSL',
    'DLM': 'Deep-Layer Mean (850-700-500mb steering flow)',
}
ALL_LEVELS = DIRECT_LEVELS + ['DLM']
DLM_COMPONENTS = ['850', '700', '500']

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
# cfgrib's automatic dataset-splitting is NOT consistent across forecast hours
# for the same GFS product -- observed directly: at F000 the isobaric u/v
# variables are bundled together in one dataset, but at F003 they're split
# into two separate single-variable datasets. Assuming u/v are co-located
# (as render_hrrr_winds.py safely can, since HRRR's structure is consistent)
# breaks silently/inconsistently across FHRs here. Search for each component
# independently and merge by level instead of assuming co-location.

def _fix_lons(lon_values, np):
    return np.where(lon_values > 180, lon_values - 360, lon_values)


def get_sfc_winds(datasets, np):
    """Extract 10m U/V from open datasets list, searching for each component
    independently (see module note above)."""
    u = v = lats = lons = None
    for ds in datasets:
        if 'heightAboveGround' not in ds.coords:
            continue
        h = ds.coords['heightAboveGround'].values
        # heightAboveGround can be a genuine 0-d scalar array OR (less often)
        # a small 1-d array of multiple heights sharing this dataset -- handle
        # both rather than assuming float() always works.
        heights = np.atleast_1d(h)
        if not np.any(np.abs(heights - 10.0) <= 1):
            continue
        if u is None and 'u10' in ds.data_vars:
            u = ds['u10'].values
            lats = ds.latitude.values
            lons = _fix_lons(ds.longitude.values, np)
        if v is None and 'v10' in ds.data_vars:
            v = ds['v10'].values
            if lats is None:
                lats = ds.latitude.values
                lons = _fix_lons(ds.longitude.values, np)
    if u is None or v is None:
        raise ValueError('No 10m wind dataset found (u10 and/or v10 missing)')
    return u, v, lats, lons


def get_pressure_winds(datasets, hpa, np):
    """Extract U/V at a pressure level, searching for each component
    independently (see module note above)."""
    u = v = lats = lons = None
    for ds in datasets:
        if 'isobaricInhPa' not in ds.coords:
            continue
        levels = np.atleast_1d(ds.isobaricInhPa.values)
        idx = None
        for i, lv in enumerate(levels):
            if abs(float(lv) - float(hpa)) < 1:
                idx = i
                break
        if idx is None:
            continue
        # levels can be a scalar coord (this dataset = exactly one level) or
        # an array coord (this dataset spans multiple levels, index with idx)
        is_multi = np.ndim(ds.isobaricInhPa.values) > 0
        if u is None and 'u' in ds.data_vars:
            u = ds['u'].values[idx] if is_multi else ds['u'].values
            lats = ds.latitude.values
            lons = _fix_lons(ds.longitude.values, np)
        if v is None and 'v' in ds.data_vars:
            v = ds['v'].values[idx] if is_multi else ds['v'].values
            if lats is None:
                lats = ds.latitude.values
                lons = _fix_lons(ds.longitude.values, np)
    if u is None or v is None:
        raise ValueError(f'No {hpa} hPa wind dataset found (u and/or v missing)')
    return u, v, lats, lons


def extract_direct_level(datasets, level_key, np):
    """Extract U/V for a directly-available (non-synthetic) level key."""
    if level_key == 'SFC':
        return get_sfc_winds(datasets, np)
    return get_pressure_winds(datasets, int(level_key), np)


def _largest_contiguous_run(idx, np):
    """_fix_lons() leaves the untouched lon=0.0 point at its original low
    array index while the wrapped block we actually want (e.g. -100..-0.25)
    sits at high indices. Since LON_MAX=0.0 is an inclusive boundary, that
    stray point satisfies the crop's range test too and gets stitched onto
    the front of the selection, corrupting the header (dx/lo1) downstream.
    Keep only the largest contiguous run of indices to drop it."""
    if len(idx) <= 1:
        return idx
    breaks = np.where(np.diff(idx) != 1)[0]
    if len(breaks) == 0:
        return idx
    runs = np.split(idx, breaks + 1)
    return max(runs, key=len)


def crop_to_atlantic(u, v, lats, lons, np):
    """Crop a full-globe regular grid down to the Atlantic basin domain.
    lats is 1D descending (90..-90), lons is 1D in -180..180 after the
    >180 wraparound fix applied in the extract functions above."""
    lat_idx = np.where((lats >= LAT_MIN) & (lats <= LAT_MAX))[0]
    lon_idx = np.where((lons >= LON_MIN) & (lons <= LON_MAX))[0]
    if len(lat_idx) == 0 or len(lon_idx) == 0:
        raise ValueError('Atlantic crop produced an empty grid -- check bounds/wraparound')
    lon_idx = _largest_contiguous_run(lon_idx, np)
    u_c = u[np.ix_(lat_idx, lon_idx)]
    v_c = v[np.ix_(lat_idx, lon_idx)]
    return u_c, v_c, lats[lat_idx], lons[lon_idx]


# ── wind-js / leaflet-velocity format ──────────────────────────────────────
def build_windjs_grid(u, v, lats, lons, ref_time, fhr):
    """Build the 2-element [U-component, V-component] wind-js grid leaflet-velocity
    expects. lats/lons must already be cropped to the target domain and be
    regular (uniform spacing) -- true here since GFS's native grid is regular
    and cropping by index preserves that.
    """
    ny, nx = u.shape
    dy = abs(float(lats[1] - lats[0])) if ny > 1 else 0.25
    dx = abs(float(lons[1] - lons[0])) if nx > 1 else 0.25
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


# ── File discovery ─────────────────────────────────────────────────────────
def find_gfs_file(date, cycle, fhr):
    """Return path to global GFS 0.25deg grib2 or None."""
    cyc_str = cycle.replace('z', '')
    fhr_str = f'{int(fhr):03d}'
    pattern = os.path.join(GFS_BASE, date,
                           f'gfs_0p25_{date}_{cyc_str}z_f{fhr_str}.grib2')
    return pattern if os.path.exists(pattern) else None


def find_available_fhrs(date, cycle):
    """Scan disk for whatever forecast hours LDM has actually delivered for
    this cycle so far -- GFS delivery to LDM is progressive, not all-at-once."""
    cyc_str = cycle.replace('z', '')
    pattern = os.path.join(GFS_BASE, date, f'gfs_0p25_{date}_{cyc_str}z_f*.grib2')
    fhrs = []
    for f in glob.glob(pattern):
        name = os.path.basename(f)
        try:
            fhr = int(name.split('_f')[-1].replace('.grib2', ''))
            fhrs.append(fhr)
        except ValueError:
            continue
    return sorted(fhrs)


def find_latest_cycle():
    """Find the most recent GFS cycle with at least an F000 file present."""
    today     = datetime.now(timezone.utc).strftime('%Y%m%d')
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
    for date in [today, yesterday]:
        date_dir = os.path.join(GFS_BASE, date)
        if not os.path.isdir(date_dir):
            continue
        cycles = sorted({f.split('_')[3] for f in os.listdir(date_dir)
                         if f.startswith('gfs_0p25_')}, reverse=True)
        for cyc in cycles:
            if find_gfs_file(date, cyc, 0):
                return date, cyc
    return None, None


# ── Per-FHR render ──────────────────────────────────────────────────────────
def render_fhr(fhr, date, cycle, out_dir, force):
    """Render all levels (direct + synthetic DLM) for one forecast hour."""
    import cfgrib
    import numpy as np

    grib_file = find_gfs_file(date, cycle, fhr)
    if not grib_file:
        log.warning(f'  F{fhr:03d}: GRIB2 file not found, skipping')
        return fhr, None, 0, 1

    try:
        datasets = cfgrib.open_datasets(grib_file)
    except Exception as e:
        log.error(f'  F{fhr:03d}: open_datasets failed: {e}')
        return fhr, None, 0, 1

    ref_time = f'{date}T{cycle.replace("z", "")}:00:00Z'
    rendered = 0
    errors   = 0
    fhr_ok   = True
    dlm_parts = {}   # level -> (u_cropped, v_cropped) for DLM averaging

    for level in DIRECT_LEVELS:
        out_file = out_dir / f'particles_{level}_f{fhr:03d}.json'
        try:
            u, v, lats, lons = extract_direct_level(datasets, level, np)
            u_c, v_c, lats_c, lons_c = crop_to_atlantic(u, v, lats, lons, np)
            if level in DLM_COMPONENTS:
                dlm_parts[level] = (u_c, v_c, lats_c, lons_c)

            if out_file.exists() and not force:
                continue
            grid = build_windjs_grid(u_c, v_c, lats_c, lons_c, ref_time, fhr)
            tmp = out_file.with_suffix(f'.{fhr:03d}_{level}.tmp')
            with open(tmp, 'w') as f:
                json.dump(grid, f, separators=(',', ':'))
            tmp.rename(out_file)
            rendered += 1
            log.info(f'  F{fhr:03d} {level}: {u_c.shape} grid -> {out_file.name}')
        except Exception as e:
            log.error(f'  F{fhr:03d} {level}: {e}')
            errors += 1
            fhr_ok = False

    # Synthetic DLM level -- vector mean of the 850/700/500 components
    if len(dlm_parts) == len(DLM_COMPONENTS):
        try:
            out_file = out_dir / f'particles_DLM_f{fhr:03d}.json'
            if force or not out_file.exists():
                u_stack = np.stack([dlm_parts[lv][0] for lv in DLM_COMPONENTS])
                v_stack = np.stack([dlm_parts[lv][1] for lv in DLM_COMPONENTS])
                u_dlm = u_stack.mean(axis=0)
                v_dlm = v_stack.mean(axis=0)
                lats_c, lons_c = dlm_parts[DLM_COMPONENTS[0]][2], dlm_parts[DLM_COMPONENTS[0]][3]
                grid = build_windjs_grid(u_dlm, v_dlm, lats_c, lons_c, ref_time, fhr)
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

    index_entry = None
    if fhr_ok and rendered > 0:
        index_entry = {
            'fhr':   fhr,
            'files': {lv: f'particles_{lv}_f{fhr:03d}.json' for lv in ALL_LEVELS}
        }
    return fhr, index_entry, rendered, errors


# ── Rendering ──────────────────────────────────────────────────────────────
def render_cycle(date, cycle, force=False):
    """Render all levels for whatever forecast hours are on disk for this cycle."""
    out_dir = Path(OUTPUT_BASE) / date / cycle
    out_dir.mkdir(parents=True, exist_ok=True)

    fhrs = find_available_fhrs(date, cycle)
    log.info(f'Rendering cycle {date} {cycle} -> {out_dir}  ({len(fhrs)} FHRs on disk: {fhrs})')

    total_rendered = 0
    total_errors   = 0
    index_entries  = {}

    for fhr in fhrs:
        fhr_result, idx_entry, rendered, errors = render_fhr(fhr, date, cycle, out_dir, force)
        total_rendered += rendered
        total_errors   += errors
        if idx_entry:
            index_entries[fhr_result] = idx_entry

    index = {
        'model':    'GFS',
        'cycle':    f'{date} {cycle}',
        'date':     date,
        'domain':   {'lat_min': LAT_MIN, 'lat_max': LAT_MAX, 'lon_min': LON_MIN, 'lon_max': LON_MAX},
        'levels':   [{'key': k, 'label': LEVEL_LABELS[k]} for k in ALL_LEVELS],
        'fhrs':     [index_entries[k] for k in sorted(index_entries.keys())],
        'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    with open(out_dir / 'index.json', 'w') as f:
        json.dump(index, f, indent=2)

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
    dirs = sorted([d for d in base.glob('*/*z') if d.is_dir() and d.name.endswith('z')])
    for d in dirs[:-MAX_CYCLES] if len(dirs) > MAX_CYCLES else []:
        log.info(f'Scouring old cycle: {d}')
        shutil.rmtree(d, ignore_errors=True)
        try:
            d.parent.rmdir()
        except OSError:
            pass


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Pre-render GFS wind-js grids for animated wind')
    parser.add_argument('--force', action='store_true', help='Re-render even if output exists')
    parser.add_argument('--cycle', type=str, default=None, help='Specific cycle, e.g. 12z')
    parser.add_argument('--date',  type=str, default=None, help='Date for --cycle, e.g. 20260716')
    args = parser.parse_args()

    log.info('=' * 60)
    log.info('GFS wind particle render started')

    if args.cycle:
        cycle = args.cycle if args.cycle.endswith('z') else args.cycle + 'z'
        date  = args.date or datetime.now(timezone.utc).strftime('%Y%m%d')
    else:
        date, cycle = find_latest_cycle()
        if not date:
            log.error('No GFS cycle found')
            sys.exit(1)

    log.info(f'Rendering cycle: {date} {cycle}')
    rendered, errors = render_cycle(date, cycle, force=args.force)
    scour_old_cycles()

    log.info(f'Done: {rendered} files rendered, {errors} errors')
    log.info('=' * 60)
    sys.exit(0 if errors == 0 else 1)


if __name__ == '__main__':
    main()
