#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_airnow.py — Fetch PM2.5 observations from EPA AirNow's bounded-obs
CSV endpoint and store into observations.airnow_pm25.

Feeds the Cadet Weather COP air-quality stoplight category (see
cadet_wx_api.py: get_nearest_airnow_pm25(), air_quality_color()).

Scoped from ARM's Atmospheric data Community Toolkit (ACT) example
notebook, which uses act.discovery.get_airnow_bounded_obs() for this same
endpoint -- but that wrapper builds a dense (variable, time, site) xarray
cube via a slow triple-nested Python loop, and crashes outright on any
row with a null site_name (confirmed against live data: 4 of 4704 rows in
a real CONUS response had one). Since only "latest reading per site" is
needed here, this fetches and parses the same CSV directly with a single
pandas groupby instead.

AirNow updates roughly hourly; this script requests a narrow (3h) trailing
window and keeps only each station's most recent reading.

Requires an AirNow API token (free, https://docs.airnowapi.org/) in
/etc/cap_wxcop_secrets.conf as AIRNOW_API=<token>.

Usage:
  ingest_airnow.py              # CONUS bounding box, last 3h
  ingest_airnow.py --bbox minLon,minLat,maxLon,maxLat
"""
import os
import sys
import logging
import argparse
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings('ignore')

sys.path.insert(0, '/var/www/cap_winds_app')

LOG_FILE = '/home/ldm/var/logs/airnow_ingest.log'
SECRETS_FILE = '/etc/cap_wxcop_secrets.conf'

# CONUS -- wide enough to catch Canadian wildfire smoke wherever it's drifted
# into US-monitored areas. AirNow's network is US-focused; it doesn't cover
# Canadian stations directly.
CONUS_BBOX = '-125,24,-66,50'

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_secret(key, path=SECRETS_FILE):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    if k.strip() == key:
                        return v.strip()
    except Exception as e:
        log.error(f'Failed to read {path}: {e}')
    return None


def fetch_pm25(token, bbox):
    """Fetch and parse AirNow's bounded-obs CSV directly rather than going
    through act.discovery.get_airnow_bounded_obs(): that wrapper builds a
    dense (variable, time, site) xarray cube via a Python triple-nested
    loop (slow for a CONUS-wide request with 1000+ sites), and -- found
    while testing against live data -- it crashes outright when any row
    has a null site_name, since pandas' unique() treats each NaN as a
    distinct value but `df['site_name'] == nan` never matches it back
    (confirmed: 4 of 4704 rows in a real CONUS response had null
    site_name). Only need "latest reading per site" here, so do that
    directly with a groupby instead."""
    import pandas as pd
    import requests as _req

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=3)
    start_str = start.strftime('%Y-%m-%dT%H')
    end_str   = end.strftime('%Y-%m-%dT%H')

    log.info(f'Requesting AirNow PM2.5, bbox={bbox}, window={start_str}..{end_str}')
    url = (
        'https://www.airnowapi.org/aq/data/?startDate=' + start_str
        + '&endDate=' + end_str
        + '&parameters=PM25&BBOX=' + bbox
        + '&dataType=B&format=text/csv&verbose=1&monitorType=2'
        + '&includerawconcentrations=1&API_KEY=' + token
    )
    names = ['latitude', 'longitude', 'time', 'parameter', 'concentration', 'unit',
              'raw_concentration', 'AQI', 'category', 'site_name', 'site_agency',
              'aqs_id', 'full_aqs_id']

    resp = _req.get(url, timeout=60)
    resp.raise_for_status()
    if not resp.text.strip():
        log.warning('Empty response -- no data for this window/bbox')
        return []

    from io import StringIO
    df = pd.read_csv(StringIO(resp.text), names=names)
    df = df.dropna(subset=['aqs_id', 'concentration', 'time'])
    # AirNow flags missing/invalid readings as -999 rather than omitting the
    # row or using NaN -- confirmed against live data (13 of 4704 rows).
    # Real PM2.5 concentrations are never negative, so drop on that basis
    # rather than the magic-number literal, in case the sentinel varies.
    df = df[df['concentration'] >= 0]
    if df.empty:
        log.warning('No usable rows after dropping incomplete records')
        return []

    df['time'] = pd.to_datetime(df['time'], utc=True)
    # Keep each station's most recent reading only
    df = df.sort_values('time').groupby('aqs_id', as_index=False).last()

    results = []
    for _, row in df.iterrows():
        aqi_val = None
        if pd.notna(row.get('AQI')):
            aqi_val = int(round(row['AQI']))
        results.append({
            'station_id':       str(row['aqs_id']),
            'station_name':     str(row['site_name']) if pd.notna(row['site_name']) else str(row['aqs_id']),
            'lat':              float(row['latitude']),
            'lon':              float(row['longitude']),
            'pm25_ugm3':        float(row['concentration']),
            'aqi_value':        aqi_val,
            'observation_time': row['time'].to_pydatetime(),
        })

    return results


def store(rows):
    from db_config import get_connection
    if not rows:
        log.info('No rows to store')
        return 0

    conn = get_connection()
    cur = conn.cursor()
    n = 0
    for r in rows:
        cur.execute("""
            INSERT INTO observations.airnow_pm25
                (station_id, station_name, lat, lon, pm25_ugm3, aqi_value, observation_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (station_id, observation_time) DO UPDATE SET
                pm25_ugm3 = EXCLUDED.pm25_ugm3,
                aqi_value = EXCLUDED.aqi_value,
                station_name = EXCLUDED.station_name
        """, (r['station_id'], r['station_name'], r['lat'], r['lon'],
              r['pm25_ugm3'], r['aqi_value'], r['observation_time']))
        n += 1
    conn.commit()
    cur.close()
    conn.close()
    return n


def main():
    parser = argparse.ArgumentParser(description='Ingest EPA AirNow PM2.5 observations via ACT')
    parser.add_argument('--bbox', type=str, default=CONUS_BBOX,
                         help='minLon,minLat,maxLon,maxLat (default: CONUS)')
    args = parser.parse_args()

    log.info('=' * 60)
    log.info('AirNow PM2.5 ingest started')

    token = os.getenv('AIRNOW_API') or load_secret('AIRNOW_API')
    if not token:
        log.error('No AIRNOW_API token found (env var or /etc/cap_wxcop_secrets.conf)')
        sys.exit(1)

    try:
        rows = fetch_pm25(token, args.bbox)
    except Exception as e:
        log.error(f'Fetch failed: {e}')
        sys.exit(1)

    log.info(f'Fetched {len(rows)} station readings')
    n = store(rows)
    log.info(f'Stored/updated {n} rows')
    log.info('Done')
    log.info('=' * 60)


if __name__ == '__main__':
    main()
