#!/usr/bin/env python3
"""
ingest_glm.py — CAP WxCOP GLM Lightning Ingest
===============================================
Reads GOES GLM Level-2 LCFA (Lightning Cluster-Filter Algorithm) NetCDF4
granules from the LDM archive and bulk-inserts flash records into
observations.glm_flashes.

Sources:
  GOES-19 East : /LDM/satellite/GLM/EAST/OR_GLM-L2-LCFA_G19_*.nc
  GOES-18 West : /LDM/satellite/GLM/WEST/OR_GLM-L2-LCFA_G18_*.nc

Cadence: 20-second granules, ~3 files/minute per satellite.
Cron:    * * * * * (every minute — processes ~3 new files per feed per run)

Design:
  - Scans for granules newer than the last processed file (state file per feed)
  - Bulk-inserts flash records with ON CONFLICT DO NOTHING (dedup by granule+flash_id)
  - Purges records older than RETENTION_MINUTES on every run
  - Skips granules already fully processed (tracked by state file)
  - Each feed (EAST/WEST) is processed independently — one feed failure
    does not affect the other

LCFA flash variables used:
  flash_id                         — integer flash identifier
  flash_time_offset_of_first_event — seconds since granule start time
  flash_lat                        — centroid latitude
  flash_lon                        — centroid longitude
  flash_energy                     — radiant energy (J), may be fill-valued
  flash_area                       — flash area (km²), may be fill-valued

Granule start time is parsed from the filename:
  OR_GLM-L2-LCFA_G19_s20260682207000_e20260682207200_c20260682207218.nc
  s = start YYYYDDDHHMMSSd  (day-of-year format)
"""

import os
import sys
import glob
import logging
import argparse
from datetime import datetime, timezone, timedelta

import netCDF4 as nc
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN = "dbname=avwx_data user=avwx_user host=localhost"

GLM_FEEDS = {
    'G19': '/LDM/satellite/GLM/EAST',
    'G18': '/LDM/satellite/GLM/WEST',
}

# Retain flash records for this many minutes (2 hours)
RETENTION_MINUTES = 240

# State files — track last processed granule mtime per feed
STATE_DIR = '/var/lib/cap_wxcop'
STATE_FILE = {sat: os.path.join(STATE_DIR, f'glm_{sat}_last.txt') for sat in GLM_FEEDS}

# Process granules no older than this (minutes) — safety net
MAX_GRANULE_AGE_MINUTES = 30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/cap_wxcop/ingest_glm.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('ingest_glm')

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def parse_granule_start(filename):
    """
    Parse granule start time from LCFA filename.
    Format: OR_GLM-L2-LCFA_GNN_sYYYYDDDHHMMSSd_e..._c....nc
    Returns datetime UTC or None.

    YYYYDDD = year + day-of-year (Julian day)
    HHMMSS  = hour, minute, second
    d       = tenths of second (ignored)
    """
    import re
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

# ---------------------------------------------------------------------------
# State management — track last processed mtime
# ---------------------------------------------------------------------------

def load_last_mtime(satellite):
    """Return mtime of last processed granule, or 0.0 if no state."""
    sf = STATE_FILE[satellite]
    try:
        with open(sf) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0.0

def save_last_mtime(satellite, mtime):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE[satellite], 'w') as f:
        f.write(str(mtime))

# ---------------------------------------------------------------------------
# Granule discovery
# ---------------------------------------------------------------------------

def find_new_granules(satellite, feed_dir, last_mtime, max_age_minutes):
    """
    Return sorted list of .nc files newer than last_mtime and
    within max_age_minutes of now.
    """
    pattern = os.path.join(feed_dir, f'OR_GLM-L2-LCFA_{satellite}_*.nc')
    files = glob.glob(pattern)
    if not files:
        return []

    cutoff_old = last_mtime
    cutoff_new = datetime.now(timezone.utc).timestamp() - (max_age_minutes * 60)
    # Don't skip files that are older than max_age if last_mtime is 0
    if last_mtime == 0.0:
        cutoff_old = cutoff_new

    new_files = [
        f for f in files
        if os.path.getmtime(f) > cutoff_old
    ]
    return sorted(new_files, key=os.path.getmtime)

# ---------------------------------------------------------------------------
# LCFA NetCDF4 reading
# ---------------------------------------------------------------------------

# GLM LCFA fill values to treat as missing
GLM_FILL_THRESHOLD = 1e30

def read_lcfa_flashes(filepath, satellite):
    """
    Read flash records from a GLM LCFA NetCDF4 granule.
    Returns list of dicts, one per flash.
    Returns empty list if file is unreadable or has no flashes.
    """
    granule_start = parse_granule_start(filepath)
    if granule_start is None:
        log.warning(f"Cannot parse start time from {os.path.basename(filepath)}")
        return []

    granule_name = os.path.basename(filepath)
    flashes = []

    try:
        with nc.Dataset(filepath, 'r') as ds:
            # Check flash count
            if 'flash_id' not in ds.variables:
                log.debug(f"No flash_id in {granule_name} — empty granule")
                return []

            flash_id  = ds.variables['flash_id'][:]
            n_flashes = len(flash_id)
            if n_flashes == 0:
                log.debug(f"Zero flashes in {granule_name}")
                return []

            # Time offset (seconds since granule start)
            time_off  = ds.variables['flash_time_offset_of_first_event'][:]
            flash_lat = ds.variables['flash_lat'][:]
            flash_lon = ds.variables['flash_lon'][:]

            # Optional fields — may have fill values
            def safe_var(name):
                if name in ds.variables:
                    v = ds.variables[name][:]
                    # Mask fill values
                    if hasattr(v, 'mask'):
                        return np.where(v.mask, None, v.data).tolist()
                    return [None if abs(float(x)) > GLM_FILL_THRESHOLD else float(x)
                            for x in v]
                return [None] * n_flashes

            energy = safe_var('flash_energy')
            area   = safe_var('flash_area')

            for i in range(n_flashes):
                try:
                    t_offset = float(time_off[i])
                    flash_time = granule_start + timedelta(seconds=t_offset)
                    lat = float(flash_lat[i])
                    lon = float(flash_lon[i])

                    # Basic sanity check
                    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                        continue

                    flashes.append({
                        'satellite':   satellite,
                        'flash_id':    int(flash_id[i]),
                        'flash_time':  flash_time,
                        'lat':         lat,
                        'lon':         lon,
                        'energy':      energy[i] if energy[i] is not None else None,
                        'area':        area[i]   if area[i]   is not None else None,
                        'granule_file': granule_name,
                    })
                except (ValueError, IndexError, TypeError) as e:
                    log.debug(f"Flash {i} in {granule_name}: {e}")
                    continue

    except Exception as e:
        log.error(f"Error reading {granule_name}: {e}")
        return []

    return flashes

# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def get_db():
    return psycopg2.connect(DB_DSN)

def insert_flashes(cur, flashes):
    """Bulk insert flash records. Returns count of newly inserted rows."""
    if not flashes:
        return 0

    rows = [
        (
            f['satellite'],
            f['flash_id'],
            f['flash_time'],
            f['lat'],
            f['lon'],
            f['energy'],
            f['area'],
            f['granule_file'],
        )
        for f in flashes
    ]

    execute_values(cur, """
        INSERT INTO observations.glm_flashes
            (satellite, flash_id, flash_time, lat, lon, energy, area, granule_file)
        VALUES %s
        ON CONFLICT (granule_file, flash_id) DO NOTHING
    """, rows)

    return cur.rowcount

def purge_old_flashes(cur, retention_minutes):
    """Delete flash records older than retention window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=retention_minutes)
    cur.execute(
        "DELETE FROM observations.glm_flashes WHERE flash_time < %s",
        (cutoff,)
    )
    return cur.rowcount

# ---------------------------------------------------------------------------
# Per-feed processing
# ---------------------------------------------------------------------------

def process_feed(satellite, feed_dir, db_conn, force=False):
    """
    Process all new granules for one satellite feed.
    Returns (granules_processed, flashes_inserted).
    """
    if not os.path.isdir(feed_dir):
        log.warning(f"[{satellite}] Feed directory not found: {feed_dir}")
        return 0, 0

    last_mtime = 0.0 if force else load_last_mtime(satellite)
    new_files  = find_new_granules(satellite, feed_dir, last_mtime, MAX_GRANULE_AGE_MINUTES)

    if not new_files:
        log.debug(f"[{satellite}] No new granules")
        return 0, 0

    log.info(f"[{satellite}] Processing {len(new_files)} new granule(s)")

    total_flashes = 0
    latest_mtime  = last_mtime

    for filepath in new_files:
        flashes = read_lcfa_flashes(filepath, satellite)
        if flashes:
            try:
                with db_conn.cursor() as cur:
                    n = insert_flashes(cur, flashes)
                db_conn.commit()
                total_flashes += n
                log.info(f"[{satellite}] {os.path.basename(filepath)}: "
                         f"{len(flashes)} flashes read, {n} inserted")
            except Exception as e:
                log.error(f"[{satellite}] DB insert error for "
                          f"{os.path.basename(filepath)}: {e}")
                db_conn.rollback()
        else:
            log.debug(f"[{satellite}] {os.path.basename(filepath)}: no flashes")

        # Track latest mtime regardless of flash count
        mtime = os.path.getmtime(filepath)
        if mtime > latest_mtime:
            latest_mtime = mtime

    # Save state
    if latest_mtime > last_mtime:
        save_last_mtime(satellite, latest_mtime)

    return len(new_files), total_flashes

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='CAP WxCOP GLM ingest')
    parser.add_argument('--satellite', choices=['G19', 'G18', 'ALL'], default='ALL',
                        help='Which satellite feed to process')
    parser.add_argument('--force', action='store_true',
                        help='Reprocess all granules within max age window')
    parser.add_argument('--no-purge', action='store_true',
                        help='Skip purge of old flash records')
    parser.add_argument('--retention', type=int, default=RETENTION_MINUTES,
                        help=f'Flash retention minutes (default {RETENTION_MINUTES})')
    args = parser.parse_args()

    # Ensure state and log dirs exist
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs('/var/log/cap_wxcop', exist_ok=True)

    try:
        db_conn = get_db()
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        sys.exit(1)

    # Purge old records first
    if not args.no_purge:
        try:
            with db_conn.cursor() as cur:
                n_purged = purge_old_flashes(cur, args.retention)
            db_conn.commit()
            if n_purged:
                log.info(f"Purged {n_purged} flash records older than "
                         f"{args.retention} minutes")
        except Exception as e:
            log.warning(f"Purge failed: {e}")
            db_conn.rollback()

    # Process feeds
    satellites = list(GLM_FEEDS.keys()) if args.satellite == 'ALL' else [args.satellite]

    for satellite in satellites:
        feed_dir = GLM_FEEDS[satellite]
        try:
            n_granules, n_flashes = process_feed(
                satellite, feed_dir, db_conn, force=args.force
            )
            if n_granules:
                log.info(f"[{satellite}] Complete: {n_granules} granules, "
                         f"{n_flashes} new flashes")
        except Exception as e:
            log.error(f"[{satellite}] Feed processing error: {e}")

    db_conn.close()
    log.debug("GLM ingest complete")

if __name__ == '__main__':
    main()

