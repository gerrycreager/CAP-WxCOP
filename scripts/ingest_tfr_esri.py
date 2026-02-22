#!/usr/bin/env python3
"""
Automated FAA/ESRI TFR Data Ingestion System
Downloads TFR data from official ESRI/FAA APIs and stores in PostGIS
Supports multiple formats: GeoJSON, WFS, REST API
"""

import requests
import json
import logging
import psycopg2
from datetime import datetime
import os
import sys

# Configuration
TFR_CONFIG = {
    # ESRI/FAA TFR Data Sources
    'geojson_url': 'https://hub.arcgis.com/api/v3/datasets/{item_id}_0/downloads/data?format=geojson&spatialRefId=4326',
    'rest_api_url': 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0/query',
    'wfs_url': 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0',
    
    # Update intervals
    'update_interval_minutes': 30,
    'retry_count': 3,
    'timeout_seconds': 60
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def get_connection():
    """Database connection using existing parameters"""
    return psycopg2.connect(
        host="localhost",
        database="avwx_data",
        user="avwx_user"
    )

def download_tfr_geojson():
    """Download TFR data in GeoJSON format from ESRI/FAA"""
    try:
        # REST API query for all active TFRs
        params = {
            'where': '1=1',  # Get all records
            'outFields': '*',  # All fields
            'f': 'geojson',   # GeoJSON format
            'returnGeometry': 'true'
        }
        
        response = requests.get(
            TFR_CONFIG['rest_api_url'], 
            params=params, 
            timeout=TFR_CONFIG['timeout_seconds']
        )
        response.raise_for_status()
        
        geojson_data = response.json()
        log.info(f"Downloaded {len(geojson_data.get('features', []))} TFR features")
        
        return geojson_data
        
    except Exception as e:
        log.error(f"Failed to download TFR GeoJSON: {e}")
        return None

def parse_tfr_geojson(geojson_data):
    """Parse GeoJSON TFR data into database-compatible format"""
    tfrs = []
    
    try:
        for feature in geojson_data.get('features', []):
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})
            
            # Extract TFR attributes (adjust field names based on actual ESRI data structure)
            tfr_data = {
                'notam_id': properties.get('NOTAM_ID') or properties.get('notam_id'),
                'facility': properties.get('FACILITY') or properties.get('facility'),
                'state': properties.get('STATE') or properties.get('state'),
                'type': properties.get('TFR_TYPE') or properties.get('type', 'UNKNOWN'),
                'description': properties.get('DESCRIPTIO') or properties.get('description'),
                'effective_start': properties.get('EFF_START') or properties.get('effective_start'),
                'effective_end': properties.get('EFF_END') or properties.get('effective_end'),
                'geometry': json.dumps(geometry) if geometry else None,
                'raw_data': json.dumps(properties)
            }
            
            if tfr_data['notam_id']:  # Only process if we have a NOTAM ID
                tfrs.append(tfr_data)
                
        log.info(f"Parsed {len(tfrs)} valid TFR records")
        return tfrs
        
    except Exception as e:
        log.error(f"Failed to parse TFR GeoJSON: {e}")
        return []

def ingest_tfr_data(tfrs):
    """Ingest TFR data into PostGIS database"""
    if not tfrs:
        log.warning("No TFR data to ingest")
        return 0
        
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Deactivate existing TFRs
        cur.execute("UPDATE observations.tfr SET active = FALSE")
        log.info("Deactivated existing TFRs")
        
        # Insert/update new TFRs
        inserted = 0
        for tfr in tfrs:
            try:
                # Insert with geometry support
                cur.execute("""
                    INSERT INTO observations.tfr 
                    (tfr_number, notam_id, facility, state, type, city, description, 
                     effective_start, effective_end, geometry, active, raw_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 
                            ST_GeomFromGeoJSON(%s), TRUE, %s)
                    ON CONFLICT (notam_id) DO UPDATE SET
                        active = TRUE,
                        facility = EXCLUDED.facility,
                        state = EXCLUDED.state,
                        type = EXCLUDED.type,
                        description = EXCLUDED.description,
                        effective_start = EXCLUDED.effective_start,
                        effective_end = EXCLUDED.effective_end,
                        geometry = EXCLUDED.geometry,
                        raw_data = EXCLUDED.raw_data
                """, (
                    tfr['notam_id'], tfr['notam_id'], tfr['facility'], 
                    tfr['state'], tfr['type'], tfr['state'], 
                    tfr['description'], tfr['effective_start'], tfr['effective_end'],
                    tfr['geometry'], tfr['raw_data']
                ))
                inserted += 1
                
            except Exception as e:
                log.error(f"Failed to insert TFR {tfr['notam_id']}: {e}")
                
        conn.commit()
        cur.close()
        conn.close()
        
        log.info(f"Successfully ingested {inserted} TFRs")
        return inserted
        
    except Exception as e:
        log.error(f"Database ingestion failed: {e}")
        return 0

def update_tfr_geometry_column():
    """Add geometry column if it doesn't exist"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Check if geometry column exists
        cur.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'tfr' AND column_name = 'geometry'
        """)
        
        if not cur.fetchone():
            # Add geometry column
            cur.execute("""
                ALTER TABLE observations.tfr 
                ADD COLUMN geometry GEOMETRY(GEOMETRY, 4326)
            """)
            
            # Add spatial index
            cur.execute("""
                CREATE INDEX idx_tfr_geometry 
                ON observations.tfr USING GIST (geometry)
            """)
            
            conn.commit()
            log.info("Added geometry column and spatial index to TFR table")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        log.error(f"Failed to update TFR table schema: {e}")

def main():
    """Main TFR ingestion process"""
    log.info("Starting automated TFR ingestion from ESRI/FAA...")
    
    # Ensure database schema is ready
    update_tfr_geometry_column()
    
    # Download and process TFR data
    geojson_data = download_tfr_geojson()
    if not geojson_data:
        log.error("Failed to download TFR data")
        return 1
        
    tfrs = parse_tfr_geojson(geojson_data)
    if not tfrs:
        log.error("No valid TFR data parsed")
        return 1
        
    result = ingest_tfr_data(tfrs)
    if result > 0:
        log.info(f"SUCCESS: Ingested {result} TFRs from ESRI/FAA API")
        return 0
    else:
        log.error("Failed to ingest TFR data")
        return 1

if __name__ == '__main__':
    sys.exit(main())
