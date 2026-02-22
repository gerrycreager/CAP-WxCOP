#!/var/www/cap_winds_app/venv/bin/python3
"""
FAA TFR (Temporary Flight Restriction) Scraper
Fetches current TFRs from FAA and stores in PostGIS

Usage:
  ./ingest_tfr.py [--force]

Cron (every 30 minutes):
  */30 * * * * /var/www/cap_winds_app/scripts/ingest_tfr.py >> /var/log/tfr_ingest.log 2>&1
"""
import sys
import os
import logging
import requests
import json
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import argparse

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# FAA TFR Sources
TFR_LIST_URL = "https://tfr.faa.gov/tfr2/list.html"
TFR_DETAIL_BASE = "https://tfr.faa.gov/save_pages/detail_"


def parse_tfr_coordinates(coord_string):
    """
    Parse TFR coordinate string into PostGIS geometry
    
    Args:
        coord_string: String like "394142N0843046W" (DDMMSSH DDDMMSSH format)
    
    Returns:
        (lat, lon) tuple or None
    """
    try:
        # Match pattern: DDMMSS[N/S] DDDMMSS[E/W]
        pattern = r'(\d{6})([NS])\s*(\d{7})([EW])'
        match = re.search(pattern, coord_string)
        
        if not match:
            return None
        
        lat_str, lat_dir, lon_str, lon_dir = match.groups()
        
        # Parse latitude
        lat_deg = int(lat_str[0:2])
        lat_min = int(lat_str[2:4])
        lat_sec = int(lat_str[4:6])
        lat = lat_deg + lat_min/60 + lat_sec/3600
        if lat_dir == 'S':
            lat = -lat
        
        # Parse longitude
        lon_deg = int(lon_str[0:3])
        lon_min = int(lon_str[3:5])
        lon_sec = int(lon_str[5:7])
        lon = lon_deg + lon_min/60 + lon_sec/3600
        if lon_dir == 'W':
            lon = -lon
        
        return (lat, lon)
        
    except Exception as e:
        log.debug(f"Failed to parse coordinates: {coord_string} - {e}")
        return None


