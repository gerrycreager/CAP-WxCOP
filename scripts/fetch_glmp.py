#!/usr/bin/env python3
"""
fetch_glmp.py — Download NOAA GLMP (Gridded LAMP) forecasts from NOMADS.

Downloads the 7 fcsts_* variable files per cycle per sector (CONUS + Alaska).
Each fcsts file contains 25 forecast hours (F001-F025) in a single grib2.

Variables downloaded:
  fcsts_cig   — ceiling (ceil/cloudCeiling)
  fcsts_t     — 2m temperature (2t/heightAboveGround)
  fcsts_td    — 2m dewpoint (2d/heightAboveGround)
  fcsts_vis   — visibility (vis/surface)
  fcsts_wdir  — 10m wind direction (10wdir/heightAboveGround)
  fcsts_wgst  — 10m wind gust (i10fg/heightAboveGround)
  fcsts_wspd  — 10m wind speed (10si/heightAboveGround)

NOMADS URL pattern:
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/glmp/prod/
  glmp.YYYYMMDD/glmp.tHHMMz.fcsts_VAR.g.SECTOR.grib2

Cycles: every 30 minutes (t0000z, t0030z, t0100z, ...)
Sectors: co (CONUS), ak (Alaska)

Output path:
  /LDM/models/glmp/YYYYMMDD/glmp_YYYYMMDD_HHMMz_SECTOR_VAR.grib2

Scour: keeps last RETAIN_HOURS of cycles (default 48h)

Cron (run as ldm or root on data1, every 30 min):
  */30 * * * * /var/www/cap_winds_app/venv/bin/python3 \
    /var/www/cap_winds_app/scripts/fetch_glmp.py \
    >> /var/log/glmp_fetch.log 2>&1

Run manually to backfill:
  python3 fetch_glmp.py --cycles 4   # fetch last 4 cycles (2 hours)
"""

import os
import sys
import logging
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NOMADS_BASE  = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/glmp/prod'
OUTPUT_BASE  = Path('/LDM/models/glmp')
RETAIN_HOURS = 48      # keep 48 hours of cycles
TIMEOUT_SECS = 120     # per-file download timeout
FILE_STABILITY_SECS = 60  # skip files newer than this (still being written)

VARIABLES = [
    'fcsts_cig',    # ceiling
    'fcsts_t',      # 2m temperature
    'fcsts_td',     # 2m dewpoint
    'fcsts_vis',    # visibility
    'fcsts_wdir',   # 10m wind direction
    'fcsts_wgst',   # 10m wind gust
    'fcsts_wspd',   # 10m wind speed
]

SECTORS = ['co', 'ak']   # CONUS and Alaska


