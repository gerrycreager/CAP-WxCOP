"""
Manual TAF Entry System for KQ Stations
Allows manual input of TAF text for USAF weather sites not in LDM feed.
"""

from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from datetime import datetime
import re
import sys
import os

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

manual_taf = Blueprint('manual_taf', __name__)

def parse_taf_header(taf_text):
    """
    Extract basic TAF information from raw text.
    Returns: (station_id, issue_time, valid_from, valid_to)
    """
    # Clean up the TAF text
    taf_text = taf_text.strip().upper()
    
    # Basic TAF pattern: TAF KQXY 171200Z 171212/181212 ...
    # Or: TAF AMD KQXY 171200Z 171212/181212 ...
    header_pattern = r'TAF\s+(?:AMD\s+|COR\s+)?([A-Z0-9]{4})\s+(\d{6}Z)\s+(\d{4,6})/(\d{4,6})'
    match = re.search(header_pattern, taf_text)
    
    if not match:
        raise ValueError("Invalid TAF format - cannot parse header")
    
    station_id = match.group(1)
    issue_dttm = match.group(2)  # DDHHMMZ
    valid_from_dt = match.group(3)  # DDHH
    valid_to_dt = match.group(4)    # DDHH
    
    # Parse issue time
    day = int(issue_dttm[:2])
    hour = int(issue_dttm[2:4])
    minute = int(issue_dttm[4:6])
    
    # Assume current month/year for simplicity
    now = datetime.utcnow()
    issue_time = datetime(now.year, now.month, day, hour, minute)
    
    # Parse validity period
    vf_day = int(valid_from_dt[:2])
    vf_hour = int(valid_from_dt[2:4])
    vt_day = int(valid_to_dt[:2])
    vt_hour = int(valid_to_dt[2:4])
    
    valid_from = datetime(now.year, now.month, vf_day, vf_hour, 0)
    valid_to = datetime(now.year, now.month, vt_day, vt_hour, 0)
    
    # Handle month rollover
    if vt_day < vf_day:
        if now.month == 12:
            valid_to = valid_to.replace(year=now.year + 1, month=1)
        else:
            valid_to = valid_to.replace(month=now.month + 1)
    
    return station_id, issue_time, valid_from, valid_to


@manual_taf.route('/manual-taf')
def manual_taf_form():
    """Display the manual TAF entry form."""
    
    # Get list of KQ stations from database
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT station_id, name, 
               ST_Y(location) as lat, ST_X(location) as lon
        FROM observations.airports 
        WHERE station_id LIKE 'KQ%' 
        ORDER BY station_id
    """)
    kq_stations = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('manual_taf.html', kq_stations=kq_stations)


@manual_taf.route('/manual-taf/submit', methods=['POST'])
def submit_manual_taf():
    """Process manual TAF submission."""
    
    try:
        taf_text = request.form.get('taf_text', '').strip()
        if not taf_text:
            return jsonify({'error': 'TAF text is required'}), 400
        
        # Parse the TAF
        station_id, issue_time, valid_from, valid_to = parse_taf_header(taf_text)
        
        # Validate station is KQ
        if not station_id.startswith('KQ'):
            return jsonify({'error': f'Station {station_id} is not a KQ station'}), 400
        
        # Check if station exists in database
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT station_id, name FROM observations.airports 
            WHERE station_id = %s
        """, (station_id,))
        station_info = cur.fetchone()
        
        if not station_info:
            cur.close()
            conn.close()
            return jsonify({
                'error': f'Station {station_id} not found in database. Add it via KQ Station Management first.',
                'redirect': '/CAP_WxCOP/admin/kq-stations'
            }), 400
        
        # Insert TAF into database
        cur.execute("""
            INSERT INTO observations.taf 
                (station_id, issue_time, valid_from, valid_to, raw_text, location)
            VALUES (%s, %s, %s, %s, %s, 
                    (SELECT location FROM observations.airports WHERE station_id = %s))
            ON CONFLICT (station_id, issue_time) 
            DO UPDATE SET
                valid_from = EXCLUDED.valid_from,
                valid_to = EXCLUDED.valid_to,
                raw_text = EXCLUDED.raw_text,
                location = EXCLUDED.location
        """, (station_id, issue_time, valid_from, valid_to, taf_text, station_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'TAF for {station_id} ({station_info[1]}) saved successfully',
            'details': {
                'station_id': station_id,
                'station_name': station_info[1],
                'issue_time': issue_time.strftime('%Y-%m-%d %H:%MZ'),
                'valid_from': valid_from.strftime('%Y-%m-%d %H:%MZ'),
                'valid_to': valid_to.strftime('%Y-%m-%d %H:%MZ')
            }
        })
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500


@manual_taf.route('/manual-taf/recent')
def recent_manual_tafs():
    """Show recently entered manual TAFs."""
    
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT t.station_id, a.name, t.issue_time, t.valid_from, t.valid_to, 
               LEFT(t.raw_text, 100) as preview
        FROM observations.taf t
        JOIN observations.airports a ON t.station_id = a.station_id
        WHERE t.station_id LIKE 'KQ%'
        ORDER BY t.issue_time DESC
        LIMIT 20
    """)
    
    recent_tafs = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('recent_manual_tafs.html', recent_tafs=recent_tafs)

