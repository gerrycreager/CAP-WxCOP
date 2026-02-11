#!/var/www/cap_winds_app/venv/bin/python3

"""
Runway Analysis Module
Calculates headwind/crosswind components and determines best runway
"""
import csv
import math
import os
from typing import List, Dict, Optional, Tuple

# Cache for runway data
_runway_cache = None
_cache_loaded = False

CACHE_FILE = '/var/www/cap_winds_app/.cache/runways.csv'

def load_runway_cache():
    """Load runway data from OurAirports CSV cache"""
    global _runway_cache, _cache_loaded
    
    if _cache_loaded:
        return _runway_cache
    
    if not os.path.exists(CACHE_FILE):
        print(f"Runway cache not found: {CACHE_FILE}")
        _cache_loaded = True
        _runway_cache = {}
        return _runway_cache
    
    _runway_cache = {}
    
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                airport_ident = row.get('airport_ident', '').strip().upper()
                if not airport_ident:
                    continue
                
                # Initialize airport runway list
                if airport_ident not in _runway_cache:
                    _runway_cache[airport_ident] = []
                
                # Parse runway data
                try:
                    runway = {
                        'le_ident': row.get('le_ident', '').strip(),
                        'he_ident': row.get('he_ident', '').strip(),
                        'le_heading': float(row['le_heading_degT']) if row.get('le_heading_degT') else None,
                        'he_heading': float(row['he_heading_degT']) if row.get('he_heading_degT') else None,
                        'length_ft': int(row['length_ft']) if row.get('length_ft') else None,
                        'width_ft': int(row['width_ft']) if row.get('width_ft') else None,
                        'surface': row.get('surface', 'UNK').strip(),
                        'lighted': row.get('lighted', '0') == '1',
                        'closed': row.get('closed', '0') == '1'
                    }
                    
                    # Only add if we have heading data
                    if runway['le_heading'] is not None and runway['he_heading'] is not None:
                        _runway_cache[airport_ident].append(runway)
                
                except (ValueError, KeyError) as e:
                    # Skip malformed entries
                    continue
        
        print(f"Loaded runway data for {len(_runway_cache)} airports")
        _cache_loaded = True
        
    except Exception as e:
        print(f"Error loading runway cache: {e}")
        _cache_loaded = True
        _runway_cache = {}
    
    return _runway_cache


def calculate_wind_components(wind_direction: float, wind_speed: float, 
                              runway_heading: float) -> Dict[str, float]:
    """
    Calculate headwind/tailwind and crosswind components
    
    Args:
        wind_direction: Wind direction in degrees (0-360)
        wind_speed: Wind speed in knots
        runway_heading: Runway magnetic heading in degrees (0-360)
    
    Returns:
        dict with 'headwind', 'crosswind', 'angle_difference'
    """
    # Calculate the angle between wind and runway
    angle_diff = wind_direction - runway_heading
    
    # Normalize to -180 to +180
    while angle_diff > 180:
        angle_diff -= 360
    while angle_diff < -180:
        angle_diff += 360
    
    # Convert to radians for trig
    angle_rad = math.radians(angle_diff)
    
    # Calculate components
    headwind = wind_speed * math.cos(angle_rad)  # Positive = headwind, negative = tailwind
    crosswind = abs(wind_speed * math.sin(angle_rad))  # Always positive
    
    return {
        'headwind': round(headwind, 1),  # Positive = headwind, negative = tailwind
        'tailwind': round(-headwind, 1) if headwind < 0 else 0,  # Positive tailwind value
        'crosswind': round(crosswind, 1),
        'angle_difference': round(angle_diff, 1)
    }


def analyze_runways_for_wind(station_id: str, wind_direction: Optional[float], 
                             wind_speed: Optional[float], 
                             wind_gust: Optional[float] = None) -> Dict:
    """
    Analyze all runways for given wind conditions and determine best runway
    
    Args:
        station_id: Airport identifier (e.g., 'KCOS')
        wind_direction: Wind direction in degrees (0-360), None for calm/variable
        wind_speed: Wind speed in knots
        wind_gust: Wind gust speed in knots (optional)
    
    Returns:
        dict with 'runways' list and 'best_runway'
    """
    station_id = station_id.upper()
    
    # Load runway cache if needed
    runway_cache = load_runway_cache()
    
    # Check if airport has runway data
    if station_id not in runway_cache:
        return {
            'station_id': station_id,
            'has_runway_data': False,
            'runways': [],
            'best_runway': None
        }
    
    # Handle calm or variable winds
    if wind_direction is None or wind_speed is None or wind_speed < 1:
        return {
            'station_id': station_id,
            'has_runway_data': True,
            'wind_calm': True,
            'runways': runway_cache[station_id],
            'best_runway': None,
            'message': 'Calm or variable winds - all runways usable'
        }
    
    # Analyze each runway
    analyzed_runways = []
    
    for runway in runway_cache[station_id]:
        # Skip closed runways
        if runway.get('closed', False):
            continue
        
        # Analyze both directions (low end and high end)
        for direction in ['le', 'he']:
            ident = runway[f'{direction}_ident']
            heading = runway[f'{direction}_heading']
            
            if not ident or heading is None:
                continue
            
            # Calculate wind components
            components = calculate_wind_components(wind_direction, wind_speed, heading)
            
            # Add gust analysis if present
            if wind_gust and wind_gust > wind_speed:
                gust_components = calculate_wind_components(wind_direction, wind_gust, heading)
                components['crosswind_gust'] = gust_components['crosswind']
                components['headwind_gust'] = gust_components['headwind']
            
            analyzed_runways.append({
                'ident': ident,
                'heading': heading,
                'length_ft': runway.get('length_ft'),
                'width_ft': runway.get('width_ft'),
                'surface': runway.get('surface'),
                'lighted': runway.get('lighted', False),
                **components
            })
    
    # Sort by best conditions (most headwind, least crosswind)
    # Priority: minimize crosswind, then maximize headwind
    analyzed_runways.sort(key=lambda x: (x['crosswind'], -x['headwind']))
    
    # Determine best runway
    best = None
    if analyzed_runways:
        best = analyzed_runways[0]
        
        # Add usability assessment
        crosswind_limit = 15  # Standard crosswind component limit (knots)
        tailwind_limit = 10   # Standard tailwind component limit (knots)
        
        best['usable'] = True
        best['cautions'] = []
        
        if best['crosswind'] > crosswind_limit:
            best['cautions'].append(f"Crosswind {best['crosswind']} kt exceeds {crosswind_limit} kt limit")
            best['usable'] = False
        
        if best['headwind'] < 0 and abs(best['headwind']) > tailwind_limit:
            best['cautions'].append(f"Tailwind {abs(best['headwind'])} kt exceeds {tailwind_limit} kt limit")
            best['usable'] = False
        
        # Check gust if present
        if 'crosswind_gust' in best and best['crosswind_gust'] > crosswind_limit:
            best['cautions'].append(f"Crosswind gust {best['crosswind_gust']} kt exceeds {crosswind_limit} kt limit")
    
    return {
        'station_id': station_id,
        'has_runway_data': True,
        'wind_calm': False,
        'wind_direction': wind_direction,
        'wind_speed': wind_speed,
        'wind_gust': wind_gust,
        'runways': analyzed_runways,
        'best_runway': best
    }


