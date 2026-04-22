#!/usr/bin/env python3
"""
ingest_lamp_tstm.py — LAMP TSTM01 thunderstorm probability ingest
Runs on data2. Reads TSTM01 grib2 via cfgrib, extracts values at all
K/T/P airport locations using KDTree spatial lookup, upserts tstm_prob
and tstm_color into observations.airport_wx_impacts.

Thresholds per CAP WxCOP standard:
  < 20%  → green  (Go)
  20-59% → yellow (Caution)
  ≥ 60%  → red    (No-Go)

Deploy: /var/www/cap_winds_app/scripts/ingest_lamp_tstm.py on data2
Cron:   45 * * * * www-data (45 min past each hour)
"""

import os
import sys
import re
import logging
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cfgrib
import psycopg2
from psycopg2.extras import execute_values
from scipy.spatial import KDTree

# ── Configuration ─────────────────────────────────────────────────────────────
LAMP_DIR        = Path('/LDM/models/lamp')
MAX_FORECAST_HR = 25
CFGRIB_IDX_DIR  = '/tmp/cfgrib_lamp_idx'

DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'

LOG_FILE = '/var/log/cap_wxcop/lamp_tstm_ingest.log'

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

# ── Colour thresholds ──────────────────────────────────────────────────────────
def tstm_color(prob):
    if prob is None: return None
    if prob >= 60:   return 'red'
    if prob >= 20:   return 'yellow'
    return 'green'

# ── Find latest complete LAMP cycle ───────────────────────────────────────────
def find_latest_cycle():
    now = datetime.now(timezone.utc)
    for delta in [0, 1]:
        date_str = (now - timedelta(days=delta)).strftime('%Y%m%d')
        date_dir = LAMP_DIR / date_str
        if not date_dir.exists():
            continue
        cycles = {}
        for f in sorted(date_dir.glob('lamp_*_f024_TSTM01.grib2')):
            m = re.match(r'lamp_(\d{8})_(\d{4})z_f024_TSTM01\.grib2', f.name)
            if m:
                cycles[m.group(2)] = (date_str, f.parent)
        if cycles:
            latest_time = sorted(cycles.keys())[-1]
            date_str, cycle_dir = cycles[latest_time]
            hh, mm = int(latest_time[:2]), int(latest_time[2:])
            cycle_dt = datetime(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
                hh, mm, tzinfo=timezone.utc
            )
            cycle_str = f"{date_str}_{latest_time}z"
            return cycle_dir, cycle_dt, cycle_str
    return None, None, None

