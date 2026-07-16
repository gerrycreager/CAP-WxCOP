#!/usr/bin/env python3
"""
Corrected FAA/ESRI National Defense Airspace Ingestion System
Updated to match actual ESRI data structure and field names
"""

import requests
import json
import logging
import psycopg2
from datetime import datetime
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def get_connection():
    """Database connection using existing parameters"""
    return psycopg2.connect(
        host="192.168.0.60",
        database="avwx_data",
        user="avwx_user"
    )

def download_nda_data():
    """Download National Defense Airspace data from ESRI/FAA"""
    try:
        url = 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0/query'
        
        params = {
            'where': '1=1',  # Get all records
            'outFields': '*',  # All fields
            'f': 'geojson',  # Standard GeoJSON -- ST_GeomFromGeoJSON() can't parse
                             # ESRI's native 'json' format (rings-based, no "type"
                             # field), which is what was silently breaking every
                             # geometry insert below.
            'returnGeometry': 'true'
        }
        
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        
        data = response.json()
        features = data.get('features', [])
        log.info(f"Downloaded {len(features)} NDA features")
        
        return features
        
    except Exception as e:
        log.error(f"Failed to download NDA data: {e}")
        return None

def parse_nda_features(features):
    """Parse ESRI NDA features into database-compatible format"""
    nda_areas = []
    
    try:
        for feature in features:
            # GeoJSON Feature format: fields under 'properties', not ESRI's
            # native 'attributes' -- same field names/casing either way.
            attrs = feature.get('properties', {})
            geometry = feature.get('geometry', {})
            
            # Map ESRI fields to our database structure
            nda_data = {
                'global_id': attrs.get('GLOBAL_ID'),  # Use as unique identifier
                'name': attrs.get('NAME'),
                'type_code': attrs.get('TYPE_CODE', 'UNKNOWN'),
                'local_type': attrs.get('LOCAL_TYPE'),
                'city': attrs.get('CITY'),
                'state': attrs.get('STATE'),
                'country': attrs.get('COUNTRY', 'US'),
                'wkhr_code': attrs.get('WKHR_CODE'),
                'wkhr_rmk': attrs.get('WKHR_RMK'),
                'geometry': json.dumps(geometry) if geometry else None,
                'raw_data': json.dumps(attrs)
            }
            
            # Only process if we have essential data
            if nda_data['global_id'] and nda_data['name']:
                nda_areas.append(nda_data)
                
        log.info(f"Parsed {len(nda_areas)} valid NDA records")
        return nda_areas
        
    except Exception as e:
        log.error(f"Failed to parse NDA features: {e}")
        return []

def create_nda_table():
    """Create National Defense Airspace table if it doesn't exist"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Create NDA table with proper structure
        cur.execute("""
            CREATE TABLE IF NOT EXISTS observations.national_defense_airspace (
                id SERIAL PRIMARY KEY,
                global_id VARCHAR(50) UNIQUE NOT NULL,
                name TEXT,
                type_code VARCHAR(20),
                local_type VARCHAR(50),
                city VARCHAR(100),
                state VARCHAR(10),
                country VARCHAR(50),
                wkhr_code VARCHAR(20),
                wkhr_rmk TEXT,
                geometry GEOMETRY(GEOMETRY, 4326),
                active BOOLEAN DEFAULT TRUE,
                last_updated TIMESTAMP DEFAULT NOW(),
                raw_data JSONB
            )
        """)
        
        # Indexes (idx_nda_geometry/state/type) are one-time schema setup,
        # already created and owned by postgres -- avwx_user isn't the table
        # owner, so CREATE INDEX IF NOT EXISTS still checks ownership before
        # the existence check and fails every run even though the indexes
        # already exist. Not runtime ingest concerns; don't recreate here.

        conn.commit()
        cur.close()
        conn.close()
        
        log.info("NDA table structure verified/created")
        
    except Exception as e:
        log.error(f"Failed to create NDA table: {e}")

def ingest_nda_data(nda_areas):
    """Ingest NDA data into PostGIS database"""
    if not nda_areas:
        log.warning("No NDA data to ingest")
        return 0
        
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Deactivate existing areas
        cur.execute("UPDATE observations.national_defense_airspace SET active = FALSE")
        log.info("Deactivated existing NDA areas")
        
        # Insert/update new areas
        inserted = 0
        for nda in nda_areas:
            try:
                if nda['geometry']:
                    cur.execute("""
                        INSERT INTO observations.national_defense_airspace
                        (global_id, name, type_code, local_type, city, state, country,
                         wkhr_code, wkhr_rmk, geometry, active, raw_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, ST_GeomFromGeoJSON(%s), TRUE, %s)
                        ON CONFLICT (global_id) DO UPDATE SET
                            active = TRUE,
                            name = EXCLUDED.name,
                            type_code = EXCLUDED.type_code,
                            local_type = EXCLUDED.local_type,
                            city = EXCLUDED.city,
                            state = EXCLUDED.state,
                            wkhr_code = EXCLUDED.wkhr_code,
                            wkhr_rmk = EXCLUDED.wkhr_rmk,
                            geometry = EXCLUDED.geometry,
                            last_updated = NOW(),
                            raw_data = EXCLUDED.raw_data
                    """, (
                        nda['global_id'], nda['name'], nda['type_code'],
                        nda['local_type'], nda['city'], nda['state'], nda['country'],
                        nda['wkhr_code'], nda['wkhr_rmk'], nda['geometry'], nda['raw_data']
                    ))
                else:
                    # Insert without geometry
                    cur.execute("""
                        INSERT INTO observations.national_defense_airspace 
                        (global_id, name, type_code, local_type, city, state, country, 
                         wkhr_code, wkhr_rmk, active, raw_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                        ON CONFLICT (global_id) DO UPDATE SET
                            active = TRUE,
                            name = EXCLUDED.name,
                            type_code = EXCLUDED.type_code,
                            local_type = EXCLUDED.local_type,
                            city = EXCLUDED.city,
                            state = EXCLUDED.state,
                            wkhr_code = EXCLUDED.wkhr_code,
                            wkhr_rmk = EXCLUDED.wkhr_rmk,
                            last_updated = NOW(),
                            raw_data = EXCLUDED.raw_data
                    """, (
                        nda['global_id'], nda['name'], nda['type_code'],
                        nda['local_type'], nda['city'], nda['state'], nda['country'],
                        nda['wkhr_code'], nda['wkhr_rmk'], nda['raw_data']
                    ))
                
                inserted += 1
                
            except Exception as e:
                log.error(f"Failed to insert NDA {nda['global_id']}: {e}")
                
        conn.commit()
        cur.close()
        conn.close()
        
        log.info(f"Successfully ingested {inserted} NDA areas")
        return inserted
        
    except Exception as e:
        log.error(f"Database ingestion failed: {e}")
        return 0

def main():
    """Main NDA ingestion process"""
    log.info("Starting National Defense Airspace ingestion from ESRI/FAA...")
    
    # Ensure database schema is ready
    create_nda_table()
    
    # Download and process NDA data
    features = download_nda_data()
    if not features:
        log.error("Failed to download NDA data")
        return 1
        
    nda_areas = parse_nda_features(features)
    if not nda_areas:
        log.error("No valid NDA data parsed")
        return 1
        
    result = ingest_nda_data(nda_areas)
    if result > 0:
        log.info(f"SUCCESS: Ingested {result} National Defense Airspace areas")
        return 0
    else:
        log.error("Failed to ingest NDA data")
        return 1

if __name__ == '__main__':
    sys.exit(main())

