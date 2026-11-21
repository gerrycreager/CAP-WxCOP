#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_model_winds.py - Model Wind Forecast Ingest
Ingests HRRR, GFS, and AIGFS wind forecasts into PostGIS database

Usage:
  ./ingest_model_winds.py [options]

Options:
  --force-hrrr    Force reprocess latest HRRR cycle
  --force-gfs     Force reprocess latest GFS cycle
  --force-aigfs   Force reprocess latest AIGFS cycle
  --reprocess     Force reprocess all models
  --verbose / -v  Enable per-forecast-hour progress logging
                  Default: summary only (cycle found/skipped, total records, elapsed)

Cron (data2):
  15 * * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py >> /var/log/model_winds_ingest.log 2>&1

For interactive monitoring of a run in progress:
  /var/www/cap_winds_app/scripts/ingest_model_winds.py --force-aigfs --verbose 2>&1 | tee /tmp/aigfs.log
  tail -f /var/log/model_winds_ingest.log

Architecture:
  - Forecasts written to observations.model_wind_forecasts on data2 PostgreSQL
  - CONUS 10m winds: RRFS by default (part of the HRRR->RRFS migration, see
    /root/.claude/plans/rosy-wobbling-music.md "Phase 6"), hourly cycles,
    f000-f012, fetched on demand via rrfs_wind_fetch.py's NOMADS byte-range
    .idx technique (2dfld product, conus/3km) and decoded with cfgrib.
    Set WIND_MODEL_SOURCE=hrrr to fall back to the legacy path reading
    /LDM/models/hrrr/ (NFS mount from data1) via pygrib -- same env var
    name render_hrrr_winds.py/render_hrrr_particles.py already use.
    model_name is 'RRFS' or 'HRRR' in the DB depending on which is active.
    RRFS ingestion runs in its own spawned subprocess (see _rrfs_worker() /
    multiprocessing.get_context('spawn') in main()) -- cfgrib and pygrib
    both bind the same underlying ecCodes C library through different
    Python wrappers, and running both in one long-lived process (this
    script's main() already uses pygrib for GFS/AIGFS later in the same
    run) segfaulted live in testing on data2 (2026-09-21): the RRFS/cfgrib
    work completed cleanly, then pygrib.open() for AIGFS crashed with a
    real Segmentation fault, reproducible only when both libraries were
    exercised back-to-back in one process -- isolated standalone calls to
    either library alone were fine. 'spawn' (not 'fork') guarantees the
    RRFS child never inherits the main process's already-imported pygrib
    C state.
  - GFS:   Global, 6-hourly cycles, f000-f024 (per-forecast-hour files,
           /LDM/models/gfs/, NFS mount from data1) -- NOT part of the RRFS
           migration (GFS isn't being retired), left untouched.
  - AIGFS: Global, 6-hourly cycles, f000-f096 (monolithic sfc file per cycle,
           /LDM/models/AIGFS/, NFS mount from data1) -- also untouched.
"""

import os
import sys
import fcntl
import argparse
import logging
import multiprocessing as mp
import time
from datetime import datetime, timedelta, timezone

import pygrib
import numpy as np
from scipy.spatial import cKDTree
import psycopg2
from psycopg2.extras import execute_values

import rrfs_wind_fetch as rwf

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Module-level verbose flag — set in main() from --verbose arg
VERBOSE = False

def vlog(msg):
    """Log only when --verbose is active."""
    if VERBOSE:
        log.info(msg)
        sys.stdout.flush()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_HOST = '192.168.0.60'
DB_PORT = 5432
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'

HRRR_BASE  = '/LDM/models/hrrr'
GFS_BASE   = '/LDM/models/gfs'
AIGFS_BASE = '/LDM/models/AIGFS'

# 'rrfs' (default) or 'hrrr' -- controls only the CONUS 10m-wind pipeline;
# GFS/AIGFS are unaffected by this switch, see module docstring.
WIND_MODEL_SOURCE = os.environ.get('WIND_MODEL_SOURCE', 'rrfs')

# Forecast hour limits per model
HRRR_FORECAST_HOURS  = 12   # F00-F12  (hourly) -- shared by the RRFS path too
GFS_FORECAST_HOURS   = 24   # F000-F024 (3-hourly)
AIGFS_FORECAST_HOURS = 96   # F000-F096 (6-hourly) -- expand as needed

# Seconds since last write before AIGFS sfc file is treated as complete
# (LDM appends over ~40 min; 5 min quiescent = done)
AIGFS_STABLE_AGE = 300

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER
    )


def check_existing_forecast(model_name, model_run):
    """Return True if a complete set of forecast hours exists for this cycle."""
    thresholds = {
        'HRRR':  HRRR_FORECAST_HOURS  + 1,
        'RRFS':  HRRR_FORECAST_HOURS  + 1,
        'GFS':   (GFS_FORECAST_HOURS  // 3) + 1,
        'AIGFS': (AIGFS_FORECAST_HOURS // 6) + 1,
    }
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT forecast_hour)
            FROM observations.model_wind_forecasts
            WHERE model_name = %s AND model_run = %s
        """, (model_name, model_run))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        threshold = thresholds.get(model_name, 1)
        vlog(f"  [{model_name}] DB: {count} forecast hours present (need {threshold})")
        return count >= threshold
    except Exception as e:
        log.error(f"  [{model_name}] Error checking existing forecast: {e}")
        return False


