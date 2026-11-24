#!/usr/bin/env python3
"""
ingest_cwop_registration.py -- weekly CWOP/APRSWXNET registration dump loader
===============================================================================
Gerry pulls a weekly "Station Table" backup from the CWOP/MADIS registration
system by hand (file like atham4backup<YYYYMMDD>.txt) into ~gerry/CWOP/.
This script loads it into the DB. See memory: project_cwop_registration.

Input format (MADIS provider Station Table, pipe-delimited, '#' = comment):

  ProviderId|AFOS(HB5)Id|StationName|Elevation|Latitude|Longitude|localTZ|
  LocationDesc|StationType|NumInst|NumLevels|Maint/CalibSch|SiteDesc|

StationName's first whitespace-separated token is the station's ham-radio
callsign (the owner/operator identity) -- the rest is a free-text place
name, e.g. "KF4HJW Tide                       VA US".

Writes to two places:
  1. observations.cwop_registration -- full-fidelity authoritative snapshot
     of the latest dump, upserted by provider_id (the stable MADIS-assigned
     per-station registration id, e.g. "ap004"). Existing stations rarely
     move; last_updated only advances on rows that actually changed.
  2. observations.madis_stations -- enrichment columns (provider_station_id,
     afos_id, reported_elevation_m, local_tz, location_desc, station_type,
     num_instruments, num_levels, maint_calib_schedule, site_desc,
     registration_dump_date/updated) are backfilled onto the matching
     APRSWXNET/mesonet2 row, joined by extracted callsign -- that's the
     only key both tables share (madis_stations' own station_id is a
     different, MADIS-internal id, not this registration's provider_id).

Run manually against a new weekly dump:
  venv/bin/python3 scripts/ingest_cwop_registration.py --file ~gerry/CWOP/atham4backup20261123.txt
"""

import argparse
import logging
import re
import sys
from datetime import datetime, date, timezone

from psycopg2.extras import execute_values

from db_config import get_connection

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('ingest_cwop_registration')

# ap004|AP004|KF4HJW Tide                       VA US|6.00| 37.10073| -76.54597|GMT|||1||||
FIELDS = [
    'provider_id', 'afos_id', 'raw_station_name', 'elevation_m',
    'latitude', 'longitude', 'local_tz', 'location_desc', 'station_type',
    'num_instruments', 'num_levels', 'maint_calib_schedule', 'site_desc',
]

# dump filenames carry the date, e.g. atham4backup20261123.txt
RE_DUMP_DATE = re.compile(r'(\d{8})')


def guess_dump_date(path: str) -> date:
    m = RE_DUMP_DATE.search(path)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y%m%d').date()
        except ValueError:
            pass
    log.warning('could not parse a date from filename %s, using today', path)
    return date.today()


def parse_dump(path: str):
    """Yield one dict per data row. Skips comments/blank lines."""
    with open(path, encoding='utf-8', errors='replace') as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if len(parts) < 7:
                log.warning('line %d: only %d fields, skipping: %r', lineno, len(parts), line[:80])
                continue
            # pad in case trailing optional fields are missing
            parts += [''] * (len(FIELDS) - len(parts))
            row = dict(zip(FIELDS, parts))
            row['raw_station_name_stripped'] = row['raw_station_name'].strip()
            callsign = row['raw_station_name_stripped'].split()[0] if row['raw_station_name_stripped'] else ''
            if not callsign:
                log.warning('line %d: no callsign extracted, skipping: %r', lineno, line[:80])
                continue
            row['callsign'] = callsign
            try:
                row['elevation_m'] = float(row['elevation_m']) if row['elevation_m'].strip() else None
                row['latitude'] = float(row['latitude'])
                row['longitude'] = float(row['longitude'])
            except ValueError:
                log.warning('line %d: bad numeric field, skipping: %r', lineno, line[:80])
                continue
            for int_field in ('num_instruments', 'num_levels'):
                v = row[int_field].strip()
                row[int_field] = int(v) if v.isdigit() else None
            yield row


