#!/usr/bin/env python3
"""Load FAA NASR 28-day subscription CSV data (airports/heliports,
runways, runway ends, navaids, fixes) for the aviation-detail map
layer Gerry asked for 2026-11-19 -- NavAids/fixes/airfields/heliports,
with runways depicted by their actual endpoints rather than a generic
dot.

NASR is a full snapshot each 28-day cycle, not an incremental feed --
every airport/navaid/fix that still exists is in the new file whether
or not it changed, and anything retired just isn't. So this does
TRUNCATE + bulk reload per table on every run, not an upsert: that's
the correct, simple way to drop anything FAA retired without tracking
deletions ourselves.

Usage: download+extract a NASR CSV cycle (see faa.gov/.../NASR_Subscription/
for the current cycle's "*_CSV.zip"), then:
    ingest_nasr.py --dir /path/to/extracted/csv/files
"""
import argparse
import csv
import logging
import sys

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)


def nz(v):
    v = (v or '').strip()
    return v if v else None


def nf(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def read_csv(path):
    with open(path, encoding='latin-1') as f:
        return list(csv.DictReader(f))


def load_airports(conn, csv_dir):
    rows = read_csv(f'{csv_dir}/APT_BASE.csv')
    values = []
    for r in rows:
        lat, lon = nf(r['LAT_DECIMAL']), nf(r['LONG_DECIMAL'])
        if lat is None or lon is None:
            continue
        values.append((
            r['SITE_NO'].strip(), r['SITE_TYPE_CODE'].strip(), nz(r['ARPT_ID']),
            nz(r['ICAO_ID']), nz(r['ARPT_NAME']), nz(r['CITY']), nz(r['STATE_CODE']),
            lon, lat, nf(r['ELEV']), nz(r['OWNERSHIP_TYPE_CODE']), nz(r['FACILITY_USE_CODE']),
            r['EFF_DATE'].strip(),
        ))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.airports")
        execute_values(cur, """
            INSERT INTO nasr.airports (
                site_no, site_type_code, arpt_id, icao_id, arpt_name, city,
                state_code, geom, elevation_ft, ownership_code, facility_use, eff_date
            ) VALUES %s ON CONFLICT (site_no) DO NOTHING
        """, values, template="""(
            %s, %s, %s, %s, %s, %s, %s,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s
        )""", page_size=1000)
    conn.commit()
    log.info('loaded %d airports/heliports', len(values))


def load_runways(conn, csv_dir):
    rows = read_csv(f'{csv_dir}/APT_RWY.csv')
    values = []
    for r in rows:
        values.append((
            r['SITE_NO'].strip(), r['RWY_ID'].strip(),
            int(nf(r['RWY_LEN'])) if nf(r['RWY_LEN']) else None,
            int(nf(r['RWY_WIDTH'])) if nf(r['RWY_WIDTH']) else None,
            nz(r['SURFACE_TYPE_CODE']), nz(r['COND']),
        ))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.runways")
        execute_values(cur, """
            INSERT INTO nasr.runways (site_no, rwy_id, rwy_len_ft, rwy_width_ft, surface_code, cond)
            VALUES %s ON CONFLICT (site_no, rwy_id) DO NOTHING
        """, values, page_size=1000)
    conn.commit()
    log.info('loaded %d runways', len(values))


def load_runway_ends(conn, csv_dir):
    rows = read_csv(f'{csv_dir}/APT_RWY_END.csv')
    values = []
    for r in rows:
        lat, lon = nf(r['LAT_DECIMAL']), nf(r['LONG_DECIMAL'])
        if lat is None or lon is None:
            continue
        values.append((
            r['SITE_NO'].strip(), r['RWY_ID'].strip(), r['RWY_END_ID'].strip(),
            lon, lat, nf(r['TRUE_ALIGNMENT']),
        ))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.runway_ends")
        execute_values(cur, """
            INSERT INTO nasr.runway_ends (site_no, rwy_id, rwy_end_id, geom, true_alignment)
            VALUES %s ON CONFLICT (site_no, rwy_id, rwy_end_id) DO NOTHING
        """, values, template="(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)",
            page_size=1000)
    conn.commit()
    log.info('loaded %d runway ends', len(values))


def load_navaids(conn, csv_dir):
    rows = read_csv(f'{csv_dir}/NAV_BASE.csv')
    values = []
    for r in rows:
        lat, lon = nf(r['LAT_DECIMAL']), nf(r['LONG_DECIMAL'])
        if lat is None or lon is None:
            continue
        values.append((
            r['NAV_ID'].strip(), r['NAV_TYPE'].strip(), nz(r['NAME']), nz(r['STATE_CODE']),
            lon, lat, nz(r['FREQ']), nz(r['CHAN']), nf(r['ELEV']),
        ))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.navaids")
        execute_values(cur, """
            INSERT INTO nasr.navaids (nav_id, nav_type, name, state_code, geom, freq, chan, elevation_ft)
            VALUES %s ON CONFLICT (nav_id, nav_type) DO NOTHING
        """, values, template="""(
            %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s
        )""", page_size=1000)
    conn.commit()
    log.info('loaded %d navaids', len(values))


def load_fixes(conn, csv_dir):
    rows = read_csv(f'{csv_dir}/FIX_BASE.csv')
    values = []
    for r in rows:
        lat, lon = nf(r['LAT_DECIMAL']), nf(r['LONG_DECIMAL'])
        if lat is None or lon is None:
            continue
        values.append((
            r['FIX_ID'].strip(), nz(r['STATE_CODE']), lon, lat, nz(r['FIX_USE_CODE']),
        ))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.fixes")
        execute_values(cur, """
            INSERT INTO nasr.fixes (fix_id, state_code, geom, fix_use_code)
            VALUES %s ON CONFLICT (fix_id, geom) DO NOTHING
        """, values, template="(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)",
            page_size=1000)
    conn.commit()
    log.info('loaded %d fixes', len(values))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True, help='directory with extracted NASR CSV files')
    args = ap.parse_args()

    conn = get_connection()
    try:
        load_airports(conn, args.dir)
        load_runways(conn, args.dir)
        load_runway_ends(conn, args.dir)
        load_navaids(conn, args.dir)
        load_fixes(conn, args.dir)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
