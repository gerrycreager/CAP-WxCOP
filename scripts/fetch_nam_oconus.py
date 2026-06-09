#!/var/www/cap_winds_app/venv/bin/python3
"""
fetch_nam_oconus.py — CAP WxCOP NAM OCONUS Fetch
=================================================
Fetches NAM OCONUS nest/AFWA grib2 files from NOMADS (primary) or
AWS S3 (failover) for AK, HI, and CARIB domains.

Domains:
    AK    — awak3d{FH:02d}  F00-F42 3-hourly  NCEP Alaska nest
    HI    — afwahi{FH:02d}  F00-F27 3-hourly  AFWA Hawaii
    CARIB — afwaca{FH:02d}  F00-F27 3-hourly  AFWA Caribbean/PR

Sources (tried in order):
    1. NOMADS: https://nomads.ncep.noaa.gov/pub/data/nccf/com/nam/prod/
    2. AWS S3:  https://noaa-nam-pds.s3.amazonaws.com/

Output:
    /LDM/models/nam/ak/    nam.tHHz.awak3d{FH}.tm00.grib2
    /LDM/models/nam/hi/    nam.tHHz.afwahi{FH}.tm00.grib2
    /LDM/models/nam/carib/ nam.tHHz.afwaca{FH}.tm00.grib2

Scour: files older than RETAIN_DAYS are removed on each run.

Usage:
    fetch_nam_oconus.py --cycle HH        # fetch specific cycle (00/06/12/18)
    fetch_nam_oconus.py --cycle HH --dry-run
    fetch_nam_oconus.py --cycle HH --domain ak

Cron (www-data on r815):
    20 4  * * * /var/www/cap_winds_app/venv/bin/python3 /var/www/cap_winds_app/scripts/fetch_nam_oconus.py --cycle 00
    20 10 * * * /var/www/cap_winds_app/venv/bin/python3 /var/www/cap_winds_app/scripts/fetch_nam_oconus.py --cycle 06
    20 16 * * * /var/www/cap_winds_app/venv/bin/python3 /var/www/cap_winds_app/scripts/fetch_nam_oconus.py --cycle 12
    20 22 * * * /var/www/cap_winds_app/venv/bin/python3 /var/www/cap_winds_app/scripts/fetch_nam_oconus.py --cycle 18
"""

import os
import sys
import argparse
import logging
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOMADS_BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/nam/prod'
S3_BASE     = 'https://noaa-nam-pds.s3.amazonaws.com'

SOURCES = [NOMADS_BASE, S3_BASE]

OUTPUT_BASE = Path('/LDM/models/nam')

RETAIN_DAYS = 3       # scour files older than this many days
TIMEOUT_SEC = 120     # per-file download timeout
RETRY_COUNT = 3       # retries per source before trying next source
RETRY_DELAY = 10      # seconds between retries

HEADERS = {
    'User-Agent': 'CAP-WxCOP/1.0 (Civil Air Patrol weather operations; '
                  'contact: wx@cap.gov)'
}

