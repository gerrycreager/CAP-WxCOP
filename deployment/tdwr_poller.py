#!/usr/bin/env python3
"""
tdwr_poller.py — Fetch TDWR Level-3 NIDS files from NWS tgftp RPCCDS
Runs every 2.5 minutes via systemd timer on r815.

Fetches DS.180z0 (TZ0), DS.180z1 (TZ1), DS.180z2 (TZ2) for all 47 TDWR sites.
Writes to /LDM/radar/level3/T{SITE}/TZ{n}/nids/YYYYMMDD/ to match nids_api.py paths.
Scours files older than KEEP_HOURS.

Usage:
    python3 tdwr_poller.py [--site TDFW] [--product TZ0] [--dry-run]
"""

import os
import sys
import time
import logging
import hashlib
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
TGFTP_BASE  = 'https://tgftp.nws.noaa.gov/SL.us008001/DF.of/DC.radar'
L3_BASE     = '/LDM/radar/level3'
KEEP_HOURS  = 24
TIMEOUT     = 15        # HTTP timeout seconds
MIN_SIZE    = 50_000    # bytes — reject truncated/empty fetches
LOG_FILE    = '/home/ldm/var/logs/tdwr_poller.log'

# tgftp directory name → our product code
PRODUCTS = {
    'DS.180z0': 'TZ0',   # Base refl tilt 1, 48nmi, 150m
    'DS.180z1': 'TZ1',   # Base refl tilt 2
    'DS.180z2': 'TZ2',   # Base refl tilt 3
    'DS.186zl': 'TZL',   # Long range refl, 225nmi, 300m
}

# All 47 TDWR sites — tgftp SI directory uses lowercase t prefix
TDWR_SITES = [
    'TADW','TATL','TBNA','TBOS','TBWI','TCLT','TCMH','TCVG','TDAL','TDAY',
    'TDCA','TDEN','TDFW','TDTW','TEWR','TFLL','THOU','TIAD','TIAH','TICH',
    'TIDS','TJBQ','TJFK','TJRV','TLAS','TLVE','TMCI','TMCO','TMDW','TMEM',
    'TMIA','TMKE','TMSP','TMSY','TOKC','TORD','TPBI','TPHL','TPHX','TPIT',
    'TRDU','TSDF','TSJU','TSLC','TSTL','TTPA','TTUL',
]

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('tdwr_poller')


def fetch_nids(url: str, dest: Path, dry_run: bool = False) -> bool:
    """Fetch a NIDS file from tgftp. Returns True if new data written."""
    try:
        # HEAD first to get Content-Length and Last-Modified
        head = requests.head(url, timeout=TIMEOUT)
        if head.status_code != 200:
            return False

        remote_size = int(head.headers.get('Content-Length', 0))
        if remote_size < MIN_SIZE:
            log.debug(f'Skip {url} — remote size {remote_size} < {MIN_SIZE}')
            return False

        # Dedup by Last-Modified header — sn.last size is stable per scan
        # but Last-Modified changes when tgftp updates the file
        remote_mtime = head.headers.get('Last-Modified', '')
        stamp_file   = dest.parent / (dest.stem + '.mtime')
        if dest.exists() and stamp_file.exists():
            if stamp_file.read_text().strip() == remote_mtime:
                return False  # Already have this scan

        if dry_run:
            log.info(f'DRY-RUN would fetch {url} → {dest} ({remote_size}B)')
            return True

        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return False
        if len(resp.content) < MIN_SIZE:
            log.warning(f'Truncated response from {url}: {len(resp.content)}B')
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        # Save Last-Modified stamp for dedup on next poll
        stamp_file = dest.parent / (dest.stem + '.mtime')
        stamp_file.write_text(remote_mtime)
        log.info(f'Fetched {dest.name} ({len(resp.content)}B) ← {url}')
        return True

    except requests.exceptions.Timeout:
        log.warning(f'Timeout fetching {url}')
        return False
    except Exception as e:
        log.error(f'Error fetching {url}: {e}')
        return False


