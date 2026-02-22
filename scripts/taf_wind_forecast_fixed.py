"""
TAF Wind Forecast Generator for KQ Stations - Fixed Version
"""
import re
from datetime import datetime, timedelta
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

def parse_taf_winds(raw_taf, valid_from):
    """Parse wind information from TAF raw text"""
    winds = []
    
    # Extract initial validity period and wind
    validity_match = re.search(r'(\d{4})/(\d{4})', raw_taf)
    if not validity_match:
        return winds
        
    initial_from = int(validity_match.group(1)[-2:])
    
    # Find initial wind after validity period  
    initial_wind_match = re.search(r'(\d{4})/(\d{4})\s+(\d{3})(\d{2})(?:G(\d{2}))?KT', raw_taf)
    if initial_wind_match:
        wind_dir = int(initial_wind_match.group(3))
        wind_speed = int(initial_wind_match.group(4))
        wind_gust = int(initial_wind_match.group(5)) if initial_wind_match.group(5) else None
        winds.append((0, 12, wind_dir, wind_speed, wind_gust, 'INITIAL'))
    
    # Parse TEMPO periods
    tempo_pattern = r'TEMPO\s+(\d{4})/(\d{4}).*?(\d{3})(\d{2})(?:G(\d{2}))?KT'
    for match in re.finditer(tempo_pattern, raw_taf):
        tempo_from = int(match.group(1)[-2:])
        tempo_to = int(match.group(2)[-2:])
        start_hour = tempo_from - initial_from
        end_hour = tempo_to - initial_from
        if start_hour < 0: start_hour += 24
        if end_hour <= start_hour: end_hour += 24
        wind_dir = int(match.group(3))
        wind_speed = int(match.group(4))
        wind_gust = int(match.group(5)) if match.group(5) else None
        if start_hour < 12:
            winds.append((max(0, start_hour), min(12, end_hour), wind_dir, wind_speed, wind_gust, 'TEMPO'))
    
    # Parse BECMG periods
    becmg_pattern = r'BECMG\s+(\d{4})/(\d{4}).*?(\d{3})(\d{2})(?:G(\d{2}))?KT'
    for match in re.finditer(becmg_pattern, raw_taf):
        becmg_from = int(match.group(1)[-2:])
        becmg_to = int(match.group(2)[-2:])
        start_hour = becmg_from - initial_from
        end_hour = becmg_to - initial_from
        if start_hour < 0: start_hour += 24
        if end_hour <= start_hour: end_hour += 24
        wind_dir = int(match.group(3))
        wind_speed = int(match.group(4))
        wind_gust = int(match.group(5)) if match.group(5) else None
        if start_hour < 12:
            winds.append((max(0, start_hour), min(12, end_hour), wind_dir, wind_speed, wind_gust, 'BECMG'))
    
    return sorted(winds, key=lambda x: x[0])

def calculate_wind_category(wind_speed, wind_gust=None):
    """Calculate CAPR 70-1 wind constraint category"""
    max_wind = wind_gust if wind_gust else wind_speed
    gust_spread = (wind_gust - wind_speed) if wind_gust else 0
    
    if max_wind >= 25 or gust_spread >= 15:
        return 'OUT_OF_LIMITS'
    elif max_wind >= 20 or gust_spread >= 10:
        return 'CAUTION'
    else:
        return 'NORMAL'

def generate_kq_wind_forecasts():
    """Generate wind forecasts for KQ stations"""
    conn = get_connection()
    cur = conn.cursor()
    
    # Get current valid TAFs for KQ stations
    cur.execute("""
        SELECT t.station_id, t.raw_text, t.valid_from, t.valid_to, a.location
        FROM observations.taf t
        JOIN observations.airports a ON t.station_id = a.station_id  
        WHERE t.station_id LIKE 'KQ%'
        AND t.valid_to > NOW()
        AND t.valid_from <= NOW()
        ORDER BY t.station_id, t.issue_time DESC
    """)
    
    current_time = datetime.now()
    model_run = current_time.replace(hour=current_time.hour//6*6, minute=0, second=0, microsecond=0)
    
    processed_stations = set()
    
    for row in cur.fetchall():
        station_id, raw_taf, valid_from, valid_to, location = row
        
        if station_id in processed_stations:
            continue
        processed_stations.add(station_id)
        
        print(f"Processing {station_id}")
        
        winds = parse_taf_winds(raw_taf, valid_from)
        
        for hour in range(13):
            # Find applicable wind for this hour
            wind_dir, wind_speed, wind_gust = 0, 0, None
            
            # Start with initial conditions
            for start_h, end_h, w_dir, w_speed, w_gust, w_type in winds:
                if w_type == 'INITIAL' and start_h <= hour < end_h:
                    wind_dir, wind_speed, wind_gust = w_dir, w_speed, w_gust
            
            # Override with BECMG if applicable
            for start_h, end_h, w_dir, w_speed, w_gust, w_type in winds:
                if w_type == 'BECMG' and start_h <= hour < end_h:
                    wind_dir, wind_speed, wind_gust = w_dir, w_speed, w_gust
                    
            # Override with TEMPO if applicable (highest priority)
            for start_h, end_h, w_dir, w_speed, w_gust, w_type in winds:
                if w_type == 'TEMPO' and start_h <= hour < end_h:
                    wind_dir, wind_speed, wind_gust = w_dir, w_speed, w_gust
            
            category = calculate_wind_category(wind_speed, wind_gust)
            
            # Insert without forecast_time column
            cur.execute("""
                INSERT INTO observations.model_wind_forecasts 
                (station_id, model_run, forecast_hour, wind_speed_kts, wind_dir, wind_gust_kts, wind_category, model_name, location)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (station_id, model_run, forecast_hour) 
                DO UPDATE SET
                    wind_speed_kts = EXCLUDED.wind_speed_kts,
                    wind_dir = EXCLUDED.wind_dir,
                    wind_gust_kts = EXCLUDED.wind_gust_kts,
                    wind_category = EXCLUDED.wind_category,
                    location = EXCLUDED.location
            """, (station_id, model_run, hour, wind_speed, wind_dir, wind_gust, category, 'TAF', location))
            
            print(f"  Hour {hour:2d}: {wind_dir:03d}@{wind_speed:02d}{'G'+str(wind_gust) if wind_gust else '':>4} kt -> {category}")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"\nGenerated TAF-based wind forecasts for {len(processed_stations)} KQ stations")

if __name__ == '__main__':
    generate_kq_wind_forecasts()
