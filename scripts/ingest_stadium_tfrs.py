#!/usr/bin/env python3
"""
Stadium TFR Ingestion Script
Handles point-based circular TFR areas around stadiums
Perfect for actual temporary flight restrictions
"""

import requests
import json
import logging
import psycopg2
from datetime import datetime
import sys
import re

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def get_connection():
    """Database connection using existing parameters"""
    return psycopg2.connect(
        host="localhost",
        database="avwx_data",
        user="avwx_user"
    )

def parse_lat_lon(lat_str, lon_str):
    """Parse FAA coordinate format like '40-26-49.38N' to decimal degrees"""
    try:
        # Parse latitude (e.g., '40-26-49.38N')
        lat_match = re.match(r'(\d+)-(\d+)-(\d+\.?\d*)([NS])', lat_str)
        if lat_match:
            deg, min_val, sec, direction = lat_match.groups()
            lat_decimal = float(deg) + float(min_val)/60 + float(sec)/3600
            if direction == 'S':
                lat_decimal = -lat_decimal
        else:
            return None, None
            
        # Parse longitude (e.g., '080-00-22.17W') 
        lon_match = re.match(r'(\d+)-(\d+)-(\d+\.?\d*)([EW])', lon_str)
        if lon_match:
            deg, min_val, sec, direction = lon_match.groups()
            lon_decimal = float(deg) + float(min_val)/60 + float(sec)/3600
            if direction == 'W':
                lon_decimal = -lon_decimal
        else:
            return None, None
            
        return lat_decimal, lon_decimal
        
    except Exception as e:
        log.error(f"Error parsing coordinates {lat_str}, {lon_str}: {e}")
        return None, None

def download_stadium_tfrs():
    """Download Stadium TFR data from ESRI/FAA"""
    try:
        url = 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Stadiums/FeatureServer/0/query'
        
        params = {
            'where': '1=1',  # Get all records
            'outFields': '*',  # All fields
            'f': 'json',     # JSON format
            'returnGeometry': 'true'
        }
        
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        
        data = response.json()
        features = data.get('features', [])
        log.info(f"Downloaded {len(features)} Stadium TFR features")
        
        return features
        
    except Exception as e:
        log.error(f"Failed to download Stadium TFR data: {e}")
        return None

def parse_stadium_features(features):
    """Parse Stadium TFR features into database-compatible format"""
    stadium_tfrs = []
    
    try:
        for feature in features:
            attrs = feature.get('attributes', {})
            esri_geometry = feature.get('geometry', {})
            
            # Parse coordinates from string format
            lat_str = attrs.get('LATITUDE', '')
            lon_str = attrs.get('LONGITUDE', '')
            lat_decimal, lon_decimal = parse_lat_lon(lat_str, lon_str)
            
            # Create point geometry if coordinates are valid
            point_geometry = None
            if lat_decimal is not None and lon_decimal is not None:
                point_geometry = {
                    "type": "Point",
                    "coordinates": [lon_decimal, lat_decimal]
                }
            
            # Map ESRI fields to our database structure
            stadium_data = {
                'global_id': attrs.get('GLOBAL_ID'),
                'name': attrs.get('NAME'),
                'city': attrs.get('CITY'),
                'state': attrs.get('STATE'),
                'status_code': attrs.get('STATUS_CODE'),
                'opening_on': attrs.get('OPENING_ON'),
                'latitude': lat_decimal,
                'longitude': lon_decimal,
                'lat_str': lat_str,
                'lon_str': lon_str,
                'point_geometry': point_geometry,
                'raw_data': json.dumps(attrs)
            }
            
            # Only process if we have essential data
            if stadium_data['global_id'] and stadium_data['name']:
                stadium_tfrs.append(stadium_data)
                log.debug(f"Parsed Stadium TFR: {stadium_data['name']} ({stadium_data['city']}, {stadium_data['state']})")
                
        log.info(f"Parsed {len(stadium_tfrs)} valid Stadium TFR records")
        return stadium_tfrs
        
    except Exception as e:
        log.error(f"Failed to parse Stadium TFR features: {e}")
        return []

def create_stadium_tfr_table():
    """Create Stadium TFR table if it doesn't exist"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Create Stadium TFR table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS observations.stadium_tfrs (
                id SERIAL PRIMARY KEY,
                global_id VARCHAR(50) UNIQUE NOT NULL,
                name TEXT,
                city VARCHAR(100),
                state VARCHAR(10),
                status_code VARCHAR(20),
                opening_on TIMESTAMP,
                latitude DECIMAL(10,7),
                longitude DECIMAL(10,7),
                lat_str VARCHAR(20),
                lon_str VARCHAR(20),
                geometry GEOMETRY(POINT, 4326),
                buffer_3nm GEOMETRY(POLYGON, 4326),
                buffer_5nm GEOMETRY(POLYGON, 4326),
                active BOOLEAN DEFAULT TRUE,
                last_updated TIMESTAMP DEFAULT NOW(),
                raw_data JSONB
            )
        """)
        
        # Create indexes
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_stadium_tfr_geometry 
            ON observations.stadium_tfrs USING GIST (geometry)
        """)
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_stadium_tfr_buffer_3nm 
            ON observations.stadium_tfrs USING GIST (buffer_3nm)
        """)
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_stadium_tfr_state 
            ON observations.stadium_tfrs (state)
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        
        log.info("Stadium TFR table structure verified/created")
        
    except Exception as e:
        log.error(f"Failed to create Stadium TFR table: {e}")

