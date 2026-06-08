#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_taf.py — CAP WxCOP TAF Ingest
=====================================
Parses TAF bulletin files from /LDM/text/taf/ and ingests into
observations.taf on data2.

Handles:
  - TAF ICAO DDHHMMz DDHH/DDHH ...    (standard with TAF prefix)
  - TAF AMD ICAO DDHHMMz DDHH/DDHH ... (amendment)
  - TAF COR ICAO DDHHMMz DDHH/DDHH ... (correction)
  - ICAO DDHHMMz DDHH/DDHH ...         (no TAF prefix, regional format)
  - = end-of-TAF terminator
  - Multiple bulletins concatenated in one file (KWBC format)
  - Military TAFs with QNH/non-standard groups

Key fix vs previous version:
  - Pattern handles TAF AMD / TAF COR prefixes
  - = terminator forces record boundary (prevents cross-bulletin bleed)
  - WMO header lines explicitly skipped (not treated as continuation)
  - Byte-count lines explicitly skipped
"""

import os
import re
import sys
import psycopg2
import signal
from datetime import datetime, timedelta
from pathlib import Path
import logging

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('/var/log/taf_ingest.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_CONFIG = {
    'dbname': 'avwx_data',
    'user':   'avwx_user',
    'host':   '192.168.0.60'
}

# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
class TimeoutError(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutError("File parsing timed out")

# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def parse_taf_time(time_str, reference_date=None):
    """Parse DDHHMM timestamp → UTC datetime."""
    if reference_date is None:
        reference_date = datetime.utcnow()
    try:
        day    = int(time_str[0:2])
        hour   = int(time_str[2:4])
        minute = int(time_str[4:6])
        dt = datetime(reference_date.year, reference_date.month, day, hour, minute)
        # Handle month rollover — if date is >15 days in the future, step back a month
        if dt > reference_date + timedelta(days=15):
            if reference_date.month == 1:
                dt = dt.replace(year=reference_date.year - 1, month=12)
            else:
                dt = dt.replace(month=reference_date.month - 1)
        return dt
    except Exception as e:
        logger.debug(f"Error parsing time '{time_str}': {e}")
        return None

def parse_taf_valid_period(period_str, issue_time):
    """Parse DDHH/DDHH valid period → (valid_from, valid_to)."""
    try:
        from_str, to_str = period_str.split('/')
        from_day  = int(from_str[0:2]); from_hour = int(from_str[2:4])
        to_day    = int(to_str[0:2]);   to_hour   = int(to_str[2:4])
        if to_hour == 24:
            to_hour = 0; to_day += 1
        valid_from = datetime(issue_time.year, issue_time.month, from_day, from_hour, 0)
        valid_to   = datetime(issue_time.year, issue_time.month, to_day,   to_hour,   0)
        if valid_to < valid_from:
            valid_to += timedelta(days=1)
            if valid_to < valid_from:
                if valid_to.month == 12:
                    valid_to = valid_to.replace(year=valid_to.year + 1, month=1)
                else:
                    valid_to = valid_to.replace(month=valid_to.month + 1)
        return valid_from, valid_to
    except Exception as e:
        logger.debug(f"Error parsing valid period '{period_str}': {e}")
        return None, None

# ---------------------------------------------------------------------------
# TAF header patterns
# ---------------------------------------------------------------------------
# Match any of:
#   TAF ICAO DDHHMMz DDHH/DDHH ...
#   TAF AMD ICAO DDHHMMz DDHH/DDHH ...
#   TAF COR ICAO DDHHMMz DDHH/DDHH ...
#   ICAO DDHHMMz DDHH/DDHH ...
#
# Group 1 = ICAO (4 uppercase letters)

_TAF_HEADER = re.compile(
    r'^(?:TAF\s+(?:AMD\s+|COR\s+)?)?'   # optional TAF / TAF AMD / TAF COR prefix
    r'([A-Z]{4})\s+'                      # ICAO — group 1
    r'(\d{6}Z)\s+'                        # DDHHMMz — group 2
    r'(\d{4}/\d{4})',                     # DDHH/DDHH valid period — group 3
    re.IGNORECASE
)

# WMO bulletin header: FTxx## CCCC DDHHMM [qualifier]
_WMO_HEADER = re.compile(r'^[A-Z]{2}[A-Z0-9]{2}\d{2}\s+[A-Z]{4}\s+\d{6}')

# Byte-count line: one or more digits possibly followed by spaces (standalone)
_BYTE_COUNT = re.compile(r'^\d+\s*$')

# Continuation line: starts with whitespace AND has non-whitespace content
_CONTINUATION = re.compile(r'^\s+\S')

def _is_skip_line(line):
    """Return True for lines that are bulletin overhead, not TAF content."""
    stripped = line.strip()
    if not stripped:
        return True   # blank
    if _BYTE_COUNT.match(stripped):
        return True   # byte count
    if _WMO_HEADER.match(stripped):
        return True   # WMO header
    # Standalone keywords that are not TAF content
    if stripped in ('TAF', 'TAF AMD', 'TAF COR', 'TAF CCA', 'TAF RRA'):
        return True
    return False

# ---------------------------------------------------------------------------
# File parser
# ---------------------------------------------------------------------------

def parse_taf_file(filepath, timeout_seconds=60):
    """
    Parse all TAFs from a single bulletin file.

    Key design decisions:
    - TAF header regex handles TAF/TAF AMD/TAF COR/bare ICAO forms
    - '=' end-of-TAF terminator forces record save immediately
    - WMO headers and byte-count lines are explicitly skipped
    - Blank lines are skipped (not treated as record boundaries)
    - Only the ICAO from the TAF header line is used as station_id
    """
    tafs = []
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout_seconds)

    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        if len(content) > 1_000_000:
            logger.warning(f"Skipping very large file (>1MB): {filepath.name}")
            signal.alarm(0)
            return []

        current_lines = []   # lines accumulating for current TAF
        current_match = None # regex match for the current TAF header

        def _save_current():
            """Save current_lines as a TAF record if valid."""
            if not current_lines or current_match is None:
                return
            try:
                station_id    = current_match.group(1).upper()
                issue_time    = parse_taf_time(current_match.group(2)[:-1])  # strip Z
                valid_from, valid_to = parse_taf_valid_period(
                    current_match.group(3), issue_time)
                if issue_time and valid_from and valid_to:
                    tafs.append({
                        'station_id': station_id,
                        'issue_time': issue_time,
                        'valid_from': valid_from,
                        'valid_to':   valid_to,
                        'raw_text':   '\n'.join(current_lines),
                    })
            except Exception as e:
                logger.debug(f"Error saving TAF: {e}")

        for raw_line in content.splitlines():
            line = raw_line.rstrip()

            # Skip bulletin overhead
            if _is_skip_line(line):
                continue

            # Check for TAF header
            m = _TAF_HEADER.match(line)
            if m:
                # Save whatever was accumulating
                _save_current()
                current_lines = [line]
                current_match = m
                # If line ends with = it's a single-line TAF
                if line.rstrip().endswith('='):
                    _save_current()
                    current_lines = []
                    current_match = None
                continue

            # End-of-TAF terminator on a continuation line
            if current_lines and _CONTINUATION.match(line):
                current_lines.append(line)
                if line.rstrip().endswith('='):
                    _save_current()
                    current_lines = []
                    current_match = None
                continue

            # Line doesn't match header or continuation — could be a bare
            # end-of-bulletin line or unknown format; if we have an open TAF
            # and it ends with = already we'd have closed it. Otherwise discard.
            # This prevents non-TAF bulletin text from polluting raw_text.

        # Save final TAF
        _save_current()

        if tafs:
            stations = sorted(set(t['station_id'] for t in tafs))
            logger.info(f"Parsed {len(tafs)} TAFs from {filepath.name} — "
                        f"{', '.join(stations[:10])}"
                        f"{'...' if len(stations) > 10 else ''}")

        signal.alarm(0)
        return tafs

    except TimeoutError:
        logger.error(f"TIMEOUT parsing {filepath} after {timeout_seconds}s — skipping")
        signal.alarm(0)
        return []
    except Exception as e:
        logger.error(f"Error parsing {filepath}: {e}")
        signal.alarm(0)
        return []

# ---------------------------------------------------------------------------
# Database insert
# ---------------------------------------------------------------------------

def insert_taf(conn, taf):
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO observations.taf
                (station_id, issue_time, valid_from, valid_to, raw_text, location)
            VALUES (
                %s, %s, %s, %s, %s,
                (SELECT location FROM observations.airports WHERE station_id = %s)
            )
            ON CONFLICT (station_id, issue_time)
            DO NOTHING
        """, (
            taf['station_id'], taf['issue_time'],
            taf['valid_from'], taf['valid_to'],
            taf['raw_text'],   taf['station_id']
        ))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Error inserting TAF for {taf['station_id']}: {e}")
        conn.rollback()
        return False

