#!/usr/bin/env python3
"""
Keeps observations.airports.has_reporting in sync with reality for the
general (non-military) airport population.

Root cause this fixes (found 2026-11-14): has_reporting defaults to
false on every row and was only ever set true by kq_admin.py's KQ
station sync (which also sets is_military=true for those rows,
deliberately, regardless of literal METAR presence -- that's a
separate mechanism this script does not touch). The bulk-imported
general airport dataset (~10,928 rows) never had has_reporting
populated at all -- confirmed live: EVERY row was false, including
4,107+ stations with METAR rows landing every few minutes (e.g. KCFD /
Coulter Field, AWOS, 20-min cycle). has_reporting gates real display
logic (radar_api.py's reporting-only layer requires it true outright;
airport_tiers_endpoint.py's Tier-2 "Major Hub" classification requires
it true), so this wasn't cosmetic -- it silently hid thousands of
actively-reporting stations from the map.

Fix: drive the flag from observations.metar activity instead of a
static import value. A station counts as reporting if it has ANY METAR
row in the last REPORTING_WINDOW_DAYS -- long enough to tolerate a
short outage without flickering has_reporting off, short enough to
correctly age out a station that's gone silent for good.

Run via cron (daily is plenty -- this is a slow-changing property, not
something that needs minute-level freshness) and safe to run manually
any time; idempotent, single UPDATE, no state kept between runs.
"""
import sys
import logging

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

LOG_PATH = '/home/ldm/var/logs/update_airport_reporting_status.log'
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

REPORTING_WINDOW_DAYS = 7

UPDATE_SQL = """
    UPDATE observations.airports a
    SET has_reporting = EXISTS (
        SELECT 1 FROM observations.metar m
        WHERE m.station_id = a.station_id
          AND m.observation_time > now() - interval '%s days'
    )
    WHERE a.is_military IS NOT TRUE
      AND a.has_reporting IS DISTINCT FROM EXISTS (
        SELECT 1 FROM observations.metar m
        WHERE m.station_id = a.station_id
          AND m.observation_time > now() - interval '%s days'
    )
"""


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(UPDATE_SQL, (REPORTING_WINDOW_DAYS, REPORTING_WINDOW_DAYS))
            changed = cur.rowcount
        conn.commit()
        log.info('updated has_reporting on %d row(s) (window=%dd)', changed, REPORTING_WINDOW_DAYS)
        print(f'updated has_reporting on {changed} row(s)')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
