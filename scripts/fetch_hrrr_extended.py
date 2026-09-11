#!/var/www/cap_winds_app/venv/bin/python3
"""
fetch_hrrr_extended.py — top up local HRRR cache with the F19-F48 extended
forecast, for the four synoptic cycles (00/06/12/18 UTC).

Why this exists:
  The IDD/NOAAPort relay this app subscribes to (REQUEST NGRID "HRRR"
  idd.aos.wisc.edu, in /home/ldm/etc/ldmd.conf) has never once delivered a
  HRRR forecast hour beyond F18 -- confirmed empirically (zero F19+ files
  across the entire local cache as of 2026-09-06). Every hourly HRRR cycle
  is limited to F18 on that feed, even the four cycles NCEP itself runs out
  to F48 (00/06/12/18 UTC -- extended to 48h since HRRRv4, Dec 2020; NOT
  36h). Same class of gap as HRRR smoke/MASSDEN, which also isn't on this
  relay and is instead pulled directly from NOAA's public S3 bucket
  (see hrrr_smoke_cache_updater.py) -- this script does the same thing for
  the extended surface tail.

What it does:
  For each local hrrr.YYYYMMDD/HHz directory already populated by the
  normal LDM feed (HH in 00/06/12/18), checks which of F019-F048 are
  missing and byte-range-fetches just the fields CAP WxCOP actually uses
  (matching ingest_model_site_wx.py's field list) from NOAA's public HRRR
  bucket (noaa-hrrr-bdp-pds, NODD open data, no auth), using the .idx
  sidecar to find each message's byte offset -- not the full ~150-175MB
  file. Output lands at the SAME path/naming convention the LDM feed uses
  (hrrr.tHHz.wrfsfcfFFF.grib2, 3-digit forecast hour), so any downstream
  reader of /LDM/models/hrrr just sees the extra hours -- no other script
  needs to change. Safe: LDM never writes anything past F018 for any
  cycle, confirmed empirically, so there's no collision risk.

  NOTE: this writes a CURATED SUBSET of fields (temp/dewpoint/wind/gust/
  precip-type/CAPE/cloud-base/downward-shortwave), not full field parity
  with the ~150MB LDM-delivered F00-F18 files. Good enough for FITS/heat-
  stress and wind/precip type; extend FIELDS below if something else needs
  it.

Run as ldm user, every 30 min:
  */30 * * * * /var/www/cap_winds_app/venv/bin/python3 \
    /var/www/cap_winds_app/scripts/fetch_hrrr_extended.py \
    >> /home/ldm/var/logs/hrrr_extended_fetch.log 2>&1
"""

import os
import re
import sys
import fcntl
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

LOCAL_ROOT   = Path('/LDM/models/hrrr')
S3_BASE      = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
SYNOPTIC_HRS = (0, 6, 12, 18)
MIN_FHR      = 19
MAX_FHR      = 48
LOOKBACK_CYCLES = 8          # how many recent synoptic cycles to check each run
RETAIN_DAYS  = 3             # prune extended-hour files older than this
LOCKFILE     = '/home/ldm/var/run/fetch_hrrr_extended.lock'
LOG_FILE     = '/home/ldm/var/logs/hrrr_extended_fetch.log'