def ingest_stadium_tfrs(stadium_tfrs):
    """Ingest Stadium TFR data into PostGIS database with circular buffers"""
    if not stadium_tfrs:
        log.warning("No Stadium TFR data to ingest")
        return 0
        
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Deactivate existing TFRs
        cur.execute("UPDATE observations.stadium_tfrs SET active = FALSE")
        log.info("Deactivated existing Stadium TFRs")
        
        # Insert/update new TFRs
        inserted = 0
        for tfr in stadium_tfrs:
            try:
                if tfr['point_geometry'] and tfr['latitude'] is not None and tfr['longitude'] is not None:
                    # Insert with point geometry and circular buffers
                    cur.execute("""
                        INSERT INTO observations.stadium_tfrs 
                        (global_id, name, city, state, status_code, opening_on,
                         latitude, longitude, lat_str, lon_str, geometry, 
                         buffer_3nm, buffer_5nm, active, raw_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
                                ST_GeomFromGeoJSON(%s),
                                ST_Buffer(ST_GeomFromGeoJSON(%s)::geography, 5556)::geometry,
                                ST_Buffer(ST_GeomFromGeoJSON(%s)::geography, 9260)::geometry,
                                TRUE, %s)
                        ON CONFLICT (global_id) DO UPDATE SET
                            active = TRUE,
                            name = EXCLUDED.name,
                            city = EXCLUDED.city,
                            state = EXCLUDED.state,
                            status_code = EXCLUDED.status_code,
                            opening_on = EXCLUDED.opening_on,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            geometry = EXCLUDED.geometry,
                            buffer_3nm = EXCLUDED.buffer_3nm,
                            buffer_5nm = EXCLUDED.buffer_5nm,
                            last_updated = NOW(),
                            raw_data = EXCLUDED.raw_data
                    """, (
                        tfr['global_id'], tfr['name'], tfr['city'], tfr['state'],
                        tfr['status_code'], tfr['opening_on'], tfr['latitude'], tfr['longitude'],
                        tfr['lat_str'], tfr['lon_str'],
                        json.dumps(tfr['point_geometry']),  # Point geometry
                        json.dumps(tfr['point_geometry']),  # For 3NM buffer
                        json.dumps(tfr['point_geometry']),  # For 5NM buffer  
                        tfr['raw_data']
                    ))
                else:
                    # Insert without geometry
                    cur.execute("""
                        INSERT INTO observations.stadium_tfrs 
                        (global_id, name, city, state, status_code, opening_on,
                         lat_str, lon_str, active, raw_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                        ON CONFLICT (global_id) DO UPDATE SET
                            active = TRUE,
                            name = EXCLUDED.name,
                            city = EXCLUDED.city,
                            state = EXCLUDED.state,
                            status_code = EXCLUDED.status_code,
                            opening_on = EXCLUDED.opening_on,
                            last_updated = NOW(),
                            raw_data = EXCLUDED.raw_data
                    """, (
                        tfr['global_id'], tfr['name'], tfr['city'], tfr['state'],
                        tfr['status_code'], tfr['opening_on'], 
                        tfr['lat_str'], tfr['lon_str'], tfr['raw_data']
                    ))
                
                inserted += 1
                log.debug(f"Inserted Stadium TFR: {tfr['name']}")
                
            except Exception as e:
                log.error(f"Failed to insert Stadium TFR {tfr['global_id']}: {e}")
                
        conn.commit()
        cur.close()
        conn.close()
        
        log.info(f"Successfully ingested {inserted} Stadium TFRs")
        return inserted
        
    except Exception as e:
        log.error(f"Database ingestion failed: {e}")
        return 0

def main():
    """Main Stadium TFR ingestion process"""
    log.info("Starting Stadium TFR ingestion from ESRI/FAA...")
    
    # Ensure database schema is ready
    create_stadium_tfr_table()
    
    # Download and process Stadium TFR data
    features = download_stadium_tfrs()
    if not features:
        log.error("Failed to download Stadium TFR data")
        return 1
        
    stadium_tfrs = parse_stadium_features(features)
    if not stadium_tfrs:
        log.error("No valid Stadium TFR data parsed")
        return 1
        
    result = ingest_stadium_tfrs(stadium_tfrs)
    if result > 0:
        log.info(f"SUCCESS: Ingested {result} Stadium TFRs")
        
        # Show summary
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT state, COUNT(*) 
                FROM observations.stadium_tfrs 
                WHERE active = TRUE 
                GROUP BY state 
                ORDER BY COUNT(*) DESC
            """)
            results = cur.fetchall()
            cur.close()
            conn.close()
            
            log.info("Stadium TFRs by State:")
            for state, count in results:
                log.info(f"  {state}: {count} stadiums")
                
        except Exception as e:
            log.error(f"Error generating summary: {e}")
            
        return 0
    else:
        log.error("Failed to ingest Stadium TFR data")
        return 1

if __name__ == '__main__':
    sys.exit(main())

