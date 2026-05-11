#!/usr/bin/env python3
"""
ingest_aigfs_impacts.py — CAPR 70-1 Wind Impacts ingest from AIGFS for OCONUS airports.

Reads AIGFS surface grib2 files from /LDM/models/AIGFS/AIGFS_YYYYMMDD_HHMM_sfc.grib2.
AIGFS cycles: 0000, 0600, 1200, 1800 UTC, 9 forecast steps at 6-hour intervals (F000-F048).

Variables available in AIGFS sfc:
  u10  — 10m U-wind component (m/s)
  v10  — 10m V-wind component (m/s)
  t2m  — 2m temperature (K)
  tp   — total precipitation (not used for VFR stoplight)

NOTE: AIGFS does NOT provide ceiling or visibility. VFR stoplight is WIND ONLY.
      Ceiling/visibility shown as UNKNOWN. This is clearly labeled in the UI.

OCONUS sectors covered:
  AK  — Alaska (lat 51-72, lon -180 to +180, handles antimeridian)
  HI  — Hawaii (lat 18-23, lon -162 to -154)
  PR  — Puerto Rico / USVI (lat 17-19, lon -68 to -64)
  GU  — Guam / CNMI (lat 13-16, lon 144-146)

Output: observations.airport_wx_impacts (upsert, model_source='AIGFS')
        source_priority=3 (GLMP=1, HRRR=2, AIGFS=3, LAMP=4)

Runs on data1 every 6 hours after AIGFS arrives (~1-2 hrs after cycle time).
Cron: 15 1,7,13,19 * * * (15 min after expected arrival)
"""

import os
import sys
import fcntl
import logging
import math
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cfgrib
import psycopg2
from psycopg2.extras import execute_values
from scipy.spatial import KDTree

# ── Configuration ──────────────────────────────────────────────────────────────
AIGFS_DIR       = Path('/LDM/models/AIGFS')
LOCKFILE        = '/home/ldm/var/run/ingest_aigfs_impacts.lock'
CFGRIB_IDX_DIR  = '/tmp/cfgrib_aigfs_idx'

DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'

# OCONUS sector bounds (lat_min, lat_max, lon_min, lon_max)
# AK uses two ranges to handle antimeridian — handled specially
SECTORS = {
    'AK': (51.0,  72.0, -180.0,  180.0),   # full lon range, AK spans antimeridian
    'HI': (18.0,  23.0, -162.0, -154.0),
    'PR': (17.0,  19.5,  -68.5,  -64.0),
    'GU': (13.0,  16.0,  144.0,  147.0),
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)


# ── Unit conversions ───────────────────────────────────────────────────────────
def ms_to_kts(ms):
    return ms * 1.94384

def k_to_f(k):
    return (k - 273.15) * 9/5 + 32


# ── Database ───────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS)