LOCKFILE = '/tmp/ingest_model_winds.lock'


def load_airports(conn):
    """
    Load airports from the database.
    Returns dict with numpy arrays for vectorized nearest-neighbour lookup:
      {
        'icao':  list of station_id strings  (length N)
        'lats':  np.array of latitudes       (length N)
        'lons':  np.array of longitudes      (length N)
      }
    Matches the existing query filter: has_paved_runway=True, 4-char station_id.
    """
    log.info("  Loading airports from database...")
    sys.stdout.flush()
    cur = conn.cursor()
    cur.execute("""
        SELECT station_id, ST_Y(location), ST_X(location)
        FROM observations.airports
        WHERE has_paved_runway = true
          AND LENGTH(station_id) = 4
          AND station_id NOT LIKE '%-%'
          AND location IS NOT NULL
    """)
    rows = cur.fetchall()
    cur.close()
    icao = [r[0] for r in rows]
    lats = np.array([float(r[1]) for r in rows])
    lons = np.array([float(r[2]) for r in rows])
    log.info(f"  Loaded {len(icao):,} airports")
    sys.stdout.flush()
    return {'icao': icao, 'lats': lats, 'lons': lons}


# ---------------------------------------------------------------------------
# GRIB2 helpers
# ---------------------------------------------------------------------------

def get_grid_latlons(grb):
    """Return flattened (lats, lons) arrays from a pygrib message."""
    lats_2d, lons_2d = grb.latlons()
    return lats_2d.flatten(), lons_2d.flatten()


def build_airport_indices(airports, grid_lats, grid_lons):
    """
    Pre-compute nearest grid-point index for each airport using a KD-tree.
    Called ONCE per file — grid is identical for all forecast hours.
    Returns np.array of integer indices into the flattened grid (length N airports).

    Memory: O(grid_points) for the tree (~70 MB for a 2.95M-point grid).
    Time:   O(M log M) build + O(N log M) query — typically 5-15s total.
    No large temporary matrices allocated.
    """
    log.info(f"  Building KD-tree for {len(grid_lats):,}-point grid...")
    sys.stdout.flush()
    t0 = time.time()

    # Stack grid coords into (M, 2) array for the tree
    grid_points = np.column_stack((grid_lats, grid_lons))
    tree = cKDTree(grid_points)
    log.info(f"  KD-tree built in {time.time()-t0:.1f}s")
    sys.stdout.flush()

    # Query all airports in one vectorized call
    t1 = time.time()
    ap_points = np.column_stack((airports['lats'], airports['lons']))
    _, indices = tree.query(ap_points, workers=-1)   # workers=-1 = all CPU cores
    log.info(f"  {len(airports['icao']):,} airports indexed in {time.time()-t1:.1f}s "
             f"(total {time.time()-t0:.1f}s)")
    sys.stdout.flush()
    return indices.astype(np.int32)


def extract_winds_from_message(grb, airports, grid_indices):
    """
    Extract wind component values for all airports using pre-computed grid indices.
    Returns dict: icao -> float (m/s).
    O(N airports) — index lookup only, no distance calculation.
    """
    data = grb.values.flatten()
    results = {}
    for i, icao in enumerate(airports['icao']):
        val = data[grid_indices[i]]
        if not np.ma.is_masked(val):
            results[icao] = float(val)
    return results


def extract_winds_from_array(data, airports, grid_indices):
    """
    Same contract as extract_winds_from_message(), for a flattened numpy
    array (RRFS/cfgrib path) instead of a pygrib message object.
    Returns dict: icao -> float (m/s).
    """
    results = {}
    for i, icao in enumerate(airports['icao']):
        val = data[grid_indices[i]]
        if not np.ma.is_masked(val):
            results[icao] = float(val)
    return results


def wind_speed_dir(u, v):
    """Return (speed m/s, direction degrees-from) from U/V components."""
    wspd = float(np.sqrt(u**2 + v**2))
    wdir = float((270.0 - np.degrees(np.arctan2(v, u))) % 360.0)
    return wspd, wdir


# ---------------------------------------------------------------------------
# Shared DB insert
# ---------------------------------------------------------------------------

MPS_TO_KTS = 1.94384   # m/s to knots conversion