# Domain definitions
# name       → (template, max_fh, output_subdir)
# template   → Python format string; {cycle} = HH, {fh:02d} = forecast hour
DOMAINS = {
    'ak': {
        'template':  'nam.t{cycle}z.awak3d{fh:02d}.tm00.grib2',
        'max_fh':    42,
        'step':      3,
        'subdir':    'ak',
        'label':     'AK (awak3d)',
    },
    'hi': {
        'template':  'nam.t{cycle}z.afwahi{fh:02d}.tm00.grib2',
        'max_fh':    27,
        'step':      3,
        'subdir':    'hi',
        'label':     'HI (afwahi)',
    },
    'carib': {
        'template':  'nam.t{cycle}z.afwaca{fh:02d}.tm00.grib2',
        'max_fh':    27,
        'step':      3,
        'subdir':    'carib',
        'label':     'CARIB/PR (afwaca)',
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [fetch_nam_oconus] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/fetch_nam_oconus.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('fetch_nam_oconus')

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def cycle_date(cycle_hh: int) -> datetime:
    """
    Return the most recent past UTC datetime for the given cycle hour.
    If the cycle hasn't started yet today, return yesterday's cycle.
    """
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=cycle_hh, minute=0, second=0, microsecond=0)
    if today > now:
        today -= timedelta(days=1)
    return today

def date_str(dt: datetime) -> str:
    return dt.strftime('%Y%m%d')

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def build_url(base: str, date: str, filename: str) -> str:
    """Construct full URL for a NAM file."""
    return f'{base}/nam.{date}/{filename}'

def download_file(url: str, dest: Path, dry_run: bool = False) -> bool:
    """
    Download url to dest (atomic write via temp file).
    Returns True on success.
    """
    if dry_run:
        log.info(f'DRY-RUN: would fetch {url} → {dest.name}')
        return True

    tmp = dest.with_suffix('.tmp')
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC,
                          stream=True) as r:
            if r.status_code == 404:
                return False   # file doesn't exist — not an error
            r.raise_for_status()
            with open(tmp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                    f.write(chunk)
        tmp.rename(dest)   # atomic on same filesystem
        return True
    except requests.exceptions.Timeout:
        log.warning(f'Timeout fetching {url}')
    except requests.exceptions.RequestException as e:
        log.warning(f'Request error for {url}: {e}')
    except Exception as e:
        log.error(f'Unexpected error fetching {url}: {e}')
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return False

def fetch_with_failover(date: str, filename: str, dest: Path,
                        dry_run: bool = False) -> bool:
    """
    Try each source in SOURCES order, with RETRY_COUNT retries per source.
    Returns True if file was successfully downloaded (or already exists).
    """
    if dest.exists() and dest.stat().st_size > 0:
        log.debug(f'Already exists: {dest.name}')
        return True

    for source in SOURCES:
        url = build_url(source, date, filename)
        for attempt in range(1, RETRY_COUNT + 1):
            log.info(f'Fetching {filename} from {source.split("/")[2]} '
                     f'(attempt {attempt}/{RETRY_COUNT})')
            if download_file(url, dest, dry_run=dry_run):
                size_mb = dest.stat().st_size / (1 << 20) if not dry_run else 0
                log.info(f'OK: {dest.name} ({size_mb:.1f} MB)')
                return True
            if attempt < RETRY_COUNT:
                log.debug(f'Retry in {RETRY_DELAY}s...')
                time.sleep(RETRY_DELAY)

        log.warning(f'All {RETRY_COUNT} attempts failed for {source}')

    log.error(f'FAILED all sources for {filename}')
    return False

# ---------------------------------------------------------------------------
# Scour
# ---------------------------------------------------------------------------

def scour_old_files(subdir: Path, retain_days: int = RETAIN_DAYS):
    """Remove grib2 files older than retain_days."""
    cutoff = time.time() - retain_days * 86400
    removed = 0
    for f in subdir.glob('*.grib2'):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        log.info(f'Scoured {removed} old files from {subdir}')

# ---------------------------------------------------------------------------
# Main fetch logic
# ---------------------------------------------------------------------------

def fetch_domain(domain_key: str, cycle: str, dt: datetime,
                 dry_run: bool = False) -> dict:
    """
    Fetch all forecast hours for one domain/cycle.
    Returns stats dict.
    """
    cfg     = DOMAINS[domain_key]
    subdir  = OUTPUT_BASE / cfg['subdir']
    subdir.mkdir(parents=True, exist_ok=True)
    date    = date_str(dt)

    fhours  = range(0, cfg['max_fh'] + 1, cfg['step'])
    ok = 0; skip = 0; fail = 0

    log.info(f'--- {cfg["label"]} cycle {date}/{cycle}Z '
             f'({len(list(fhours))} files) ---')

    for fh in range(0, cfg['max_fh'] + 1, cfg['step']):
        filename = cfg['template'].format(cycle=cycle, fh=fh)
        dest     = subdir / filename
        result   = fetch_with_failover(date, filename, dest, dry_run=dry_run)
        if result:
            if dest.exists() and dest.stat().st_mtime < time.time() - 60:
                skip += 1   # already existed
            else:
                ok += 1
        else:
            fail += 1

    scour_old_files(subdir, RETAIN_DAYS)
    return {'ok': ok, 'skip': skip, 'fail': fail}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Fetch NAM OCONUS nest/AFWA files')
    ap.add_argument('--cycle', required=True, choices=['00','06','12','18'],
                    help='NAM cycle hour (00/06/12/18)')
    ap.add_argument('--domain', choices=list(DOMAINS.keys()) + ['all'],
                    default='all', help='Domain to fetch (default: all)')
    ap.add_argument('--date', default=None,
                    help='Override date YYYYMMDD (default: most recent cycle date)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what would be fetched without downloading')
    args = ap.parse_args()

    cycle = args.cycle

    if args.date:
        try:
            dt = datetime.strptime(args.date, '%Y%m%d').replace(
                hour=int(cycle), tzinfo=timezone.utc)
        except ValueError:
            log.error(f'Invalid date format: {args.date} (use YYYYMMDD)')
            sys.exit(1)
    else:
        dt = cycle_date(int(cycle))

    domains = list(DOMAINS.keys()) if args.domain == 'all' else [args.domain]

    log.info('=' * 65)
    log.info(f'NAM OCONUS fetch: cycle {date_str(dt)}/{cycle}Z '
             f'domains={domains}'
             f'{" DRY-RUN" if args.dry_run else ""}')
    log.info('=' * 65)

    total = {'ok': 0, 'skip': 0, 'fail': 0}
    for dk in domains:
        stats = fetch_domain(dk, cycle, dt, dry_run=args.dry_run)
        for k in total:
            total[k] += stats[k]
        log.info(f'{DOMAINS[dk]["label"]}: '
                 f'{stats["ok"]} fetched, {stats["skip"]} skipped, '
                 f'{stats["fail"]} failed')

    log.info('=' * 65)
    log.info(f'Complete: {total["ok"]} fetched, {total["skip"]} skipped, '
             f'{total["fail"]} failed')
    log.info('=' * 65)

    sys.exit(1 if total['fail'] > 0 else 0)

if __name__ == '__main__':
    main()