def format_runway_analysis(analysis: Dict) -> str:
    """
    Format runway analysis into human-readable text
    
    Args:
        analysis: Output from analyze_runways_for_wind()
    
    Returns:
        Formatted string for display
    """
    if not analysis.get('has_runway_data'):
        return "No runway data available"
    
    if analysis.get('wind_calm'):
        return "Winds calm - all runways usable"
    
    best = analysis.get('best_runway')
    if not best:
        return "No suitable runway found"
    
    # Format the recommendation
    lines = []
    lines.append(f"Best Runway: {best['ident']}")
    
    if best['headwind'] >= 0:
        lines.append(f"  Headwind: {best['headwind']} kt")
    else:
        lines.append(f"  Tailwind: {abs(best['headwind'])} kt")
    
    lines.append(f"  Crosswind: {best['crosswind']} kt")
    
    if 'crosswind_gust' in best:
        lines.append(f"  Crosswind (gust): {best['crosswind_gust']} kt")
    
    if best.get('length_ft'):
        lines.append(f"  Length: {best['length_ft']:,} ft")
    
    if best.get('surface'):
        lines.append(f"  Surface: {best['surface']}")
    
    # Add cautions
    if best.get('cautions'):
        lines.append("  ⚠️ CAUTIONS:")
        for caution in best['cautions']:
            lines.append(f"    • {caution}")
    
    return "\n".join(lines)


def format_runway_analysis_html(analysis: Dict) -> str:
    """
    Format runway analysis into HTML for display
    
    Args:
        analysis: Output from analyze_runways_for_wind()
    
    Returns:
        HTML string for display
    """
    if not analysis.get('has_runway_data'):
        return '<span style="color: #888;">No runway data available</span>'
    
    if analysis.get('wind_calm'):
        return '<span style="color: #00AA00;">Winds calm - all runways usable</span>'
    
    best = analysis.get('best_runway')
    if not best:
        return '<span style="color: #FF0000;">No suitable runway found</span>'
    
    # Build HTML
    html = []
    
    # Runway identifier
    html.append(f'<div style="font-weight: bold; font-size: 14px; margin-bottom: 5px;">Best Runway: {best["ident"]}</div>')
    
    # Wind components
    if best['headwind'] >= 0:
        html.append(f'<div style="color: #00AA00;">▲ Headwind: {best["headwind"]} kt</div>')
    else:
        html.append(f'<div style="color: #FF9900;">▼ Tailwind: {abs(best["headwind"])} kt</div>')
    
    # Crosswind color coding
    xwind_color = '#00AA00' if best['crosswind'] < 10 else ('#FF9900' if best['crosswind'] < 15 else '#FF0000')
    html.append(f'<div style="color: {xwind_color};">↔ Crosswind: {best["crosswind"]} kt</div>')
    
    # Gust if present
    if 'crosswind_gust' in best:
        gust_color = '#00AA00' if best['crosswind_gust'] < 10 else ('#FF9900' if best['crosswind_gust'] < 15 else '#FF0000')
        html.append(f'<div style="color: {gust_color};">↔ Crosswind (gust): {best["crosswind_gust"]} kt</div>')
    
    # Runway details
    if best.get('length_ft'):
        html.append(f'<div style="font-size: 11px; color: #666; margin-top: 5px;">{best["length_ft"]:,} ft × {best.get("width_ft", "?")} ft • {best.get("surface", "UNK")}</div>')
    
    # Cautions
    if best.get('cautions'):
        html.append('<div style="margin-top: 8px; padding: 5px; background: #FFF3CD; border-left: 3px solid #FF9900; font-size: 11px;">')
        html.append('<strong>⚠️ CAUTIONS:</strong><br>')
        for caution in best['cautions']:
            html.append(f'• {caution}<br>')
        html.append('</div>')
    
    return ''.join(html)


# Preload cache on module import
load_runway_cache()
