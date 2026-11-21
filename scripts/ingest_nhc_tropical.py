#!/usr/bin/env python3
"""
NHC Current Tropical Cyclones Ingestion

Polls NHC's NHC_tropical_weather_summary ArcGIS MapServer (plain HTTPS
GeoJSON -- not LDM/NOAAPort), which unifies ALL active storms across
ALL basins into single layers (no need to enumerate per-storm
AT1-AT5/EP1-EP5/CP1-CP4 groups), and stores a fresh snapshot across 3
tables for EWMC's "Tropical Cyclones" overlay: current position +
forecast points (layer 5, tau=0 vs tau>0), forecast track (layer 6),
forecast cone of uncertainty (layer 7).

Intentionally independent of the separate tropical_dashboard app's
`tropical_*` schema (which parses NHC text bulletins via LDM into its
own tables, unrelated and unwired) -- no relation, no shared table
names, and confirmed that app never queries mapservices.weather.noaa.gov
itself, so there's no risk of double-polling the same endpoint.

Write strategy: TRUNCATE + INSERT across all 3 tables inside one
transaction (all-or-nothing), not an upsert-on-objectid -- see
ingest_wpc_fronts.py's module docstring for the same reasoning (NOAA's
objectid stability across polls is not verified either way). A fetch
failure aborts before TRUNCATE. A fetch that succeeds but legitimately
returns zero storms (off-season) still proceeds to TRUNCATE+insert-
nothing -- that's correct, it clears stale storms from a prior season
rather than leaving a ghost storm displayed forever.
"""
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

import requests

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

BASE_URL = 'https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather_summary/MapServer'
LAYER_POINTS = 5
LAYER_TRACKS = 6
LAYER_CONES = 7
REQUEST_HEADERS = {
    'User-Agent': 'CAP-WxCOP/1.0 (CAP WxCOP aviation weather situational awareness tool)'
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

CREATE_TABLE_POINTS_SQL = """
    CREATE TABLE IF NOT EXISTS observations.nhc_storm_points (
        id           SERIAL PRIMARY KEY,
        basin        VARCHAR(4),
        stormname    VARCHAR(64),
        stormtype    VARCHAR(8),
        dvlbl        VARCHAR(4),
        ssnum        SMALLINT,
        stormnum     INTEGER,
        binnumber    VARCHAR(16),
        advisnum     INTEGER,
        tau          SMALLINT,
        maxwind      INTEGER,
        gust         INTEGER,
        mslp         INTEGER,
        tcdir        INTEGER,
        tcspd        INTEGER,
        advdate      VARCHAR(64),
        idp_filedate TIMESTAMP,
        geom         GEOMETRY(POINT, 4326),
        inserted_at  TIMESTAMP DEFAULT NOW()
    )
"""
CREATE_TABLE_TRACKS_SQL = """
    CREATE TABLE IF NOT EXISTS observations.nhc_storm_tracks (
        id          SERIAL PRIMARY KEY,
        basin       VARCHAR(4),
        stormname   VARCHAR(64),
        stormnum    INTEGER,
        binnumber   VARCHAR(16),
        advisnum    INTEGER,
        geom        GEOMETRY(GEOMETRY, 4326),
        inserted_at TIMESTAMP DEFAULT NOW()
    )
"""
CREATE_TABLE_CONES_SQL = """
    CREATE TABLE IF NOT EXISTS observations.nhc_storm_cones (
        id          SERIAL PRIMARY KEY,
        basin       VARCHAR(4),
        stormname   VARCHAR(64),
        stormnum    INTEGER,
        binnumber   VARCHAR(16),
        advisnum    INTEGER,
        geom        GEOMETRY(GEOMETRY, 4326),
        inserted_at TIMESTAMP DEFAULT NOW()
    )
"""
CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_nhc_storm_points_geom ON observations.nhc_storm_points USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_nhc_storm_points_key  ON observations.nhc_storm_points (basin, stormnum, tau)",
    "CREATE INDEX IF NOT EXISTS idx_nhc_storm_tracks_geom ON observations.nhc_storm_tracks USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_nhc_storm_cones_geom  ON observations.nhc_storm_cones USING GIST (geom)",
]


def epoch_ms_to_dt(v):
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def fetch_layer(layer_id):
    url = f'{BASE_URL}/{layer_id}/query'
    params = {'where': '1=1', 'outFields': '*', 'f': 'geojson', 'returnGeometry': 'true'}
    r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json().get('features', [])