def wind_category(speed_kts):
    """CAPR 70-1 wind constraint category."""
    if speed_kts >= 30:
        return 'NO-GO'
    elif speed_kts >= 16:
        return 'CAUTION'
    return 'NORMAL'


def bulk_insert(conn, records, model_name):
    """
    Bulk-insert wind forecast records matching observations.model_wind_forecasts schema.
    records: list of tuples (icao, model_name, model_run, forecast_hour,
                             valid_time, wspd_ms, wdir_deg, u_ms, v_ms,
                             ap_lat, ap_lon)
    Converts m/s -> knots, computes wind_category, builds PostGIS location.
    Idempotent via ON CONFLICT DO UPDATE on (station_id, model_run, valid_time).

    NOTE: model_name is part of the DO UPDATE SET list -- without it, two
    models that happen to share a (station_id, model_run, valid_time) key
    (e.g. HRRR and RRFS both writing hourly CONUS cycles with aligned
    model_run/valid_time) can leave a row's wind values updated to the new
    model's data while its model_name label silently keeps the OLD model's
    name. Found live 2026-09-21 switching WIND_MODEL_SOURCE from hrrr to
    rrfs: 27,312 rows had their values overwritten to RRFS but stayed
    labeled 'HRRR' until this fix, because data2's cron had already written
    HRRR rows for the same 21z cycle before the RRFS repoint was deployed.
    """
    if not records:
        log.warning(f"  [{model_name}] No records to insert")
        return
    log.info(f"  [{model_name}] Inserting {len(records):,} records...")
    sys.stdout.flush()
    t0 = time.time()
    try:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO observations.model_wind_forecasts
                (station_id, model_name, model_run, forecast_hour,
                 valid_time, wind_speed_kts, wind_dir, wind_category,
                 location)
            VALUES %s
            ON CONFLICT (station_id, model_run, valid_time)
            DO UPDATE SET
                model_name     = EXCLUDED.model_name,
                wind_speed_kts = EXCLUDED.wind_speed_kts,
                wind_dir       = EXCLUDED.wind_dir,
                wind_category  = EXCLUDED.wind_category,
                forecast_hour  = EXCLUDED.forecast_hour
        """, [
            (
                icao, mn, mr, fh, vt,
                round(wspd * MPS_TO_KTS, 1),
                int(round(wdir)) % 360,
                wind_category(wspd * MPS_TO_KTS),
                f'SRID=4326;POINT({lon} {lat})'
            )
            for icao, mn, mr, fh, vt, wspd, wdir, _u, _v, lat, lon in records
        ], page_size=5000)
        conn.commit()
        cur.close()
        log.info(f"  [{model_name}] Insert complete in {time.time()-t0:.1f}s")
    except Exception as e:
        conn.rollback()
        log.error(f"  [{model_name}] DB insert failed: {e}")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# HRRR
# ---------------------------------------------------------------------------

def find_latest_hrrr_cycle():
    """
    Layout: /LDM/models/hrrr/hrrr.YYYYMMDD/HHz/hrrr.tHHz.wrfsfcf000.grib2
    Returns (cycle_datetime, cycle_dir) or (None, None).
    """
    vlog("  Scanning for latest HRRR cycle...")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for hours_back in range(0, 7):
        t  = now - timedelta(hours=hours_back)
        ds = t.strftime('%Y%m%d')
        hh = t.hour
        d  = f"{HRRR_BASE}/hrrr.{ds}/{hh:02d}z"
        f0 = os.path.join(d, f"hrrr.t{hh:02d}z.wrfsfcf000.grib2")
        if os.path.isdir(d) and os.path.exists(f0):
            ct = datetime(t.year, t.month, t.day, hh)
            log.info(f"  [HRRR] Found cycle: {ct.strftime('%Y-%m-%d %H:00Z')}")
            return ct, d
    log.info("  [HRRR] No cycle found in last 6 hours")
    return None, None


def ingest_hrrr_forecasts(cycle_time, cycle_dir, force=False):
    model_name = 'HRRR'
    hh = cycle_time.hour
    log.info(f"  [{model_name}] Ingesting {cycle_time.strftime('%Y-%m-%d %H:00Z')}")
    t_total = time.time()

    conn     = get_connection()
    airports = load_airports(conn)

    if force:
        vlog(f"  [{model_name}] Deleting existing records for this cycle")
        cur = conn.cursor()
        cur.execute("DELETE FROM observations.model_wind_forecasts "
                    "WHERE model_name=%s AND model_run=%s", (model_name, cycle_time))
        conn.commit()
        cur.close()

    records = []
    grid_indices = None
    processed = skipped = 0

    for fhour in range(0, HRRR_FORECAST_HOURS + 1):
        fname = f"hrrr.t{hh:02d}z.wrfsfcf{fhour:03d}.grib2"
        fpath = os.path.join(cycle_dir, fname)
        if not os.path.exists(fpath):
            vlog(f"  [{model_name}] Missing F{fhour:03d}: {fname}")
            skipped += 1
            continue

        vlog(f"  [{model_name}] F{fhour:03d} {fname}")
        sys.stdout.flush()
        t0 = time.time()

        try:
            grbs   = pygrib.open(fpath)
            u_msgs = grbs.select(shortName='10u', typeOfLevel='heightAboveGround', level=10)
            v_msgs = grbs.select(shortName='10v', typeOfLevel='heightAboveGround', level=10)
            grbs.close()
        except Exception as e:
            log.error(f"  [{model_name}] F{fhour:03d} pygrib error: {e}")
            skipped += 1
            continue

        if not u_msgs or not v_msgs:
            vlog(f"  [{model_name}] F{fhour:03d}: no 10m wind messages")
            skipped += 1
            continue

        if grid_indices is None:
            grid_lats, grid_lons = get_grid_latlons(u_msgs[0])
            grid_indices = build_airport_indices(airports, grid_lats, grid_lons)

        u_vals = extract_winds_from_message(u_msgs[0], airports, grid_indices)
        v_vals = extract_winds_from_message(v_msgs[0], airports, grid_indices)
        valid_time = cycle_time + timedelta(hours=fhour)

        n = 0
        for i, icao in enumerate(airports['icao']):
            if icao in u_vals and icao in v_vals:
                wspd, wdir = wind_speed_dir(u_vals[icao], v_vals[icao])
                records.append((icao, model_name, cycle_time, fhour,
                                 valid_time, wspd, wdir, u_vals[icao], v_vals[icao],
                                 float(airports['lats'][i]), float(airports['lons'][i])))
                n += 1
        processed += 1
        vlog(f"  [{model_name}] F{fhour:03d}: {n:,} airports in {time.time()-t0:.1f}s")

    bulk_insert(conn, records, model_name)
    conn.close()
    log.info(f"  [{model_name}] Done: {processed} steps, {skipped} skipped, "
             f"{len(records):,} records, {time.time()-t_total:.0f}s elapsed")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# RRFS (default CONUS 10m-wind source -- see WIND_MODEL_SOURCE)
# ---------------------------------------------------------------------------

def find_latest_rrfs_cycle():
    """
    RRFS CONUS 2dfld product, fetched via rrfs_wind_fetch.py's NOMADS
    byte-range .idx technique (no local LDM feed for RRFS -- see
    project_noaaport_sbn_href_retirement memory). Mirrors
    find_latest_hrrr_cycle()'s (cycle_datetime, cycle_dir)-shaped return;
    here "cycle_dir" is a (date, cycle_num) tuple threaded through to
    ingest_rrfs_forecasts(), not a filesystem path.
    """
    vlog("  Scanning for latest ready RRFS cycle...")
    date, cyc = rwf.find_ready_cycle(product='2dfld', domain='conus', resolution='3km')
    if not date:
        log.info("  [RRFS] No ready cycle found within lookback window")
        return None, None
    ct = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H')
    log.info(f"  [RRFS] Found cycle: {ct.strftime('%Y-%m-%d %H:00Z')}")
    return ct, (date, cyc)


def get_rrfs_sfc_winds(grib_path):
    """
    Decode 10m U/V + native lat/lon from an RRFS 2dfld curated subset
    (fetched via rrfs_wind_fetch.fetch_and_cache()). Returns flattened
    (u, v, lats, lons) numpy arrays, matching get_grid_latlons()'s
    flattened-array contract so build_airport_indices() needs no changes.
    Same cfgrib extraction pattern as render_hrrr_winds.py's get_sfc_winds().
    """
    import cfgrib
    datasets = cfgrib.open_datasets(str(grib_path), backend_kwargs={'indexpath': ''})
    for ds in datasets:
        if 'heightAboveGround' not in ds.coords:
            continue
        heights = np.atleast_1d(ds.coords['heightAboveGround'].values)
        if not np.any(np.abs(heights - 10.0) <= 1):
            continue
        if 'u10' in ds.data_vars and 'v10' in ds.data_vars:
            lons = np.where(ds.longitude.values > 180,
                             ds.longitude.values - 360, ds.longitude.values)
            return (ds['u10'].values.flatten(), ds['v10'].values.flatten(),
                    ds.latitude.values.flatten(), lons.flatten())
    raise ValueError('No 10m wind dataset found in RRFS 2dfld subset')


def ingest_rrfs_forecasts(cycle_time, cycle_key, force=False):
    model_name = 'RRFS'
    date, cyc = cycle_key
    log.info(f"  [{model_name}] Ingesting {cycle_time.strftime('%Y-%m-%d %H:00Z')}")
    t_total = time.time()

    conn     = get_connection()
    airports = load_airports(conn)

    if force:
        vlog(f"  [{model_name}] Deleting existing records for this cycle")
        cur = conn.cursor()
        cur.execute("DELETE FROM observations.model_wind_forecasts "
                    "WHERE model_name=%s AND model_run=%s", (model_name, cycle_time))
        conn.commit()
        cur.close()

    records = []
    grid_indices = None
    processed = skipped = 0
    sfc_wanted = {('UGRD', '10 m above ground'), ('VGRD', '10 m above ground')}

    for fhour in range(0, HRRR_FORECAST_HOURS + 1):
        grib_path = rwf.fetch_and_cache('2dfld', date, cyc, fhour, sfc_wanted,
                                         tag='modelwinds', domain='conus', resolution='3km')
        if not grib_path:
            vlog(f"  [{model_name}] F{fhour:03d}: not posted yet")
            skipped += 1
            continue

        vlog(f"  [{model_name}] F{fhour:03d} {grib_path.name}")
        sys.stdout.flush()
        t0 = time.time()

        try:
            u_flat, v_flat, grid_lats, grid_lons = get_rrfs_sfc_winds(grib_path)
        except Exception as e:
            log.error(f"  [{model_name}] F{fhour:03d} decode error: {e}")
            skipped += 1
            continue

        if grid_indices is None:
            grid_indices = build_airport_indices(airports, grid_lats, grid_lons)

        u_vals = extract_winds_from_array(u_flat, airports, grid_indices)
        v_vals = extract_winds_from_array(v_flat, airports, grid_indices)
        valid_time = cycle_time + timedelta(hours=fhour)

        n = 0
        for i, icao in enumerate(airports['icao']):
            if icao in u_vals and icao in v_vals:
                wspd, wdir = wind_speed_dir(u_vals[icao], v_vals[icao])
                records.append((icao, model_name, cycle_time, fhour,
                                 valid_time, wspd, wdir, u_vals[icao], v_vals[icao],
                                 float(airports['lats'][i]), float(airports['lons'][i])))
                n += 1
        processed += 1
        vlog(f"  [{model_name}] F{fhour:03d}: {n:,} airports in {time.time()-t0:.1f}s")

    bulk_insert(conn, records, model_name)
    conn.close()
    log.info(f"  [{model_name}] Done: {processed} steps, {skipped} skipped, "
             f"{len(records):,} records, {time.time()-t_total:.0f}s elapsed")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# GFS
# ---------------------------------------------------------------------------

def find_latest_gfs_cycle():
    """
    Layout: /LDM/models/gfs/0p25/YYYYMMDD/gfs_0p25_YYYYMMDD_HHz_f000.grib2
    GFS runs 00/06/12/18Z.
    Returns (cycle_datetime, cycle_dir) or (None, None).
    """
    vlog("  Scanning for latest GFS cycle...")
    now       = datetime.now(timezone.utc).replace(tzinfo=None)
    gfs_hours = [0, 6, 12, 18]
    for days_back in range(0, 2):
        d  = now - timedelta(days=days_back)
        ds = d.strftime('%Y%m%d')
        for hh in reversed(gfs_hours):
            ct = datetime(d.year, d.month, d.day, hh)
            if ct > now:
                continue
            cdir = f"{GFS_BASE}/0p25/{ds}"
            f0   = os.path.join(cdir, f"gfs_0p25_{ds}_{hh:02d}z_f000.grib2")
            if os.path.exists(f0):
                log.info(f"  [GFS] Found cycle: {ct.strftime('%Y-%m-%d %H:00Z')}")
                return ct, cdir
    log.info("  [GFS] No cycle found in last 48 hours")
    return None, None


def ingest_gfs_forecasts(cycle_time, cycle_dir, force=False):
    model_name = 'GFS'
    ds = cycle_time.strftime('%Y%m%d')
    hh = cycle_time.hour
    log.info(f"  [{model_name}] Ingesting {cycle_time.strftime('%Y-%m-%d %H:00Z')}")
    t_total = time.time()

    conn     = get_connection()
    airports = load_airports(conn)

    if force:
        vlog(f"  [{model_name}] Deleting existing records for this cycle")
        cur = conn.cursor()
        cur.execute("DELETE FROM observations.model_wind_forecasts "
                    "WHERE model_name=%s AND model_run=%s", (model_name, cycle_time))
        conn.commit()
        cur.close()

    records = []
    grid_indices = None
    processed = skipped = 0

    for fhour in range(0, GFS_FORECAST_HOURS + 1, 3):
        fname = f"gfs_0p25_{ds}_{hh:02d}z_f{fhour:03d}.grib2"
        fpath = os.path.join(cycle_dir, fname)
        if not os.path.exists(fpath):
            vlog(f"  [{model_name}] Missing F{fhour:03d}: {fname}")
            skipped += 1
            continue

        vlog(f"  [{model_name}] F{fhour:03d} {fname}")
        sys.stdout.flush()
        t0 = time.time()

        try:
            grbs   = pygrib.open(fpath)
            u_msgs = grbs.select(shortName='10u', typeOfLevel='heightAboveGround', level=10)
            v_msgs = grbs.select(shortName='10v', typeOfLevel='heightAboveGround', level=10)
            grbs.close()
        except Exception as e:
            log.error(f"  [{model_name}] F{fhour:03d} pygrib error: {e}")
            skipped += 1
            continue

        if not u_msgs or not v_msgs:
            vlog(f"  [{model_name}] F{fhour:03d}: no 10m wind messages")
            skipped += 1
            continue

        if grid_indices is None:
            grid_lats, grid_lons = get_grid_latlons(u_msgs[0])
            grid_indices = build_airport_indices(airports, grid_lats, grid_lons)

        u_vals = extract_winds_from_message(u_msgs[0], airports, grid_indices)
        v_vals = extract_winds_from_message(v_msgs[0], airports, grid_indices)
        valid_time = cycle_time + timedelta(hours=fhour)

        n = 0
        for i, icao in enumerate(airports['icao']):
            if icao in u_vals and icao in v_vals:
                wspd, wdir = wind_speed_dir(u_vals[icao], v_vals[icao])
                records.append((icao, model_name, cycle_time, fhour,
                                 valid_time, wspd, wdir, u_vals[icao], v_vals[icao],
                                 float(airports['lats'][i]), float(airports['lons'][i])))
                n += 1
        processed += 1
        vlog(f"  [{model_name}] F{fhour:03d}: {n:,} airports in {time.time()-t0:.1f}s")

    bulk_insert(conn, records, model_name)
    conn.close()
    log.info(f"  [{model_name}] Done: {processed} steps, {skipped} skipped, "
             f"{len(records):,} records, {time.time()-t_total:.0f}s elapsed")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# AIGFS
# ---------------------------------------------------------------------------

def _aigfs_sfc_path(date_str, hh):
    return os.path.join(AIGFS_BASE, f"AIGFS_{date_str}_{hh:02d}00_sfc.grib2")


def _aigfs_file_stable(fpath):
    """
    Probe the file twice 10s apart. Return True if size is unchanged
    and mtime is at least AIGFS_STABLE_AGE seconds ago.
    """
    if not os.path.exists(fpath):
        return False
    s1 = os.stat(fpath)
    vlog(f"  [AIGFS] Stability probe 1: {s1.st_size/1e6:.0f} MB  "
         f"mtime {datetime.utcfromtimestamp(s1.st_mtime).strftime('%H:%MZ')}")
    sys.stdout.flush()
    time.sleep(10)
    s2 = os.stat(fpath)
    vlog(f"  [AIGFS] Stability probe 2: {s2.st_size/1e6:.0f} MB")
    sys.stdout.flush()
    if s1.st_size != s2.st_size:
        log.info("  [AIGFS] File still growing — deferring")
        return False
    age = time.time() - s2.st_mtime
    if age < AIGFS_STABLE_AGE:
        log.info(f"  [AIGFS] File stable but only {age:.0f}s old "
                 f"(need {AIGFS_STABLE_AGE}s) — deferring")
        return False
    vlog(f"  [AIGFS] File stable, {age:.0f}s quiescent — proceeding")
    return True


def find_latest_aigfs_cycle():
    """
    Scan for most recent AIGFS sfc file.
    Checks split naming (AIGFS_YYYYMMDD_HH00_sfc.grib2) first,
    then falls back to legacy monolithic (AIGFS_YYYYMMDD_HH00.grib2).
    AIGFS runs 00/06/12/18Z; arrives 3-4h after cycle time.
    Returns (cycle_datetime, filepath) or (None, None).
    """
    vlog("  Scanning for latest AIGFS cycle...")
    now         = datetime.now(timezone.utc).replace(tzinfo=None)
    aigfs_hours = [0, 6, 12, 18]
    for days_back in range(0, 3):
        d  = now - timedelta(days=days_back)
        ds = d.strftime('%Y%m%d')
        for hh in reversed(aigfs_hours):
            ct = datetime(d.year, d.month, d.day, hh)
            if (now - ct).total_seconds() < 3.5 * 3600:
                continue
            for candidate in [
                _aigfs_sfc_path(ds, hh),
                os.path.join(AIGFS_BASE, f"AIGFS_{ds}_{hh:02d}00.grib2"),
            ]:
                if os.path.exists(candidate):
                    sz = os.path.getsize(candidate)
                    log.info(f"  [AIGFS] Found: {ct.strftime('%Y-%m-%d %H:00Z')} "
                             f"— {os.path.basename(candidate)} ({sz/1e6:.0f} MB)")
                    return ct, candidate
    log.info("  [AIGFS] No file found")
    return None, None


def ingest_aigfs_forecasts(cycle_time, sfc_fpath, force=False):
    """
    Ingest AIGFS 10m winds from the sfc file.
    Works with both split (~250 MB) and legacy monolithic (~5 GB) files.
    All 10m U/V messages selected in a single pygrib index pass.
    """
    model_name = 'AIGFS'
    log.info(f"  [{model_name}] Ingesting {cycle_time.strftime('%Y-%m-%d %H:00Z')}")
    log.info(f"  [{model_name}] Source: {os.path.basename(sfc_fpath)} "
             f"({os.path.getsize(sfc_fpath)/1e6:.0f} MB)")
    t_total = time.time()
    sys.stdout.flush()

    log.info(f"  [{model_name}] Checking file stability (10s probe)...")
    sys.stdout.flush()
    if not _aigfs_file_stable(sfc_fpath):
        log.warning(f"  [{model_name}] Deferring — file not yet stable")
        return

    conn     = get_connection()
    airports = load_airports(conn)

    if force:
        vlog(f"  [{model_name}] Deleting existing records for this cycle")
        cur = conn.cursor()
        cur.execute("DELETE FROM observations.model_wind_forecasts "
                    "WHERE model_name=%s AND model_run=%s", (model_name, cycle_time))
        conn.commit()
        cur.close()

    log.info(f"  [{model_name}] Opening GRIB2 and building message index...")
    sys.stdout.flush()
    t0 = time.time()
    try:
        grbs = pygrib.open(sfc_fpath)
    except Exception as e:
        log.error(f"  [{model_name}] Cannot open {sfc_fpath}: {e}")
        conn.close()
        return
    log.info(f"  [{model_name}] File opened in {time.time()-t0:.1f}s")

    log.info(f"  [{model_name}] Selecting all 10m U/V messages...")
    sys.stdout.flush()
    t0 = time.time()
    try:
        u_msgs = grbs.select(shortName='10u', typeOfLevel='heightAboveGround', level=10)
        v_msgs = grbs.select(shortName='10v', typeOfLevel='heightAboveGround', level=10)
    except Exception as e:
        log.error(f"  [{model_name}] pygrib.select() failed: {e}")
        grbs.close()
        conn.close()
        return
    grbs.close()
    log.info(f"  [{model_name}] {len(u_msgs)} U + {len(v_msgs)} V messages "
             f"selected in {time.time()-t0:.1f}s")
    sys.stdout.flush()

    if not u_msgs or not v_msgs:
        log.error(f"  [{model_name}] No 10m wind messages — check shortName convention")
        conn.close()
        return

    # Index by stepRange for O(1) lookup
    u_by_step = {}
    for m in u_msgs:
        try:
            u_by_step[int(m.stepRange)] = m
        except (ValueError, AttributeError):
            pass
    v_by_step = {}
    for m in v_msgs:
        try:
            v_by_step[int(m.stepRange)] = m
        except (ValueError, AttributeError):
            pass

    paired   = sorted(set(u_by_step.keys()) & set(v_by_step.keys()))
    to_proc  = [s for s in paired if s <= AIGFS_FORECAST_HOURS]
    log.info(f"  [{model_name}] {len(paired)} paired steps available "
             f"(max F{max(paired):03d}); processing {len(to_proc)} <= F{AIGFS_FORECAST_HOURS:03d}")
    sys.stdout.flush()

    lats, lons = get_grid_latlons(u_by_step[to_proc[0]])
    grid_indices = build_airport_indices(airports, lats, lons)

    records = []
    for fhour in to_proc:
        valid_time = cycle_time + timedelta(hours=fhour)
        t0 = time.time()

        u_vals = extract_winds_from_message(u_by_step[fhour], airports, grid_indices)
        v_vals = extract_winds_from_message(v_by_step[fhour], airports, grid_indices)

        n = 0
        for i, icao in enumerate(airports['icao']):
            if icao in u_vals and icao in v_vals:
                wspd, wdir = wind_speed_dir(u_vals[icao], v_vals[icao])
                records.append((icao, model_name, cycle_time, fhour,
                                 valid_time, wspd, wdir, u_vals[icao], v_vals[icao],
                                 float(airports['lats'][i]), float(airports['lons'][i])))
                n += 1

        vlog(f"  [{model_name}] F{fhour:03d} "
             f"({valid_time.strftime('%Y-%m-%dT%H:00Z')}): "
             f"{n:,} airports in {time.time()-t0:.1f}s")

    bulk_insert(conn, records, model_name)
    conn.close()
    log.info(f"  [{model_name}] Done: {len(to_proc)} steps, "
             f"{len(records):,} records, {time.time()-t_total:.0f}s elapsed")
    sys.stdout.flush()


def _rrfs_worker(verbose, cycle_time, cycle_key, force):
    """
    Runs ingest_rrfs_forecasts() in a spawned child process -- see the
    cfgrib/pygrib segfault note in the module docstring for why this can't
    just be a plain function call from main().
    """
    global VERBOSE
    VERBOSE = verbose
    ingest_rrfs_forecasts(cycle_time, cycle_key, force=force)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        description='Model wind forecast ingest (HRRR, GFS, AIGFS)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Logging modes:
  default   Summary only: cycle found/skipped/complete, total records, elapsed time.
            Suitable for cron — produces minimal log output.
  --verbose Per-forecast-hour progress: file names, grid size, airport counts,
            per-step timing. Use for interactive runs and troubleshooting.
            Combine with tee: script --verbose 2>&1 | tee /tmp/run.log
        """
    )
    parser.add_argument('--force-hrrr',  action='store_true',
                        help='Force reprocess latest HRRR cycle')
    parser.add_argument('--force-gfs',   action='store_true',
                        help='Force reprocess latest GFS cycle')
    parser.add_argument('--force-aigfs', action='store_true',
                        help='Force reprocess latest AIGFS cycle')
    parser.add_argument('--reprocess',   action='store_true',
                        help='Force reprocess all models')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable per-forecast-hour progress logging')
    args = parser.parse_args()

    VERBOSE = args.verbose

    # Exclusive lock — prevents concurrent cron instances piling up.
    # If a previous run is still in progress, exit immediately (not an error).
    try:
        lockfile = open(LOCKFILE, 'w')
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%SZ')} "
              f"- WARNING - Previous instance still running (lock held on {LOCKFILE}). Exiting.",
              flush=True)
        return 0

    log.info("=" * 70)
    log.info(f"Model wind ingest started: "
             f"{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%SZ')}"
             + (" [verbose]" if VERBOSE else ""))
    log.info("=" * 70)

    # --- CONUS 10m winds: RRFS by default, HRRR via WIND_MODEL_SOURCE=hrrr ---
    conus_model = 'RRFS' if WIND_MODEL_SOURCE == 'rrfs' else 'HRRR'
    log.info(f"Checking for new {conus_model} data...")
    if WIND_MODEL_SOURCE == 'rrfs':
        conus_run, conus_key = find_latest_rrfs_cycle()
    else:
        conus_run, conus_key = find_latest_hrrr_cycle()
    if conus_run:
        exists = check_existing_forecast(conus_model, conus_run)
        if args.force_hrrr or args.reprocess or not exists:
            log.info(f"✓ Processing {conus_model} {conus_run.strftime('%Y-%m-%d %H:00Z')}")
            if WIND_MODEL_SOURCE == 'rrfs':
                ctx = mp.get_context('spawn')
                proc = ctx.Process(target=_rrfs_worker,
                                    args=(VERBOSE, conus_run, conus_key,
                                          args.force_hrrr or args.reprocess))
                proc.start()
                proc.join()
                if proc.exitcode != 0:
                    log.error(f"  [RRFS] worker process exited with code {proc.exitcode}")
            else:
                ingest_hrrr_forecasts(conus_run, conus_key,
                                      force=args.force_hrrr or args.reprocess)
        else:
            log.info(f"○ {conus_model} {conus_run.strftime('%Y-%m-%d %H:00Z')} already complete")
    else:
        log.info(f"○ No {conus_model} data available")

    # --- GFS ---
    log.info("=" * 70)
    log.info("Checking for new GFS data...")
    gfs_run, gfs_dir = find_latest_gfs_cycle()
    if gfs_run:
        exists = check_existing_forecast('GFS', gfs_run)
        if args.force_gfs or args.reprocess or not exists:
            log.info(f"✓ Processing GFS {gfs_run.strftime('%Y-%m-%d %H:00Z')}")
            ingest_gfs_forecasts(gfs_run, gfs_dir,
                                 force=args.force_gfs or args.reprocess)
        else:
            log.info(f"○ GFS {gfs_run.strftime('%Y-%m-%d %H:00Z')} already complete")
    else:
        log.info("○ No GFS data available")

    # --- AIGFS ---
    log.info("=" * 70)
    log.info("Checking for new AIGFS data...")
    aigfs_run, aigfs_sfc = find_latest_aigfs_cycle()
    if aigfs_run:
        exists = check_existing_forecast('AIGFS', aigfs_run)
        if args.force_aigfs or args.reprocess or not exists:
            log.info(f"✓ Processing AIGFS {aigfs_run.strftime('%Y-%m-%d %H:00Z')}")
            ingest_aigfs_forecasts(aigfs_run, aigfs_sfc,
                                   force=args.force_aigfs or args.reprocess)
        else:
            log.info(f"○ AIGFS {aigfs_run.strftime('%Y-%m-%d %H:00Z')} already complete")
    else:
        log.info("○ No AIGFS data available")

    log.info("=" * 70)
    log.info(f"Model wind ingest complete: "
             f"{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%SZ')}")
    log.info("=" * 70)
    return 0


if __name__ == '__main__':
    sys.exit(main())

