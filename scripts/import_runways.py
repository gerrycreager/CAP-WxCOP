#!/usr/bin/env python3
"""
import_runways.py — Create and populate observations.runways from OurAirports data.

Filters to:
  - Paved surfaces only (ASP*, CON*, PEM*, BIT*, TAR*, MAC*)
  - Length >= 2500 ft (CAP minimum for normal fixed-wing operations per CAPR 70-1)
  - Matches airports in observations.airports by station_id = airport_ident
  - Excludes closed runways

Both runway ends (le and he) are stored so crosswind calculation can pick the
best-aligned end for any wind direction.

Usage:
  python3 import_runways.py [--dry-run] [--csv /path/to/runways.csv]

Run once, then re-run whenever OurAirports data is refreshed.
"""

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN      = os.environ.get('DB_DSN',
                             'dbname=avwx_data user=avwx_user host=192.168.0.60')
CSV_DEFAULT = Path('/var/www/cap_winds_dev/.cache/runways.csv')
MIN_LENGTH_FT = 2500

# OurAirports surface codes considered paved.
# Match on prefix — actual values include ASPH-G, ASPH/CONC, CONC, etc.
PAVED_PREFIXES = ('ASP', 'CON', 'PEM', 'BIT', 'TAR', 'MAC', 'TURF/ASP')

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS observations.runways (
    id                  SERIAL PRIMARY KEY,
    airport_id          INTEGER NOT NULL
                            REFERENCES observations.airports(id) ON DELETE CASCADE,
    station_id          VARCHAR(10) NOT NULL,   -- ICAO / KQxx identifier
    ourairports_id      INTEGER,                 -- runways.csv id column
    le_ident            VARCHAR(6),              -- e.g. "09L"
    le_heading_degt     REAL,                    -- true heading, low end
    le_length_ft        INTEGER,                 -- same for both ends
    he_ident            VARCHAR(6),              -- e.g. "27R"
    he_heading_degt     REAL,                    -- true heading, high end
    surface             VARCHAR(40),
    lighted             BOOLEAN,
    closed              BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast airport lookup
CREATE INDEX IF NOT EXISTS runways_airport_id_idx
    ON observations.runways (airport_id);
CREATE INDEX IF NOT EXISTS runways_station_id_idx
    ON observations.runways (station_id);
"""

UPSERT_SQL = """
INSERT INTO observations.runways
    (airport_id, station_id, ourairports_id,
     le_ident, le_heading_degt, le_length_ft,
     he_ident, he_heading_degt,
     surface, lighted, closed)
VALUES %s
ON CONFLICT DO NOTHING;
"""

# ---------------------------------------------------------------------------
# Surface classification
# ---------------------------------------------------------------------------
def is_paved(surface: str) -> bool:
    """Return True if the surface string indicates a paved runway."""
    if not surface:
        return False
    s = surface.strip().upper()
    return any(s.startswith(p) for p in PAVED_PREFIXES)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Import OurAirports runway data')
    parser.add_argument('--dry-run', action='store_true',
                        help='Parse and report counts without writing to DB')
    parser.add_argument('--csv', type=Path, default=CSV_DEFAULT,
                        help=f'Path to runways.csv (default: {CSV_DEFAULT})')
    parser.add_argument('--drop', action='store_true',
                        help='Drop and recreate the runways table before import')
    args = parser.parse_args()

    if not args.csv.exists():
        log.error(f'runways.csv not found: {args.csv}')
        sys.exit(1)

    # ── Load runway CSV ──────────────────────────────────────────────────────
    log.info(f'Reading {args.csv}')
    all_rows = []
    skipped_closed = skipped_unpaved = skipped_short = skipped_noheading = 0

    with open(args.csv, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip closed
            if row.get('closed', '0').strip() == '1':
                skipped_closed += 1
                continue
            # Skip unpaved
            if not is_paved(row.get('surface', '')):
                skipped_unpaved += 1
                continue
            # Skip short
            try:
                length = int(float(row['length_ft'])) if row['length_ft'] else 0
            except (ValueError, KeyError):
                length = 0
            if length < MIN_LENGTH_FT:
                skipped_short += 1
                continue
            # Skip if both headings missing (can't compute crosswind)
            le_hdg = row.get('le_heading_degT', '').strip()
            he_hdg = row.get('he_heading_degT', '').strip()
            if not le_hdg and not he_hdg:
                skipped_noheading += 1
                continue

            all_rows.append({
                'ident':     row['airport_ident'].strip().upper(),
                'oa_id':     int(row['id']) if row['id'] else None,
                'le_ident':  row.get('le_ident', '').strip() or None,
                'le_hdg':    float(le_hdg) if le_hdg else None,
                'he_ident':  row.get('he_ident', '').strip() or None,
                'he_hdg':    float(he_hdg) if he_hdg else None,
                'length':    length,
                'surface':   row.get('surface', '').strip()[:40],
                'lighted':   row.get('lighted', '0').strip() == '1',
            })

    log.info(f'CSV rows kept: {len(all_rows):,}')
    log.info(f'  Skipped closed: {skipped_closed:,}')
    log.info(f'  Skipped unpaved: {skipped_unpaved:,}')
    log.info(f'  Skipped <{MIN_LENGTH_FT}ft: {skipped_short:,}')
    log.info(f'  Skipped no heading: {skipped_noheading:,}')

    if args.dry_run:
        log.info('DRY RUN — not writing to database')
        # Show sample
        for r in all_rows[:5]:
            le = f"{r['le_hdg']:.0f}" if r['le_hdg'] is not None else '---'
            he = f"{r['he_hdg']:.0f}" if r['he_hdg'] is not None else '---'
            log.info(f"  Sample: {r['ident']} Rwy {r['le_ident']}/{r['he_ident']} "
                     f"{le}/{he}° {r['length']}ft {r['surface']}")
        return

    # ── Connect to DB ────────────────────────────────────────────────────────
    log.info(f'Connecting to {DB_DSN}')
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Optional drop
            if args.drop:
                log.info('Dropping existing runways table...')
                cur.execute('DROP TABLE IF EXISTS observations.runways CASCADE;')

            # Create table
            log.info('Creating observations.runways if not exists...')
            cur.execute(CREATE_TABLE_SQL)

            # Load airport id map: station_id -> id
            log.info('Loading airport station_id -> id map...')
            cur.execute('SELECT id, station_id FROM observations.airports;')
            airport_map = {row[1].upper(): row[0] for row in cur.fetchall()}
            log.info(f'  {len(airport_map):,} airports in DB')

            # Build insert records
            records = []
            skipped_no_airport = 0
            for r in all_rows:
                apt_id = airport_map.get(r['ident'])
                if apt_id is None:
                    skipped_no_airport += 1
                    continue
                records.append((
                    apt_id,
                    r['ident'],
                    r['oa_id'],
                    r['le_ident'],
                    r['le_hdg'],
                    r['length'],
                    r['he_ident'],
                    r['he_hdg'],
                    r['surface'],
                    r['lighted'],
                    False,  # closed already filtered
                ))

            log.info(f'Runways matched to airports: {len(records):,}')
            log.info(f'Skipped (airport not in DB): {skipped_no_airport:,}')

            if not records:
                log.warning('No records to insert — check airport_ident matching')
                conn.rollback()
                return

            # Insert
            log.info('Inserting runway records...')
            psycopg2.extras.execute_values(cur, UPSERT_SQL, records, page_size=500)
            conn.commit()
            log.info(f'Done — {len(records):,} runway records inserted.')

            # Summary
            cur.execute("""
                SELECT COUNT(DISTINCT station_id) as airports,
                       COUNT(*) as runways
                FROM observations.runways;
            """)
            row = cur.fetchone()
            log.info(f'Table now has {row[1]:,} runways across {row[0]:,} airports.')

            # Sample crosswind-capable airports
            cur.execute("""
                SELECT r.station_id, a.name,
                       COUNT(*) as rwy_count,
                       array_agg(r.le_ident || '/' || r.he_ident) as runways
                FROM observations.runways r
                JOIN observations.airports a ON a.id = r.airport_id
                WHERE r.le_heading_degt IS NOT NULL
                  AND r.he_heading_degt IS NOT NULL
                GROUP BY r.station_id, a.name
                ORDER BY rwy_count DESC
                LIMIT 10;
            """)
            log.info('Top airports by runway count:')
            for row in cur.fetchall():
                log.info(f'  {row[0]:6s} {row[1][:35]:35s} {row[2]} rwy: {row[3]}')

    except Exception as e:
        conn.rollback()
        log.error(f'Import failed: {e}')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()