def load_oconus_airports(conn):
    """Load OCONUS airports (AK/HI/PR/GU/CNMI) with runways and ICL overrides."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                a.id, a.station_id,
                ST_X(a.location::geometry) AS lon,
                ST_Y(a.location::geometry) AS lat,
                a.is_military,
                r.le_heading_degt, r.he_heading_degt,
                r.le_length_ft,
                -- Wing ICL overrides
                MAX(CASE WHEN wi.parameter='wind_vfr_yellow'      THEN wi.threshold END) as wind_vfr_y,
                MAX(CASE WHEN wi.parameter='wind_vfr_red'         THEN wi.threshold END) as wind_vfr_r,
                MAX(CASE WHEN wi.parameter='crosswind_vfr_yellow' THEN wi.threshold END) as xw_vfr_y,
                MAX(CASE WHEN wi.parameter='crosswind_vfr_red'    THEN wi.threshold END) as xw_vfr_r,
                MAX(CASE WHEN wi.parameter='crosswind_ifr_yellow' THEN wi.threshold END) as xw_ifr_y,
                MAX(CASE WHEN wi.parameter='crosswind_ifr_red'    THEN wi.threshold END) as xw_ifr_r,
                MAX(CASE WHEN wi.parameter='ceil_vfr_yellow'      THEN wi.threshold END) as ceil_vfr_y,
                MAX(CASE WHEN wi.parameter='ceil_vfr_red'         THEN wi.threshold END) as ceil_vfr_r,
                MAX(CASE WHEN wi.parameter='vis_vfr_yellow'       THEN wi.threshold END) as vis_vfr_y,
                MAX(CASE WHEN wi.parameter='vis_vfr_red'          THEN wi.threshold END) as vis_vfr_r,
                MAX(CASE WHEN wi.parameter='temp_cold_yellow'     THEN wi.threshold END) as tmp_cold_y,
                MAX(CASE WHEN wi.parameter='temp_cold_red'        THEN wi.threshold END) as tmp_cold_r
            FROM observations.airports a
            JOIN observations.runways r ON r.airport_id = a.id
            LEFT JOIN observations.wing_map wm ON wm.iso_region = a.iso_region
            LEFT JOIN observations.wing_icl wi ON (
                wi.wing_id = wm.wing_id OR wi.wing_id = wm.region_code
            ) AND (wi.expires IS NULL OR wi.expires > NOW())
            WHERE a.has_paved_runway = true
              AND r.le_length_ft >= 2500
              AND r.surface NOT ILIKE '%water%'
              AND r.closed = false
              AND a.location IS NOT NULL
              AND a.iso_region IN ('US-AK','US-HI','PR-U-A','GU-U-A','MP-U-A')
            GROUP BY a.id, a.station_id, a.location, a.is_military,
                     r.le_heading_degt, r.he_heading_degt, r.le_length_ft
            ORDER BY a.station_id
        """)
        rows = cur.fetchall()
    conn.commit()
    log.info(f"Loaded {len(rows)} OCONUS qualifying airport/runway combinations")
    return rows


