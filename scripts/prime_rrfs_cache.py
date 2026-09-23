#!/var/www/cap_winds_app/venv/bin/python3
"""
prime_rrfs_cache.py -- proactively pre-warm the RRFS byte-range cache so
real requests (Gerry's laptop MEF tool via model_taf_api.py, mission_taf_extract.py's
KBAK-cluster generator, and ad-hoc analysis like the KBAK ceiling comparisons)
hit a warm cache instead of paying NOMADS fetch latency live.

Why this exists: found live 2026-09-23 that nothing primed the cache
proactively -- data only ever got fetched reactively, on the first real
request for a given cycle/hour. Gerry now treats RRFS-based TAF generation
as routine (not ad-hoc testing), so the cache should be kept warm routinely
too, run on a schedule rather than left to whichever request happens to be
first each cycle.

Uses the exact same fetch path (rrfs_model_taf.fetch_fhour(), tag='modeltaf')
that the real TAF generator uses, so a subsequent real request is a
guaranteed cache hit -- not a separate/duplicate cache.

Primes F000-F018 always (works for any cycle, synoptic or not -- matches
RRFS's confirmed structural cap on non-synoptic cycles), extended to
AREA_MEF_MAX_FHOUR (30) on synoptic cycles (00/06/12/18Z), matching
model_taf_api.py's real batch-job range.

Also primes MSLET (mean sea level pressure) alongside the production field
set -- not used by TAF generation itself, but routinely needed for the
KBAK altimeter/MSLP comparison work this same cache now supports.

Cadence: RRFS's `para` feed was confirmed (2026-09-23) to post on a strict
3-hourly cadence (00/03/06/09/12/15/18/21Z), not hourly -- but Gerry
suspects this moves to hourly once HPC production takes over from the
current "open space" allocation. Scheduled hourly rather than every 3h so
no cron change is needed if/when that happens -- each run is a cheap no-op
(fetch_and_cache() skips anything already cached) when there's nothing new
to fetch.

Run as www-data, hourly:
  15 * * * * /var/www/cap_winds_app/venv/bin/python3 \
    /var/www/cap_winds_app/scripts/prime_rrfs_cache.py \
    >> /var/log/rrfs_cache_prime.log 2>&1
"""
import fcntl
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/var/www/cap_winds_app')
from scripts import rrfs_model_taf as rmt
from scripts import rrfs_wind_fetch as rwf

LOCKFILE = '/tmp/prime_rrfs_cache.lock'
AREA_MEF_MAX_FHOUR = 30
BASE_MAX_FHOUR = 18
SYNOPTIC_CYCLES = ('00', '06', '12', '18')
MSLP_WANTED = {('MSLET', 'mean sea level')}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('prime_rrfs_cache')


def main():
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info('Another instance is already running, exiting.')
        return

    date, cyc = rmt.find_ready_cycle()
    if date is None:
        log.warning('No ready RRFS cycle found, nothing to prime.')
        return

    max_fhour = AREA_MEF_MAX_FHOUR if cyc in SYNOPTIC_CYCLES else BASE_MAX_FHOUR
    log.info('Priming RRFS %s %sz through F%03d (%s cycle)',
              date, cyc, max_fhour, 'synoptic' if cyc in SYNOPTIC_CYCLES else 'hourly')

    # Deliberately does NOT break out of the loop on the first None/failure:
    # found live 2026-09-23 that a None return isn't reliably "not posted
    # yet this far out" -- it can also be a transient rate-limit-induced
    # empty .idx parse (fetch_and_cache()'s _parse_idx treats a malformed
    # response the same as "not ready", by design, since it can't tell
    # them apart). Breaking early on that would skip hours that are
    # already cached or genuinely fetchable further out. Each hour gets
    # its own outcome; anything that fails just waits for next run.
    ok, failed = 0, []
    for fhr in range(0, max_fhour + 1):
        try:
            path = rmt.fetch_fhour(date, cyc, fhr)
            if path is None:
                failed.append(fhr)
                continue
            ok += 1
        except Exception as e:
            log.warning('F%03d modeltaf fetch failed: %s', fhr, e)
            failed.append(fhr)
            continue
        try:
            rwf.fetch_and_cache('2dfld', date, cyc, fhr, MSLP_WANTED, 'kbak_mslp')
        except Exception as e:
            log.warning('F%03d MSLET fetch failed: %s', fhr, e)

    log.info('Done: %d/%d hours primed (%s%s)', ok, max_fhour + 1,
              date, cyc)
    if failed:
        log.info('Failed hours (will retry next run): %s', failed)


if __name__ == '__main__':
    main()