def parse_points(features):
    rows = []
    for f in features:
        geom = f.get('geometry')
        if not geom:
            continue
        p = f.get('properties', {})
        rows.append({
            'basin': p.get('basin'), 'stormname': p.get('stormname'),
            'stormtype': p.get('stormtype'), 'dvlbl': p.get('dvlbl'),
            'ssnum': p.get('ssnum'), 'stormnum': p.get('stormnum'),
            'binnumber': p.get('binnumber'), 'advisnum': p.get('advisnum'),
            'tau': p.get('tau'), 'maxwind': p.get('maxwind'), 'gust': p.get('gust'),
            'mslp': p.get('mslp'), 'tcdir': p.get('tcdir'), 'tcspd': p.get('tcspd'),
            'advdate': p.get('advdate'),
            'idp_filedate': epoch_ms_to_dt(p.get('idp_filedate')),
            'geom_json': json.dumps(geom),
        })
    return rows


def parse_line_or_polygon(features):
    """Shared parse for tracks (polyline) and cones (polygon) -- same field subset."""
    rows = []
    for f in features:
        geom = f.get('geometry')
        if not geom:
            continue
        p = f.get('properties', {})
        rows.append({
            'basin': p.get('basin'), 'stormname': p.get('stormname'),
            'stormnum': p.get('stormnum'), 'binnumber': p.get('binnumber'),
            'advisnum': p.get('advisnum'),
            'geom_json': json.dumps(geom),
        })
    return rows


def ensure_schema(cur):
    cur.execute(CREATE_TABLE_POINTS_SQL)
    cur.execute(CREATE_TABLE_TRACKS_SQL)
    cur.execute(CREATE_TABLE_CONES_SQL)
    for stmt in CREATE_INDEX_SQL:
        cur.execute(stmt)


def write_snapshot(points, tracks, cones, dry_run=False):
    if dry_run:
        log.info("[DRY RUN] points=%d tracks=%d cones=%d; sample point: %s",
                  len(points), len(tracks), len(cones), points[:1])
        return

    conn = get_connection()
    try:
        cur = conn.cursor()
        ensure_schema(cur)

        cur.execute("TRUNCATE TABLE observations.nhc_storm_points")
        cur.execute("TRUNCATE TABLE observations.nhc_storm_tracks")
        cur.execute("TRUNCATE TABLE observations.nhc_storm_cones")

        for row in points:
            cur.execute("""
                INSERT INTO observations.nhc_storm_points
                    (basin, stormname, stormtype, dvlbl, ssnum, stormnum, binnumber,
                     advisnum, tau, maxwind, gust, mslp, tcdir, tcspd, advdate,
                     idp_filedate, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """, (row['basin'], row['stormname'], row['stormtype'], row['dvlbl'],
                  row['ssnum'], row['stormnum'], row['binnumber'], row['advisnum'],
                  row['tau'], row['maxwind'], row['gust'], row['mslp'], row['tcdir'],
                  row['tcspd'], row['advdate'], row['idp_filedate'], row['geom_json']))

        for row in tracks:
            cur.execute("""
                INSERT INTO observations.nhc_storm_tracks
                    (basin, stormname, stormnum, binnumber, advisnum, geom)
                VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """, (row['basin'], row['stormname'], row['stormnum'], row['binnumber'],
                  row['advisnum'], row['geom_json']))

        for row in cones:
            cur.execute("""
                INSERT INTO observations.nhc_storm_cones
                    (basin, stormname, stormnum, binnumber, advisnum, geom)
                VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """, (row['basin'], row['stormname'], row['stormnum'], row['binnumber'],
                  row['advisnum'], row['geom_json']))

        conn.commit()
        log.info("Committed points=%d tracks=%d cones=%d (TRUNCATE+INSERT)",
                  len(points), len(tracks), len(cones))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                         help='Fetch and parse only; print rows, skip DB write')
    args = parser.parse_args()

    log.info("Starting NHC tropical cyclone ingestion...")
    try:
        points = parse_points(fetch_layer(LAYER_POINTS))
        tracks = parse_line_or_polygon(fetch_layer(LAYER_TRACKS))
        cones = parse_line_or_polygon(fetch_layer(LAYER_CONES))
    except Exception as e:
        log.error("Fetch/parse failed, aborting without touching DB: %s", e)
        return 1

    # Zero storms is a VALID, expected state (off-season) -- not an error.
    log.info("Parsed %d points / %d tracks / %d cones", len(points), len(tracks), len(cones))

    try:
        write_snapshot(points, tracks, cones, dry_run=args.dry_run)
        log.info("SUCCESS%s", ' (dry-run)' if args.dry_run else '')
        return 0
    except Exception as e:
        log.error("DB write failed: %s", e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