# Fields to pull out of each extended-hour file (name must match the .idx
# entry's VAR:LEVEL text exactly). Mirrors ingest_model_site_wx.py's field
# list minus PRATE/APCP (accumulation-window bookkeeping isn't worth the
# complexity here -- add back if a consumer needs it).
FIELDS = [
    'GUST:surface',
    'TMP:2 m above ground',
    'DPT:2 m above ground',
    'UGRD:10 m above ground',
    'VGRD:10 m above ground',
    'CRAIN:surface',
    'CSNOW:surface',
    'CICEP:surface',
    'CFRZR:surface',
    'CAPE:surface',
    'HGT:cloud base',
    'DSWRF:surface',
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def candidate_cycles(now):
    """Most recent LOOKBACK_CYCLES synoptic (00/06/12/18Z) cycle datetimes, newest first."""
    cycles = []
    t = now.replace(minute=0, second=0, microsecond=0)
    while len(cycles) < LOOKBACK_CYCLES:
        if t.hour in SYNOPTIC_HRS:
            cycles.append(t)
        t -= timedelta(hours=1)
    return cycles


def parse_idx(idx_text):
    """wgrib2-style idx: 'msgnum:byte_offset:d=YYYYMMDDHH:VAR:LEVEL:FCST:' """
    entries = []
    for line in idx_text.strip().splitlines():
        parts = line.split(':')
        if len(parts) < 5:
            continue
        msgnum, offset = int(parts[0]), int(parts[1])
        var, level = parts[3], parts[4]
        entries.append((msgnum, offset, f'{var}:{level}'))
    return entries


def fetch_extended_hour(cycle_dt, fhr, out_path):
    date_str = cycle_dt.strftime('%Y%m%d')
    hh = cycle_dt.hour
    key = f'hrrr.{date_str}/conus/hrrr.t{hh:02d}z.wrfsfcf{fhr:02d}.grib2'
    idx_url = f'{S3_BASE}/{key}.idx'
    grib_url = f'{S3_BASE}/{key}'

    r = requests.get(idx_url, timeout=20)
    if r.status_code == 404:
        return False, 'not posted yet (idx 404)'
    r.raise_for_status()
    entries = parse_idx(r.text)
    if not entries:
        return False, 'empty idx'

    # figure out end-of-file size for the final message's range
    head = requests.head(grib_url, timeout=20)
    head.raise_for_status()
    total_size = int(head.headers['Content-Length'])

    entries.sort(key=lambda e: e[1])
    wanted = []
    for i, (msgnum, offset, name) in enumerate(entries):
        if name not in FIELDS:
            continue
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_size - 1
        wanted.append((name, offset, end))

    missing_fields = set(FIELDS) - {w[0] for w in wanted}
    if missing_fields:
        log.warning(f'  {key}: fields not found in idx: {missing_fields}')

    if not wanted:
        return False, 'none of the wanted fields present'

    tmp_path = out_path.with_suffix('.tmp')
    with open(tmp_path, 'wb') as out:
        for name, start, end in wanted:
            resp = requests.get(grib_url, headers={'Range': f'bytes={start}-{end}'}, timeout=30)
            if resp.status_code not in (200, 206):
                tmp_path.unlink(missing_ok=True)
                return False, f'range fetch failed for {name}: HTTP {resp.status_code}'
            out.write(resp.content)
    tmp_path.rename(out_path)
    return True, f'{len(wanted)}/{len(FIELDS)} fields, {out_path.stat().st_size} bytes'


def prune_old(now):
    cutoff = now - timedelta(days=RETAIN_DAYS)
    pruned = 0
    if not LOCAL_ROOT.is_dir():
        return
    for date_dir in LOCAL_ROOT.glob('hrrr.????????'):
        m = re.match(r'hrrr\.(\d{8})$', date_dir.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), '%Y%m%d').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d < cutoff - timedelta(days=1):
            continue  # only bother scanning dirs near the cutoff boundary
        for hz_dir in date_dir.glob('*z'):
            for f in hz_dir.glob('hrrr.t*z.wrfsfcf0*.grib2'):
                m2 = re.search(r'wrfsfcf(\d{3})\.grib2$', f.name)
                if not m2 or int(m2.group(1)) < MIN_FHR:
                    continue
                if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
                    try:
                        f.unlink()
                        pruned += 1
                    except OSError:
                        pass
    if pruned:
        log.info(f'Pruned {pruned} expired extended-hour files (older than {RETAIN_DAYS}d)')


def main():
    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.warning('Another instance is running — exiting')
        sys.exit(0)

    log.info('=' * 60)
    now = datetime.now(timezone.utc)
    log.info(f'HRRR extended-hour fetch started: {now.strftime("%Y-%m-%d %H:%MZ")}')

    total_fetched = 0
    for cycle_dt in candidate_cycles(now):
        # Extended output for F48 typically isn't fully posted until ~3.5-4h
        # after cycle start; don't bother checking cycles too young.
        if now - cycle_dt < timedelta(hours=3, minutes=30):
            continue

        cycle_dir = LOCAL_ROOT / f'hrrr.{cycle_dt.strftime("%Y%m%d")}' / f'{cycle_dt.hour:02d}z'
        if not cycle_dir.is_dir():
            continue  # normal LDM feed hasn't even created this cycle's dir

        for fhr in range(MIN_FHR, MAX_FHR + 1):
            out_path = cycle_dir / f'hrrr.t{cycle_dt.hour:02d}z.wrfsfcf{fhr:03d}.grib2'
            if out_path.exists():
                continue
            ok, info = fetch_extended_hour(cycle_dt, fhr, out_path)
            tag = f'{cycle_dt.strftime("%Y%m%d")}/{cycle_dt.hour:02d}z F{fhr:03d}'
            if ok:
                log.info(f'  {tag}: OK ({info})')
                total_fetched += 1
            else:
                log.info(f'  {tag}: skipped ({info})')
                break  # hours are sequential on S3; if this one isn't posted, later ones won't be either

    prune_old(now)

    log.info(f'Done. {total_fetched} new extended-hour files fetched.')
    log.info('=' * 60)


if __name__ == '__main__':
    main()
