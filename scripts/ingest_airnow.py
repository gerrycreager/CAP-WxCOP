#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_airnow.py — Fetch PM2.5 observations from EPA AirNow via ACT
(Atmospheric data Community Toolkit, act.discovery.get_airnow_bounded_obs)
and store into observations.airnow_pm25.

Feeds the Cadet Weather COP air-quality stoplight category (see
cadet_wx_api.py: get_nearest_airnow_pm25(), air_quality_color()).

AirNow updates roughly hourly; this script requests a narrow (3h) trailing
window and keeps only each station's most recent non-NaN reading, matching
the "latest valid PM2.5 per site" pattern from ARM's example notebook.

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
    import act.discovery
    import numpy as np

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=3)
    start_str = start.strftime('%Y-%m-%dT%H')
    end_str   = end.strftime('%Y-%m-%dT%H')

    log.info(f'Requesting AirNow PM2.5, bbox={bbox}, window={start_str}..{end_str}')
    ds = act.discovery.get_airnow_bounded_obs(
        token, start_str, end_str, bbox, parameters='PM25', data_type='B', mon_type=2,
    )

    if 'PM25' not in ds.data_vars:
        log.warning('No PM25 variable in response -- no data for this window/bbox')
        return []

    sites = ds.coords['sites'].values
    times = ds.coords['time'].values
    lats  = ds['latitude'].values
    lons  = ds['longitude'].values
    aqs   = ds['aqs_id'].values
    pm25  = ds['PM25'].values          # shape (time, sites)
    aqi   = ds['AQI'].values if 'AQI' in ds.data_vars else None

    results = []
    for s_idx, site_name in enumerate(sites):
        # Latest non-NaN PM2.5 reading for this site
        col = pm25[:, s_idx]
        valid_idx = np.where(~np.isnan(col))[0]
        if len(valid_idx) == 0:
            continue
        t_idx = valid_idx[-1]   # times is ascending, so last valid = most recent

        aqi_val = None
        if aqi is not None:
            av = aqi[t_idx, s_idx]
            if not np.isnan(av):
                aqi_val = int(round(av))

        station_id = str(aqs[s_idx]) if aqs[s_idx] not in (None, '', 'nan') else str(site_name)
        obs_time = times[t_idx]
        # numpy datetime64 -> aware UTC datetime
        obs_dt = obs_time.astype('datetime64[s]').astype(datetime).replace(tzinfo=timezone.utc)

        results.append({
            'station_id':   station_id,
            'station_name': str(site_name),
            'lat':          float(lats[s_idx]),
            'lon':          float(lons[s_idx]),
            'pm25_ugm3':    float(col[t_idx]),
            'aqi_value':    aqi_val,
            'observation_time': obs_dt,
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
