#!/home/ldm/venv/bin/python3
"""
Radar Status API Endpoint
Location: /var/www/cap_winds_app/api/radar_status.py

Flask API to serve radar operational status
"""

from flask import Flask, jsonify, request
import sqlite3
from datetime import datetime, timedelta

app = Flask(__name__)

DB_PATH = "/var/www/cap_winds_app/data/radar_status.db"


def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/api/radar/status/<site_id>', methods=['GET'])
def get_radar_status(site_id):
    """Get current status for specific radar site"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT site_id, status, vcp_mode, vcp_description, 
                   operation_mode, last_update, message
            FROM radar_status
            WHERE site_id = ?
        ''', (site_id.upper(),))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return jsonify({
                'site_id': site_id,
                'status': 'UNKNOWN',
                'operational': False,
                'message': 'No status data available'
            }), 404
        
        return jsonify({
            'site_id': row['site_id'],
            'status': row['status'],
            'operational': row['status'] == 'OPERATIONAL',
            'vcp_mode': row['vcp_mode'],
            'vcp_description': row['vcp_description'],
            'operation_mode': row['operation_mode'],
            'last_update': row['last_update'],
            'display_status': get_display_status(row['status']),
            'icon_color': get_icon_color(row['status'])
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/radar/status', methods=['GET'])
def get_all_radar_status():
    """Get status for all radar sites"""
    try:
        # Optional: filter by region
        region = request.args.get('region')
        
        # Optional: only failed/non-operational
        failed_only = request.args.get('failed_only', 'false').lower() == 'true'
        
        conn = get_db()
        cursor = conn.cursor()
        
        query = 'SELECT * FROM radar_status'
        params = []
        
        if failed_only:
            query += ' WHERE status != ?'
            params.append('OPERATIONAL')
        
        query += ' ORDER BY site_id'
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        sites = []
        for row in rows:
            sites.append({
                'site_id': row['site_id'],
                'status': row['status'],
                'operational': row['status'] == 'OPERATIONAL',
                'vcp_mode': row['vcp_mode'],
                'vcp_description': row['vcp_description'],
                'operation_mode': row['operation_mode'],
                'last_update': row['last_update'],
                'display_status': get_display_status(row['status']),
                'icon_color': get_icon_color(row['status'])
            })
        
        return jsonify({
            'count': len(sites),
            'sites': sites
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/radar/status/<site_id>/history', methods=['GET'])
def get_radar_status_history(site_id):
    """Get status history for a site"""
    try:
        # Optional: limit number of records
        limit = int(request.args.get('limit', 100))
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT site_id, status, vcp_mode, operation_mode, timestamp, message
            FROM status_history
            WHERE site_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (site_id.upper(), limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for row in rows:
            history.append({
                'site_id': row['site_id'],
                'status': row['status'],
                'vcp_mode': row['vcp_mode'],
                'operation_mode': row['operation_mode'],
                'timestamp': row['timestamp'],
                'message': row['message']
            })
        
        return jsonify({
            'site_id': site_id,
            'count': len(history),
            'history': history
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def get_display_status(status):
    """Convert status to display string"""
    display_map = {
        'OPERATIONAL': 'Operational',
        'FAILED': 'Failed',
        'MAINTENANCE': 'Maintenance',
        'TEST': 'Test Mode',
        'OFFLINE': 'Offline',
        'UNKNOWN': 'Unknown'
    }
    return display_map.get(status, status)


def get_icon_color(status):
    """Get icon color for status"""
    color_map = {
        'OPERATIONAL': 'green',
        'FAILED': 'red',
        'MAINTENANCE': 'orange',
        'TEST': 'yellow',
        'OFFLINE': 'gray',
        'UNKNOWN': 'gray'
    }
    return color_map.get(status, 'gray')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