def upsert_registration(conn, rows, dump_date, dry_run):
    sql = """
        INSERT INTO observations.cwop_registration
            (provider_id, afos_id, callsign, raw_station_name, elevation_m,
             geom, local_tz, location_desc, station_type, num_instruments,
             num_levels, maint_calib_schedule, site_desc, dump_date,
             first_loaded, last_updated)
        VALUES %s
        ON CONFLICT (provider_id) DO UPDATE SET
            afos_id = EXCLUDED.afos_id,
            callsign = EXCLUDED.callsign,
            raw_station_name = EXCLUDED.raw_station_name,
            elevation_m = EXCLUDED.elevation_m,
            geom = EXCLUDED.geom,
            local_tz = EXCLUDED.local_tz,
            location_desc = EXCLUDED.location_desc,
            station_type = EXCLUDED.station_type,
            num_instruments = EXCLUDED.num_instruments,
            num_levels = EXCLUDED.num_levels,
            maint_calib_schedule = EXCLUDED.maint_calib_schedule,
            site_desc = EXCLUDED.site_desc,
            dump_date = EXCLUDED.dump_date,
            last_updated = now()
        WHERE
            observations.cwop_registration.afos_id IS DISTINCT FROM EXCLUDED.afos_id OR
            observations.cwop_registration.callsign IS DISTINCT FROM EXCLUDED.callsign OR
            observations.cwop_registration.raw_station_name IS DISTINCT FROM EXCLUDED.raw_station_name OR
            observations.cwop_registration.elevation_m IS DISTINCT FROM EXCLUDED.elevation_m OR
            observations.cwop_registration.geom IS DISTINCT FROM EXCLUDED.geom OR
            observations.cwop_registration.local_tz IS DISTINCT FROM EXCLUDED.local_tz OR
            observations.cwop_registration.location_desc IS DISTINCT FROM EXCLUDED.location_desc OR
            observations.cwop_registration.station_type IS DISTINCT FROM EXCLUDED.station_type OR
            observations.cwop_registration.num_instruments IS DISTINCT FROM EXCLUDED.num_instruments OR
            observations.cwop_registration.num_levels IS DISTINCT FROM EXCLUDED.num_levels OR
            observations.cwop_registration.maint_calib_schedule IS DISTINCT FROM EXCLUDED.maint_calib_schedule OR
            observations.cwop_registration.site_desc IS DISTINCT FROM EXCLUDED.site_desc
        RETURNING
            provider_id,
            (xmax = 0) AS inserted
    """
    values = [
        (
            r['provider_id'], r['afos_id'] or None, r['callsign'], r['raw_station_name_stripped'],
            r['elevation_m'],
            f"SRID=4326;POINT({r['longitude']} {r['latitude']})",
            r['local_tz'] or None, r['location_desc'] or None, r['station_type'] or None,
            r['num_instruments'], r['num_levels'], r['maint_calib_schedule'] or None,
            r['site_desc'] or None, dump_date, datetime.now(timezone.utc), datetime.now(timezone.utc),
        )
        for r in rows
    ]
    if dry_run:
        log.info('[dry-run] would upsert %d rows into cwop_registration', len(values))
        return len(values), 0, 0

    with conn.cursor() as cur:
        results = execute_values(cur, sql, values, fetch=True)
    inserted = sum(1 for _, is_new in results if is_new)
    changed = sum(1 for _, is_new in results if not is_new)
    unchanged = len(values) - inserted - changed
    return inserted, changed, unchanged


def enrich_madis_stations(conn, dump_date, dry_run, rows=None):
    """Backfill registration fields onto matching madis_stations rows,
    joined by extracted callsign (the only key both tables share)."""
    if dry_run:
        # cwop_registration hasn't actually been written yet in dry-run mode,
        # so preview the match against the in-memory parsed rows instead.
        callsigns = list({r['callsign'] for r in rows})
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM observations.madis_stations ms
                WHERE ms.data_provider = 'APRSWXNET'
                  AND split_part(ms.station_name, ' ', 1) = ANY(%s)
            """, (callsigns,))
            n = cur.fetchone()[0]
        log.info('[dry-run] would enrich %d madis_stations rows', n)
        return n

    sql = """
        UPDATE observations.madis_stations ms
        SET
            provider_station_id = r.provider_id,
            afos_id = r.afos_id,
            reported_elevation_m = r.elevation_m,
            local_tz = r.local_tz,
            location_desc = r.location_desc,
            station_type = r.station_type,
            num_instruments = r.num_instruments,
            num_levels = r.num_levels,
            maint_calib_schedule = r.maint_calib_schedule,
            site_desc = r.site_desc,
            registration_dump_date = r.dump_date,
            registration_updated = now()
        FROM observations.cwop_registration r
        WHERE ms.data_provider = 'APRSWXNET'
          AND split_part(ms.station_name, ' ', 1) = r.callsign
          AND r.dump_date = %s
        RETURNING ms.station_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (dump_date,))
        matched = cur.rowcount
    return matched


def main():
    ap = argparse.ArgumentParser(description='Load a weekly CWOP/APRSWXNET registration dump')
    ap.add_argument('--file', metavar='PATH', required=True, help='Path to the atham4backup-style Station Table dump')
    ap.add_argument('--dump-date', metavar='YYYY-MM-DD', help='Override the dump date (default: parsed from filename)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    dump_date = (
        datetime.strptime(args.dump_date, '%Y-%m-%d').date()
        if args.dump_date else guess_dump_date(args.file)
    )

    rows = list(parse_dump(args.file))
    log.info('parsed %d valid rows from %s (dump_date=%s)', len(rows), args.file, dump_date)
    if not rows:
        log.error('no valid rows parsed, aborting')
        sys.exit(1)

    conn = get_connection()
    try:
        inserted, changed, unchanged = upsert_registration(conn, rows, dump_date, args.dry_run)
        log.info('cwop_registration: %d new, %d changed, %d unchanged', inserted, changed, unchanged)

        matched = enrich_madis_stations(conn, dump_date, args.dry_run, rows=rows)
        log.info('madis_stations: %d rows enriched with registration fields', matched)

        if args.dry_run:
            conn.rollback()
            log.info('[dry-run] rolled back, no changes written')
        else:
            conn.commit()
            log.info('committed')
    except Exception:
        conn.rollback()
        log.exception('failed, rolled back')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