def parse_tfr_radius(radius_string):
    """
    Parse radius string like "5 NM" or "10 NM RADIUS"
    Returns radius in nautical miles
    """
    try:
        match = re.search(r'(\d+\.?\d*)\s*(?:NM|NAUTICAL\s*MILES?)', radius_string, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None
    except:
        return None


def create_circle_polygon(lat, lon, radius_nm, points=32):
    """
    Create a circular polygon for TFR
    
    Args:
        lat, lon: Center coordinates
        radius_nm: Radius in nautical miles
        points: Number of points in polygon
    
    Returns:
        WKT polygon string
    """
    import math
    
    # Convert NM to degrees (approximate)
    radius_deg = radius_nm / 60.0
    
    coords = []
    for i in range(points + 1):
        angle = 2 * math.pi * i / points
        point_lat = lat + radius_deg * math.sin(angle)
        point_lon = lon + radius_deg * math.cos(angle) / math.cos(math.radians(lat))
        coords.append(f"{point_lon} {point_lat}")
    
    return f"POLYGON(({', '.join(coords)}))"


def fetch_tfr_list():
    """
    Fetch list of active TFRs from FAA
    Returns list of TFR numbers
    """
    try:
        log.info("Fetching TFR list from FAA...")
        response = requests.get(TFR_LIST_URL, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all TFR links
        tfr_numbers = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            # Look for links like "detail_1_1234.html"
            match = re.search(r'detail_(\d+_\d+)\.html', href)
            if match:
                tfr_numbers.append(match.group(1))
        
        log.info(f"Found {len(tfr_numbers)} active TFRs")
        return tfr_numbers
        
    except Exception as e:
        log.error(f"Failed to fetch TFR list: {e}")
        return []


def parse_tfr_detail(tfr_number):
    """
    Fetch and parse detailed TFR information
    
    Returns dict with TFR data or None
    """
    try:
        url = f"{TFR_DETAIL_BASE}{tfr_number}.html"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        
        tfr_data = {
            'tfr_number': tfr_number.replace('_', '/'),
            'raw_text': text[:5000],  # Limit size
            'raw_data': {}
        }
        
        # Extract NOTAM ID
        notam_match = re.search(r'!FDC\s+(\S+)', text)
        if notam_match:
            tfr_data['notam_id'] = notam_match.group(1)
        
        # Extract effective times
        # Look for patterns like "2023-01-15/1800Z" or "FROM 231800Z TO 241200Z"
        time_match = re.search(r'FROM\s+(\d{6})Z?\s+TO\s+(\d{6})Z?', text)
        if not time_match:
            time_match = re.search(r'(\d{4}-\d{2}-\d{2}/\d{4})Z?\s+(?:TO|UNTIL)\s+(\d{4}-\d{2}-\d{2}/\d{4})Z?', text)
        
        if time_match:
            try:
                # Parse start/end times (this is simplified - actual parsing is complex)
                start_str = time_match.group(1)
                end_str = time_match.group(2)
                
                # For now, use a simple placeholder
                # Real implementation would need proper date parsing
                tfr_data['effective_start'] = datetime.utcnow()
                tfr_data['effective_end'] = datetime.utcnow() + timedelta(hours=24)
            except:
                return None
        else:
            return None
        
        # Extract location
        facility_match = re.search(r'FACILITY\s+(\S+)', text)
        if facility_match:
            tfr_data['facility'] = facility_match.group(1)
        
        city_state_match = re.search(r'([A-Z\s]+),\s+([A-Z]{2})', text)
        if city_state_match:
            tfr_data['city'] = city_state_match.group(1).strip()
            tfr_data['state'] = city_state_match.group(2)
        
        # Extract type/reason
        if 'SPACE OPERATIONS' in text.upper():
            tfr_data['type'] = 'Space Operations'
        elif 'VIP' in text.upper():
            tfr_data['type'] = 'VIP Movement'
        elif 'STADIUM' in text.upper() or 'SPORTING EVENT' in text.upper():
            tfr_data['type'] = 'Special Event'
        elif 'DISASTER' in text.upper() or 'EMERGENCY' in text.upper():
            tfr_data['type'] = 'Emergency'
        else:
            tfr_data['type'] = 'Other'
        
        # Extract coordinates and radius
        # Look for pattern like "5NM RADIUS OF 394142N0843046W"
        coord_pattern = r'(\d+\.?\d*)\s*NM\s+(?:RADIUS\s+)?(?:OF\s+)?(\d{6}[NS]\d{7}[EW])'
        coord_match = re.search(coord_pattern, text)
        
        if coord_match:
            radius_nm = float(coord_match.group(1))
            coord_str = coord_match.group(2)
            coords = parse_tfr_coordinates(coord_str)
            
            if coords:
                lat, lon = coords
                tfr_data['geometry_wkt'] = create_circle_polygon(lat, lon, radius_nm)
                tfr_data['raw_data']['center_lat'] = lat
                tfr_data['raw_data']['center_lon'] = lon
                tfr_data['raw_data']['radius_nm'] = radius_nm
        
        # Extract altitudes
        alt_match = re.search(r'FROM\s+(?:SURFACE|SFC|(\d+))\s+(?:FT\s+)?(?:MSL\s+)?TO\s+(\d+)\s*(?:FT\s+)?(?:MSL)?', text, re.IGNORECASE)
        if alt_match:
            tfr_data['lower_altitude_ft'] = int(alt_match.group(1)) if alt_match.group(1) else 0
            tfr_data['upper_altitude_ft'] = int(alt_match.group(2))
        
        return tfr_data
        
    except Exception as e:
        log.error(f"Failed to parse TFR {tfr_number}: {e}")
        return None


def store_tfr_in_database(tfr_data):
    """
    Store TFR in PostGIS database
    
    Returns True on success, False on failure
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Check if TFR already exists
        cur.execute("SELECT id FROM observations.tfr WHERE tfr_number = %s", (tfr_data['tfr_number'],))
        existing = cur.fetchone()
        
        if existing:
            # Update existing TFR
            cur.execute("""
                UPDATE observations.tfr
                SET effective_start = %s,
                    effective_end = %s,
                    facility = %s,
                    city = %s,
                    state = %s,
                    type = %s,
                    geometry = ST_GeomFromText(%s, 4326),
                    lower_altitude_ft = %s,
                    upper_altitude_ft = %s,
                    raw_text = %s,
                    raw_data = %s,
                    active = TRUE,
                    fetched_at = NOW()
                WHERE tfr_number = %s
            """, (
                tfr_data.get('effective_start'),
                tfr_data.get('effective_end'),
                tfr_data.get('facility'),
                tfr_data.get('city'),
                tfr_data.get('state'),
                tfr_data.get('type'),
                tfr_data.get('geometry_wkt'),
                tfr_data.get('lower_altitude_ft'),
                tfr_data.get('upper_altitude_ft'),
                tfr_data.get('raw_text'),
                json.dumps(tfr_data.get('raw_data', {})),
                tfr_data['tfr_number']
            ))
            log.info(f"  Updated TFR {tfr_data['tfr_number']}")
        else:
            # Insert new TFR
            cur.execute("""
                INSERT INTO observations.tfr (
                    tfr_number, notam_id, effective_start, effective_end,
                    facility, city, state, type, geometry,
                    lower_altitude_ft, upper_altitude_ft,
                    raw_text, raw_data, active
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    ST_GeomFromText(%s, 4326),
                    %s, %s, %s, %s, TRUE
                )
            """, (
                tfr_data['tfr_number'],
                tfr_data.get('notam_id'),
                tfr_data.get('effective_start'),
                tfr_data.get('effective_end'),
                tfr_data.get('facility'),
                tfr_data.get('city'),
                tfr_data.get('state'),
                tfr_data.get('type'),
                tfr_data.get('geometry_wkt'),
                tfr_data.get('lower_altitude_ft'),
                tfr_data.get('upper_altitude_ft'),
                tfr_data.get('raw_text'),
                json.dumps(tfr_data.get('raw_data', {}))
            ))
            log.info(f"  Inserted TFR {tfr_data['tfr_number']}")
        
        conn.commit()
        cur.close()
        conn.close()
        return True
        
    except Exception as e:
        log.error(f"Failed to store TFR {tfr_data.get('tfr_number')}: {e}")
        return False


def deactivate_expired_tfrs():
    """Mark expired TFRs as inactive"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE observations.tfr
            SET active = FALSE
            WHERE effective_end < NOW()
              AND active = TRUE
        """)
        
        count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        
        if count > 0:
            log.info(f"Deactivated {count} expired TFR(s)")
        
    except Exception as e:
        log.error(f"Failed to deactivate expired TFRs: {e}")


def main():
    parser = argparse.ArgumentParser(description='FAA TFR Ingestion')
    parser.add_argument('--force', action='store_true', help='Force re-fetch all TFRs')
    args = parser.parse_args()
    
    log.info("=" * 70)
    log.info(f"FAA TFR Ingestion - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 70)
    
    # Fetch TFR list
    tfr_numbers = fetch_tfr_list()
    
    if not tfr_numbers:
        log.warning("No TFRs found or fetch failed")
        return 1
    
    # Process each TFR
    success_count = 0
    fail_count = 0
    
    for tfr_number in tfr_numbers:
        tfr_data = parse_tfr_detail(tfr_number)
        
        if tfr_data and tfr_data.get('geometry_wkt'):
            if store_tfr_in_database(tfr_data):
                success_count += 1
            else:
                fail_count += 1
        else:
            log.warning(f"  Skipped TFR {tfr_number} (no geometry)")
            fail_count += 1
    
    # Deactivate expired TFRs
    deactivate_expired_tfrs()
    
    log.info("=" * 70)
    log.info(f"✓ TFR ingestion complete: {success_count} success, {fail_count} failed")
    log.info("=" * 70)
    
    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