# ---------------------------------------------------------------------------
# Directory ingest
# ---------------------------------------------------------------------------

# Station prefixes to ingest — K=CONUS, P=Pacific, T=Caribbean, C=Canada
# Expand as needed for OCONUS ops (e.g. 'M' for Mexico/Caribbean, 'N' for Pacific)
INGEST_PREFIXES = frozenset('KPTC')

def ingest_taf_directory(directory, conn):
    taf_count = 0; file_count = 0; skipped_count = 0; error_count = 0

    for filepath in sorted(Path(directory).glob('*.txt')):
        # Filter by originating center prefix in filename
        # KWBC, KSHV, KLZK → K prefix passes
        # NWCC, BGGH, LLBD → non-K/P/T/C prefix skipped
        # Note: KWBC carries worldwide TAFs — station_id filter below handles that
        if filepath.name[0] not in INGEST_PREFIXES:
            skipped_count += 1
            continue

        file_count += 1
        if file_count % 100 == 0:
            logger.info(f"Progress: {file_count} files, {taf_count} TAFs, "
                        f"{skipped_count} skipped, {error_count} errors")

        tafs = parse_taf_file(filepath, timeout_seconds=60)
        if not tafs:
            skipped_count += 1
            continue

        for taf in tafs:
            # Only store TAFs for K/P/T stations (CONUS + Pacific + Caribbean)
            # KWBC bulletins contain worldwide TAFs — filter here not at file level
            if taf['station_id'][0] not in INGEST_PREFIXES:
                continue
            if insert_taf(conn, taf):
                taf_count += 1
            else:
                error_count += 1

    logger.info(f"Directory {directory}: {file_count} files processed, "
                f"{taf_count} TAFs ingested, {skipped_count} skipped, "
                f"{error_count} errors")
    return taf_count

# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------

def ingest_recent_tafs(hours=24):
    base_dir = Path('/LDM/text/taf')
    now = datetime.utcnow()
    dates_to_process = []
    for i in range(int(hours / 24) + 2):
        d = base_dir / (now - timedelta(days=i)).strftime('%Y/%m/%d')
        if d.exists():
            dates_to_process.append(d)

    logger.info(f"Processing {len(dates_to_process)} date directories")
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    total = 0
    for d in dates_to_process:
        logger.info(f"Processing: {d}")
        total += ingest_taf_directory(d, conn)
    conn.close()
    logger.info(f"Total ingested: {total} TAFs")
    return total

def cleanup_old_tafs(days=7):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)
        cur.execute("DELETE FROM observations.taf WHERE issue_time < %s", (cutoff,))
        deleted = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        logger.info(f"Deleted {deleted} old TAFs (>{days} days)")
        return deleted
    except Exception as e:
        logger.error(f"Error cleaning up old TAFs: {e}")
        return 0

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='TAF Ingest')
    ap.add_argument('--recent', type=int, default=90,
                    help='Lookback in minutes (default 90)')
    args = ap.parse_args()

    hours = args.recent / 60.0

    logger.info('=' * 70)
    logger.info('TAF Ingest Starting')
    logger.info('=' * 70)

    count = ingest_recent_tafs(hours=hours)
    cleanup_old_tafs(days=7)

    logger.info('=' * 70)
    logger.info(f'TAF Ingest Complete — {count} TAFs processed')
    logger.info('=' * 70)
