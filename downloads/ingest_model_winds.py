#!/var/www/cap_winds_app/venv/bin/python3
"""
Model Wind Forecast Ingest Script
Extracts 12-hour wind forecasts from HRRR/GFS and stores in PostGIS

This script:
1. Reads latest HRRR/GFS model run
2. Extracts wind forecasts for all airports (0-12 hour forecasts)
3. Stores in observations.model_wind_forecasts table
4. Calculates maximum winds and categories

Usage:
  python3 ingest_model_winds.py --model HRRR --hours 12
  python3 ingest_model_winds.py --model GFS --hours 12

Cron:
  # Run after each new model cycle
  15 */1 * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py --model HRRR >> /var/log/model_winds.log 2>&1
  25 */6 * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py --model GFS >> /var/log/model_winds.log 2>&1
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timedelta
import requests
import csv
import io

sys.path.insert(0, '/var/www/cap_winds_app')

try:
    import pygrib
    import psycopg2
    from db_config import get_connection
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install: pip install pygrib psycopg2-binary")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# LDM directories
HRRR_ROOT = "/LDM/models/hrrr"
GFS_ROOT = "/LDM/models/gfs"

# Airport database
AIRPORT_COORDS = {}


def load_airport_coordinates():
    """Load airport coordinates from OurAirports CSV"""
    global AIRPORT_COORDS
    
    cache_file = "/var/www/cap_winds_app/.cache/airports.csv"
    
    if not os.path.exists(cache_file):
        log.info("Downloading airport database...")
        try:
            url = "https://davidmegginson.github.io/ourairports-data/airports.csv"
            response = requests.get(url, timeout=30)
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, 'w') as f:
                f.write(response.text)
        except Exception as e:
            log.error(f"Failed to download airports: {e}")
            return False
    
    try:
        with open(cache_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ident = row.get('ident') or row.get('gps_code')
                if ident:
                    try:
                        AIRPORT_COORDS[ident] = (
                            float(row['latitude_deg']),
                            float(row['longitude_deg'])
                        )
                    except:
                        pass
        
        log.info(f"Loaded {len(AIRPORT_COORDS)} airport coordinates")
        return True
        
    except Exception as e:
        log.error(f"Failed to load airports: {e}")
        return False


def find_latest_model_run(model_name):
    """Find latest model run directory"""
    if model_name == 'HRRR':
        root = HRRR_ROOT
        today = datetime.utcnow().strftime('%Y%m%d')
        model_dir = os.path.join(root, f'hrrr.{today}')
        
        if not os.path.exists(model_dir):
            yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y%m%d')
            model_dir = os.path.join(root, f'hrrr.{yesterday}')
        
        # Find latest cycle
        cycles = [d for d in os.listdir(model_dir) if d.endswith('z')]
        if not cycles:
            return None, None
        
        latest_cycle = sorted(cycles)[-1]
        cycle_hour = int(latest_cycle[:-1])
        model_run = datetime.strptime(f"{today}{cycle_hour:02d}", '%Y%m%d%H')
        
        return os.path.join(model_dir, latest_cycle), model_run
        
    else:  # GFS
        root = GFS_ROOT
        today = datetime.utcnow().strftime('%Y%m%d')
        model_dir = os.path.join(root, f'gfs.{today}')
        
        if not os.path.exists(model_dir):
            yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y%m%d')
            model_dir = os.path.join(root, f'gfs.{yesterday}')
        
        # Find latest cycle
        cycles = [d for d in os.listdir(model_dir) if d.endswith('z')]
        if not cycles:
            return None, None
        
        latest_cycle = sorted(cycles)[-1]
        cycle_hour = int(latest_cycle[:-1])
        model_run = datetime.strptime(f"{today}{cycle_hour:02d}", '%Y%m%d%H')
        
        return os.path.join(model_dir, latest_cycle), model_run


def extract_winds_from_grib(grib_file, airports):
    """
    Extract wind data for airports from GRIB file
    Returns dict: {station_id: (wind_dir, wind_speed, wind_gust)}
    """
    winds = {}
    
    try:
        grbs = pygrib.open(grib_file)
        
        # Get U and V wind components at 10m
        u_wind = grbs.select(name='10 metre U wind component')[0]
        v_wind = grbs.select(name='10 metre V wind component')[0]
        
        # Get wind gust if available
        try:
            gust = grbs.select(name='Wind speed (gust)')[0]
        except:
            gust = None
        
        # Sample winds at each airport
        for station_id, (lat, lon) in airports.items():
            try:
                u = u_wind.data(lat1=lat, lat2=lat, lon1=lon, lon2=lon)[0][0][0]
                v = v_wind.data(lat1=lat, lat2=lat, lon1=lon, lon2=lon)[0][0][0]
                
                # Calculate wind speed and direction
                import math
                speed_ms = math.sqrt(u**2 + v**2)
                speed_kts = speed_ms * 1.94384  # m/s to knots
                
                direction = (math.degrees(math.atan2(u, v)) + 180) % 360
                
                # Get gust if available
                gust_kts = None
                if gust:
                    try:
                        gust_ms = gust.data(lat1=lat, lat2=lat, lon1=lon, lon2=lon)[0][0][0]
                        gust_kts = gust_ms * 1.94384
                    except:
                        pass
                
                winds[station_id] = (int(direction), round(speed_kts, 1), 
                                    round(gust_kts, 1) if gust_kts else None)
                
            except Exception as e:
                log.debug(f"Failed to extract wind for {station_id}: {e}")
                continue
        
        grbs.close()
        
    except Exception as e:
        log.error(f"Failed to process GRIB file {grib_file}: {e}")
    
    return winds


def calculate_wind_category(wind_speed, gust):
    """Calculate wind category"""
    max_wind = gust if gust else wind_speed
    
    if max_wind >= 25:
        return 'EXTREME'
    elif max_wind >= 15:
        return 'CAUTION'
    else:
        return 'NORMAL'


def ingest_model_forecasts(model_name, forecast_hours=12):
    """Ingest model wind forecasts into database"""
    
    # Load airports
    if not load_airport_coordinates():
        return False
    
    # Find latest model run
    cycle_dir, model_run = find_latest_model_run(model_name)
    
    if not cycle_dir or not model_run:
        log.error(f"Could not find latest {model_name} run")
        return False
    
    log.info(f"Using {model_name} run: {model_run.strftime('%Y-%m-%d %H:00 UTC')}")
    
    # Get database connection
    conn = get_connection()
    cur = conn.cursor()
    
    # Delete old forecasts for this model run (in case of re-run)
    cur.execute("""
        DELETE FROM observations.model_wind_forecasts
        WHERE model_name = %s AND model_run = %s
    """, (model_name, model_run))
    
    total_inserted = 0
    
    # Process each forecast hour
    for fhr in range(0, forecast_hours + 1):
        # Find GRIB file for this forecast hour
        if model_name == 'HRRR':
            grib_pattern = f"hrrr.t{model_run.hour:02d}z.wrfsfcf{fhr:02d}.grib2"
        else:
            grib_pattern = f"gfs.t{model_run.hour:02d}z.pgrb2.0p25.f{fhr:03d}"
        
        grib_file = os.path.join(cycle_dir, grib_pattern)
        
        if not os.path.exists(grib_file):
            log.warning(f"GRIB file not found: {grib_file}")
            continue
        
        log.info(f"Processing forecast hour {fhr}: {grib_file}")
        
        # Extract winds for all airports
        winds = extract_winds_from_grib(grib_file, AIRPORT_COORDS)
        
        if not winds:
            log.warning(f"No winds extracted from {grib_file}")
            continue
        
        # Valid time for this forecast
        valid_time = model_run + timedelta(hours=fhr)
        
        # Insert into database
        for station_id, (wind_dir, wind_speed, gust) in winds.items():
            lat, lon = AIRPORT_COORDS[station_id]
            category = calculate_wind_category(wind_speed, gust)
            
            try:
                cur.execute("""
                    INSERT INTO observations.model_wind_forecasts (
                        station_id, location, model_name, model_run,
                        valid_time, forecast_hour,
                        wind_dir, wind_speed_kts, wind_gust_kts,
                        wind_category
                    ) VALUES (
                        %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (station_id, model_run, valid_time) DO UPDATE SET
                        wind_dir = EXCLUDED.wind_dir,
                        wind_speed_kts = EXCLUDED.wind_speed_kts,
                        wind_gust_kts = EXCLUDED.wind_gust_kts,
                        wind_category = EXCLUDED.wind_category
                """, (station_id, lon, lat, model_name, model_run,
                      valid_time, fhr, wind_dir, wind_speed, gust, category))
                
                total_inserted += 1
                
            except Exception as e:
                log.error(f"Failed to insert {station_id}: {e}")
                continue
        
        conn.commit()
        log.info(f"  Inserted {len(winds)} forecasts for hour {fhr}")
    
    # Calculate maximum winds for each airport in this forecast period
    log.info("Calculating maximum winds...")
    cur.execute("""
        UPDATE observations.model_wind_forecasts mwf
        SET 
            max_wind_kts = subq.max_wind,
            max_gust_kts = subq.max_gust,
            max_wind_time = subq.max_time
        FROM (
            SELECT 
                station_id,
                model_run,
                MAX(wind_speed_kts) as max_wind,
                MAX(wind_gust_kts) as max_gust,
                MAX(valid_time) FILTER (WHERE wind_speed_kts = MAX(wind_speed_kts)) as max_time
            FROM observations.model_wind_forecasts
            WHERE model_run = %s
            GROUP BY station_id, model_run
        ) subq
        WHERE mwf.station_id = subq.station_id
          AND mwf.model_run = subq.model_run
    """, (model_run,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    log.info(f"Successfully ingested {total_inserted} wind forecasts from {model_name}")
    return True


def main():
    parser = argparse.ArgumentParser(description='Ingest model wind forecasts')
    parser.add_argument('--model', choices=['HRRR', 'GFS'], default='HRRR',
                       help='Model to ingest')
    parser.add_argument('--hours', type=int, default=12,
                       help='Number of forecast hours to ingest (default: 12)')
    
    args = parser.parse_args()
    
    success = ingest_model_forecasts(args.model, args.hours)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()


"""
DEPLOYMENT
==========

1. Deploy script:
   cp ingest_model_winds.py /var/www/cap_winds_app/scripts/
   chmod +x /var/www/cap_winds_app/scripts/ingest_model_winds.py

2. Install dependencies:
   pip install pygrib psycopg2-binary --break-system-packages

3. Test:
   sudo -u www-data /var/www/cap_winds_app/scripts/ingest_model_winds.py --model HRRR

4. Set up cron:
   sudo crontab -u www-data -e
   
   # HRRR updates every hour
   15 * * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py --model HRRR >> /var/log/model_winds.log 2>&1
   
   # GFS updates every 6 hours
   25 */6 * * * /var/www/cap_winds_app/scripts/ingest_model_winds.py --model GFS >> /var/log/model_winds.log 2>&1

5. Check results:
   sudo -u postgres psql -d avwx_data -c "SELECT model_name, model_run, COUNT(*) FROM observations.model_wind_forecasts GROUP BY model_name, model_run ORDER BY model_run DESC;"
"""
