#!/usr/bin/env python3
"""
radar_status_scan.py — Scan Level-3 NIDS files to compute per-site latency.
Location: /var/www/cap_winds_app/scripts/radar_status_scan.py  (r815)
Runs every 5 min via cron. Reads newest N0H file per site, computes age,
writes latency_minutes and last_l3_time to radar.radar_status.

Color tiers:
  green:  < 10 min
  blue:   10-15 min  (slightly stale)
  yellow: 15-60 min  (degraded)
  red:    > 60 min OR FTM outage

L3 path: /LDM/radar/level3/{SITE}/N0H/nids/{YYYYMMDD}/{SITE}_N0H_{DDHHMM}.nids
"""
import os
import re
import glob
import logging
from datetime import datetime, timezone, timedelta
import psycopg2

LOG_FILE = '/home/ldm/var/logs/radar_status_scan.log'
L3_BASE  = '/LDM/radar/level3'
PRODUCT  = 'N0H'   # base reflectivity 0.5° — most reliable latency indicator
DB_DSN   = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def parse_nids_timestamp(filename, ref_dt):
    """
    Extract timestamp from NIDS filename like FTG_N0H_192034.nids
    Returns UTC datetime or None.
    Filename timestamp is DDHHMM — day+hour+minute.
    """
    m = re.search(r'_(\d{2})(\d{2})(\d{2})\.nids$', filename)
    if not m:
        return None
    day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Build datetime — use ref_dt year/month, handle month rollover
    try:
        dt = ref_dt.replace(day=day, hour=hour, minute=minute,
                            second=0, microsecond=0)
        # If day is in the future, step back a month
        if dt > ref_dt + timedelta(hours=2):
            # Go back one month
            if ref_dt.month == 1:
                dt = dt.replace(year=ref_dt.year-1, month=12)
            else:
                dt = dt.replace(month=ref_dt.month-1)
        return dt
    except ValueError:
        return None


def get_latest_l3_time(site_id):
    """Find the newest N0H NIDS file for a site and return its timestamp."""
    now = datetime.now(timezone.utc)
    today     = now.strftime('%Y%m%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y%m%d')
    for date_str in [today, yesterday]:
        pattern = os.path.join(L3_BASE, site_id, PRODUCT, 'nids',
                               date_str, f'{site_id}_{PRODUCT}_*.nids')
        files = sorted(glob.glob(pattern))
        if files:
            newest = files[-1]
            ts = parse_nids_timestamp(os.path.basename(newest), now)
            return ts, newest
    return None, None


def latency_color(minutes, ftm_status=None):
    """Return display color based on latency and FTM status."""
    if ftm_status and ftm_status.upper() in ('OFFLINE', 'MAINTENANCE'):
        return 'red'
    if minutes is None:
        return 'red'
    if minutes < 10:
        return 'green'
    if minutes < 15:
        return 'blue'
    if minutes < 60:
        return 'yellow'
    return 'red'


def main():
    log.info('=== radar_status_scan start ===')
    now = datetime.now(timezone.utc)

    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor()

        # Get all known sites
        cur.execute('SELECT site_id FROM radar.radar_sites ORDER BY site_id')
        sites = [row[0] for row in cur.fetchall()]
        log.info(f'Scanning {len(sites)} sites for L3 latency')

        updated = 0
        no_data = 0

        for site_id in sites:
            ts, filepath = get_latest_l3_time(site_id)

            if ts:
                # Make ts timezone-aware if needed
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                latency = int((now - ts).total_seconds() / 60)
                latency = max(0, latency)
            else:
                latency = None
                no_data += 1

            # Get current FTM status to incorporate into color
            cur.execute(
                'SELECT ftm_status FROM radar.radar_status WHERE site_id = %s',
                (site_id,))
            row = cur.fetchone()
            ftm_status = row[0] if row else None

            color = latency_color(latency, ftm_status)

            # Determine overall status
            if ftm_status in ('OFFLINE', 'MAINTENANCE'):
                status = ftm_status
            elif latency is None or latency >= 60:
                status = 'DEGRADED'
            else:
                status = 'OPERATIONAL'

            cur.execute("""
                INSERT INTO radar.radar_status
                    (site_id, status, last_update, latency_minutes, last_l3_time)
                VALUES (%s, %s, NOW(), %s, %s)
                ON CONFLICT (site_id) DO UPDATE SET
                    status          = EXCLUDED.status,
                    last_update     = NOW(),
                    latency_minutes = EXCLUDED.latency_minutes,
                    last_l3_time    = EXCLUDED.last_l3_time
            """, (site_id, status, latency, ts))
            updated += 1

        conn.commit()
        conn.close()
        log.info(f'Done: {updated} updated, {no_data} with no L3 data')

    except Exception as e:
        log.error(f'Scan failed: {e}')
        raise


if __name__ == '__main__':
    main()
