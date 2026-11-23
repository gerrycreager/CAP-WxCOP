#!/usr/bin/env python3
"""Load the FAA Digital Obstacle File (DOF) national data file into
nasr.obstructions, for the aviation-detail map layer (see the "master"
low-zoom layer scoping discussion, 2026-11-22).

This is the legacy fixed-width DOF.DAT format (NOT the Daily-DOF CSV
variant, which has pre-parsed decimal lat/lon) -- the column byte
offsets below are taken verbatim from DOF_README.pdf's "THE DOF FORMAT"
table and verified against real sample rows. Lat/lon are DMS and need
converting to decimal; AGL/AMSL heights are plain integers; the action
date is a Julian (YYYYDDD) field.

Published every 56 days as a single national DOF.DAT (~657k records as
of the 2026-09-27 cycle) -- same TRUNCATE + bulk reload approach as
ingest_nasr.py, for the same reason (full snapshot each cycle, not an
incremental feed -- anything FAA retired just isn't in the new file;
there's also a CHG.DAT change-list in each cycle's zip if an
incremental approach is ever wanted instead).

Deliberately NOT filtering by height at ingest time -- load everything
and let the API/frontend filter by a min_agl_ft parameter, so the
height cutoff used for display can change without re-ingesting. 657k
rows with a GIST index is a non-issue for Postgres (same ballpark as
nasr.fixes scaled up ~9x).

Usage: unzip a DOF cycle (see faa.gov/.../digital_products/dof/), then:
    ingest_dof.py --file /path/to/extracted/DOF.DAT
"""
import argparse
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)


def nz(v):
    v = (v or '').strip()
    return v if v else None


def dms_to_decimal(deg, minutes, seconds, hemi):
    try:
        d, m, s = float(deg), float(minutes), float(seconds)
    except (TypeError, ValueError):
        return None
    value = d + m / 60.0 + s / 3600.0
    return -value if hemi in ('S', 'W') else value


def parse_int(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def julian_to_date(v):
    v = (v or '').strip()
    if len(v) != 7:
        return None
    try:
        year, day_of_year = int(v[:4]), int(v[4:])
        return date(year, 1, 1) + timedelta(days=day_of_year - 1)
    except ValueError:
        return None


def parse_line(line):
    # Column offsets per DOF_README.pdf "THE DOF FORMAT" (1-indexed,
    # inclusive, in the README; converted to 0-indexed Python slices here).
    if len(line) < 127:
        return None

    oas_number = line[0:9].strip()
    if not oas_number:
        return None

    lat = dms_to_decimal(line[35:37], line[38:40], line[41:46], line[46:47].strip())
    lon = dms_to_decimal(line[48:51], line[52:54], line[55:60], line[60:61].strip())
    if lat is None or lon is None:
        return None

    return (
        oas_number,
        nz(line[10:11]),            # verification_status: O/U
        nz(line[12:14]),            # country_code
        nz(line[15:17]),            # state_code
        nz(line[18:34]),            # city
        lon, lat,
        nz(line[62:80]),            # obstacle_type
        parse_int(line[81:82]),     # quantity
        parse_int(line[83:88]),     # agl_ft
        parse_int(line[89:94]),     # amsl_ft
        nz(line[95:96]),            # lighting
        nz(line[97:98]),            # horiz_accuracy
        nz(line[99:100]),           # vert_accuracy
        nz(line[101:102]),          # mark_indicator
        nz(line[103:117]),          # faa_study_number
        nz(line[118:119]),          # action: A/C
        julian_to_date(line[120:127]),  # action_date
    )


def load_obstructions(conn, dof_path):
    values = []
    skipped = 0
    with open(dof_path, encoding='latin-1') as f:
        lines = f.readlines()
    # First 4 lines are the currency-date header, two column-header lines,
    # and a line of "-" separators -- not data.
    for line in lines[4:]:
        line = line.rstrip('\r\n')
        if not line.strip():
            continue
        row = parse_line(line)
        if row is None:
            skipped += 1
            continue
        values.append(row)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE nasr.obstructions")
        execute_values(cur, """
            INSERT INTO nasr.obstructions (
                oas_number, verification_status, country_code, state_code, city,
                geom, obstacle_type, quantity, agl_ft, amsl_ft, lighting,
                horiz_accuracy, vert_accuracy, mark_indicator, faa_study_number,
                action, action_date
            ) VALUES %s ON CONFLICT (oas_number) DO NOTHING
        """, values, template="""(
            %s, %s, %s, %s, %s,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s
        )""", page_size=1000)
    conn.commit()
    log.info('loaded %d obstructions (%d rows skipped: unparseable or missing coordinates)',
              len(values), skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True, help='path to extracted national DOF.DAT')
    args = ap.parse_args()

    conn = get_connection()
    try:
        load_obstructions(conn, args.file)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
