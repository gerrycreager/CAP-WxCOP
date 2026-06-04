#!/usr/bin/env python3
"""
ingest_glm_file.py — CAP WxCOP GLM Single-File Ingest
======================================================
Ingests a single GLM LCFA NetCDF4 granule into observations.glm_flashes.
Called by pqact PIPE action for each arriving GLM file.

Usage:
    ingest_glm_file.py <path/to/OR_GLM-L2-LCFA_GNN_s....nc>

Design:
    - Single file per invocation — no scanning, no state, no polling
    - Derives satellite (G18/G19) from filename
    - Bulk-inserts with ON CONFLICT DO NOTHING
    - Exits 0 on success, 1 on error
    - Purges old records once per hour (based on minute == 0)
"""

import os
import sys
import re
import logging
from datetime import datetime, timezone, timedelta

import netCDF4 as nc
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN = "dbname=avwx_data user=avwx_user host=192.168.0.60"
RETENTION_MINUTES = 420
GLM_FILL_THRESHOLD = 1e30

# ---------------------------------------------------------------------------
# Logging — syslog-style, no file rotation issues
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [ingest_glm_file] %(message)s',
    handlers=[
        logging.FileHandler('/home/ldm/var/logs/cap_wxcop/ingest_glm.log'),
    ]
)
log = logging.getLogger('ingest_glm_file')

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def parse_granule_start(filename):
    """Parse granule start time from LCFA filename. Returns UTC datetime or None."""
    fn = os.path.basename(filename)
    m = re.search(r'_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})', fn)
    if not m:
        return None
    try:
        year, doy, hh, mm, ss = (int(x) for x in m.groups())
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        return base.replace(hour=hh, minute=mm, second=ss)
    except (ValueError, OverflowError):
        return None

def satellite_from_filename(filename):
    """Return 'G19' or 'G18' from filename, or None."""
    fn = os.path.basename(filename)
    m = re.search(r'_(G1[89])_', fn)
    return m.group(1) if m else None

# ---------------------------------------------------------------------------
# LCFA reading
# ---------------------------------------------------------------------------

def read_lcfa_flashes(filepath, satellite):
    """Read flashes from a single GLM LCFA granule. Returns list of dicts."""
    granule_start = parse_granule_start(filepath)
    if granule_start is None:
        log.warning(f"Cannot parse start time: {os.path.basename(filepath)}")
        return []

    granule_name = os.path.basename(filepath)
    flashes = []

    try:
        with nc.Dataset(filepath, 'r') as ds:
            if 'flash_id' not in ds.variables:
                return []
            flash_id = ds.variables['flash_id'][:]
            n_flashes = len(flash_id)
            if n_flashes == 0:
                return []

            time_off  = ds.variables['flash_time_offset_of_first_event'][:]
            flash_lat = ds.variables['flash_lat'][:]
            flash_lon = ds.variables['flash_lon'][:]

            def safe_var(name):
                if name in ds.variables:
                    v = ds.variables[name][:]
                    if hasattr(v, 'mask'):
                        return np.where(v.mask, None, v.data).tolist()
                    return [None if abs(float(x)) > GLM_FILL_THRESHOLD else float(x)
                            for x in v]
                return [None] * n_flashes

            energy = safe_var('flash_energy')
            area   = safe_var('flash_area')

            for i in range(n_flashes):
                try:
                    flash_time = granule_start + timedelta(seconds=float(time_off[i]))
                    lat = float(flash_lat[i])
                    lon = float(flash_lon[i])
                    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                        continue
                    flashes.append({
                        'satellite':    satellite,
                        'flash_id':     int(flash_id[i]),
                        'flash_time':   flash_time,
                        'lat':          lat,
                        'lon':          lon,
                        'energy':       energy[i],
                        'area':         area[i],
                        'granule_file': granule_name,
                    })
                except (ValueError, IndexError, TypeError):
                    continue
    except Exception as e:
        log.error(f"Error reading {granule_name}: {e}")
        return []

    return flashes

# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def insert_flashes(cur, flashes):
    if not flashes:
        return 0
    rows = [(f['satellite'], f['flash_id'], f['flash_time'],
             f['lat'], f['lon'], f['energy'], f['area'], f['granule_file'])
            for f in flashes]
    execute_values(cur, """
        INSERT INTO observations.glm_flashes
            (satellite, flash_id, flash_time, lat, lon, energy, area, granule_file)
        VALUES %s
        ON CONFLICT (granule_file, flash_id) DO NOTHING
    """, rows)
    return cur.rowcount

def maybe_purge(cur):
    """Purge old records once per hour (when minute == 0)."""
    if datetime.now(timezone.utc).minute == 0:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=RETENTION_MINUTES)
        cur.execute("DELETE FROM observations.glm_flashes WHERE flash_time < %s",
                    (cutoff,))
        if cur.rowcount:
            log.info(f"Purged {cur.rowcount} old flash records")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <granule.nc>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.isfile(filepath):
        log.error(f"File not found: {filepath}")
        sys.exit(1)

    satellite = satellite_from_filename(filepath)
    if not satellite:
        log.error(f"Cannot determine satellite from filename: {filepath}")
        sys.exit(1)

    os.makedirs('/var/log/cap_wxcop', exist_ok=True)

    flashes = read_lcfa_flashes(filepath, satellite)
    if not flashes:
        sys.exit(0)

    try:
        conn = psycopg2.connect(DB_DSN)
        with conn.cursor() as cur:
            n = insert_flashes(cur, flashes)
            maybe_purge(cur)
        conn.commit()
        conn.close()
        if n:
            log.info(f"{os.path.basename(filepath)}: {len(flashes)} read, {n} inserted")
    except Exception as e:
        log.error(f"DB error for {os.path.basename(filepath)}: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
