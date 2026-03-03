#!/usr/bin/env python3
"""
Aviation Weather API Fetcher for AIRMETs/SIGMETs (Updated for G-AIRMETs)
Fetches from both /airsigmet and /gairmet endpoints
"""

import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional


class AviationWeatherAPI:
    """Fetch AIRMETs and SIGMETs from Aviation Weather Center API"""
    
    API_BASE = "https://aviationweather.gov/api/data"
    
    # Color codes for different phenomena
    COLORS = {
        'TURB': '#FFA500',      # Orange - Turbulence
        'CONVECTIVE': '#FF0000', # Red - Convective
        'ICE': '#87CEEB',       # Light Blue - Icing
        'IFR': '#808080',       # Gray - IFR
        'MTN_OBSC': '#696969',  # Dim Gray - Mountain Obscuration
        'ASH': '#8B4513',       # Brown - Volcanic Ash
        'TSGR': '#FF0000',      # Red - Thunderstorms/Hail
    }
    
    def __init__(self):
        """Initialize API fetcher"""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'CAP-Winds-Weather-Display/1.0'
        })
    
    def fetch_all(self) -> List[Dict]:
        """
        Fetch all current AIRMETs and SIGMETs from both endpoints
        
        Returns:
            List of parsed AIRMET/SIGMET dictionaries
        """
        all_products = []
        
        # Fetch SIGMETs and international AIRMETs
        try:
            response = self.session.get(
                f'{self.API_BASE}/airsigmet',
                params={'format': 'json'},
                timeout=10
            )
            response.raise_for_status()
            
            # Handle 204 No Content
            if response.status_code != 204 and response.text:
                data = response.json()
                if isinstance(data, list):
                    all_products.extend([self._parse_airsigmet(item) for item in data])
        except Exception as e:
            print(f"Error fetching airsigmet: {e}")
        
        # Fetch G-AIRMETs (CONUS)
        try:
            response = self.session.get(
                f'{self.API_BASE}/gairmet',
                params={'format': 'json'},
                timeout=10
            )
            response.raise_for_status()
            
            # Handle 204 No Content
            if response.status_code != 204 and response.text:
                data = response.json()
                if isinstance(data, list):
                    all_products.extend([self._parse_gairmet(item) for item in data])
        except Exception as e:
            print(f"Error fetching gairmet: {e}")
        
        return all_products
    
    def _parse_airsigmet(self, item: Dict) -> Dict:
        """
        Parse AIRMET/SIGMET from airsigmet endpoint
        
        Args:
            item: Raw API response item from airsigmet
        
        Returns:
            Parsed dictionary with standardized fields
        """
        # Extract coordinates
        coords = []
        if 'coords' in item and item['coords']:
            coords = [(coord['lat'], coord['lon']) for coord in item['coords']]
        
        # Get valid times (Unix timestamps)
        valid_from = None
        valid_until = None
        
        if 'validTimeFrom' in item and item['validTimeFrom']:
            valid_from = datetime.fromtimestamp(item['validTimeFrom'], tz=timezone.utc)
        
        if 'validTimeTo' in item and item['validTimeTo']:
            valid_until = datetime.fromtimestamp(item['validTimeTo'], tz=timezone.utc)
        
        # Get flight levels
        flight_levels = self._format_flight_levels(item)
        
        # Get hazard type
        hazard = item.get('hazard', 'UNKNOWN')
        
        # Determine severity
        severity = self._get_severity(item)
        
        # Get color
        color = self.COLORS.get(hazard, '#808080')
        
        # Determine type (AIRMET vs SIGMET)
        product_type = item.get('airSigmetType', 'UNKNOWN')
        
        return {
            'type': product_type,
            'phenomenon': hazard,
            'severity': severity,
            'coordinates': coords,
            'valid_from': valid_from.isoformat() if valid_from else None,
            'valid_until': valid_until.isoformat() if valid_until else None,
            'flight_levels': flight_levels,
            'text': item.get('rawAirSigmet', ''),
            'color': color,
            'source': 'Aviation Weather API'
        }
    
    def _parse_gairmet(self, item: Dict) -> Dict:
        """
        Parse G-AIRMET from gairmet endpoint (different format)
        
        Args:
            item: Raw API response item from gairmet
        
        Returns:
            Parsed dictionary with standardized fields
        """
        # FZLVL LINE records are freezing level contours — not hazard polygons.
        # Return them as a distinct type so the map can render them as labeled polylines.
        hazard_type = item.get('hazard', '')
        geom_type   = item.get('geom', item.get('geometryType', ''))
        is_fzlvl_line = (hazard_type == 'FZLVL' and geom_type == 'LINE')

        # Extract coordinates (different format than airsigmet)
        coords = []
        if 'coords' in item and item['coords']:
            coords = [(float(coord['lat']), float(coord['lon'])) for coord in item['coords']]
        
        # G-AIRMETs use ISO timestamp and Unix timestamp
        valid_from = None
        valid_until = None
        
        if 'validTime' in item:
            # ISO format: "2026-02-15T06:00:00.000Z"
            valid_from = datetime.fromisoformat(item['validTime'].replace('Z', '+00:00'))
        
        if 'expireTime' in item and item['expireTime']:
            # Unix timestamp
            valid_until = datetime.fromtimestamp(item['expireTime'], tz=timezone.utc)
        
        # Map G-AIRMET product names to hazard types
        product_hazard_map = {
            'SIERRA': 'IFR',        # IFR and Mountain Obscuration
            'TANGO': 'TURB',        # Turbulence
            'ZULU': 'ICE'           # Icing
        }
        
        # Get hazard - prefer mapped product name
        hazard = item.get('hazard', 'UNKNOWN')
        if item.get('product') in product_hazard_map:
            hazard = product_hazard_map[item['product']]
        
        # Get color
        color = self.COLORS.get(hazard, '#808080')
        
        # Format flight levels — field names differ by product type:
        #   ZULU (icing):        fzlbase / fzltop  (freezing level fields)
        #   TANGO (turbulence):  base / top         (FL values)
        #   SIERRA (IFR/MTN):    all fields empty   (surface phenomenon, no FL)
        product = item.get('product', '')
        flight_levels = self._format_gairmet_levels(item, product, hazard)

        # Build descriptive text
        text_parts = []
        if product:
            text_parts.append(f"{product} G-AIRMET")
        if item.get('due_to'):
            text_parts.append(item['due_to'])
        if item.get('hazard'):
            text_parts.append(f"({item['hazard']})")

        text = ' - '.join(text_parts) if text_parts else 'G-AIRMET'
        
        # Build label for FZLVL contour lines
        if is_fzlvl_line:
            level = (item.get('level') or '').strip().upper()
            if level == 'SFC':
                fzlvl_label = 'FZLVL SFC'
            elif level.isdigit():
                fzlvl_label = f"FZLVL FL{int(level):03d}"
            else:
                fzlvl_label = 'FZLVL'
        else:
            fzlvl_label = None

        return {
            'type': 'FZLVL_CONTOUR' if is_fzlvl_line else 'AIRMET',
            'phenomenon': hazard,
            'severity': item.get('severity') or 'MOD',
            'coordinates': coords,
            'valid_from': valid_from.isoformat() if valid_from else None,
            'valid_until': valid_until.isoformat() if valid_until else None,
            'flight_levels': flight_levels,
            'label': fzlvl_label,
            'due_to': item.get('due_to', ''),
            'product': product,
            'geom': geom_type,
            'forecast_hour': item.get('forecastHour', 0),
            'text': text,
            'color': color,
            'source': 'Aviation Weather API (G-AIRMET)'
        }
    
    def _format_gairmet_levels(self, item: Dict, product: str, hazard: str) -> str:
        """
        Format altitude/flight level string for a G-AIRMET.

        The AWC /gairmet API uses different altitude fields depending on product:

        ZULU (icing):
          fzlbase — freezing level base, e.g. "SFC", "FL020", or empty
          fzltop  — freezing level top,  e.g. "FL080"
          Icing exists between fzlbase and fzltop.
          When fzlbase is empty/SFC, icing extends from the surface/freezing level up.
          Example display: "SFC–FL080"  or  "FZLVL–FL080"

        TANGO (turbulence):
          base — FL value, e.g. "FL180"
          top  — FL value, e.g. "FL380"
          Example display: "FL180–FL380"

        SIERRA (IFR / mountain obscuration):
          All altitude fields are empty — this is a surface/visibility phenomenon
          with no meaningful flight level bounds.
          Display: "Surface (IFR/MTN OBSCN)" with the due_to reason.

        The 'level' field is occasionally populated for turbulence in layers.
        """
        def clean(v):
            return (v or '').strip().upper()

        if product == 'ZULU':
            hazard_type = clean(item.get('hazard'))
            geom = clean(item.get('geom'))

            if hazard_type == 'ICE' and geom == 'AREA':
                # Icing hazard area — fzlbase/fzltop is the icing layer
                # 'top' is cloud tops, not the icing layer ceiling — ignore it here
                fzlbase = clean(item.get('fzlbase'))
                fzltop  = clean(item.get('fzltop'))
                # Normalize to FL notation
                def to_fl(v):
                    if not v:
                        return ''
                    if v.isdigit():
                        return f"FL{int(v):03d}"
                    return v
                b = to_fl(fzlbase)
                t = to_fl(fzltop)
                if b and t:
                    return f"{b}–{t}"
                elif t:
                    return f"FZLVL–{t}"
                elif b:
                    return f"{b} and above"
                else:
                    return "FZLVL (variable)"

            elif hazard_type == 'FZLVL' and geom == 'LINE':
                # Freezing level contour line — level field in hundreds of feet
                level = clean(item.get('level'))
                if level == 'SFC':
                    return "FZLVL at Surface"
                elif level.isdigit():
                    fl = int(level)
                    ft = fl * 100
                    return f"FZLVL FL{fl:03d} ({ft:,} ft MSL)"
                else:
                    return "FZLVL contour"

            elif hazard_type == 'M_FZLVL' and geom == 'AREA':
                # Multiple/variable freezing levels area
                top = clean(item.get('top'))
                def to_fl(v):
                    if not v:
                        return ''
                    if v.isdigit():
                        return f"FL{int(v):03d}"
                    return v
                t = to_fl(top)
                if t:
                    return f"Multiple FZLVL, tops {t}"
                else:
                    return "Multiple FZLVL (variable)"

            else:
                # Unknown ZULU sub-type — best effort
                fzlbase = clean(item.get('fzlbase'))
                fzltop  = clean(item.get('fzltop'))
                level   = clean(item.get('level'))
                if fzlbase and fzltop:
                    return f"{fzlbase}–{fzltop}"
                elif level:
                    return f"FZLVL FL{level}"
                return "FZLVL (variable)"

        elif product == 'TANGO':
            # Turbulence — use base/top FL fields
            base  = clean(item.get('base'))
            top   = clean(item.get('top'))
            level = clean(item.get('level'))

            if base and top:
                return f"{base}–{top}"
            elif level:
                return level
            elif top:
                return f"Below {top}"
            elif base:
                return f"Above {base}"
            else:
                return "Not specified"

        elif product == 'SIERRA':
            # IFR / Mountain Obscuration — surface phenomenon, no FL bounds
            # Return a meaningful label rather than "Not specified"
            due_to = clean(item.get('due_to', ''))
            if 'MTN' in due_to or 'OBSC' in due_to:
                return "Surface–Bases (MTN OBSCN)"
            else:
                return "Surface (IFR)"

        else:
            # Unknown product — try base/top, then give up gracefully
            base = clean(item.get('base'))
            top  = clean(item.get('top'))
            if base and top:
                return f"{base}–{top}"
            elif top:
                return f"Below {top}"
            elif base:
                return f"Above {base}"
            return "Not specified"

    def _format_flight_levels(self, item: Dict) -> str:
        """Format flight level information from airsigmet API data.

        Primary: structured altitudeHi1 / altitudeLow1 fields (feet MSL, integer).
        Fallback: parse altitude from rawAirSigmet text when structured fields absent.

        Common raw text patterns:
          BLW FL190        -> Below FL190
          ABV FL180        -> Above FL180
          BTN FL180 AND FL300  -> FL180-FL300
          SFC-FL180        -> SFC-FL180
          FL180-FL300      -> FL180-FL300
        """
        import re

        alt_hi  = item.get('altitudeHi1')
        alt_low = item.get('altitudeLow1')

        if alt_hi and alt_low:
            fl_hi  = alt_hi  // 100
            fl_low = alt_low // 100
            return f"FL{fl_low:03d}-FL{fl_hi:03d}"
        elif alt_hi:
            fl_hi = alt_hi // 100
            return f"Below FL{fl_hi:03d}"
        elif alt_low:
            fl_low = alt_low // 100
            return f"Above FL{fl_low:03d}"

        # --- Fallback: parse altitude from raw text ---
        raw = (item.get('rawAirSigmet') or '').upper()

        # BLW FL190  /  BELOW FL190
        m = re.search(r'\bBL[OW]+\s+FL(\d{2,3})\b', raw)
        if m:
            return f"Below FL{int(m.group(1)):03d}"

        # ABV FL180  /  ABOVE FL180
        m = re.search(r'\bABV\s+FL(\d{2,3})\b', raw)
        if m:
            return f"Above FL{int(m.group(1)):03d}"

        # BTN FL180 AND FL300  (between)
        m = re.search(r'\bBTN\s+FL(\d{2,3})\s+AND\s+FL(\d{2,3})\b', raw)
        if m:
            return f"FL{int(m.group(1)):03d}-FL{int(m.group(2)):03d}"

        # SFC/FL180  or  SFC-FL180
        m = re.search(r'\bSFC[-/]FL(\d{2,3})\b', raw)
        if m:
            return f"SFC-FL{int(m.group(1)):03d}"

        # FL180/FL300  or  FL180-FL300
        m = re.search(r'\bFL(\d{2,3})[-/]FL(\d{2,3})\b', raw)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return f"FL{lo:03d}-FL{hi:03d}"

        # Single FL mention as last resort
        m = re.search(r'\bFL(\d{2,3})\b', raw)
        if m:
            return f"FL{int(m.group(1)):03d}"

        return "Not specified"
    
    def _get_severity(self, item: Dict) -> str:
        """Determine severity from airsigmet API data"""
        # SIGMETs are inherently more severe than AIRMETs
        if item.get('airSigmetType') == 'SIGMET':
            return 'SEV'
        
        # Check for severity indicators in text
        text = item.get('rawAirSigmet', '').upper()
        if 'SEV' in text or 'SEVERE' in text:
            return 'SEV'
        elif 'MOD' in text or 'MODERATE' in text:
            return 'MOD'
        elif 'LIGHT' in text or 'LGT' in text:
            return 'LIGHT'
        
        return 'UNSPECIFIED'
    
    def get_active_airmets(self, timestamp: Optional[datetime] = None) -> List[Dict]:
        """
        Get currently active AIRMETs (including G-AIRMETs)
        
        Args:
            timestamp: Time to check (default: now)
        
        Returns:
            List of active AIRMET dictionaries
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        all_items = self.fetch_all()
        
        # Filter for AIRMETs and FZLVL contours (exclude SIGMETs)
        airmets = []
        for item in all_items:
            if item['type'] not in ('AIRMET', 'FZLVL_CONTOUR'):
                continue
            
            if item['valid_from'] and item['valid_until']:
                valid_from = datetime.fromisoformat(item['valid_from'])
                valid_until = datetime.fromisoformat(item['valid_until'])
                
                # G-AIRMETs are issued every 3h; expireTime == validTime on the AWC
                # API (no overlap between cycles). The winds page covers a 12h forecast
                # horizon, so show any G-AIRMET whose valid time falls within that
                # window — lookahead matches the full forecast period.
                lookahead = timedelta(hours=12)
                if (valid_from - lookahead) <= timestamp <= valid_until:
                    airmets.append(item)
        
        return airmets
    
    def get_active_sigmets(self, timestamp: Optional[datetime] = None) -> List[Dict]:
        """
        Get currently active SIGMETs
        
        Args:
            timestamp: Time to check (default: now)
        
        Returns:
            List of active SIGMET dictionaries
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        all_items = self.fetch_all()
        
        # Filter for SIGMETs only and currently valid
        sigmets = []
        for item in all_items:
            if item['type'] != 'SIGMET':
                continue
            
            if item['valid_from'] and item['valid_until']:
                valid_from = datetime.fromisoformat(item['valid_from'])
                valid_until = datetime.fromisoformat(item['valid_until'])
                
                if valid_from <= timestamp <= valid_until:
                    sigmets.append(item)
        
        return sigmets
    
    def to_geojson(self, products: List[Dict]) -> Dict:
        """
        Convert AIRMET/SIGMET/FZLVL_CONTOUR list to GeoJSON FeatureCollection.

        FZLVL_CONTOUR records are emitted as LineString features.
        AIRMET/SIGMET records are emitted as Polygon features.
        """
        features = []

        for product in products:
            coords = product.get('coordinates', [])
            if not coords:
                continue

            # Convert coordinates to GeoJSON format [lon, lat]
            coords_geojson = [[lon, lat] for lat, lon in coords]

            is_contour = product.get('type') == 'FZLVL_CONTOUR'

            if is_contour:
                # LineString — needs at least 2 points
                if len(coords_geojson) < 2:
                    continue
                geometry = {
                    'type': 'LineString',
                    'coordinates': coords_geojson
                }
            else:
                # Polygon — needs at least 3 points
                if len(coords_geojson) < 3:
                    continue
                geometry = {
                    'type': 'Polygon',
                    'coordinates': [coords_geojson]
                }

            feature = {
                'type': 'Feature',
                'geometry': geometry,
                'properties': {
                    'type': product['type'],
                    'phenomenon': product.get('phenomenon', ''),
                    'severity': product.get('severity', ''),
                    'flight_levels': product.get('flight_levels', ''),
                    'label': product.get('label', ''),
                    'due_to': product.get('due_to', ''),
                    'valid_from': product.get('valid_from'),
                    'valid_until': product.get('valid_until'),
                    'text': product.get('text', ''),
                    'color': product.get('color', '#808080'),
                    'source': product.get('source', '')
                }
            }

            features.append(feature)

        return {
            'type': 'FeatureCollection',
            'features': features
        }


# Test function
def test_api():
    """Test the Aviation Weather API fetcher"""
    api = AviationWeatherAPI()
    
    print("=== Testing Aviation Weather API (Updated for G-AIRMETs) ===")
    print()
    
    print("Fetching AIRMETs (including G-AIRMETs)...")
    airmets = api.get_active_airmets()
    print(f"Found {len(airmets)} active AIRMETs")
    
    for airmet in airmets[:5]:
        print(f"  {airmet['phenomenon']} - {airmet['severity']} - {len(airmet['coordinates'])} coords - {airmet['source']}")
    
    print()
    print("Fetching SIGMETs...")
    sigmets = api.get_active_sigmets()
    print(f"Found {len(sigmets)} active SIGMETs")
    
    for sigmet in sigmets[:3]:
        print(f"  {sigmet['phenomenon']} - {sigmet['severity']} - {len(sigmet['coordinates'])} coords")
    
    print()
    print("=== GeoJSON Sample ===")
    all_products = airmets + sigmets
    if all_products:
        geojson = api.to_geojson(all_products[:2])
        
        import json
        print(json.dumps(geojson, indent=2))
    else:
        print("No products available")


if __name__ == '__main__':
    test_api()

