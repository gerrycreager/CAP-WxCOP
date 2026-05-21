#!/usr/bin/env python3
"""
radar_status_api.py — Radar Status API Blueprint
Serves NEXRAD site status from PostGIS (avwx_data on data2).
Replaces old SQLite-based version.

Endpoints:
  GET /api/radar/sites          — all sites with status + latency
  GET /api/radar/status/<site>  — single site detail
  GET /api/radar/summary        — counts by color tier
"""
import logging
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
radar_status_bp = Blueprint('radar_status', __name__)
DB_DSN = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'


def get_conn():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def latency_color(latency, ftm_status=None):
    if ftm_status and ftm_status.upper() in ('OFFLINE', 'MAINTENANCE'):
        return 'red'
    if latency is None:
        return 'red'
    if latency < 10:
        return 'green'
    if latency < 15:
        return 'blue'
    if latency < 60:
        return 'yellow'
    return 'red'


def format_site(row):
    lat = row.get('lat')
    lon = row.get('lon')
    latency = row.get('latency_minutes')
    ftm_status = row.get('ftm_status')
    color = latency_color(latency, ftm_status)
    last_l3 = row.get('last_l3_time')
    ftm_time = row.get('ftm_time')
    return {
        'site_id':       row['site_id'],
        'name':          row.get('name', ''),
        'state':         row.get('state', ''),
        'lat':           float(lat) if lat else None,
        'lon':           float(lon) if lon else None,
        'status':        row.get('status', 'UNKNOWN'),
        'latency_min':   latency,
        'color':         color,
        'vcp_mode':      row.get('vcp_mode'),
        'vcp_desc':      row.get('vcp_description'),
        'ftm_status':    ftm_status,
        'ftm_message':   row.get('ftm_message'),
        'ftm_time':      ftm_time.isoformat() if ftm_time else None,
        'last_l3_time':  last_l3.isoformat() if last_l3 else None,
        'last_update':   row['last_update'].isoformat() if row.get('last_update') else None,
    }


@radar_status_bp.route('/sites')
def get_all_sites():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT s.site_id, s.name, s.state, s.lat, s.lon,
                   r.status, r.vcp_mode, r.vcp_description,
                   r.latency_minutes, r.last_l3_time,
                   r.ftm_status, r.ftm_message, r.ftm_time,
                   r.last_update
            FROM radar.radar_sites s
            LEFT JOIN radar.radar_status r USING (site_id)
            ORDER BY s.site_id
        """)
        rows = cur.fetchall()
        conn.close()
        sites = [format_site(r) for r in rows]
        return jsonify({
            'count':      len(sites),
            'generated':  datetime.now(timezone.utc).isoformat(),
            'sites':      sites,
        })
    except Exception as e:
        log.error(f'get_all_sites error: {e}')
        return jsonify({'error': str(e)}), 500


@radar_status_bp.route('/status/<site_id>')
def get_site_status(site_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT s.site_id, s.name, s.state, s.lat, s.lon,
                   r.status, r.vcp_mode, r.vcp_description,
                   r.latency_minutes, r.last_l3_time,
                   r.ftm_status, r.ftm_message, r.ftm_time,
                   r.last_update, r.message
            FROM radar.radar_sites s
            LEFT JOIN radar.radar_status r USING (site_id)
            WHERE s.site_id = %s
        """, (site_id.upper(),))
        row = cur.fetchone()

        # History
        cur.execute("""
            SELECT status, vcp_mode, operation_mode, timestamp, message
            FROM radar.status_history
            WHERE site_id = %s
            ORDER BY timestamp DESC LIMIT 20
        """, (site_id.upper(),))
        history = [dict(r) for r in cur.fetchall()]
        conn.close()

        if not row:
            return jsonify({'error': f'Site {site_id} not found'}), 404

        result = format_site(row)
        result['history'] = [
            {**h,
             'timestamp': h['timestamp'].isoformat() if h.get('timestamp') else None}
            for h in history
        ]
        return jsonify(result)
    except Exception as e:
        log.error(f'get_site_status error: {e}')
        return jsonify({'error': str(e)}), 500


@radar_status_bp.route('/summary')
def get_summary():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT s.site_id, r.latency_minutes, r.ftm_status
            FROM radar.radar_sites s
            LEFT JOIN radar.radar_status r USING (site_id)
        """)
        rows = cur.fetchall()
        conn.close()
        counts = {'green': 0, 'blue': 0, 'yellow': 0, 'red': 0, 'unknown': 0}
        for r in rows:
            c = latency_color(r['latency_minutes'], r['ftm_status'])
            counts[c] = counts.get(c, 0) + 1
        return jsonify({
            'total':     len(rows),
            'counts':    counts,
            'generated': datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
