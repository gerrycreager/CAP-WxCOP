"""
TAF Decoder Utility
Converts raw TAF text into human-readable format
"""
import re
from datetime import datetime, timedelta

def decode_taf(raw_taf):
    """
    Decode a TAF into human-readable sections
    
    Returns:
        dict with 'header', 'valid_period', and 'forecasts' list
    """
    if not raw_taf:
        return None
    
    lines = raw_taf.strip().split('\n')
    decoded = {
        'raw': raw_taf,
        'station': None,
        'issue_time': None,
        'valid_period': None,
        'forecasts': []
    }
    
    # Parse header line
    header_line = lines[0]
    
    # Extract station (4 letters after TAF or TAF AMD/COR)
    station_match = re.search(r'TAF\s+(?:AMD\s+|COR\s+)?([A-Z]{4})', header_line)
    if station_match:
        decoded['station'] = station_match.group(1)
    
    # Extract issue time (DDHHMM)
    issue_match = re.search(r'(\d{6})Z', header_line)
    if issue_match:
        decoded['issue_time'] = format_taf_time(issue_match.group(1))
    
    # Extract valid period (DDHH/DDHH)
    valid_match = re.search(r'(\d{4})/(\d{4})', header_line)
    if valid_match:
        from_time = format_taf_time(valid_match.group(1) + '00')
        to_time = format_taf_time(valid_match.group(2) + '00')
        decoded['valid_period'] = f"{from_time} to {to_time}"
    
    # Join all lines and split by forecast groups
    full_text = ' '.join(lines)
    
    # Split by FM, TEMPO, BECMG, PROB
    sections = re.split(r'\s+(FM\d{6}|TEMPO|BECMG|PROB\d{2})', full_text)
    
    # Process sections
    current_forecast = None
    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue
        
        # Check if this is a change indicator
        if section.startswith('FM'):
            # FROM group - starts a new forecast period
            time_str = section[2:]  # Remove 'FM'
            current_forecast = {
                'type': 'FROM',
                'time': format_taf_time(time_str),
                'conditions': []
            }
            decoded['forecasts'].append(current_forecast)
        
        elif section.startswith('TEMPO'):
            current_forecast = {
                'type': 'TEMPORARY',
                'time': None,
                'conditions': []
            }
            decoded['forecasts'].append(current_forecast)
        
        elif section.startswith('BECMG'):
            current_forecast = {
                'type': 'BECOMING',
                'time': None,
                'conditions': []
            }
            decoded['forecasts'].append(current_forecast)
        
        elif section.startswith('PROB'):
            prob = section[4:6]
            current_forecast = {
                'type': f'PROBABILITY {prob}%',
                'time': None,
                'conditions': []
            }
            decoded['forecasts'].append(current_forecast)
        
        else:
            # This is forecast content
            if current_forecast is None:
                # Initial forecast (before any FM/TEMPO/BECMG)
                current_forecast = {
                    'type': 'INITIAL',
                    'time': None,
                    'conditions': []
                }
                decoded['forecasts'].append(current_forecast)
            
            # Parse the forecast conditions
            conditions = decode_forecast_line(section)
            current_forecast['conditions'] = conditions
    
    return decoded

def format_taf_time(time_str):
    """
    Format TAF time string (DDHHMM or DDHH) into readable format
    Returns: DDHHMMz format
    """
    if len(time_str) == 6:
        return f"{time_str[0:2]}/{time_str[2:4]}{time_str[4:6]}Z"
    elif len(time_str) == 4:
        return f"{time_str[0:2]}/{time_str[2:4]}00Z"
    return time_str