def poll_site(site: str, ds_dir: str, product: str,
              now: datetime, dry_run: bool) -> int:
    """Poll one site/product combination. Returns count of new files written."""
    si_dir   = site.lower()
    url_last = f'{TGFTP_BASE}/{ds_dir}/SI.{si_dir}/sn.last'
    fetched  = 0

    try:
        head = requests.head(url_last, timeout=TIMEOUT)
        if head.status_code != 200:
            return 0
        remote_size  = int(head.headers.get('Content-Length', 0))
        remote_mtime = head.headers.get('Last-Modified', '')
        if remote_size < MIN_SIZE:
            return 0
    except Exception as e:
        log.warning(f'HEAD failed {site}/{product}: {e}')
        return 0

    # Derive filename and date from Last-Modified header (scan time, not fetch time)
    # Last-Modified format: "Sun, 31 May 2026 20:09:00 GMT"
    try:
        from email.utils import parsedate_to_datetime
        scan_dt  = parsedate_to_datetime(remote_mtime).astimezone(timezone.utc)
        date_str = scan_dt.strftime('%Y%m%d')
        ts       = scan_dt.strftime('%d%H%M')
    except Exception:
        # Fallback to local time if header unparseable
        date_str = now.strftime('%Y%m%d')
        ts       = now.strftime('%d%H%M')

    out_dir  = Path(L3_BASE) / site / product / 'nids' / date_str
    fname    = f'{site}_{product}_{ts}.nids'
    dest     = out_dir / fname

    # Dedup: if this exact file already exists with correct size, skip
    if dest.exists() and dest.stat().st_size == remote_size:
        return 0

    if dry_run:
        log.info(f'DRY-RUN would fetch {url_last} → {dest} ({remote_size}B)')
        return 1

    try:
        resp = requests.get(url_last, timeout=TIMEOUT)
        if resp.status_code != 200 or len(resp.content) < MIN_SIZE:
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        log.info(f'Fetched {dest.name} ({len(resp.content)}B) ← {url_last}')
        fetched = 1
    except requests.exceptions.Timeout:
        log.warning(f'Timeout: {site}/{product}')
    except Exception as e:
        log.error(f'Fetch error {site}/{product}: {e}')

    return fetched


def scour_old_files(hours: int = KEEP_HOURS) -> int:
    """Remove NIDS files older than `hours` from L3_BASE TDWR subdirs."""
    cutoff  = time.time() - hours * 3600
    removed = 0
    base    = Path(L3_BASE)
    if not base.exists():
        return 0
    for site in TDWR_SITES:
        site_dir = base / site
        if not site_dir.exists():
            continue
        for f in site_dir.rglob('*.nids'):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
                    # Remove companion .mtime stamp file
                    stamp = f.parent / (f.stem + '.mtime')
                    try: stamp.unlink()
                    except OSError: pass
                    # Remove empty parent dirs
                    try: f.parent.rmdir()
                    except OSError: pass
            except Exception:
                pass
    if removed:
        log.info(f'Scoured {removed} old TDWR NIDS files')
    return removed


def main():
    parser = argparse.ArgumentParser(description='TDWR tgftp poller')
    parser.add_argument('--site',    help='Poll only this site (e.g. TDFW)')
    parser.add_argument('--product', help='Poll only this product (e.g. TZ0)')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--scour-only', action='store_true')
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.scour_only:
        scour_old_files()
        return

    sites    = [args.site.upper()] if args.site else TDWR_SITES
    products = {k: v for k, v in PRODUCTS.items()
                if not args.product or v == args.product.upper()}

    if not products:
        log.error(f'Unknown product: {args.product}')
        sys.exit(1)

    total   = 0
    t_start = time.time()

    for ds_dir, product in products.items():
        for site in sites:
            n = poll_site(site, ds_dir, product, now, args.dry_run)
            total += n

    elapsed = time.time() - t_start
    if total or elapsed > 10:
        log.info(f'Poll complete: {total} new files in {elapsed:.1f}s '
                 f'({len(sites)} sites × {len(products)} products)')

    # Scour once per hour (when minute < 3)
    if now.minute < 3:
        scour_old_files()


if __name__ == '__main__':
    main()
