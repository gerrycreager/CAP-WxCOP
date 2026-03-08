#!/var/www/cap_winds_app/venv/bin/python3
"""
Smart Model Wind Forecast Ingest - FILTERED VERSION
Ingests HRRR and GFS wind forecasts into PostGIS database
NOW WITH RUNWAY FILTERING: Only airports with paved runways >= 2500 ft

GFS covers global domain including AK, HI, PR, Guam (MANDATORY for OCONUS).
HRRR covers CONUS only.

Usage:
  ./ingest_model_winds.py [--force-hrrr] [--force-gfs] [--reprocess]
  ./ingest_model_winds.py --gfs-only      # force GFS only for testing
  ./ingest_model_winds.py --hrrr-only     # force HRRR only

Cron (run at :15 past hour, after GRIB download at :05):
  15 * * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py >> /var/log/model_winds_ingest.log 2>&1
"""
import sys
import os
import logging
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

import pygrib
import numpy as np
from scipy.spatial import cKDTree

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
HRRR_BASE = "/LDM/models/hrrr"
GFS_BASE  = "/LDM/models/gfs/0p25"   # LDM writes: /LDM/models/gfs/0p25/YYYYMMDD/

FORECAST_HOURS    = 12   # F00-F12 for both models
MIN_RUNWAY_LENGTH = 2500 # feet — must match repopulate_airports_table script

# Global airport coordinate cache (loaded once per run)
AIRPORT_COORDS = {}


# ── Database helpers ──────────────────────────────────────────────────────────

def check_existing_forecast(model_name, model_run):
    """
    Return True if a complete set of forecasts for this model run already
    exists in the database (all FORECAST_HOURS+1 hours present).
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT forecast_hour)
            FROM observations.model_wind_forecasts
            WHERE model_name = %s AND model_run = %s
        """, (model_name, model_run))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return bool(result and result[0] >= (FORECAST_HOURS + 1))
    except Exception as e:
        log.error(f"Error checking existing forecast: {e}")
        return False


