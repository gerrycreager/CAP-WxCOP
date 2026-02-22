#!/var/www/cap_winds_app/venv/bin/python3
"""
TAF Wind Forecast Populator
Extracts wind forecast periods from TAFs and stores in wind forecast table

This script:
1. Reads TAFs from observations.taf table
2. Parses forecast periods (FM, TEMPO, BECMG, PROB)
3. Extracts wind forecasts from each period
4. Stores in observations.taf_wind_forecasts table

Usage:
  python3 populate_taf_winds.py --recent 6     # Last 6 hours of TAFs
  python3 populate_taf_winds.py --all          # All TAFs in database

Cron:
  # Run hourly to process new TAFs
  30 * * * * /var/www/cap_winds_app/scripts/populate_taf_winds.py --recent 2 >> /var/log/taf_winds.log 2>&1
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timedelta
import re
import json

sys.path.insert(0, '/var/www/cap_winds_app')

try:
    import psycopg2
    from db_config import get_connection
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


def parse_wind_group(wind_str):
    """
    Parse TAF wind group (e.g., '27015G25KT', 'VRB05KT')
    Returns (direction, speed, gust) or None
    """
    if not wind_str:
        return None
    
    # Pattern: dddssGggKT or dddssKT or VRBssKT
    match = re.match(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT', wind_str)
    
    if not match:
        return None
    
    dir_str, speed_str, gust_str = match.groups()
    
    direction = None if dir_str == 'VRB' else int(dir_str)
    speed = int(speed_str)
    gust = int(gust_str) if gust_str else None
    
    return (direction, speed, gust)


def parse_taf_periods(taf_text):
    """
    Parse TAF forecast periods
    Returns list of dicts with period info
    """
    periods = []
    
    # Remove METAR-style header if present
    lines = taf_text.split('\n')
    taf_body = ' '.join(lines)
    
    # Split into change groups (FM, TEMPO, BECMG, PROB)
    # This is simplified - real TAF parsing is complex
    
    # Look for FM groups (From)
    fm_pattern = r'FM(\d{6})\s+(.*?)(?=FM|TEMPO|BECMG|PROB|$)'
    for match in re.finditer(fm_pattern, taf_body):
        time_str, content = match.groups()
        
        # Parse valid time
        try:
            # FM time is DDhhmm
            day = int(time_str[:2])
            hour = int(time_str[2:4])
            minute = int(time_str[4:6])
            
            # Create datetime (approximate - need TAF issue time for year/month)
            valid_from = datetime.utcnow().replace(day=day, hour=hour, minute=minute, second=0)
            
            # Parse winds from content
            wind_match = re.search(r'(\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT', content)
            if wind_match:
                wind_info = parse_wind_group(wind_match.group(0))
                if wind_info:
                    periods.append({
                        'type': 'FM',
                        'valid_from': valid_from,
                        'valid_to': None,  # Extends to next change group or TAF end
                        'wind': wind_info,
                        'text': match.group(0)
                    })
        except:
            continue
    
    # Look for TEMPO groups
    tempo_pattern = r'TEMPO\s+(\d{4})/(\d{4})\s+(.*?)(?=FM|TEMPO|BECMG|PROB|$)'
    for match in re.finditer(tempo_pattern, taf_body):
        from_str, to_str, content = match.groups()
        
        try:
            # Parse times (DDhh/DDhh format)
            from_day = int(from_str[:2])
            from_hour = int(from_str[2:4])
            to_day = int(to_str[:2])
            to_hour = int(to_str[2:4])
            
            valid_from = datetime.utcnow().replace(day=from_day, hour=from_hour, minute=0)
            valid_to = datetime.utcnow().replace(day=to_day, hour=to_hour, minute=0)
            
            # Parse winds
            wind_match = re.search(r'(\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT', content)
            if wind_match:
                wind_info = parse_wind_group(wind_match.group(0))
                if wind_info:
                    periods.append({
                        'type': 'TEMPO',
                        'valid_from': valid_from,
                        'valid_to': valid_to,
                        'wind': wind_info,
                        'text': match.group(0)
                    })
        except:
            continue
    
    # Look for PROB groups
    prob_pattern = r'PROB(\d{2})\s+(\d{4})/(\d{4})\s+(.*?)(?=FM|TEMPO|BECMG|PROB|$)'
    for match in re.finditer(prob_pattern, taf_body):
        prob_str, from_str, to_str, content = match.groups()
        
        try:
            probability = int(prob_str)
            from_day = int(from_str[:2])
            from_hour = int(from_str[2:4])
            to_day = int(to_str[:2])
            to_hour = int(to_str[2:4])
            
            valid_from = datetime.utcnow().replace(day=from_day, hour=from_hour, minute=0)
            valid_to = datetime.utcnow().replace(day=to_day, hour=to_hour, minute=0)
            
            wind_match = re.search(r'(\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT', content)
            if wind_match:
                wind_info = parse_wind_group(wind_match.group(0))
                if wind_info:
                    periods.append({
                        'type': f'PROB{probability}',
                        'probability': probability,
                        'valid_from': valid_from,
                        'valid_to': valid_to,
                        'wind': wind_info,
                        'text': match.group(0)
                    })
        except:
            continue
    
    return periods


def calculate_wind_category(wind_speed, gust):
    """Calculate wind category"""
    max_wind = gust if gust else wind_speed
    
    if max_wind >= 25:
        return 'EXTREME'
    elif max_wind >= 15:
        return 'CAUTION'
    else:
        return 'NORMAL'


def populate_taf_winds(hours_back=None):
    """Populate TAF wind forecasts from TAF table"""
    
    conn = get_connection()
    cur = conn.cursor()
    
    # Build query to get recent TAFs
    if hours_back:
        query = """
            SELECT id, station_id, issue_time, raw_text, 
                   ST_X(location) as lon, ST_Y(location) as lat
            FROM observations.taf
            WHERE issue_time > NOW() - INTERVAL '%s hours'
            ORDER BY issue_time DESC
        """
        cur.execute(query, (hours_back,))
    else:
        query = """
            SELECT id, station_id, issue_time, raw_text,
                   ST_X(location) as lon, ST_Y(location) as lat
            FROM observations.taf
            ORDER BY issue_time DESC
        """
        cur.execute(query)
    
    tafs = cur.fetchall()
    log.info(f"Processing {len(tafs)} TAFs")
    
    total_periods = 0
    total_inserted = 0
    
    for taf_id, station_id, issue_time, raw_text, lon, lat in tafs:
        try:
            # Parse TAF periods
            periods = parse_taf_periods(raw_text)
            
            if not periods:
                log.debug(f"No wind periods found in TAF for {station_id}")
                continue
            
            total_periods += len(periods)
            
            # Insert each period
            for period in periods:
                wind_dir, wind_speed, gust = period['wind']
                category = calculate_wind_category(wind_speed, gust)
                
                try:
                    cur.execute("""
                        INSERT INTO observations.taf_wind_forecasts (
                            station_id, location, taf_issue_time,
                            valid_from, valid_to,
                            wind_dir, wind_speed_kts, wind_gust_kts,
                            change_indicator, probability, wind_category,
                            taf_line, taf_id
                        ) VALUES (
                            %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                    """, (
                        station_id, lon, lat, issue_time,
                        period['valid_from'], period.get('valid_to'),
                        wind_dir, wind_speed, gust,
                        period['type'], period.get('probability'),
                        category, period['text'], taf_id
                    ))
                    
                    total_inserted += 1
                    
                except Exception as e:
                    log.error(f"Failed to insert period for {station_id}: {e}")
                    continue
            
            conn.commit()
            
        except Exception as e:
            log.error(f"Failed to process TAF for {station_id}: {e}")
            continue
    
    cur.close()
    conn.close()
    
    log.info(f"Processed {total_periods} forecast periods")
    log.info(f"Inserted {total_inserted} wind forecasts")
    
    return True


def main():
    parser = argparse.ArgumentParser(description='Populate TAF wind forecasts')
    parser.add_argument('--recent', type=int, metavar='HOURS',
                       help='Process TAFs from last N hours')
    parser.add_argument('--all', action='store_true',
                       help='Process all TAFs in database')
    
    args = parser.parse_args()
    
    if args.all:
        success = populate_taf_winds()
    elif args.recent:
        success = populate_taf_winds(hours_back=args.recent)
    else:
        # Default: last 6 hours
        success = populate_taf_winds(hours_back=6)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()


"""
DEPLOYMENT
==========

1. Deploy script:
   cp populate_taf_winds.py /var/www/cap_winds_app/scripts/
   chmod +x /var/www/cap_winds_app/scripts/populate_taf_winds.py

2. Test:
   sudo -u www-data /var/www/cap_winds_app/scripts/populate_taf_winds.py --recent 6

3. Set up cron:
   sudo crontab -u www-data -e
   
   # Run hourly after TAF ingest
   30 * * * * /var/www/cap_winds_app/scripts/populate_taf_winds.py --recent 2 >> /var/log/taf_winds.log 2>&1

4. Check results:
   sudo -u postgres psql -d avwx_data -c "SELECT station_id, COUNT(*) FROM observations.taf_wind_forecasts GROUP BY station_id ORDER BY COUNT(*) DESC LIMIT 10;"

NOTES
=====
- This is a simplified TAF parser
- Real TAF parsing is complex (metar library doesn't handle TAF changes well)
- For production, consider using aviation-focused TAF parser library
- Or enhance this parser with more complete TAF grammar
"""