# ── AIGFS file handling ────────────────────────────────────────────────────────
def find_latest_aigfs_cycle():
    """Find the latest complete AIGFS sfc file. Cycles at 0000/0600/1200/1800Z."""
    now = datetime.now(timezone.utc)
    # Look back up to 12 hours
    for delta_h in range(0, 13):
        candidate = now - timedelta(hours=delta_h)
        # Round down to nearest 6-hour cycle
        cycle_h = (candidate.hour // 6) * 6
        cycle_dt = candidate.replace(hour=cycle_h, minute=0, second=0, microsecond=0)
        date_str = cycle_dt.strftime('%Y%m%d')
        hhmm = cycle_dt.strftime('%H%M')
        fname = AIGFS_DIR / f"AIGFS_{date_str}_{hhmm}_sfc.grib2"
        if fname.exists() and fname.stat().st_size > 1000000:
            log.info(f"Found AIGFS cycle: {fname.name}")
            return fname, cycle_dt
        # avoid checking same cycle multiple times
        if delta_h > 0 and candidate.hour // 6 == (now - timedelta(hours=delta_h-1)).hour // 6:
            continue

    return None, None


def build_global_kdtree(sfc_file):
    """Build KDTree from AIGFS global 0.25° grid, converting lons to -180/+180."""
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    ds = cfgrib.open_dataset(str(sfc_file),
                             filter_by_keys={'shortName': '10u'})
    lats = ds['latitude'].values   # 1D, 721 points
    lons = ds['longitude'].values  # 1D, 1440 points, 0-359.75
    lons_180 = np.where(lons > 180, lons - 360, lons)
    ds.close()

    # Build 2D meshgrid
    LON2D, LAT2D = np.meshgrid(lons_180, lats)
    flat_lats = LAT2D.flatten()
    flat_lons = LON2D.flatten()

    tree = KDTree(np.column_stack([flat_lats, flat_lons]))
    log.info(f"AIGFS KDTree built: {len(lats)}×{len(lons)} = {len(flat_lats):,} points")
    return tree, flat_lats, flat_lons, lats, lons_180


def read_aigfs_wind(sfc_file):
    """Read u10, v10 from AIGFS sfc file. Returns (u, v) each shape (n_steps, nlat, nlon)."""
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    u_ds = cfgrib.open_dataset(str(sfc_file), filter_by_keys={'shortName': '10u'})
    v_ds = cfgrib.open_dataset(str(sfc_file), filter_by_keys={'shortName': '10v'})
    u = u_ds['u10'].values   # (n_steps, nlat, nlon)
    v = v_ds['v10'].values
    u_ds.close()
    v_ds.close()
    return u, v


def read_aigfs_temp(sfc_file):
    """Read t2m from AIGFS sfc file. Returns array shape (n_steps, nlat, nlon)."""
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    try:
        ds = cfgrib.open_dataset(str(sfc_file), filter_by_keys={'shortName': '2t'})
        t = ds['t2m'].values
        ds.close()
        return t
    except Exception as e:
        log.warning(f"Could not read t2m: {e}")
        return None


# ── Airport filtering per sector ───────────────────────────────────────────────
def filter_airports_for_sector(airports, sector):
    lat_min, lat_max, lon_min, lon_max = SECTORS[sector]
    result = []
    for a in airports:
        lon, lat = a[2], a[3]
        # Handle AK antimeridian: AK airports can have lon near +180 or -180
        if sector == 'AK':
            # Accept any lon for AK since bounds are -180 to +180
            if lat_min <= lat <= lat_max:
                result.append(a)
        else:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                result.append(a)
    return result


# ── Main processing ────────────────────────────────────────────────────────────
def main():
    # Lock
    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.warning("Another AIGFS instance is running — exiting")
        sys.exit(0)

    log.info("=" * 60)
    log.info(f"AIGFS impacts ingest started: "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")
    t0 = datetime.now()

    # Find latest AIGFS cycle
    sfc_file, cycle_dt = find_latest_aigfs_cycle()
    if sfc_file is None:
        log.error("No AIGFS sfc file found — exiting")
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        sys.exit(1)

    conn = get_conn()
    airports = load_oconus_airports(conn)
    if not airports:
        log.warning("No OCONUS airports found")
        conn.close()
        sys.exit(0)

    # Build global KDTree
    tree, flat_lats, flat_lons, lats_1d, lons_1d = build_global_kdtree(sfc_file)

    # Read wind and temp arrays
    log.info("Reading AIGFS wind fields...")
    u_all, v_all = read_aigfs_wind(sfc_file)
    t_all = read_aigfs_temp(sfc_file)

    n_steps = u_all.shape[0]
    log.info(f"AIGFS: {n_steps} forecast steps")

    total = 0
    spriority = 3  # AIGFS priority

    for sector_name, _ in SECTORS.items():
        sect_airports = filter_airports_for_sector(airports, sector_name)
        if not sect_airports:
            log.info(f"AIGFS {sector_name}: no qualifying airports")
            continue

        log.info(f"AIGFS {sector_name}: {len(sect_airports)} airports")

        # Map airports to grid indices
        coords = np.array([(a[3], a[2]) for a in sect_airports])  # (lat, lon)
        _, indices = tree.query(coords, workers=-1)

        n_apts = len(sect_airports)
        apt_ids    = np.array([a[0] for a in sect_airports], dtype=np.int64)
        apt_sids   = [a[1] for a in sect_airports]
        apt_le_hdg = np.array([a[5] if a[5] is not None else np.nan for a in sect_airports])
        apt_he_hdg = np.array([a[6] if a[6] is not None else np.nan for a in sect_airports])

        def icl_arr(idx, default):
            return np.array([a[idx] if a[idx] is not None else default
                             for a in sect_airports])

        icl_wind_y   = icl_arr(8,  21.0)
        icl_wind_r   = icl_arr(9,  30.0)
        icl_xw_vfr_y = icl_arr(10,  8.0)
        icl_xw_vfr_r = icl_arr(11, 15.0)
        icl_tc_y     = icl_arr(18, 20.0)
        icl_tc_r     = icl_arr(19, -10.0)

        now_utc = datetime.now(timezone.utc)

        for step_idx in range(n_steps):
            # AIGFS steps are 6-hourly: F000, F006, F012, ...
            fhr = step_idx * 6
            valid_time = cycle_dt + timedelta(hours=fhr)

            # Extract wind at airport grid points
            u_flat = u_all[step_idx].flatten()
            v_flat = v_all[step_idx].flatten()
            u_v = u_flat[indices]
            v_v = v_flat[indices]

            # Wind speed and direction
            wind_kts = ms_to_kts(np.sqrt(u_v**2 + v_v**2))
            # Direction: meteorological (direction FROM)
            wdir_deg = (np.degrees(np.arctan2(-u_v, -v_v)) % 360)

            # Temperature
            if t_all is not None:
                t_flat = t_all[step_idx].flatten()
                tmp_f = k_to_f(t_flat[indices])
            else:
                tmp_f = np.full(n_apts, np.nan)

            # Wind chill (NWS formula)
            wind_mph = wind_kts * 1.15078
            wc_valid = (tmp_f <= 50) & (wind_mph >= 3) & ~np.isnan(tmp_f)
            wc_f = np.where(wc_valid,
                            35.74 + 0.6215*tmp_f - 35.75*(wind_mph**0.16)
                            + 0.4275*tmp_f*(wind_mph**0.16),
                            np.nan)

            # Best runway crosswind
            nan_le = np.isnan(apt_le_hdg) | np.isnan(wind_kts)
            nan_he = np.isnan(apt_he_hdg) | np.isnan(wind_kts)

            def xwind_vec(hdg):
                return np.abs(wind_kts * np.sin(np.deg2rad(wdir_deg - hdg)))
            def headwind_vec(hdg):
                return wind_kts * np.cos(np.deg2rad(wdir_deg - hdg))

            xw_le = np.where(nan_le, np.nan, xwind_vec(apt_le_hdg))
            xw_he = np.where(nan_he, np.nan, xwind_vec(apt_he_hdg))
            hw_le = np.where(nan_le, np.nan, headwind_vec(apt_le_hdg))
            hw_he = np.where(nan_he, np.nan, headwind_vec(apt_he_hdg))
            hw_diff = np.where(~nan_le & ~nan_he, hw_he - hw_le, np.nan)
            use_he = (
                (~nan_he & (np.nan_to_num(hw_diff) > 0.5)) |
                (~nan_he & ~nan_le & (np.abs(np.nan_to_num(hw_diff)) <= 0.5) & (xw_he < xw_le)) |
                (nan_le & ~nan_he)
            )
            xw = np.where(use_he, xw_he, xw_le)
            xw = np.where(np.isnan(xw), np.fmin(xw_le, xw_he), xw)
            best_hdg = np.where(use_he, apt_he_hdg, apt_le_hdg)

            # ── Stoplights: WIND ONLY (no ceil/vis from AIGFS) ────────────────
            RED=0; YELLOW=1; GREEN=2; UNK=3

            def c_wind(kts):
                r = np.full(n_apts, GREEN)
                r = np.where(kts > icl_wind_y, YELLOW, r)
                r = np.where(kts > icl_wind_r, RED, r)
                r = np.where(np.isnan(kts), UNK, r)
                return r

            def c_xwind(kts):
                r = np.full(n_apts, GREEN)
                r = np.where(kts > icl_xw_vfr_y, YELLOW, r)
                r = np.where(kts > icl_xw_vfr_r, RED, r)
                r = np.where(np.isnan(kts), UNK, r)
                return r

            def c_tmp_cold(f):
                r = np.full(n_apts, GREEN)
                r = np.where(f <= icl_tc_y, YELLOW, r)
                r = np.where(f < icl_tc_r, RED, r)
                r = np.where(np.isnan(f), UNK, r)
                return r

            def c_wc(f):
                r = np.full(n_apts, GREEN)
                r = np.where(f <= 22, YELLOW, r)
                r = np.where(f < 0, RED, r)
                r = np.where(np.isnan(f), GREEN, r)
                return r

            # VFR: wind + crosswind + temp (no ceil/vis — AIGFS limitation)
            vfr_stack = np.stack([
                c_wind(wind_kts),
                c_xwind(xw),
                c_tmp_cold(tmp_f),
                c_wc(wc_f),
            ])
            VFR_NAMES = ['wind', 'crosswind', 'temp_cold', 'wind_chill']

            vfr_worst_idx = np.argmin(vfr_stack, axis=0)
            vfr_color_int = vfr_stack[vfr_worst_idx, np.arange(n_apts)]
            INT_TO_COLOR  = {RED:'RED', YELLOW:'YELLOW', GREEN:'GREEN', UNK:'UNKNOWN'}

            # Build rows
            rows = []
            for i in range(n_apts):
                wk  = float(wind_kts[i]) if not np.isnan(wind_kts[i]) else None
                wd  = int(wdir_deg[i])   if not np.isnan(wdir_deg[i]) else None
                tf  = float(tmp_f[i])    if not np.isnan(tmp_f[i])    else None
                wf  = float(wc_f[i])     if not np.isnan(wc_f[i])     else None
                xwv = float(xw[i])       if not np.isnan(xw[i])       else None
                bh  = int(best_hdg[i])   if not np.isnan(best_hdg[i]) else None

                vc  = INT_TO_COLOR[int(vfr_color_int[i])]
                vw  = VFR_NAMES[int(vfr_worst_idx[i])] if vc != 'GREEN' else None

                rows.append((
                    int(apt_ids[i]),
                    apt_sids[i],
                    'AIGFS',
                    cycle_dt,
                    valid_time,
                    fhr,
                    None,    # ceil_ft — not available from AIGFS
                    None,    # vis_m   — not available from AIGFS
                    round(wk, 1) if wk is not None else None,
                    wd,
                    None,    # wind_gust — not in AIGFS sfc
                    None,    # tmp_c
                    None,    # dpt_c
                    round(tf, 1) if tf is not None else None,
                    None,    # heat_index_f
                    round(wf, 1) if wf is not None else None,
                    round(xwv, 1) if xwv is not None else None,
                    bh,
                    vc,
                    vw,
                    'UNKNOWN',   # ifr_color — no ceil/vis
                    None,        # ifr_worst_param
                    spriority,
                    now_utc
                ))

            # Deduplicate: keep best (min crosswind) per airport
            seen = {}
            for row in rows:
                key = row[0]
                if key not in seen:
                    seen[key] = row
                else:
                    xw_new = row[16] if row[16] is not None else 9999
                    xw_old = seen[key][16] if seen[key][16] is not None else 9999
                    if xw_new < xw_old:
                        seen[key] = row
            rows = list(seen.values())

            with conn.cursor() as cur:
                execute_values(cur, """
                    INSERT INTO observations.airport_wx_impacts
                        (airport_id, station_id, model_source, model_run,
                         valid_time, forecast_hour,
                         ceil_ft, vis_m, wind_speed_kts, wind_dir, wind_gust_kts,
                         tmp_c, dpt_c, tmp_f, heat_index_f, wind_chill_f,
                         crosswind_kts, best_runway_hdg,
                         vfr_color, vfr_worst_param, ifr_color, ifr_worst_param,
                         source_priority, ingested_at)
                    VALUES %s
                    ON CONFLICT (airport_id, model_run, forecast_hour)
                    DO UPDATE SET
                        vfr_color       = EXCLUDED.vfr_color,
                        vfr_worst_param = EXCLUDED.vfr_worst_param,
                        ifr_color       = EXCLUDED.ifr_color,
                        ifr_worst_param = EXCLUDED.ifr_worst_param,
                        wind_speed_kts  = EXCLUDED.wind_speed_kts,
                        wind_dir        = EXCLUDED.wind_dir,
                        tmp_f           = EXCLUDED.tmp_f,
                        wind_chill_f    = EXCLUDED.wind_chill_f,
                        crosswind_kts   = EXCLUDED.crosswind_kts,
                        best_runway_hdg = EXCLUDED.best_runway_hdg,
                        source_priority = EXCLUDED.source_priority,
                        ingested_at     = EXCLUDED.ingested_at
                """, rows)
            conn.commit()
            total += len(rows)
            log.info(f"  AIGFS {sector_name} F{fhr:03d}: {len(rows)} upserted "
                     f"(valid {valid_time.strftime('%Y-%m-%d %H:%MZ')})")

    elapsed = (datetime.now() - t0).total_seconds()
    log.info(f"Total: {total} records in {elapsed:.0f}s")

    # Scour old AIGFS records — keep only latest 2 cycles (12 hours)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM observations.airport_wx_impacts
                WHERE model_source = 'AIGFS'
                  AND model_run < (
                      SELECT MAX(model_run) - INTERVAL '13 hours'
                      FROM observations.airport_wx_impacts
                      WHERE model_source = 'AIGFS'
                  )
            """)
            log.info(f"Scoured {cur.rowcount} old AIGFS records")
        conn.commit()
    except Exception as e:
        log.warning(f"Scour failed: {e}")

    log.info("AIGFS impacts ingest complete")
    log.info("=" * 60)

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    conn.close()


if __name__ == '__main__':
    main()
