#!/var/www/cap_winds_app/venv/bin/python3
"""
METAR/SPECI Ingest Script - OPTIMIZED VERSION
Monitors LDM METAR/SPECI directories and ingests into PostGIS

Key Features:
- Loads airport cache ONCE at startup (89,000+ airports)
- Checks custom_stations table for KQ temporary stations
- Handles international METAR formats gracefully
- Uses ON CONFLICT for duplicate handling
- Inserts partial data even without dewpoint/altimeter

Usage:
  # Process all METARs from today
  python3 ingest_metar.py --today
  
  # Process specific date
  python3 ingest_metar.py --date 20260103
  
  # Monitor and process new files (for cron)
  python3 ingest_metar.py --recent 60
  
Cron:
  # Run every 15 minutes to process recent data
  */15 * * * * /var/www/cap_winds_app/scripts/ingest_metar.py --recent 20 >> /var/log/metar_ingest.log 2>&1
"""
import sys
import os
import argparse
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
import csv
import json

# Add app directory to path
sys.path.insert(0, '/var/www/cap_winds_app')

try:
    from metar import Metar
    import psycopg2
    from db_config import get_connection
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-metar psycopg2-binary")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# LDM directory structure
LDM_METAR_ROOT = "/LDM/text/metar"
LDM_SPECI_ROOT = "/LDM/text/speci"

# Global airport coordinates cache (loaded once at startup)
AIRPORT_COORDS = {}