# ── Load airports ──────────────────────────────────────────────────────────────
def load_airports(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, station_id,
                   ST_X(location::geometry) AS lon,
                   ST_Y(location::geometry) AS lat
            FROM observations.airports
            WHERE station_id ~ '^[KTP]'
              AND location IS NOT NULL
            ORDER BY station_id
        """)
        rows = cur.fetchall()
    conn.commit()
    log.info(f"Loaded {len(rows)} K/T/P airports")
    return rows

# ── Build KDTree from first grib file ─────────────────────────────────────────
def build_grid_tree(grib_file):
    """
    Open one TSTM01 grib2, extract lat/lon grid, build KDTree.
    Returns (tree, flat_lats, flat_lons, shape) for reuse across all hours.
    """
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    # Set cfgrib index dir to writable location
    os.environ['CFGRIB_OPEN_KWARGS'] = ''

    try:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True,
                                                 'indexing': {'filename':
                                                 f'{CFGRIB_IDX_DIR}/{grib_file.name}.idx'}})
    except Exception:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True})

    lats = ds['latitude'].values
    lons = ds['longitude'].values
    # Normalize lons to -180/180
    lons = np.where(lons > 180, lons - 360, lons)
    shape = lats.shape

    flat_lats = lats.flatten()
    flat_lons = lons.flatten()

    # Build KDTree on (lat, lon) pairs
    coords = np.column_stack([flat_lats, flat_lons])
    tree = KDTree(coords)
    log.info(f"Grid: {shape[0]}×{shape[1]} = {shape[0]*shape[1]:,} points, KDTree built")
    ds.close()
    return tree, flat_lats, flat_lons, shape

def get_airport_indices(tree, airports):
    """Query KDTree once for all airports. Returns array of grid indices."""
    ap_coords = np.array([(lat, lon) for _, _, lon, lat in airports])
    _, indices = tree.query(ap_coords, workers=-1)
    return indices

# ── Extract values from one grib file ─────────────────────────────────────────
def extract_values(grib_file, grid_indices):
    """
    Open grib file, extract flattened data values at pre-computed grid indices.
    Returns numpy array of probabilities (0-100).
    """
    try:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True,
                                                 'indexing': {'filename':
                                                 f'{CFGRIB_IDX_DIR}/{grib_file.name}.idx'}})
    except Exception:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True})

    var_name = list(ds.data_vars)[0]
    flat_data = ds[var_name].values.flatten()
    ds.close()

    vals = flat_data[grid_indices]
    # Convert NaN/negative to None-sentinel (-1)
    vals = np.where(np.isnan(vals) | (vals < 0), -1, np.round(vals)).astype(int)
    return vals

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"LAMP TSTM01 ingest started: "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")

    t0 = datetime.now()

    cycle_dir, cycle_dt, cycle_str = find_latest_cycle()
    if not cycle_dir:
        log.error("No complete LAMP cycle found")
        sys.exit(1)
    log.info(f"Cycle: {cycle_str}")

    conn = psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )
    airports = load_airports(conn)
    if not airports:
        log.error("No airports loaded")
        conn.close()
        sys.exit(1)

    # Build KDTree from F001 (grid is same for all hours)
    f001 = cycle_dir / f"lamp_{cycle_str}_f001_TSTM01.grib2"
    if not f001.exists():
        log.error(f"Cannot find F001: {f001}")
        conn.close()
        sys.exit(1)

    tree, flat_lats, flat_lons, shape = build_grid_tree(f001)

    # Pre-compute airport→grid mapping (done once, reused for all 25 hours)
    t1 = datetime.now()
    grid_indices = get_airport_indices(tree, airports)
    log.info(f"Airport→grid mapping: {(datetime.now()-t1).total_seconds():.1f}s")

    total = 0
    for fhr in range(1, MAX_FORECAST_HR + 1):
        grib_file = cycle_dir / f"lamp_{cycle_str}_f{fhr:03d}_TSTM01.grib2"
        if not grib_file.exists():
            log.warning(f"Missing F{fhr:03d}")
            continue

        valid_time = cycle_dt + timedelta(hours=fhr)
        vals = extract_values(grib_file, grid_indices)

        rows = []
        for i, (aid, sid, lon, lat) in enumerate(airports):
            prob = None if vals[i] < 0 else int(vals[i])
            rows.append((
                aid, sid, 'LAMP', cycle_dt, valid_time, fhr,
                prob, tstm_color(prob),
                datetime.now(timezone.utc)
            ))

        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO observations.airport_wx_impacts
                    (airport_id, station_id, model_source, model_run,
                     valid_time, forecast_hour, tstm_prob, tstm_color,
                     ingested_at)
                VALUES %s
                ON CONFLICT (airport_id, model_run, forecast_hour)
                DO UPDATE SET
                    tstm_prob   = EXCLUDED.tstm_prob,
                    tstm_color  = EXCLUDED.tstm_color,
                    ingested_at = EXCLUDED.ingested_at
            """, rows)
        conn.commit()
        total += len(rows)
        log.info(f"  F{fhr:03d}: {len(rows)} upserted "
                 f"(valid {valid_time.strftime('%H:%MZ')})")


    # Merge TSTM into GLMP rows so API returns combined data
    log.info("Merging TSTM into GLMP rows...")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE observations.airport_wx_impacts glmp
            SET tstm_prob  = lamp.tstm_prob,
                tstm_color = lamp.tstm_color,
                ingested_at = NOW()
            FROM (
                SELECT DISTINCT ON (airport_id, forecast_hour)
                    airport_id, forecast_hour, tstm_prob, tstm_color
                FROM observations.airport_wx_impacts
                WHERE model_source = 'LAMP'
                  AND tstm_prob IS NOT NULL
                ORDER BY airport_id, forecast_hour, model_run DESC
            ) lamp
            WHERE glmp.model_source LIKE 'GLMP%'
              AND glmp.model_run = (
                  SELECT MAX(model_run) FROM observations.airport_wx_impacts
                  WHERE model_source LIKE 'GLMP%'
              )
              AND glmp.airport_id = lamp.airport_id
              AND glmp.forecast_hour = lamp.forecast_hour
        """)
        merged = cur.rowcount
    conn.commit()
    log.info(f"Merged {merged} GLMP rows with TSTM data")

    elapsed = (datetime.now() - t0).total_seconds()
    log.info(f"Total: {total} records in {elapsed:.0f}s "
             f"({elapsed/MAX_FORECAST_HR:.1f}s/hr)")
    log.info("LAMP TSTM01 ingest complete")
    log.info("=" * 60)
    conn.close()

if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        pass
    finally:
        # Suppress cfgrib/eccodes cleanup segfault
        os._exit(0)
