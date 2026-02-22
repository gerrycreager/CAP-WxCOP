#!/var/www/cap_winds_app/venv/bin/python3
"""
FAA TFR Selenium Scraper - Headless Browser Approach
Loads the TFR web app and extracts dynamically generated data
"""
import sys
import logging
import base64
import xml.etree.ElementTree as ET
import time
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def get_tfr_data_with_selenium():
    """Use headless Chrome to load TFR page and extract download data"""
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        log.info("Loading TFR webpage...")
        
        # Load the main TFR page
        driver.get("https://tfr.faa.gov/tfr3/")
        
        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        # Try to find and click export/download link
        log.info("Looking for XML export functionality...")
        
        # Look for export links or buttons
        export_elements = driver.find_elements(By.XPATH, "//a[contains(@href,'export') or contains(text(),'XML') or contains(text(),'Export')]")
        
        for element in export_elements:
            log.info(f"Found potential export element: {element.get_attribute('href')} - {element.text}")
        
        # Try clicking the XML export if found
        xml_link = None
        try:
            xml_link = driver.find_element(By.XPATH, "//a[contains(@href,'export/xml')]")
            log.info("Found XML export link, clicking...")
            xml_link.click()
            time.sleep(3)  # Wait for download/response
        except:
            log.info("No direct XML export link found")
        
        # Check if there's any base64 data in the page source now
        page_source = driver.page_source
        
        # Look for base64 data patterns
        import re
        base64_patterns = [
            r'data:application/octet-stream[^"]*base64,([A-Za-z0-9+/=]+)',
            r'data:[^"]*base64,([A-Za-z0-9+/=]+)',
            r'base64,([A-Za-z0-9+/=]{100,})'  # Any long base64 string
        ]
        
        for pattern in base64_patterns:
            matches = re.findall(pattern, page_source)
            if matches:
                log.info(f"Found {len(matches)} base64 data matches")
                return matches[0]  # Return first match
                
        log.warning("No base64 TFR data found in page")
        return None
        
    except Exception as e:
        log.error(f"Selenium error: {e}")
        return None
    finally:
        if driver:
            driver.quit()

def parse_and_ingest_tfrs(base64_data):
    """Parse base64 XML data and ingest TFRs"""
    try:
        # Decode base64
        decoded_bytes = base64.b64decode(base64_data)
        xml_content = decoded_bytes.decode('utf-8')
        
        log.info(f"Decoded XML length: {len(xml_content)}")
        
        # Parse XML
        root = ET.fromstring(xml_content)
        tfrs = []
        
        for tfr in root.findall('TFR'):
            tfr_data = {
                'notam_id': tfr.find('NOTAMID').text if tfr.find('NOTAMID') is not None else None,
                'facility': tfr.find('Facility').text if tfr.find('Facility') is not None else None,
                'state': tfr.find('State').text if tfr.find('State') is not None else None,
                'type': tfr.find('Type').text if tfr.find('Type') is not None else None,
                'description': tfr.find('Description').text if tfr.find('Description') is not None else None,
            }
            
            if tfr_data['notam_id']:
                tfrs.append(tfr_data)
        
        log.info(f"Parsed {len(tfrs)} TFRs from XML")
        
        if not tfrs:
            return
            
        # Insert into database
        conn = get_connection()
        cur = conn.cursor()
        
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
        
    except Exception as e:
        log.error(f"Failed to parse/ingest TFRs: {e}")

def main():
    log.info("Starting TFR ingestion with Selenium...")
    base64_data = get_tfr_data_with_selenium()
    
    if base64_data:
        parse_and_ingest_tfrs(base64_data)
    else:
        log.error("Failed to extract TFR data")

if __name__ == '__main__':
    main()
