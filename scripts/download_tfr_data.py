#!/var/www/cap_winds_app/venv/bin/python3
"""
TFR Data Downloader - Extracts and decodes base64 TFR data from FAA webpage
"""
import sys
import logging
import requests
import base64
import json
import re
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def download_tfr_data():
    """Download and decode TFR data from FAA webpage"""
    try:
        # Get the FAA TFR export page
        response = requests.get("https://tfr.faa.gov/tfr3/export/xml", timeout=30)
        response.raise_for_status()
        
        html_content = response.text
        log.info(f"Downloaded HTML content: {len(html_content)} characters")
        
        # Look for base64 data patterns in the HTML
        patterns = [
            r'data:application/[^,]*,([A-Za-z0-9+/=]+)',
            r'data:[^,]*base64,([A-Za-z0-9+/=]+)',
            r'base64,([A-Za-z0-9+/=]{500,})',  # Long base64 strings
        ]
        
        base64_data = None
        for pattern in patterns:
            matches = re.findall(pattern, html_content)
            if matches:
                base64_data = matches[0]
                log.info(f"Found base64 data using pattern: {pattern[:30]}...")
                break
                
        if not base64_data:
            # Check if there's any substantial base64-looking data in the HTML
            b64_candidates = re.findall(r'[A-Za-z0-9+/=]{100,}', html_content)
            if b64_candidates:
                # Try the longest candidate
                base64_data = max(b64_candidates, key=len)
                log.info(f"Found potential base64 candidate: {len(base64_data)} chars")
            else:
                log.error("No base64 data found in HTML")
                return None
                
        # Try to decode the base64 data
        try:
            decoded_bytes = base64.b64decode(base64_data)
            
            # Try different encodings
            for encoding in ['utf-8', 'utf-16le', 'utf-16be', 'latin1']:
                try:
                    decoded_text = decoded_bytes.decode(encoding)
                    log.info(f"Successfully decoded with {encoding}: {len(decoded_text)} chars")
                    
                    # Check if it looks like XML or JSON
                    if decoded_text.strip().startswith('<?xml') or decoded_text.strip().startswith('<'):
                        log.info("Detected XML format")
                        return ('xml', decoded_text)
                    elif decoded_text.strip().startswith('[') or decoded_text.strip().startswith('{'):
                        log.info("Detected JSON format")
                        return ('json', decoded_text)
                    else:
                        # Save first 200 chars for inspection
                        log.info(f"Unknown format. First 200 chars: {decoded_text[:200]}")
                        
                except UnicodeDecodeError:
                    continue
                    
        except Exception as e:
            log.error(f"Failed to decode base64: {e}")
            
        return None
        
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None

def save_tfr_data(data_type, content, filename=None):
    """Save TFR data to file"""
    if not filename:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"/var/www/cap_winds_app/scripts/tfr_data_{timestamp}.{data_type}"
    
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        log.info(f"Saved TFR data to: {filename}")
        return filename
    except Exception as e:
        log.error(f"Failed to save data: {e}")
        return None

if __name__ == '__main__':
    log.info("Starting TFR data download...")
    result = download_tfr_data()
    
    if result:
        data_type, content = result
        filename = save_tfr_data(data_type, content)
        if filename:
            print(f"SUCCESS: Downloaded TFR data saved to {filename}")
            print(f"Data type: {data_type}")
            print(f"Content length: {len(content)} characters")
        else:
            print("FAILED: Could not save data")
    else:
        print("FAILED: Could not download/decode TFR data")
