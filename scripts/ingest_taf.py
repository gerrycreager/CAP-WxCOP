#!/var/www/cap_winds_app/venv/bin/python3
"""
TAF Ingest Script - Fixed for Actual File Formats
Handles both KWBC (with TAF prefix) and KLSX (without prefix) formats
"""

import os
import re
import sys
import psycopg2
import signal
from datetime import datetime, timedelta
from pathlib import Path
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('/var/log/taf_ingest.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Database connection
DB_CONFIG = {
    'dbname': 'avwx_data',
    'user': 'avwx_user',
    'host': 'localhost'
}

class TimeoutError(Exception):
    """Raised when file parsing times out"""
    pass

def timeout_handler(signum, frame):
    """Handler for timeout signal"""
    raise TimeoutError("File parsing timed out")

def parse_taf_time(time_str, reference_date=None):
    """Parse TAF timestamp - Format: DDHHMM"""
    if reference_date is None:
        reference_date = datetime.utcnow()
    
    try:
        day = int(time_str[:2])
        hour = int(time_str[2:4])
        minute = int(time_str[4:6])
        
        dt = datetime(
            reference_date.year,
            reference_date.month,
            day,
            hour,
            minute
        )
        
        # Handle month rollover
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
    """Parse TAF valid period - Format: DDHH/DDHH"""
    try:
        from_str, to_str = period_str.split('/')
        
        from_day = int(from_str[:2])
        from_hour = int(from_str[2:4])
        
        to_day = int(to_str[:2])
        to_hour = int(to_str[2:4])
        
        # Handle hour 24
        if to_hour == 24:
            to_hour = 0
            to_day += 1
        
        valid_from = datetime(
            issue_time.year,
            issue_time.month,
            from_day,
            from_hour,
            0
        )
        
        valid_to = datetime(
            issue_time.year,
            issue_time.month,
            to_day,
            to_hour,
            0
        )
        
        # Handle rollover
        if valid_to < valid_from:
            # Try adding a day first
            valid_to = valid_to + timedelta(days=1)
            
            # If still less, add a month
            if valid_to < valid_from:
                if valid_to.month == 12:
                    valid_to = valid_to.replace(year=valid_to.year + 1, month=1)
                else:
                    valid_to = valid_to.replace(month=valid_to.month + 1)
        
        return valid_from, valid_to
        
    except Exception as e:
        logger.debug(f"Error parsing valid period '{period_str}': {e}")
        return None, None

def parse_taf_file_v3(filepath, timeout_seconds=60):
    """
    Parse TAF file handling both formats:
    1. KWBC format: TAF STATION DDHHMMz DDHH/DDHH ...
    2. KLSX format: STATION DDHHMMz DDHH/DDHH ... (after TAF AMD header)
    """
    tafs = []
    
    # Set up timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout_seconds)
    
    try:
        filename = filepath.name
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # Skip very large files (military TAFs can be longer, so allow up to 1MB)
        if len(content) > 1000000:
            logger.warning(f"Skipping very large file (>1MB): {filename}")
            signal.alarm(0)
            return []
        
        lines = content.split('\n')
        current_taf = []
        
        # Two patterns for worldwide TAF formats:
        # Pattern 1: TAF STATION DDHHMMz DDHH/DDHH ... (KWBC/international format)
        # Pattern 2: STATION DDHHMMz DDHH/DDHH ... (KLSX/regional format)
        # Matches ALL ICAO codes (A-Z): K=US, C=Canada, L=Europe, U=Russia, etc.
        
        pattern1 = re.compile(r'^TAF\s+([A-Z]{4})\s+(\d{6}Z)\s+(\d{4}/\d{4})\s+(.*)$')
        pattern2 = re.compile(r'^([A-Z]{4})\s+(\d{6}Z)\s+(\d{4}/\d{4})\s+(.*)$')
        continuation = re.compile(r'^\s+(.+)$')
        
        for line in lines:
            line = line.rstrip()
            
            # Try pattern 1 first (with TAF prefix)
            match = pattern1.match(line)
            if match:
                # Save previous TAF
                if current_taf:
                    taf_text = '\n'.join(current_taf)
                    taf_obj = parse_taf_from_match(match)
                    if taf_obj:
                        taf_obj['raw_text'] = taf_text
                        tafs.append(taf_obj)
                
                # Start new TAF
                current_taf = [line]
                continue
            
            # Try pattern 2 (without TAF prefix)
            match = pattern2.match(line)
            if match:
                # Save previous TAF
                if current_taf:
                    taf_text = '\n'.join(current_taf)
                    # Need to extract match from first line
                    first_line_match = pattern2.match(current_taf[0]) or pattern1.match(current_taf[0])
                    if first_line_match:
                        taf_obj = parse_taf_from_match(first_line_match)
                        if taf_obj:
                            taf_obj['raw_text'] = taf_text
                            tafs.append(taf_obj)
                
                # Start new TAF
                current_taf = [line]
                continue
            
            # Continuation line
            if current_taf and continuation.match(line):
                current_taf.append(line)
        
        # Don't forget last TAF
        if current_taf:
            taf_text = '\n'.join(current_taf)
            first_line_match = pattern2.match(current_taf[0]) or pattern1.match(current_taf[0])
            if first_line_match:
                taf_obj = parse_taf_from_match(first_line_match)
                if taf_obj:
                    taf_obj['raw_text'] = taf_text
                    tafs.append(taf_obj)
        
        if tafs:
            stations = sorted(set(t['station_id'] for t in tafs))
            logger.info(f"Parsed {len(tafs)} TAFs from {filename} - Stations: {', '.join(stations[:10])}")
        
        signal.alarm(0)
        return tafs
        
    except TimeoutError:
        logger.error(f"TIMEOUT parsing {filepath} after {timeout_seconds} seconds - SKIPPING")
        signal.alarm(0)
        return []
    except Exception as e:
        logger.error(f"Error parsing file {filepath}: {e}")
        signal.alarm(0)
        return []

def parse_taf_from_match(match):
    """Parse TAF from regex match object"""
    try:
        station_id = match.group(1)
        issue_time_str = match.group(2)[:-1]  # Remove 'Z'
        valid_period = match.group(3)
        
        # Parse times
        issue_time = parse_taf_time(issue_time_str)
        if issue_time is None:
            return None
        
        valid_from, valid_to = parse_taf_valid_period(valid_period, issue_time)
        
        return {
            'station_id': station_id,
            'issue_time': issue_time,
            'valid_from': valid_from,
            'valid_to': valid_to
        }
        
    except Exception as e:
        logger.debug(f"Error parsing TAF from match: {e}")
        return None

def insert_taf(conn, taf):
    """Insert TAF into database"""
    try:
        cur = conn.cursor()
        
        # Insert or update
        cur.execute("""
            INSERT INTO observations.taf 
                (station_id, issue_time, valid_from, valid_to, raw_text, location)
            VALUES (%s, %s, %s, %s, %s, (SELECT location FROM observations.airports WHERE station_id = %s))
            ON CONFLICT (station_id, issue_time) 
            DO UPDATE SET
                valid_from = EXCLUDED.valid_from,
                valid_to = EXCLUDED.valid_to,
                raw_text = EXCLUDED.raw_text,
                location = EXCLUDED.location
        """, (
            taf['station_id'],
            taf['issue_time'],
            taf['valid_from'],
            taf['valid_to'],
            taf['raw_text'],
            taf['station_id']
        ))
        
        cur.close()
        return True
        
    except Exception as e:
        logger.error(f"Error inserting TAF for {taf['station_id']}: {e}")
        return False

def ingest_taf_directory(directory, conn):
    """Ingest all TAF files from a directory"""
    taf_count = 0
    file_count = 0
    skipped_count = 0
    error_count = 0
    
    for filepath in sorted(Path(directory).glob('*.txt')):
        file_count += 1
        
        # Log progress every 100 files
        if file_count % 100 == 0:
            logger.info(f"Progress: {file_count} files, {taf_count} TAFs ingested, {skipped_count} skipped, {error_count} errors")
        
        tafs = parse_taf_file_v3(filepath, timeout_seconds=60)
        
        if not tafs:
            skipped_count += 1
            continue
        
        for taf in tafs:
            if insert_taf(conn, taf):
                taf_count += 1
            else:
                error_count += 1
    
    logger.info(f"Directory {directory}: {file_count} files, {taf_count} TAFs ingested, {skipped_count} skipped, {error_count} errors")
    return taf_count

def ingest_recent_tafs(hours=24):
    """Ingest TAFs from last N hours"""
    base_dir = Path('/LDM/text/taf')
    
    # Get date directories to process
    now = datetime.utcnow()
    dates_to_process = []
    
    for i in range(int(hours / 24) + 2):
        date = now - timedelta(days=i)
        date_dir = base_dir / date.strftime('%Y/%m/%d')
        if date_dir.exists():
            dates_to_process.append(date_dir)
    
    logger.info(f"Processing {len(dates_to_process)} date directories")
    
    # Connect to database
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    
    total_tafs = 0
    
    for date_dir in dates_to_process:
        logger.info(f"Processing directory: {date_dir}")
        count = ingest_taf_directory(date_dir, conn)
        total_tafs += count
    
    conn.close()
    
    logger.info(f"Total ingested: {total_tafs} TAFs")
    return total_tafs

def cleanup_old_tafs(days=7):
    """Remove TAFs older than N days"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        cutoff = datetime.now() - timedelta(days=days)
        
        cur.execute("""
            DELETE FROM observations.taf
            WHERE issue_time < %s
        """, (cutoff,))
        
        deleted = cur.rowcount
        
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info(f"Deleted {deleted} old TAFs (older than {days} days)")
        return deleted
        
    except Exception as e:
        logger.error(f"Error cleaning up old TAFs: {e}")
        return 0

if __name__ == '__main__':
    logger.info("=" * 70)
    logger.info("TAF Ingest Starting")
    logger.info("=" * 70)
    
    # Ingest last 24 hours
    count = ingest_recent_tafs(hours=24)
    
    # Cleanup old data
    cleanup_old_tafs(days=7)
    
    logger.info("=" * 70)
    logger.info(f"TAF Ingest Complete - Processed {count} TAFs")
    logger.info("=" * 70)

