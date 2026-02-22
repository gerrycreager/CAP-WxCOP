#!/var/www/cap_winds_app/venv/bin/python3
"""
FAA TFR XML Ingestion Script - Fixed Version
"""
import sys
import logging
import requests
import xml.etree.ElementTree as ET
import base64
import re
from datetime import datetime
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def fetch_and_decode_tfr_xml():
    """Fetch TFR XML data from the webpage and extract base64 content"""
    try:
        # The XML endpoint returns HTML with base64 data embedded
        response = requests.get("https://tfr.faa.gov/tfr3/export/xml", timeout=30)
        response.raise_for_status()
        
        # Look for base64 data in the HTML response
        html_content = response.text
        
        # Find base64 data pattern in the HTML
        base64_pattern = r'data:application/octet-stream;charset=utf-16le;base64,([A-Za-z0-9+/=]+)'
        match = re.search(base64_pattern, html_content)
        
        if match:
            base64_data = match.group(1)
            log.info("Found base64 encoded TFR data in HTML")
            
            # Decode base64
            decoded_bytes = base64.b64decode(base64_data)
            # Decode as UTF-16LE (as specified in the data URL)
            xml_content = decoded_bytes.decode('utf-16le')
            
            log.info(f"Successfully decoded {len(xml_content)} characters of XML")
            return xml_content
        else:
            log.error("No base64 TFR data found in HTML response")
            return None
            
    except Exception as e:
        log.error(f"Failed to fetch/decode TFR XML: {e}")
        return None

def parse_tfr_xml(xml_content):
    """Parse TFR XML and extract TFR data"""
    try:
        # Clean up XML content
        xml_content = xml_content.strip()
        
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
            
            if tfr_data['notam_id']:
                tfrs.append(tfr_data)
                
        log.info(f"Parsed {len(tfrs)} TFRs from XML")
        return tfrs
        
    except ET.ParseError as e:
        log.error(f"Failed to parse TFR XML: {e}")
        # Print first 200 chars of XML for debugging
        log.error(f"XML content start: {xml_content[:200]}...")
        return []

def ingest_tfrs():
    """Main ingestion function"""
    log.info("Starting TFR XML ingestion...")
    
    xml_content = fetch_and_decode_tfr_xml()
    if not xml_content:
        return
        
    tfrs = parse_tfr_xml(xml_content)
    if not tfrs:
        log.warning("No TFRs found in XML")
        return
        
    conn = get_connection()
    cur = conn.cursor()
    
    # Deactivate existing TFRs
    cur.execute("UPDATE observations.tfr SET active = FALSE")
    
    inserted = 0
    for tfr in tfrs:
        try:
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
