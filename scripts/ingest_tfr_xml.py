#!/var/www/cap_winds_app/venv/bin/python3
"""
FAA TFR XML Ingestion Script
Fetches current TFRs from FAA XML endpoint and stores in PostGIS
"""
import sys
import logging
import requests
import xml.etree.ElementTree as ET
import base64
from datetime import datetime
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

TFR_XML_URL = "https://tfr.faa.gov/tfr3/export/xml"

def fetch_and_decode_tfr_xml():
    """Fetch TFR XML data and decode if base64 encoded"""
    try:
        response = requests.get(TFR_XML_URL, timeout=30)
        response.raise_for_status()
        
        content = response.text.strip()
        
        # Check if content is base64 encoded (starts with data: prefix)
        if content.startswith('data:'):
            # Extract base64 part after the comma
            base64_data = content.split(',')[1]
            decoded_bytes = base64.b64decode(base64_data)
            xml_content = decoded_bytes.decode('utf-8')
        else:
            xml_content = content
            
        return xml_content
        
    except Exception as e:
        log.error(f"Failed to fetch TFR XML: {e}")
        return None

def parse_tfr_xml(xml_content):
    """Parse TFR XML and extract TFR data"""
    try:
        root = ET.fromstring(xml_content)
        tfrs = []
        
        for tfr in root.findall('TFR'):
            tfr_data = {
                'notam_id': tfr.find('NOTAMID').text if tfr.find('NOTAMID') is not None else None,
                'facility': tfr.find('Facility').text if tfr.find('Facility') is not None else None,
                'state': tfr.find('State').text if tfr.find('State') is not None else None,
                'type': tfr.find('Type').text if tfr.find('Type') is not None else None,
                'description': tfr.find('Description').text if tfr.find('Description') is not None else None,
                'notam_detail': tfr.find('NotamDetail').text if tfr.find('NotamDetail') is not None else None,
                'date': tfr.find('Date').text if tfr.find('Date') is not None else None
            }
            
            if tfr_data['notam_id']:  # Only add if we have essential data
                tfrs.append(tfr_data)
                
        log.info(f"Parsed {len(tfrs)} TFRs from XML")
        return tfrs
        
    except ET.ParseError as e:
        log.error(f"Failed to parse TFR XML: {e}")
        return []

def ingest_tfrs():
    """Main ingestion function"""
    log.info("Starting TFR XML ingestion...")
    
    # Fetch and decode XML
    xml_content = fetch_and_decode_tfr_xml()
    if not xml_content:
        return
        
    # Parse TFRs
    tfrs = parse_tfr_xml(xml_content)
    if not tfrs:
        log.warning("No TFRs found in XML")
        return
        
    # Store in database
    conn = get_connection()
    cur = conn.cursor()
    
    # Deactivate all existing TFRs
    cur.execute("UPDATE observations.tfr SET active = FALSE")
    
    inserted = 0
    for tfr in tfrs:
        try:
            # Simple insertion without geometry for now
            cur.execute("""
                INSERT INTO observations.tfr 
                (tfr_number, notam_id, facility, state, type, city, description, active, raw_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (notam_id) DO UPDATE SET
                    active = TRUE,
                    description = EXCLUDED.description,
                    raw_data = EXCLUDED.raw_data
            """, (
                tfr['notam_id'], tfr['notam_id'], tfr['facility'], 
                tfr['state'], tfr['type'], tfr['state'], 
                tfr['description'], str(tfr)
            ))
            inserted += 1
            
        except Exception as e:
            log.error(f"Failed to insert TFR {tfr['notam_id']}: {e}")
            
    conn.commit()
    cur.close()
    conn.close()
    
    log.info(f"Successfully ingested {inserted} TFRs")

if __name__ == '__main__':
    ingest_tfrs()
