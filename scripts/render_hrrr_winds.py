#!/var/www/cap_winds_app/venv/bin/python3
"""
render_hrrr_winds.py — Pre-render HRRR wind vectors to static GeoJSON files.

Runs hourly via cron on r815. Reads HRRR GRIB2 from /LDM/models/hrrr/,
writes compact GeoJSON to /LDM/models/hrrr_winds/{date}/{cycle}z/

Output files:
  winds_{LEVEL}_f{FHR}.json   per level/fhr
  index.json                  manifest of available files + metadata
  latest -> symlink to newest cycle dir

Levels: SFC 925 850 700 600 500
FHR:    000-018

Usage:
  render_hrrr_winds.py              # auto-detect and render latest cycle
  render_hrrr_winds.py --force      # re-render even if output exists
  render_hrrr_winds.py --cycle 15z  # render a specific cycle
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────
HRRR_BASE   = '/LDM/models/hrrr'
OUTPUT_BASE = '/LDM/models/hrrr_winds'
GRID_STEP   = 14        # subsample step — ~42km, ~700 pts CONUS
MAX_CYCLES  = 3         # number of cycle dirs to keep on disk
LOG_FILE    = '/home/ldm/var/logs/hrrr_winds_render.log'

LEVELS = ['SFC', '925', '850', '700', '600', '500']
LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
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
def get_sfc_winds(datasets, np):
    """Extract 10m U/V from open datasets list."""
    for ds in datasets:
        if 'heightAboveGround' not in ds.coords:
            continue
        if abs(float(ds.coords['heightAboveGround'].values) - 10.0) > 1:
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
        levels = ds.isobaricInhPa.values
        for i, lv in enumerate(levels):
            if abs(float(lv) - float(hpa)) < 1:
                lons = np.where(ds.longitude.values > 180,
                                ds.longitude.values - 360,
                                ds.longitude.values)
                return ds['u'].values[i], ds['v'].values[i], \
                       ds.latitude.values, lons
    raise ValueError(f'No {hpa} hPa wind dataset found')


def extract_level(datasets, level_key, np):
    """Extract U/V for a named level key."""
    if level_key == 'SFC':
        return get_sfc_winds(datasets, np)
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
    fhr_str = f'{int(fhr):03d}'
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
            # Check F000 and F018 both exist
            f000 = find_hrrr_file(date, f'{cyc}z', 0)
            f018 = find_hrrr_file(date, f'{cyc}z', 18)
            if f000 and f018:
                return date, f'{cyc}z'
    return None, None


# ── Rendering ──────────────────────────────────────────────────────────────
def render_cycle(date, cycle, force=False):
    """Render all levels and forecast hours for one HRRR cycle."""
    import cfgrib
    import numpy as np

    out_dir = Path(OUTPUT_BASE) / date / cycle
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f'Rendering cycle {date} {cycle} → {out_dir}')
    rendered = 0
    skipped  = 0
    errors   = 0
    index_entries = []

    for fhr in range(0, 19):
        grib_file = find_hrrr_file(date, cycle, fhr)
        if not grib_file:
            log.warning(f'  F{fhr:03d}: GRIB2 file not found, skipping')
            errors += 1
            continue

        # Open datasets once per FHR, reuse for all levels
        try:
            datasets = cfgrib.open_datasets(grib_file)
        except Exception as e:
            log.error(f'  F{fhr:03d}: open_datasets failed: {e}')
            errors += 1
            continue

        fhr_ok = True
        for level in LEVELS:
            out_file = out_dir / f'winds_{level}_f{fhr:03d}.json'
            if out_file.exists() and not force:
                skipped += 1
                continue

            try:
                u, v, lats, lons = extract_level(datasets, level, np)
                geojson = build_geojson(u, v, lats, lons)
                geojson['metadata'] = {
                    'model':   'HRRR',
                    'cycle':   f'{date} {cycle}',
                    'fhr':     fhr,
                    'level':   level,
                    'label':   LEVEL_LABELS[level],
                    'n_pts':   len(geojson['features']),
                    'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                }
                # Write atomically via temp file
                tmp = out_file.with_suffix('.tmp')
                with open(tmp, 'w') as f:
                    json.dump(geojson, f, separators=(',', ':'))
                tmp.rename(out_file)
                rendered += 1
                log.info(f'  F{fhr:03d} {level}: {len(geojson["features"])} pts → {out_file.name}')
            except Exception as e:
                log.error(f'  F{fhr:03d} {level}: {e}')
                errors += 1
                fhr_ok = False

        # Build index entry for this FHR
        if fhr_ok:
            index_entries.append({
                'fhr':   fhr,
                'files': {lv: f'winds_{lv}_f{fhr:03d}.json' for lv in LEVELS}
            })

    # Write index.json
    index = {
        'cycle':    f'{date} {cycle}',
        'date':     date,
        'levels':   [{'key': k, 'label': LEVEL_LABELS[k]} for k in LEVELS],
        'fhrs':     index_entries,
        'rendered': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    with open(out_dir / 'index.json', 'w') as f:
        json.dump(index, f, indent=2)

    # Update 'latest' symlink
    latest_link = Path(OUTPUT_BASE) / 'latest'
    if latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(out_dir)
    log.info(f'latest → {out_dir}')

    log.info(f'Cycle {date} {cycle}: rendered={rendered} skipped={skipped} errors={errors}')
    return rendered, errors


def scour_old_cycles():
    """Remove cycle dirs older than MAX_CYCLES."""
    base = Path(OUTPUT_BASE)
    if not base.exists():
        return
    # Collect all date/cycle dirs sorted by name (chronological)
    dirs = sorted([
        d for d in base.glob('*/*z')
        if d.is_dir() and d.name.endswith('z')
    ])
    # Remove all but the newest MAX_CYCLES
    to_remove = dirs[:-MAX_CYCLES] if len(dirs) > MAX_CYCLES else []
    for d in to_remove:
        log.info(f'Scouring old cycle: {d}')
        shutil.rmtree(d, ignore_errors=True)
        # Remove parent date dir if empty
        try:
            d.parent.rmdir()
        except OSError:
            pass


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Pre-render HRRR wind GeoJSON')
    parser.add_argument('--force',  action='store_true',
                        help='Re-render even if output files exist')
    parser.add_argument('--cycle',  type=str, default=None,
                        help='Specific cycle to render, e.g. 15z')
    parser.add_argument('--date',   type=str, default=None,
                        help='Date for specific cycle, e.g. 20260519 (default: today)')
    args = parser.parse_args()

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
