#!/usr/bin/env python3
"""
AIRMET/SIGMET Parser for CAP Winds Application
Phase 2A: Parse text files and extract geographic polygons

Handles:
- AIRMETs (TANGO=Turbulence, ZULU=Icing, SIERRA=IFR)
- SIGMETs (Convective and Non-convective)
- Coordinate parsing (N4500 W04300 format)
- Valid time extraction
- Flight level parsing
"""

import os
import re
import glob
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional


class AirmetSigmetParser:
    """Parser for AIRMET and SIGMET text products"""
    
    # Base directory for LDM text data
    BASE_DIR = "/LDM/text"
    
    # Color codes for different phenomena
    COLORS = {
        'TURB': '#FFA500',      # Orange - Turbulence
        'ICE': '#87CEEB',       # Light Blue - Icing
        'IFR': '#808080',       # Gray - IFR/Mountain Obscuration
        'CONVECTIVE': '#FF0000', # Red - Convective SIGMET
        'NONCONVECTIVE': '#FF4500',  # Dark Orange - Other SIGMETs
        'MTN_OBSC': '#696969',  # Dim Gray - Mountain Obscuration
        'SFC_WND': '#FFD700',   # Gold - Strong Surface Winds
    }
    
    # US FIR/ARTCC codes for geographic filtering
    US_FIRS = {
        # CONUS ARTCCs
        'KZAB', 'KZBW', 'KZAU', 'KZDC', 'KZDV', 'KZFW', 'KZHU', 'KZID',
        'KZJX', 'KZKC', 'KZLA', 'KZLC', 'KZMA', 'KZME', 'KZMP', 'KZNY',
        'KZOA', 'KZOB', 'KZSE', 'KZTL',
        # Alaska
        'PAZA',
        # Hawaii
        'PHZH',
        # Guam
        'PGZU',
        # Puerto Rico (part of Miami)
        'TJZS',
    }
    
    def __init__(self):
        """Initialize parser"""
        pass
    
    def parse_coordinate(self, coord_str: str) -> Optional[Tuple[float, float]]:
        """
        Parse a single coordinate in NEXRAD format
        
        Examples:
            N4500 W04300 → (45.0, -43.0)
            N45 W043 → (45.0, -43.0)
            N4530 W04315 → (45.5, -43.25)
        """
        # Pattern: N/S followed by 2-4 digits, W/E followed by 3-5 digits
        pattern = r'([NS])(\d{2,4})\s+([WE])(\d{3,5})'
        match = re.search(pattern, coord_str)
        
        if not match:
            return None
        
        ns, lat_str, we, lon_str = match.groups()
        
        # Parse latitude
        if len(lat_str) == 2:
            lat = float(lat_str)
        elif len(lat_str) == 4:
            lat = float(lat_str[:2]) + float(lat_str[2:]) / 60.0
        else:
            return None
        
        if ns == 'S':
            lat = -lat
        
        # Parse longitude
        if len(lon_str) == 3:
            lon = float(lon_str)
        elif len(lon_str) == 5:
            lon = float(lon_str[:3]) + float(lon_str[3:]) / 60.0
        else:
            return None
        
        if we == 'W':
            lon = -lon
        
        return (lat, lon)
    
    def parse_coordinates_sequence(self, text: str) -> List[Tuple[float, float]]:
        """
        Parse a sequence of coordinates from AIRMET/SIGMET text
        
        Example:
            "WI N4500 W04300 - N4500 W04000 - N4100 W04000"
            Returns: [(45.0, -43.0), (45.0, -40.0), (41.0, -40.0)]
        """
        coordinates = []
        
        # Find all coordinate pairs
        pattern = r'([NS]\d{2,4}\s+[WE]\d{3,5})'
        matches = re.findall(pattern, text)
        
        for match in matches:
            coord = self.parse_coordinate(match)
            if coord:
                coordinates.append(coord)
        
        # Close polygon if not already closed
        if len(coordinates) > 2 and coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0])
        
        return coordinates
    
    def parse_valid_time(self, text: str) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Parse valid time from AIRMET/SIGMET
        
        Example:
            "VALID 091600/092200" → (2026-02-09 16:00, 2026-02-09 22:00)
        """
        pattern = r'VALID\s+(\d{6})/(\d{6})'
        match = re.search(pattern, text)
        
        if not match:
            return None, None
        
        start_str, end_str = match.groups()
        
        # Parse DDHHMM format
        try:
            day_start = int(start_str[0:2])
            hour_start = int(start_str[2:4])
            min_start = int(start_str[4:6])
            
            day_end = int(end_str[0:2])
            hour_end = int(end_str[2:4])
            min_end = int(end_str[4:6])
            
            # Assume current month/year (can be improved)
            now = datetime.utcnow()
            
            valid_from = datetime(now.year, now.month, day_start, hour_start, min_start)
            valid_until = datetime(now.year, now.month, day_end, hour_end, min_end)
            
            # Handle month rollover
            if valid_until < valid_from:
                if now.month == 12:
                    valid_until = valid_until.replace(year=now.year + 1, month=1)
                else:
                    valid_until = valid_until.replace(month=now.month + 1)
            
            return valid_from, valid_until
            
        except (ValueError, IndexError):
            return None, None
    
    def parse_flight_levels(self, text: str) -> str:
        """
        Extract flight level information
        
        Examples:
            "BLW FL380" → "Below FL380"
            "FL100-FL200" → "FL100-FL200"
            "SFC-FL180" → "Surface to FL180"
        """
        # Look for common flight level patterns
        patterns = [
            r'(BLW\s+FL\d{3})',
            r'(ABV\s+FL\d{3})',
            r'(FL\d{3}[-]FL\d{3})',
            r'(SFC[-]FL\d{3})',
            r'(IN\s+LYR\s+\d{3}[-]FL\d{3})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        
        return "Not specified"
    
    def determine_phenomenon_type(self, text: str) -> str:
        """
        Determine the phenomenon type from AIRMET/SIGMET text
        """
        text_upper = text.upper()
        
        # AIRMET types
        if 'AIRMET TANGO' in text_upper or 'AIRMET TURB' in text_upper:
            return 'TURB'
        elif 'AIRMET ZULU' in text_upper or 'AIRMET.*ICE' in text_upper:
            return 'ICE'
        elif 'AIRMET SIERRA' in text_upper or 'IFR' in text_upper:
            return 'IFR'
        elif 'MTN OBSC' in text_upper:
            return 'MTN_OBSC'
        elif 'STG SFC WND' in text_upper or 'STRONG SURFACE WIND' in text_upper:
            return 'SFC_WND'
        
        # SIGMET types
        elif 'CONVECTIVE SIGMET' in text_upper or 'CONVECTIVE' in text_upper:
            return 'CONVECTIVE'
        elif 'SIGMET' in text_upper:
            # Non-convective SIGMET
            if any(word in text_upper for word in ['TURB', 'TURBULENCE']):
                return 'TURB'
            elif any(word in text_upper for word in ['ICE', 'ICING']):
                return 'ICE'
            else:
                return 'NONCONVECTIVE'
        
        return 'UNKNOWN'
    
    def extract_severity(self, text: str) -> str:
        """
        Extract severity level (for turbulence/icing)
        """
        text_upper = text.upper()
        
        if 'SEV' in text_upper or 'SEVERE' in text_upper:
            return 'SEV'
        elif 'MOD' in text_upper or 'MODERATE' in text_upper:
            return 'MOD'
        elif 'LIGHT' in text_upper or 'LGT' in text_upper:
            return 'LIGHT'
        
        return 'UNSPECIFIED'
    
    def is_us_product(self, text: str) -> bool:
        """
        Check if AIRMET/SIGMET is for US airspace
        """
        # Check for US FIR codes
        for fir in self.US_FIRS:
            if fir in text:
                return True
        
        # Check for common US indicators
        us_indicators = ['CONUS', 'ALASKA', 'HAWAII', 'GUAM', 'PUERTO RICO']
        text_upper = text.upper()
        
        for indicator in us_indicators:
            if indicator in text_upper:
                return True
        
        return False
    
    def parse_airmet_file(self, filepath: str) -> List[Dict]:
        """
        Parse a single AIRMET file
        
        Returns list because one file can contain multiple AIRMETs
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return []
        
        # Filter non-US products
        if not self.is_us_product(content):
            return []
        
        airmets = []
        
        # Split on '=' which separates multiple AIRMETs
        sections = content.split('=')
        
        for section in sections:
            if len(section.strip()) < 50:  # Skip small fragments
                continue
            
            # Extract coordinates
            coords = self.parse_coordinates_sequence(section)
            if len(coords) < 3:  # Need at least 3 points for polygon
                continue
            
            # Parse valid times
            valid_from, valid_until = self.parse_valid_time(section)
            
            # Determine phenomenon
            phenomenon = self.determine_phenomenon_type(section)
            
            # Extract flight levels
            flight_levels = self.parse_flight_levels(section)
            
            # Extract severity
            severity = self.extract_severity(section)
            
            # Get color
            color = self.COLORS.get(phenomenon, '#808080')
            
            airmet = {
                'type': 'AIRMET',
                'phenomenon': phenomenon,
                'severity': severity,
                'coordinates': coords,
                'valid_from': valid_from.isoformat() if valid_from else None,
                'valid_until': valid_until.isoformat() if valid_until else None,
                'flight_levels': flight_levels,
                'text': section.strip(),
                'color': color,
                'filepath': filepath
            }
            
            airmets.append(airmet)
        
        return airmets
    
    def parse_sigmet_file(self, filepath: str) -> List[Dict]:
        """
        Parse a single SIGMET file
        
        Similar to AIRMET parsing but for SIGMETs
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return []
        
        # Filter non-US products
        if not self.is_us_product(content):
            return []
        
        sigmets = []
        
        # Extract coordinates
        coords = self.parse_coordinates_sequence(content)
        if len(coords) < 3:
            return []
        
        # Parse valid times
        valid_from, valid_until = self.parse_valid_time(content)
        
        # Determine phenomenon
        phenomenon = self.determine_phenomenon_type(content)
        
        # Extract flight levels
        flight_levels = self.parse_flight_levels(content)
        
        # Extract severity
        severity = self.extract_severity(content)
        
        # Get color
        color = self.COLORS.get(phenomenon, '#FF4500')
        
        sigmet = {
            'type': 'SIGMET',
            'phenomenon': phenomenon,
            'severity': severity,
            'coordinates': coords,
            'valid_from': valid_from.isoformat() if valid_from else None,
            'valid_until': valid_until.isoformat() if valid_until else None,
            'flight_levels': flight_levels,
            'text': content.strip(),
            'color': color,
            'filepath': filepath
        }
        
        sigmets.append(sigmet)
        
        return sigmets
    
    def get_active_airmets(self, timestamp: Optional[datetime] = None) -> List[Dict]:
        """
        Get all active AIRMETs at a given time
        
        Args:
            timestamp: Time to check for active products (default: now)
        
        Returns:
            List of active AIRMET dictionaries
        """
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        airmets = []
        
        # Look at today and yesterday
        for days_back in range(2):
            date = timestamp - timedelta(days=days_back)
            date_str = date.strftime('%Y/%m/%d')
            airmet_dir = os.path.join(self.BASE_DIR, 'airmet', date_str)
            
            if not os.path.exists(airmet_dir):
                continue
            
            # Find all AIRMET files
            pattern = os.path.join(airmet_dir, '*AIRMET*.txt')
            files = glob.glob(pattern)
            
            for filepath in files:
                parsed = self.parse_airmet_file(filepath)
                
                # Filter for currently valid
                for airmet in parsed:
                    if airmet['valid_from'] and airmet['valid_until']:
                        valid_from = datetime.fromisoformat(airmet['valid_from'])
                        valid_until = datetime.fromisoformat(airmet['valid_until'])
                        
                        if valid_from <= timestamp <= valid_until:
                            airmets.append(airmet)
        
        return airmets
    
    def get_active_sigmets(self, timestamp: Optional[datetime] = None) -> List[Dict]:
        """
        Get all active SIGMETs at a given time
        """
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        sigmets = []
        
        # Look at today and yesterday
        for days_back in range(2):
            date = timestamp - timedelta(days=days_back)
            date_str = date.strftime('%Y/%m/%d')
            sigmet_dir = os.path.join(self.BASE_DIR, 'sigmet', date_str)
            
            if not os.path.exists(sigmet_dir):
                continue
            
            # Find all SIGMET files
            pattern = os.path.join(sigmet_dir, '*SIGMET*.txt')
            files = glob.glob(pattern)
            
            for filepath in files:
                parsed = self.parse_sigmet_file(filepath)
                
                # Filter for currently valid
                for sigmet in parsed:
                    if sigmet['valid_from'] and sigmet['valid_until']:
                        valid_from = datetime.fromisoformat(sigmet['valid_from'])
                        valid_until = datetime.fromisoformat(sigmet['valid_until'])
                        
                        if valid_from <= timestamp <= valid_until:
                            sigmets.append(sigmet)
        
        return sigmets
    
    def to_geojson(self, products: List[Dict]) -> Dict:
        """
        Convert AIRMET/SIGMET list to GeoJSON FeatureCollection
        """
        features = []
        
        for product in products:
            # Convert coordinates to GeoJSON format [lon, lat]
            coords_geojson = [[lon, lat] for lat, lon in product['coordinates']]
            
            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Polygon',
                    'coordinates': [coords_geojson]
                },
                'properties': {
                    'type': product['type'],
                    'phenomenon': product['phenomenon'],
                    'severity': product['severity'],
                    'flight_levels': product['flight_levels'],
                    'valid_from': product['valid_from'],
                    'valid_until': product['valid_until'],
                    'text': product['text'],
                    'color': product['color']
                }
            }
            
            features.append(feature)
        
        return {
            'type': 'FeatureCollection',
            'features': features
        }


# Standalone test function
def test_parser():
    """Test the parser with sample data"""
    parser = AirmetSigmetParser()
    
    print("=== Testing AIRMET Parser ===")
    airmets = parser.get_active_airmets()
    print(f"Found {len(airmets)} active AIRMETs")
    
    for airmet in airmets[:3]:  # Show first 3
        print(f"\n{airmet['phenomenon']} - {airmet['severity']}")
        print(f"Valid: {airmet['valid_from']} to {airmet['valid_until']}")
        print(f"Flight levels: {airmet['flight_levels']}")
        print(f"Coordinates: {len(airmet['coordinates'])} points")
    
    print("\n=== Testing SIGMET Parser ===")
    sigmets = parser.get_active_sigmets()
    print(f"Found {len(sigmets)} active SIGMETs")
    
    for sigmet in sigmets[:3]:
        print(f"\n{sigmet['phenomenon']} - {sigmet['severity']}")
        print(f"Valid: {sigmet['valid_from']} to {sigmet['valid_until']}")
        print(f"Coordinates: {len(sigmet['coordinates'])} points")
    
    # Test GeoJSON conversion
    print("\n=== GeoJSON Output ===")
    geojson = parser.to_geojson(airmets[:1])
    import json
    print(json.dumps(geojson, indent=2))


if __name__ == '__main__':
    test_parser()