def decode_forecast_line(line):
    """
    Decode a forecast line into components
    """
    conditions = []
    tokens = line.split()
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        # Wind (e.g., 25010KT, 27012G22KT, VRB05KT)
        wind_match = re.match(r'(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT', token)
        if wind_match:
            dir_part = wind_match.group(1)
            spd = wind_match.group(2)
            gust = wind_match.group(3)
            
            if dir_part == 'VRB':
                wind_text = f"Wind: Variable at {spd} knots"
            else:
                wind_text = f"Wind: {dir_part}° at {spd} knots"
            
            if gust:
                wind_text += f" gusting to {gust[1:]} knots"
            
            conditions.append(wind_text)
            i += 1
            continue
        
        # Visibility (e.g., 10SM, 6SM, 3/4SM, P6SM)
        if 'SM' in token:
            vis = token.replace('SM', '').replace('P', '>')
            conditions.append(f"Visibility: {vis} statute miles")
            i += 1
            continue
        
        # Sky conditions (e.g., SKC, CLR, FEW015, SCT020, BKN030, OVC050)
        sky_match = re.match(r'(SKC|CLR|FEW|SCT|BKN|OVC)(\d{3})?', token)
        if sky_match:
            cover = sky_match.group(1)
            height = sky_match.group(2)
            
            cover_names = {
                'SKC': 'Sky clear',
                'CLR': 'Clear',
                'FEW': 'Few clouds',
                'SCT': 'Scattered clouds',
                'BKN': 'Broken clouds',
                'OVC': 'Overcast'
            }
            
            sky_text = cover_names.get(cover, cover)
            if height:
                sky_text += f" at {int(height) * 100} feet"
            
            conditions.append(sky_text)
            i += 1
            continue
        
        # Weather phenomena (e.g., -RA, +TSRA, VCSH, BR, FG)
        wx_match = re.match(r'^[+-]?[A-Z]{2,}$', token)
        if wx_match and not token.startswith('FM') and token not in ['TEMPO', 'BECMG', 'RMK']:
            wx_decoded = decode_weather_phenomenon(token)
            if wx_decoded:
                conditions.append(f"Weather: {wx_decoded}")
            i += 1
            continue
        
        # Skip RMK and everything after
        if token == 'RMK':
            break
        
        i += 1
    
    return conditions

def decode_weather_phenomenon(wx_code):
    """
    Decode weather phenomenon codes
    """
    intensity = ''
    if wx_code.startswith('+'):
        intensity = 'Heavy '
        wx_code = wx_code[1:]
    elif wx_code.startswith('-'):
        intensity = 'Light '
        wx_code = wx_code[1:]
    
    # Common weather codes
    wx_types = {
        'RA': 'rain',
        'SN': 'snow',
        'DZ': 'drizzle',
        'FG': 'fog',
        'BR': 'mist',
        'HZ': 'haze',
        'TS': 'thunderstorm',
        'TSRA': 'thunderstorm with rain',
        'SHRA': 'rain showers',
        'SHSN': 'snow showers',
        'FZ': 'freezing',
        'FZRA': 'freezing rain',
        'PL': 'ice pellets',
        'GR': 'hail',
        'SQ': 'squalls',
        'FC': 'funnel cloud',
        'VA': 'volcanic ash',
        'DU': 'dust',
        'SA': 'sand',
        'SS': 'sandstorm',
        'DS': 'duststorm',
        'PO': 'dust devils',
        'VCSH': 'showers in vicinity',
        'VCTS': 'thunderstorm in vicinity'
    }
    
    return intensity + wx_types.get(wx_code, wx_code)

def format_taf_for_display(decoded_taf):
    """
    Format decoded TAF into HTML for display
    """
    if not decoded_taf:
        return ""
    
    html = []
    
    for forecast in decoded_taf['forecasts']:
        # Forecast type and time
        if forecast['type'] == 'INITIAL':
            html.append("<strong>Initial Forecast:</strong>")
        elif forecast['type'] == 'FROM':
            html.append(f"<strong>From {forecast['time']}:</strong>")
        elif forecast['type'] == 'TEMPORARY':
            html.append("<strong>Temporary conditions:</strong>")
        elif forecast['type'] == 'BECOMING':
            html.append("<strong>Becoming:</strong>")
        else:
            html.append(f"<strong>{forecast['type']}:</strong>")
        
        # Conditions
        if forecast['conditions']:
            for condition in forecast['conditions']:
                html.append(f"&nbsp;&nbsp;• {condition}")
        
        html.append("")  # Blank line between forecasts
    
    return "<br>".join(html)