# ---------------------------------------------------------------------------
# Cycle helpers
# ---------------------------------------------------------------------------
def all_cycles_today_and_yesterday(now_utc):
    """
    Generate all 30-min cycle datetimes for today and yesterday,
    newest first, up to RETAIN_HOURS back.
    """
    cycles = []
    # Round down to nearest 30-min boundary
    minute = (now_utc.minute // 30) * 30
    current = now_utc.replace(minute=minute, second=0, microsecond=0)
    cutoff  = now_utc - timedelta(hours=RETAIN_HOURS)
    while current >= cutoff:
        cycles.append(current)
        current -= timedelta(minutes=30)
    return cycles


def cycle_url(cycle_dt, variable, sector):
    """Build NOMADS URL for a given cycle/variable/sector."""
    date_str = cycle_dt.strftime('%Y%m%d')
    cycle_str = cycle_dt.strftime('%H%Mz')
    fname = f'glmp.t{cycle_str}.{variable}.g.{sector}.grib2'
    return f'{NOMADS_BASE}/glmp.{date_str}/{fname}'


def output_path(cycle_dt, variable, sector):
    """Local output path for a given cycle/variable/sector."""
    date_str  = cycle_dt.strftime('%Y%m%d')
    cycle_str = cycle_dt.strftime('%H%Mz')
    fname = f'glmp_{date_str}_{cycle_str}_{sector}_{variable}.grib2'
    return OUTPUT_BASE / date_str / fname


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download_file(url, dest_path, timeout=TIMEOUT_SECS):
    """
    Download url to dest_path. Returns True on success, False on failure.
    Uses atomic write (temp file + rename) to avoid partial files.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix('.tmp')
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
        tmp_path.write_bytes(data)
        tmp_path.rename(dest_path)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False   # cycle not yet available — normal
        log.warning(f'HTTP {e.code} fetching {url}')
        return False
    except Exception as e:
        log.warning(f'Download failed {url}: {e}')
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Scour
# ---------------------------------------------------------------------------
def scour_old_files(now_utc):
    """Remove GLMP files older than RETAIN_HOURS."""
    cutoff = now_utc - timedelta(hours=RETAIN_HOURS)
    removed = 0
    for date_dir in sorted(OUTPUT_BASE.glob('*')):
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, '%Y%m%d').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # If entire day is before cutoff, remove directory
        if dir_date + timedelta(days=1) < cutoff:
            for f in date_dir.glob('*.grib2'):
                f.unlink()
                removed += 1
            try:
                date_dir.rmdir()
                log.info(f'Scoured directory {date_dir.name}')
            except OSError:
                pass
        else:
            # Remove individual files by mtime
            for f in date_dir.glob('*.grib2'):
                if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
                    f.unlink()
                    removed += 1
    if removed:
        log.info(f'Scoured {removed} old GLMP files')


# ---------------------------------------------------------------------------
# Main fetch logic
# ---------------------------------------------------------------------------
def fetch_cycle(cycle_dt, sectors=SECTORS, variables=VARIABLES, force=False):
    """
    Fetch all variable/sector combinations for one cycle.
    Skips files that already exist (unless force=True).
    Returns (downloaded, skipped, failed) counts.
    """
    downloaded = skipped = failed = 0
    cycle_str = cycle_dt.strftime('%Y%m%d %H:%MZ')

    for sector in sectors:
        for variable in variables:
            dest = output_path(cycle_dt, variable, sector)
            if dest.exists() and not force:
                skipped += 1
                continue
            url = cycle_url(cycle_dt, variable, sector)
            ok  = download_file(url, dest)
            if ok:
                downloaded += 1
                log.debug(f'  OK  {dest.name}')
            else:
                failed += 1
                log.debug(f'  --  {dest.name} (not available)')

    if downloaded or failed:
        log.info(f'Cycle {cycle_str}: {downloaded} downloaded, '
                 f'{skipped} skipped, {failed} not available')
    return downloaded, skipped, failed


def main():
    parser = argparse.ArgumentParser(description='Fetch NOMADS GLMP forecasts')
    parser.add_argument('--cycles', type=int, default=2,
                        help='Number of most recent cycles to fetch (default: 2)')
    parser.add_argument('--force', action='store_true',
                        help='Re-download even if file exists')
    parser.add_argument('--sectors', nargs='+', default=SECTORS,
                        choices=['co', 'ak'],
                        help='Sectors to download (default: co ak)')
    parser.add_argument('--variables', nargs='+', default=VARIABLES,
                        help='Variables to download (default: all 7)')
    parser.add_argument('--scour', action='store_true', default=True,
                        help='Scour old files after fetching (default: True)')
    parser.add_argument('--no-scour', dest='scour', action='store_false')
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    log.info(f'GLMP fetch started: {now_utc.strftime("%Y-%m-%d %H:%MZ")}')
    log.info(f'Fetching {args.cycles} most recent cycles, '
             f'sectors={args.sectors}, variables={len(args.variables)}')

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    cycles = all_cycles_today_and_yesterday(now_utc)[:args.cycles]
    total_dl = total_skip = total_fail = 0

    for cycle_dt in cycles:
        dl, sk, fa = fetch_cycle(cycle_dt, args.sectors, args.variables, args.force)
        total_dl   += dl
        total_skip += sk
        total_fail += fa

    log.info(f'Total: {total_dl} downloaded, {total_skip} already present, '
             f'{total_fail} not yet available')

    if args.scour:
        scour_old_files(now_utc)

    log.info('GLMP fetch complete')


if __name__ == '__main__':
    main()
