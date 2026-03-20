#!/usr/bin/env python3
"""
CAP Winds Flask Application - Complete Example with TAF Endpoints

This is a COMPLETE Flask app example showing where to add TAF endpoints.
Use this as a reference to integrate into your existing app.py.

The TAF endpoints are marked with comments so you can easily find and copy them.
"""

from flask import Flask, render_template, jsonify, request
import psycopg2
from datetime import datetime
import os

app = Flask(__name__)

# =============================================================================
# DATABASE CONFIGURATION
# =============================================================================

DB_CONFIG = {
    'dbname': 'avwx_data',
    'user': 'avwx_user',
    'host': '192.168.0.60'
}

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(**DB_CONFIG)


# =============================================================================
# EXISTING ROUTES (Your existing routes stay here)
# =============================================================================

@app.route('/')
def index():
    """Homepage"""
    return render_template('index.html')


@app.route('/station/<station_id>')
def station_detail(station_id):
    """
    Station detail page
    
    This is where the TAF component will be displayed.
    Pass station_id to the template so the TAF component can use it.
    """
    station_id = station_id.upper()
    
    # Your existing station data retrieval code here...
    # For example:
    # station_data = get_station_data(station_id)
    
    return render_template(
        'station.html',
        station_id=station_id
        # Your other template variables...
    )


# =============================================================================
# TAF API ENDPOINTS - START
# =============================================================================

@app.route('/api/taf/<station_id>')
def get_taf(station_id):
    """
    Get current TAF for a station
    
    Returns:
        JSON with TAF data or error message
        
    Example:
        GET /api/taf/KMCO
        
    Response (success):
        {
          "station_id": "KMCO",
          "issue_time": "2026-01-19T14:00:00",
          "valid_from": "2026-01-19T15:00:00",
          "valid_to": "2026-01-20T15:00:00",
          "raw_text": "TAF KMCO 191400Z 1915/2015 ...",
          "created_at": "2026-01-19T14:05:00",
          "age_minutes": 25
        }
        
    Response (not found):
        {
          "error": "No TAF found",
          "message": "No TAF available for station KMCO",
          "station_id": "KMCO"
        }
    """
    try:
        # Validate station ID (4 letter ICAO code)
        station_id = station_id.upper().strip()
        if len(station_id) != 4 or not station_id.isalpha():
            return jsonify({
                'error': 'Invalid station ID',
                'message': 'Station ID must be 4 letters'
            }), 400
        
        # Connect to database
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get most recent TAF for station
        cur.execute("""
            SELECT 
                station_id,
                issue_time,
                valid_from,
                valid_to,
                raw_text,
                created_at
            FROM observations.taf
            WHERE station_id = %s
            ORDER BY issue_time DESC
            LIMIT 1
        """, (station_id,))
        
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if not result:
            return jsonify({
                'error': 'No TAF found',
                'message': f'No TAF available for station {station_id}',
                'station_id': station_id
            }), 404
        
        # Format response
        taf_data = {
            'station_id': result[0],
            'issue_time': result[1].isoformat() if result[1] else None,
            'valid_from': result[2].isoformat() if result[2] else None,
            'valid_to': result[3].isoformat() if result[3] else None,
            'raw_text': result[4],
            'created_at': result[5].isoformat() if result[5] else None,
            'age_minutes': int((datetime.now() - result[1]).total_seconds() / 60) if result[1] else None
        }
        
        return jsonify(taf_data)
        
    except psycopg2.Error as e:
        return jsonify({
            'error': 'Database error',
            'message': str(e)
        }), 500
    except Exception as e:
        return jsonify({
            'error': 'Server error',
            'message': str(e)
        }), 500


@app.route('/api/taf/<station_id>/all')
def get_all_tafs(station_id):
    """
    Get all recent TAFs for a station (last 24 hours)
    
    Returns:
        JSON array of TAFs
        
    Example:
        GET /api/taf/KMCO/all
        
    Response:
        {
          "station_id": "KMCO",
          "count": 4,
          "tafs": [
            {
              "station_id": "KMCO",
              "issue_time": "2026-01-19T14:00:00",
              "valid_from": "2026-01-19T15:00:00",
              "valid_to": "2026-01-20T15:00:00",
              "raw_text": "TAF KMCO 191400Z ...",
              "created_at": "2026-01-19T14:05:00"
            },
            ...
          ]
        }
    """
    try:
        station_id = station_id.upper().strip()
        if len(station_id) != 4 or not station_id.isalpha():
            return jsonify({
                'error': 'Invalid station ID'
            }), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get all TAFs from last 24 hours
        cur.execute("""
            SELECT 
                station_id,
                issue_time,
                valid_from,
                valid_to,
                raw_text,
                created_at
            FROM observations.taf
            WHERE station_id = %s
            AND issue_time > NOW() - INTERVAL '24 hours'
            ORDER BY issue_time DESC
        """, (station_id,))
        
        results = cur.fetchall()
        cur.close()
        conn.close()
        
        if not results:
            return jsonify({
                'error': 'No TAFs found',
                'station_id': station_id,
                'tafs': []
            }), 404
        
        tafs = []
        for row in results:
            tafs.append({
                'station_id': row[0],
                'issue_time': row[1].isoformat() if row[1] else None,
                'valid_from': row[2].isoformat() if row[2] else None,
                'valid_to': row[3].isoformat() if row[3] else None,
                'raw_text': row[4],
                'created_at': row[5].isoformat() if row[5] else None
            })
        
        return jsonify({
            'station_id': station_id,
            'count': len(tafs),
            'tafs': tafs
        })
        
    except Exception as e:
        return jsonify({
            'error': 'Server error',
            'message': str(e)
        }), 500


# TAF API ENDPOINTS - END
# =============================================================================


# =============================================================================
# YOUR OTHER ROUTES CONTINUE HERE
# =============================================================================

# Add your other routes...


# =============================================================================
# APPLICATION STARTUP
# =============================================================================

if __name__ == '__main__':
    # Development server
    app.run(host='0.0.0.0', port=5000, debug=True)