def load_airport_cache():
    """
    Load all airport coordinates into memory at startup
    Checks both OurAirports CSV and custom_stations table
    This is MUCH faster than querying per-station
    """
    global AIRPORT_COORDS
    
    log.info("Loading airport coordinates cache...")
    
    # Load from OurAirports CSV
    cache_file = "/var/www/cap_winds_app/.cache/airports.csv"
    count_csv = 0
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Try both ident and gps_code
                    for key in ['ident', 'gps_code']:
                        station_id = row.get(key)
                        if station_id and station_id not in AIRPORT_COORDS:
                            try:
                                lat = float(row['latitude_deg'])
                                lon = float(row['longitude_deg'])
                                AIRPORT_COORDS[station_id] = (lat, lon)
                                count_csv += 1
                            except (ValueError, KeyError):
                                continue
            log.info(f"Loaded {count_csv} airports from OurAirports CSV")
        except Exception as e:
            log.warning(f"Error reading cache file: {e}")
    else:
        log.warning(f"Airport cache file not found: {cache_file}")
    
    # Load from custom_stations table (for KQ temporary stations)
    count_custom = 0
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT station_id, latitude, longitude 
            FROM observations.custom_stations 
            WHERE active = true
        """)
        
        for row in cur.fetchall():
            station_id, lat, lon = row
            AIRPORT_COORDS[station_id] = (lat, lon)
            count_custom += 1
        
        cur.close()
        conn.close()
        
        if count_custom > 0:
            log.info(f"Loaded {count_custom} custom stations (KQ temporary, etc.)")
    
    except Exception as e:
        log.debug(f"Could not load custom stations: {e}")
    
    log.info(f"Total airports in cache: {len(AIRPORT_COORDS)}")

def get_airport_coordinates(station_id):
    """
    Get airport coordinates from pre-loaded cache
    Returns (lat, lon) or None if not found
    """
    return AIRPORT_COORDS.get(station_id)

def parse_metar(metar_text, is_speci=False):
    """
    Parse METAR/SPECI text into structured data
    Handles partial data gracefully - will insert even if missing dewpoint/altimeter
    """
    try:
        obs = Metar.Metar(metar_text)
        
        # Get coordinates
        coords = get_airport_coordinates(obs.station_id)
        if not coords:
            log.debug(f"No coordinates found for {obs.station_id}")
            return None
        
        lat, lon = coords
        
        # Extract temperature (required)
        if not obs.temp:
            log.debug(f"No temperature in METAR: {metar_text[:80]}")
            return None
        
        temp_c = obs.temp.value('C')
        
        # Extract dewpoint (may be missing in some reports)
        dewpoint_c = None
        if hasattr(obs, 'dewpt') and obs.dewpt:
            dewpoint_c = obs.dewpt.value('C')
        
        # Extract wind
        wind_dir = None
        wind_speed_kts = None
        wind_gust_kts = None
        
        if obs.wind_dir:
            try:
                wind_dir = int(obs.wind_dir.value())
            except:
                pass
        
        if obs.wind_speed:
            try:
                wind_speed_kts = int(obs.wind_speed.value('KT'))
            except:
                pass
        
        if obs.wind_gust:
            try:
                wind_gust_kts = int(obs.wind_gust.value('KT'))
            except:
                pass
        
        # Extract visibility
        visibility_sm = None
        if obs.vis:
            try:
                visibility_sm = obs.vis.value('SM')
            except:
                pass
        
        # Extract altimeter (may be missing in international METARs)
        altimeter_hg = None
        if obs.press:
            try:
                # Try to get in inches Hg
                altimeter_hg = obs.press.value('IN')
            except:
                # Pressure might be in hPa, convert to inches Hg
                try:
                    hpa = obs.press.value()
                    altimeter_hg = hpa * 0.02953
                except:
                    pass
        
        # Determine flight category
        flight_category = 'UNKNOWN'
        if visibility_sm is not None:
            ceiling = get_ceiling(obs)
            
            if visibility_sm < 1 or (ceiling and ceiling < 500):
                flight_category = 'LIFR'
            elif visibility_sm < 3 or (ceiling and ceiling < 1000):
                flight_category = 'IFR'
            elif visibility_sm < 5 or (ceiling and ceiling < 3000):
                flight_category = 'MVFR'
            else:
                flight_category = 'VFR'
        
        # Extract sky conditions
        sky_conditions = []
        if obs.sky:
            for cover, height, cloud_type in obs.sky:
                sky_dict = {'cover': cover}
                if height:
                    try:
                        sky_dict['height_ft'] = int(height.value('FT'))
                    except:
                        pass
                if cloud_type:
                    sky_dict['type'] = cloud_type
                sky_conditions.append(sky_dict)
        
        # Extract weather phenomena
        present_weather = []
        if obs.weather:
            for wx in obs.weather:
                present_weather.append(str(wx))
        
        return {
            'station_id': obs.station_id,
            'observation_time': obs.time,
            'raw_text': metar_text,
            'temp_c': temp_c,
            'dewpoint_c': dewpoint_c,
            'wind_dir': wind_dir,
            'wind_speed_kts': wind_speed_kts,
            'wind_gust_kts': wind_gust_kts,
            'visibility_sm': visibility_sm,
            'altimeter_hg': altimeter_hg,
            'flight_category': flight_category,
            'sky_conditions': sky_conditions,
            'present_weather': present_weather,
            'latitude': lat,
            'longitude': lon,
            'is_speci': is_speci
        }
        
    except Exception as e:
        # Only log first 80 chars to avoid spam
        log.debug(f"Failed to parse METAR: {metar_text[:80]}... - {e}")
        return None

def get_ceiling(obs):
    """
    Calculate ceiling from sky conditions
    Returns ceiling in feet or None
    """
    ceiling = None
    
    if obs.sky:
        for condition in obs.sky:
            cover = condition[0]
            if cover in ['BKN', 'OVC']:
                height = condition[1]
                if height:
                    try:
                        height_ft = int(height.value('FT'))
                        if ceiling is None or height_ft < ceiling:
                            ceiling = height_ft
                    except:
                        pass
    
    return ceiling

def insert_metar(conn, metar_data):
    """
    Insert METAR into database
    Uses ON CONFLICT to handle duplicates gracefully
    """
    try:
        cur = conn.cursor()
        
        # Convert sky_conditions to JSON
        sky_json = json.dumps(metar_data['sky_conditions']) if metar_data['sky_conditions'] else None
        
        cur.execute("""
            INSERT INTO observations.metar (
                station_id, observation_time, raw_text,
                temp_c, dewpoint_c, wind_dir, wind_speed_kts, wind_gust_kts,
                visibility_sm, altimeter_hg, flight_category,
                sky_conditions, present_weather, location, is_speci
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                %s
            )
            ON CONFLICT (station_id, observation_time) DO UPDATE SET
                raw_text = EXCLUDED.raw_text,
                temp_c = EXCLUDED.temp_c,
                dewpoint_c = EXCLUDED.dewpoint_c,
                wind_dir = EXCLUDED.wind_dir,
                wind_speed_kts = EXCLUDED.wind_speed_kts,
                wind_gust_kts = EXCLUDED.wind_gust_kts,
                visibility_sm = EXCLUDED.visibility_sm,
                altimeter_hg = EXCLUDED.altimeter_hg,
                flight_category = EXCLUDED.flight_category,
                sky_conditions = EXCLUDED.sky_conditions,
                present_weather = EXCLUDED.present_weather,
                location = EXCLUDED.location,
                is_speci = EXCLUDED.is_speci
        """, (
            metar_data['station_id'],
            metar_data['observation_time'],
            metar_data['raw_text'],
            metar_data['temp_c'],
            metar_data['dewpoint_c'],
            metar_data['wind_dir'],
            metar_data['wind_speed_kts'],
            metar_data['wind_gust_kts'],
            metar_data['visibility_sm'],
            metar_data['altimeter_hg'],
            metar_data['flight_category'],
            sky_json,
            metar_data['present_weather'],
            metar_data['longitude'],
            metar_data['latitude'],
            metar_data['is_speci']
        ))
        
        cur.close()
        conn.commit()
        return True
        
    except Exception as e:
        log.error(f"Failed to insert METAR for {metar_data['station_id']}: {e}")
        conn.rollback()
        return False

def process_metar_file(filepath, is_speci=False):
    """
    Process a single METAR/SPECI file
    Handles both standalone and collective bulletin formats
    Returns (success_count, fail_count)
    """
    success = 0
    failed = 0
    
    try:
        with open(filepath, 'r', errors='ignore') as f:
            content = f.read()
        
        # Split into individual lines (= separator for reports)
        lines = [line.strip() for line in content.replace('=', '\n').split('\n')]
        
        # Extract METARs - handle both formats:
        # 1. "METAR KDCA 061452Z ..." (standalone)
        # 2. "KDCA 061452Z ..." (collective bulletin - needs METAR prepended)
        metar_lines = []
        for line in lines:
            if not line:
                continue
            
            # Check if line starts with METAR/SPECI (standalone format)
            if line.startswith(('METAR ', 'SPECI ')):
                metar_lines.append(line)
            # Check if line starts with station ID + timestamp (collective format)
            # Pattern: 4 letters, space, 6-7 digits ending in Z
            elif re.match(r'^[A-Z]{4}\s+\d{6,7}Z', line):
                # Prepend METAR/SPECI for parser
                prefix = 'SPECI' if is_speci else 'METAR'
                metar_lines.append(f'{prefix} {line}')
        
        if not metar_lines:
            return 0, 0
        
        conn = get_connection()
        
        for metar_text in metar_lines:
            parsed = parse_metar(metar_text, is_speci)
            if parsed:
                if insert_metar(conn, parsed):
                    success += 1
                else:
                    failed += 1
            else:
                failed += 1
        
        conn.close()
        
    except Exception as e:
        log.error(f"Failed to process file {filepath}: {e}")
        failed += 1
    
    return success, failed

def find_metar_files(date_str=None, minutes_recent=None):
    """
    Find METAR/SPECI files to process
    
    Args:
        date_str: Date in YYYYMMDD format
        minutes_recent: Process files modified in last N minutes
        
    Returns:
        List of (filepath, is_speci) tuples
    """
    files = []
    
    if date_str:
        # Process specific date
        year = date_str[:4]
        month = date_str[4:6]
        day = date_str[6:8]
        
        # METAR files
        metar_dir = Path(LDM_METAR_ROOT) / year / month / day
        if metar_dir.exists():
            for f in metar_dir.glob('*'):
                if f.is_file():
                    files.append((str(f), False))
        
        # SPECI files
        speci_dir = Path(LDM_SPECI_ROOT) / year / month / day
        if speci_dir.exists():
            for f in speci_dir.glob('*'):
                if f.is_file():
                    files.append((str(f), True))
    
    elif minutes_recent:
        # Process recent files
        cutoff_time = datetime.now() - timedelta(minutes=minutes_recent)
        
        for root_dir, is_speci in [(LDM_METAR_ROOT, False), (LDM_SPECI_ROOT, True)]:
            root = Path(root_dir)
            if not root.exists():
                continue
            
            # Check files recursively
            for f in root.rglob('*'):
                if f.is_file():
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime > cutoff_time:
                        files.append((str(f), is_speci))
    
    return files

def main():
    parser = argparse.ArgumentParser(description='Ingest METAR/SPECI data into PostGIS')
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--today', action='store_true', help='Process today\'s data')
    group.add_argument('--date', help='Process specific date (YYYYMMDD)')
    group.add_argument('--recent', type=int, metavar='MINUTES',
                      help='Process files modified in last N minutes')
    
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    if args.verbose:
        log.setLevel(logging.DEBUG)
    
    # Load airport coordinates cache ONCE at startup
    # This is critical for performance!
    load_airport_cache()
    
    # Determine which files to process
    if args.today:
        date_str = datetime.now().strftime('%Y%m%d')
        log.info(f"Processing METARs from {date_str}")
        files = find_metar_files(date_str=date_str)
    elif args.date:
        log.info(f"Processing METARs from {args.date}")
        files = find_metar_files(date_str=args.date)
    else:
        log.info(f"Processing METARs from last {args.recent} minutes")
        files = find_metar_files(minutes_recent=args.recent)
    
    if not files:
        log.info("No files found to process")
        return
    
    log.info(f"Found {len(files)} file(s) to process")
    
    total_success = 0
    total_failed = 0
    
    for filepath, is_speci in files:
        log.debug(f"Processing: {filepath}")
        success, failed = process_metar_file(filepath, is_speci)
        total_success += success
        total_failed += failed
    
    log.info(f"Complete: {total_success} METARs inserted, {total_failed} failed")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


