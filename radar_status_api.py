"""
radar_status_api.py — Radar Status API Blueprint
Location: /var/www/cap_winds_app/radar_status_api.py  (r815)

Registered in app.py:
    from radar_status_api import radar_status_bp
    app.register_blueprint(radar_status_bp)

Endpoints:
    GET /CAP_WxCOP/api/radar/sites          — all sites with current status
    GET /CAP_WxCOP/api/radar/status/<site>  — single site status + history
"""

import logging
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

radar_status_bp = Blueprint('radar_status', __name__)

DB_DSN = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'

# A site is considered STALE if no FTM received in this many hours.
# Most sites issue at least one FTM per 12-hour period.
STALE_HOURS = 12


def get_conn():
    return psycopg2.connect(DB_DSN)


def _status_color(status, last_update):
    """
    Derive marker color from status and data age.
    green   — OPERATIONAL and current
    yellow  — MAINTENANCE or TEST
    red     — FAILED
    gray    — no status data or stale (> STALE_HOURS since last FTM)
    """
    if last_update is None:
        return 'gray'
    age = datetime.now(timezone.utc) - last_update
    if age > timedelta(hours=STALE_HOURS):
        return 'gray'
    mapping = {
        'OPERATIONAL': 'green',
        'MAINTENANCE':  'yellow',
        'TEST':         'yellow',
        'FAILED':       'red',
        'UNKNOWN':      'gray',
    }
    return mapping.get(status, 'gray')


def _age_str(last_update):
    """Human-readable age string for popup display."""
    if last_update is None:
        return 'Never'
    age = datetime.now(timezone.utc) - last_update
    mins  = int(age.total_seconds() / 60)
    hours = mins // 60
    if mins < 2:
        return 'Just now'
    if mins < 60:
        return f'{mins}m ago'
    if hours < 24:
        return f'{hours}h {mins % 60}m ago'
    return f'{age.days}d ago'


@radar_status_bp.route('/sites')
def get_all_sites():
    """
    Return all radar sites with geometry and current status.
    Sites with no status record are included (status=None, color=gray).
    Used by the frontend to build the map overlay.
    """
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT
                s.site_id,
                s.name,
                s.state,
                s.lat,
                s.lon,
                s.elevation_m,
                r.status,
                r.vcp_mode,
                r.vcp_description,
                r.operation_mode,
                r.last_update,
                r.message
            FROM radar.radar_sites s
            LEFT JOIN radar.radar_status r USING (site_id)
            ORDER BY s.site_id
        """)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        sites = []
        for row in rows:
            lu = row['last_update']
            sites.append({
                'site_id':         row['site_id'],
                'name':            row['name'],
                'state':           row['state'],
                'lat':             row['lat'],
                'lon':             row['lon'],
                'elevation_m':     row['elevation_m'],
                'status':          row['status'] or 'UNKNOWN',
                'vcp_mode':        row['vcp_mode'],
                'vcp_description': row['vcp_description'],
                'operation_mode':  row['operation_mode'],
                'last_update':     lu.isoformat() if lu else None,
                'age_str':         _age_str(lu),
                'color':           _status_color(row['status'], lu),
                'message':         row['message'],
            })

        return jsonify({'count': len(sites), 'sites': sites})

    except Exception as e:
        log.exception('radar/sites failed')
        return jsonify({'error': str(e)}), 500


@radar_status_bp.route('/status/<site_id>')
def get_site_status(site_id):
    """
    Return current status + recent history for one site.
    site_id is 3-char (MPX, VWX, TLX).
    Optional query param: ?history=N  (default 20, max 100)
    """
    site_id = site_id.upper()
    limit   = min(int(request.args.get('history', 20)), 100)

    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Current status joined to site geometry
        cur.execute("""
            SELECT
                s.site_id, s.name, s.state, s.lat, s.lon, s.elevation_m,
                r.status, r.vcp_mode, r.vcp_description,
                r.operation_mode, r.last_update, r.message
            FROM radar.radar_sites s
            LEFT JOIN radar.radar_status r USING (site_id)
            WHERE s.site_id = %s
        """, (site_id,))

        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({'error': f'Unknown site: {site_id}'}), 404

        lu = row['last_update']
        result = {
            'site_id':         row['site_id'],
            'name':            row['name'],
            'state':           row['state'],
            'lat':             row['lat'],
            'lon':             row['lon'],
            'elevation_m':     row['elevation_m'],
            'status':          row['status'] or 'UNKNOWN',
            'vcp_mode':        row['vcp_mode'],
            'vcp_description': row['vcp_description'],
            'operation_mode':  row['operation_mode'],
            'last_update':     lu.isoformat() if lu else None,
            'age_str':         _age_str(lu),
            'color':           _status_color(row['status'], lu),
            'message':         row['message'],
        }

        # Recent history
        cur.execute("""
            SELECT site_id, status, vcp_mode, operation_mode, ts, message
            FROM radar.status_history
            WHERE site_id = %s
            ORDER BY ts DESC
            LIMIT %s
        """, (site_id, limit))

        history = []
        for h in cur.fetchall():
            history.append({
                'status':         h['status'],
                'vcp_mode':       h['vcp_mode'],
                'operation_mode': h['operation_mode'],
                'ts':             h['ts'].isoformat() if h['ts'] else None,
                'message':        h['message'],
            })

        result['history'] = history
        cur.close()
        conn.close()
        return jsonify(result)

    except Exception as e:
        log.exception('radar/status/%s failed', site_id)
        return jsonify({'error': str(e)}), 500