def load_airport_coordinates():
    """
    Load ALL airports with paved runways >= 2500 ft from the database.
    No geographic filter — GFS is global, so OCONUS airports (AK/HI/PR/Guam)
    are included here and will be matched against GFS grids.
    """
    global AIRPORT_COORDS
    if AIRPORT_COORDS:
        return AIRPORT_COORDS

    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT station_id,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon
            FROM observations.airports
            WHERE location IS NOT NULL
              AND longest_runway_ft >= %s
        """, (MIN_RUNWAY_LENGTH,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        for station_id, lat, lon in rows:
            AIRPORT_COORDS[station_id] = {
                'lat': float(lat),
                'lon': float(lon)
            }

        log.info(f"Loaded {len(AIRPORT_COORDS)} airport coordinates (global, runway >= {MIN_RUNWAY_LENGTH} ft)")
        return AIRPORT_COORDS

    except Exception as e:
        log.error(f"Error loading airport coordinates: {e}")
        return {}


# ── Cycle finders ─────────────────────────────────────────────────────────────

def find_latest_hrrr_cycle():
    """
    Find the most recent available HRRR cycle.
    Path pattern: /LDM/models/hrrr/hrrr.YYYYMMDD/HHz/hrrr.tHHz.wrfsfcf000.grib2
    Returns (cycle_datetime, cycle_dir) or (None, None).
    """
    try:
        now = datetime.utcnow()
        for days_back in range(0, 2):
            check_date = now - timedelta(days=days_back)
            date_str   = check_date.strftime('%Y%m%d')
            for cycle_hour in range(23, -1, -1):
                cycle_dir = f"{HRRR_BASE}/hrrr.{date_str}/{cycle_hour:02d}z"
                if not os.path.isdir(cycle_dir):
                    continue
                test_file = os.path.join(
                    cycle_dir,
                    f"hrrr.t{cycle_hour:02d}z.wrfsfcf000.grib2"
                )
                if os.path.exists(test_file):
                    return (
                        datetime(check_date.year, check_date.month,
                                 check_date.day, cycle_hour),
                        cycle_dir
                    )
        return None, None
    except Exception as e:
        log.error(f"Error finding HRRR cycle: {e}")
        return None, None


def find_latest_gfs_cycle():
    """
    Find the most recent available GFS cycle.

    LDM writes GFS 0.25° files as:
        /LDM/models/gfs/0p25/YYYYMMDD/gfs_0p25_YYYYMMDD_HHz_f000.grib2

    GFS runs at 00, 06, 12, 18Z.
    Returns (cycle_datetime, cycle_dir) or (None, None).
    """
    try:
        now       = datetime.utcnow()
        gfs_hours = [0, 6, 12, 18]

        for days_back in range(0, 2):
            check_date = now - timedelta(days=days_back)
            date_str   = check_date.strftime('%Y%m%d')
            cycle_dir  = f"{GFS_BASE}/{date_str}"

            if not os.path.isdir(cycle_dir):
                continue

            for cycle_hour in reversed(gfs_hours):
                test_file = os.path.join(
                    cycle_dir,
                    f"gfs_0p25_{date_str}_{cycle_hour:02d}z_f000.grib2"
                )
                if os.path.exists(test_file):
                    log.info(
                        f"Found GFS cycle: {check_date.strftime('%Y-%m-%d')} "
                        f"{cycle_hour:02d}Z in {cycle_dir}"
                    )
                    return (
                        datetime(check_date.year, check_date.month,
                                 check_date.day, cycle_hour),
                        cycle_dir
                    )

        log.warning("No GFS cycle found in last 48 hours — OCONUS will have no data")
        return None, None

    except Exception as e:
        log.error(f"Error finding GFS cycle: {e}")
        return None, None


# ── Wind extraction ───────────────────────────────────────────────────────────

def extract_wind_for_airports(grib_file, airports, model_name='HRRR'):
    """
    Extract 10-metre wind (U,V) components for each airport from a GRIB2 file.

    HRRR: Lambert conformal 2D irregular grid → KDTree nearest neighbour
    GFS:  Regular 0.25° lat/lon grid → direct index lookup

    Returns: {station_id: {'u': m/s, 'v': m/s, 'speed_kts': kt}}
    """
    if not airports:
        return {}

    results = {}

    try:
        grbs = pygrib.open(grib_file)

        # Fetch U and V at 10m
        try:
            u_msgs = grbs.select(name='10 metre U wind component')
            v_msgs = grbs.select(name='10 metre V wind component')
        except Exception:
            try:
                u_msgs = grbs.select(shortName='10u')
                v_msgs = grbs.select(shortName='10v')
            except Exception as e:
                log.error(f"Cannot find 10m wind components in {grib_file}: {e}")
                grbs.close()
                return {}

        u_data, lats, lons = u_msgs[0].data()
        v_data, _,    _    = v_msgs[0].data()
        grbs.close()

        # Normalise longitudes to -180..180
        lons = np.where(lons > 180, lons - 360, lons)

        if model_name == 'HRRR':
            # ── Lambert conformal 2D grid — KDTree ──────────────────────────
            flat_lats = lats.flatten()
            flat_lons = lons.flatten()
            flat_u    = u_data.flatten()
            flat_v    = v_data.flatten()

            tree = cKDTree(np.column_stack([flat_lats, flat_lons]))

            ap_lats = np.array([a['lat'] for a in airports.values()])
            ap_lons = np.array([a['lon'] for a in airports.values()])
            ids     = list(airports.keys())

            dists, idxs = tree.query(np.column_stack([ap_lats, ap_lons]), k=1)

            for i, sid in enumerate(ids):
                if dists[i] > 0.5:   # > ~55 km — outside HRRR domain
                    continue
                u = float(flat_u[idxs[i]])
                v = float(flat_v[idxs[i]])
                results[sid] = {
                    'u': u, 'v': v,
                    'speed_kts': float(np.sqrt(u*u + v*v)) * 1.94384
                }

        else:
            # ── Regular 0.25° lat/lon grid (GFS) — direct index lookup ──────
            # lats may be 2D (721×1440) or 1D (721,); same for lons
            if lats.ndim == 2:
                lat_1d = lats[:, 0]
                lon_1d = lons[0, :]
            else:
                lat_1d = lats
                lon_1d = lons

            ap_lats = np.array([a['lat'] for a in airports.values()])
            ap_lons = np.array([a['lon'] for a in airports.values()])
            ids     = list(airports.keys())

            # Vectorised nearest-neighbour
            lat_idx = np.abs(lat_1d[:, None] - ap_lats[None, :]).argmin(axis=0)
            lon_idx = np.abs(lon_1d[:, None] - ap_lons[None, :]).argmin(axis=0)

            for i, sid in enumerate(ids):
                li = lat_idx[i]
                lo = lon_idx[i]

                # Distance sanity check — 0.4° ≈ 44 km at equator
                dlat = abs(float(lat_1d[li]) - ap_lats[i])
                dlon = abs(float(lon_1d[lo]) - ap_lons[i])
                dlon = min(dlon, 360.0 - dlon)  # dateline wrap
                if dlat > 0.4 or dlon > 0.4:
                    continue

                u = float(u_data[li, lo])
                v = float(v_data[li, lo])
                results[sid] = {
                    'u': u, 'v': v,
                    'speed_kts': float(np.sqrt(u*u + v*v)) * 1.94384
                }

    except Exception as e:
        log.error(f"Error extracting wind from {grib_file}: {e}", exc_info=True)

    return results


# ── Wind category ─────────────────────────────────────────────────────────────

def get_wind_category(speed_kts):
    """CAPR 70-1 wind categories."""
    if speed_kts >= 30:
        return 'RESTRICTED'
    elif speed_kts >= 20:
        return 'CAUTION'
    return 'NORMAL'


# ── Main ingest ───────────────────────────────────────────────────────────────

def ingest_model_forecasts(model_name, cycle_dir, model_run,
                           force_reprocess=False):
    """Ingest all forecast hours for one model run into the database."""

    if not force_reprocess and check_existing_forecast(model_name, model_run):
        log.info(
            f"✓ {model_name} {model_run.strftime('%Y-%m-%d %H:00Z')} "
            f"already complete — skipping"
        )
        return

    log.info(
        f"Processing {model_name} {model_run.strftime('%Y-%m-%d %H:00 UTC')}"
    )

    airports = load_airport_coordinates()
    if not airports:
        log.error("No airports loaded — aborting")
        return

    conn = get_connection()
    cur  = conn.cursor()

    if force_reprocess:
        cur.execute("""
            DELETE FROM observations.model_wind_forecasts
            WHERE model_name = %s AND model_run = %s
        """, (model_name, model_run))
        conn.commit()

    date_str       = model_run.strftime('%Y%m%d')
    total_inserted = 0

    for fhr in range(0, FORECAST_HOURS + 1):

        # ── Build GRIB filename ───────────────────────────────────────────────
        if model_name == 'HRRR':
            # /LDM/models/hrrr/hrrr.YYYYMMDD/HHz/hrrr.tHHz.wrfsfcfNNN.grib2
            grib_file = os.path.join(
                cycle_dir,
                f"hrrr.t{model_run.hour:02d}z.wrfsfcf{fhr:03d}.grib2"
            )
        else:
            # /LDM/models/gfs/0p25/YYYYMMDD/gfs_0p25_YYYYMMDD_HHz_fNNN.grib2
            grib_file = os.path.join(
                cycle_dir,
                f"gfs_0p25_{date_str}_{model_run.hour:02d}z_f{fhr:03d}.grib2"
            )

        if not os.path.exists(grib_file):
            log.warning(f"  F{fhr:03d} not found: {grib_file}")
            continue

        valid_time = model_run + timedelta(hours=fhr)
        wind_data  = extract_wind_for_airports(grib_file, airports, model_name)

        if not wind_data:
            log.warning(f"  F{fhr:03d} — no wind data extracted")
            continue

        batch = []
        for sid, w in wind_data.items():
            coords = airports[sid]
            batch.append((
                sid,
                coords['lon'], coords['lat'],
                model_name,
                model_run,
                valid_time,
                fhr,
                round(w['speed_kts'], 1),
                None,                        # gust — not available from 10m wind
                get_wind_category(w['speed_kts']),
            ))

        if batch:
            cur.executemany("""
                INSERT INTO observations.model_wind_forecasts (
                    station_id, location, model_name, model_run,
                    valid_time, forecast_hour,
                    wind_speed_kts, wind_gust_kts, wind_category
                )
                VALUES (
                    %s,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (station_id, model_run, valid_time) DO UPDATE SET
                    wind_speed_kts = EXCLUDED.wind_speed_kts,
                    wind_gust_kts  = EXCLUDED.wind_gust_kts,
                    wind_category  = EXCLUDED.wind_category
            """, batch)
            conn.commit()
            total_inserted += len(batch)
            log.info(f"  F{fhr:03d} — {len(batch)} forecasts inserted/updated")

    # ── Back-fill worst-case wind_category per station for this run ───────────
    cur.execute("""
        UPDATE observations.model_wind_forecasts mwf
        SET wind_category = subq.worst
        FROM (
            SELECT station_id, model_run,
                   CASE
                       WHEN MAX(wind_speed_kts) >= 30 THEN 'RESTRICTED'
                       WHEN MAX(wind_speed_kts) >= 20 THEN 'CAUTION'
                       ELSE 'NORMAL'
                   END AS worst
            FROM observations.model_wind_forecasts
            WHERE model_run  = %s
              AND model_name = %s
            GROUP BY station_id, model_run
        ) subq
        WHERE mwf.station_id = subq.station_id
          AND mwf.model_run  = subq.model_run
          AND mwf.model_name = %s
    """, (model_run, model_name, model_name))
    conn.commit()

    cur.close()
    conn.close()
    log.info(
        f"✓ {model_name} {model_run.strftime('%Y-%m-%d %H:00Z')} — "
        f"{total_inserted} total forecasts ingested"
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='CAP WxCOP model wind ingest — HRRR (CONUS) + GFS (global/OCONUS)'
    )
    parser.add_argument('--force-hrrr',  action='store_true',
                        help='Force re-ingest of latest HRRR run')
    parser.add_argument('--force-gfs',   action='store_true',
                        help='Force re-ingest of latest GFS run')
    parser.add_argument('--reprocess',   action='store_true',
                        help='Force re-ingest of both models')
    parser.add_argument('--gfs-only',    action='store_true',
                        help='Process GFS only (skip HRRR)')
    parser.add_argument('--hrrr-only',   action='store_true',
                        help='Process HRRR only (skip GFS)')
    args = parser.parse_args()

    # ── HRRR — CONUS ─────────────────────────────────────────────────────────
    if not args.gfs_only:
        hrrr_time, hrrr_dir = find_latest_hrrr_cycle()
        if hrrr_time and hrrr_dir:
            log.info(
                f"HRRR: {hrrr_time.strftime('%Y-%m-%d %H:00Z')} → {hrrr_dir}"
            )
            ingest_model_forecasts(
                'HRRR', hrrr_dir, hrrr_time,
                force_reprocess=(args.force_hrrr or args.reprocess)
            )
        else:
            log.warning("No HRRR cycle found — skipping HRRR")

    # ── GFS — global / MANDATORY for OCONUS (AK, HI, PR, Guam) ─────────────
    if not args.hrrr_only:
        gfs_time, gfs_dir = find_latest_gfs_cycle()
        if gfs_time and gfs_dir:
            log.info(
                f"GFS:  {gfs_time.strftime('%Y-%m-%d %H:00Z')} → {gfs_dir}"
            )
            ingest_model_forecasts(
                'GFS', gfs_dir, gfs_time,
                force_reprocess=(args.force_gfs or args.reprocess)
            )
        else:
            log.error(
                "No GFS cycle found — OCONUS airports (AK/HI/PR/Guam) will "
                "have NO wind forecast data. Check /LDM/models/gfs/0p25/"
            )

    log.info("Ingest run complete.")


if __name__ == '__main__':
    main()

