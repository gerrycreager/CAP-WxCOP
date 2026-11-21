#!/usr/bin/env python3
"""
WPC Surface Fronts + Pressure Centers Ingestion

Polls WPC's National Forecast Chart ArcGIS MapServer (Day 1/2/3 front
layers 2/14/26, plain HTTPS GeoJSON -- not LDM/NOAAPort) and stores a
fresh snapshot in observations.wpc_fronts for EWMC's "WPC Surface
Fronts" overlay. The service regenerates twice daily (1000Z/2200Z);
front geometry/type comes from the `feat` field (e.g. "Cold Front
Valid"), not from parsing any text bulletin.

Also polls the same MapServer's parallel "Highs and Lows" point layers
(1/13/25 -- same Day 1/2/3 grouping as the front layers, confirmed live
2026-10-24) and stores a fresh snapshot in observations.wpc_pressure_centers.
No central pressure value is exposed by this service (just center_type +
position), unlike NHC's tropical layers which do carry mslp.

Intentionally independent of the separate tropical_dashboard app's
`tropical_*` schema -- no relation, no shared table names.

Write strategy: TRUNCATE + INSERT inside one transaction, not an
upsert-on-objectid. WPC's `objectid` stability across polls of this
twice-daily-regenerated chart is NOT verified (no live diff of two
successive polls was done) -- TRUNCATE+INSERT is correct regardless of
whether objectid is stable, at the cost of no row history, which is
fine for a "current state" map overlay. A fetch failure aborts before
TRUNCATE so one bad poll never wipes a good table to empty.
"""
import sys
import re
import json
import logging
import argparse
from datetime import datetime, timezone

import requests

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

BASE_URL = 'https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/natl_fcst_wx_chart/MapServer'
LAYERS = {1: 2, 2: 14, 3: 26}  # forecast_day -> fronts MapServer layer id
HL_LAYERS = {1: 1, 2: 13, 3: 25}  # forecast_day -> Highs/Lows MapServer layer id
REQUEST_HEADERS = {
    'User-Agent': 'CAP-WxCOP/1.0 (CAP WxCOP aviation weather situational awareness tool)'
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS observations.wpc_fronts (
        id              SERIAL PRIMARY KEY,
        front_type      VARCHAR(20),
        forecast_day    SMALLINT NOT NULL,
        valid_text      TEXT,
        source_objectid INTEGER,
        idp_filedate    TIMESTAMP,
        idp_ingestdate  TIMESTAMP,
        geom            GEOMETRY(GEOMETRY, 4326),
        inserted_at     TIMESTAMP DEFAULT NOW()
    )
"""
CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_wpc_fronts_geom ON observations.wpc_fronts USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_wpc_fronts_day  ON observations.wpc_fronts (forecast_day)",
]

CREATE_TABLE_HL_SQL = """
    CREATE TABLE IF NOT EXISTS observations.wpc_pressure_centers (
        id              SERIAL PRIMARY KEY,
        center_type     VARCHAR(10),
        forecast_day    SMALLINT NOT NULL,
        valid_text      TEXT,
        source_objectid INTEGER,
        idp_filedate    TIMESTAMP,
        idp_ingestdate  TIMESTAMP,
        geom            GEOMETRY(POINT, 4326),
        inserted_at     TIMESTAMP DEFAULT NOW()
    )
"""
CREATE_INDEX_HL_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_wpc_pressure_centers_geom ON observations.wpc_pressure_centers USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS idx_wpc_pressure_centers_day  ON observations.wpc_pressure_centers (forecast_day)",
]

FRONT_TYPE_RE = re.compile(r'^(.*?)\s+Valid$', re.IGNORECASE)
CENTER_TYPE_RE = re.compile(r'^(.*?)\s+Valid$', re.IGNORECASE)


def parse_center_type(feat_value):
    """'High Valid' -> 'high', 'Low Valid' -> 'low'. Same substring-match/
    never-crash convention as parse_front_type()."""
    if not feat_value:
        return 'unknown'
    m = CENTER_TYPE_RE.match(feat_value.strip())
    base = (m.group(1) if m else feat_value.strip()).lower()
    if 'high' in base:
        return 'high'
    if 'low' in base:
        return 'low'
    log.warning("Unrecognized pressure center feat string: %r", feat_value)
    return 'unknown'


def parse_front_type(feat_value):
    """
    'Cold Front Valid' -> 'cold', 'Warm Front Valid' -> 'warm',
    'Stationary Front Valid' -> 'stationary', 'Occluded Front Valid' ->
    'occluded', 'Trough Valid' -> 'trough'. Substring match (not exact)
    to tolerate minor casing/whitespace variance. Robustness beyond the
    live sample set confirmed on 2026-10-24 is UNVERIFIED -- anything
    unrecognized logs a warning and buckets to 'unknown' rather than
    crashing, so check this log periodically after deployment.
    """
    if not feat_value:
        return 'unknown'
    m = FRONT_TYPE_RE.match(feat_value.strip())
    base = (m.group(1) if m else feat_value.strip()).lower()
    if 'cold' in base:
        return 'cold'
    if 'warm' in base:
        return 'warm'
    if 'stationary' in base:
        return 'stationary'
    if 'occluded' in base:
        return 'occluded'
    if 'trough' in base:
        return 'trough'
    log.warning("Unrecognized front feat string: %r", feat_value)
    return 'unknown'


def epoch_ms_to_dt(v):
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def fetch_layer(forecast_day, layer_id):
    url = f'{BASE_URL}/{layer_id}/query'
    params = {'where': '1=1', 'outFields': '*', 'f': 'geojson', 'returnGeometry': 'true'}
    r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=60)
    r.raise_for_status()
    features = r.json().get('features', [])
    log.info("Day %d (layer %d): %d front features", forecast_day, layer_id, len(features))
    return features


def parse_features(forecast_day, features):
    rows = []
    for f in features:
        geom = f.get('geometry')
        if not geom:
            continue
        props = f.get('properties', {})
        rows.append({
            'front_type': parse_front_type(props.get('feat')),
            'forecast_day': forecast_day,
            'valid_text': props.get('popupconte'),
            'source_objectid': props.get('objectid'),
            'idp_filedate': epoch_ms_to_dt(props.get('idp_filedate')),
            'idp_ingestdate': epoch_ms_to_dt(props.get('idp_ingestdate')),
            'geom_json': json.dumps(geom),
        })
    return rows


def parse_center_features(forecast_day, features):
    rows = []
    for f in features:
        geom = f.get('geometry')
        if not geom:
            continue
        props = f.get('properties', {})
        rows.append({
            'center_type': parse_center_type(props.get('feat')),
            'forecast_day': forecast_day,
            'valid_text': props.get('popupconte'),
            'source_objectid': props.get('objectid'),
            'idp_filedate': epoch_ms_to_dt(props.get('idp_filedate')),
            'idp_ingestdate': epoch_ms_to_dt(props.get('idp_ingestdate')),
            'geom_json': json.dumps(geom),
        })
    return rows


def ensure_schema(cur):
    cur.execute(CREATE_TABLE_SQL)
    for stmt in CREATE_INDEX_SQL:
        cur.execute(stmt)
    cur.execute(CREATE_TABLE_HL_SQL)
    for stmt in CREATE_INDEX_HL_SQL:
        cur.execute(stmt)


def write_snapshot(front_rows, center_rows, dry_run=False):
    if dry_run:
        log.info("[DRY RUN] Would write %d front rows; sample: %s", len(front_rows), front_rows[:3])
        log.info("[DRY RUN] Would write %d pressure center rows; sample: %s", len(center_rows), center_rows[:3])
        return len(front_rows), len(center_rows)

    conn = get_connection()
    try:
        cur = conn.cursor()
        ensure_schema(cur)

        cur.execute("TRUNCATE TABLE observations.wpc_fronts")
        for row in front_rows:
            cur.execute("""
                INSERT INTO observations.wpc_fronts
                    (front_type, forecast_day, valid_text, source_objectid,
                     idp_filedate, idp_ingestdate, geom)
                VALUES (%s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """, (row['front_type'], row['forecast_day'], row['valid_text'],
                  row['source_objectid'], row['idp_filedate'], row['idp_ingestdate'],
                  row['geom_json']))

        cur.execute("TRUNCATE TABLE observations.wpc_pressure_centers")
        for row in center_rows:
            cur.execute("""
                INSERT INTO observations.wpc_pressure_centers
                    (center_type, forecast_day, valid_text, source_objectid,
                     idp_filedate, idp_ingestdate, geom)
                VALUES (%s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """, (row['center_type'], row['forecast_day'], row['valid_text'],
                  row['source_objectid'], row['idp_filedate'], row['idp_ingestdate'],
                  row['geom_json']))

        conn.commit()
        log.info("Committed %d front rows + %d pressure center rows (TRUNCATE+INSERT)",
                  len(front_rows), len(center_rows))
        return len(front_rows), len(center_rows)
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

    log.info("Starting WPC Surface Fronts + Pressure Centers ingestion...")
    all_front_rows = []
    all_center_rows = []
    try:
        for day, layer_id in LAYERS.items():
            features = fetch_layer(day, layer_id)
            all_front_rows.extend(parse_features(day, features))
        for day, layer_id in HL_LAYERS.items():
            features = fetch_layer(day, layer_id)
            all_center_rows.extend(parse_center_features(day, features))
    except Exception as e:
        log.error("Fetch/parse failed, aborting without touching DB: %s", e)
        return 1

    if not all_front_rows and not all_center_rows:
        log.warning("No front or pressure-center features parsed from any layer -- aborting "
                    "without TRUNCATE (avoid wiping good tables on a bad/empty response)")
        return 1

    try:
        n_fronts, n_centers = write_snapshot(all_front_rows, all_center_rows, dry_run=args.dry_run)
        log.info("SUCCESS: %s%d fronts, %d pressure centers",
                  '(dry-run) ' if args.dry_run else '', n_fronts, n_centers)
        return 0
    except Exception as e:
        log.error("DB write failed: %s", e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
