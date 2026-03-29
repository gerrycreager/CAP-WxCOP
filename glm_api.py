"""
glm_api.py — CAP WxCOP GLM Lightning API Blueprint
====================================================
Provides GeoJSON endpoints for GLM flash display on the weather map.

Endpoints:
  GET /api/glm/flashes?minutes=30&satellite=ALL
      Returns recent GLM flashes as GeoJSON FeatureCollection.
      Parameters:
        minutes   : lookback window 1–120 (default 30)
        satellite : G19 | G18 | ALL (default ALL)

  GET /api/glm/status
      Returns flash counts and latest flash time per satellite.
      Used by the map status bar to show data freshness.
"""

import logging
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

glm_bp = Blueprint('glm', __name__)

DB_DSN = "dbname=avwx_data user=avwx_user host=192.168.0.60"

def get_db():
    return psycopg2.connect(DB_DSN)

# ---------------------------------------------------------------------------
# GET /api/glm/flashes
# ---------------------------------------------------------------------------

@glm_bp.route('/flashes')
def glm_flashes():
    try:
        minutes   = min(max(int(request.args.get('minutes', 30)), 1), 120)
        satellite = request.args.get('satellite', 'ALL').upper()
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid parameters'}), 400

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if satellite == 'ALL':
                cur.execute("""
                    SELECT satellite, flash_id, flash_time, lat, lon,
                           energy, area
                    FROM observations.glm_flashes
                    WHERE flash_time >= %s
                    ORDER BY flash_time DESC
                    LIMIT 50000
                """, (cutoff,))
            else:
                cur.execute("""
                    SELECT satellite, flash_id, flash_time, lat, lon,
                           energy, area
                    FROM observations.glm_flashes
                    WHERE flash_time >= %s
                      AND satellite = %s
                    ORDER BY flash_time DESC
                    LIMIT 50000
                """, (cutoff, satellite))
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.error(f"GLM flashes query failed: {e}")
        return jsonify({'error': 'Database error'}), 500

    now = datetime.now(timezone.utc)

    features = []
    for r in rows:
        ft = r['flash_time']
        if ft.tzinfo is None:
            ft = ft.replace(tzinfo=timezone.utc)
        age_s = int((now - ft).total_seconds())

        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [float(r['lon']), float(r['lat'])]
            },
            'properties': {
                'satellite':  r['satellite'],
                'flash_time': ft.isoformat(),
                'age_s':      age_s,
                'energy':     float(r['energy']) if r['energy'] is not None else None,
                'area':       float(r['area'])   if r['area']   is not None else None,
            }
        })

    return jsonify({
        'type':     'FeatureCollection',
        'features': features,
        'meta': {
            'count':   len(features),
            'minutes': minutes,
            'generated': now.isoformat(),
        }
    })

# ---------------------------------------------------------------------------
# GET /api/glm/status
# ---------------------------------------------------------------------------

@glm_bp.route('/status')
def glm_status():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT satellite,
                       COUNT(*)                                        AS flash_count,
                       MAX(flash_time)                                 AS latest,
                       EXTRACT(EPOCH FROM (NOW() - MAX(flash_time)))   AS age_s
                FROM observations.glm_flashes
                WHERE flash_time > NOW() - INTERVAL '30 minutes'
                GROUP BY satellite
            """)
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.error(f"GLM status query failed: {e}")
        return jsonify({'error': 'Database error'}), 500

    status = {}
    for r in rows:
        status[r['satellite']] = {
            'flash_count': int(r['flash_count']),
            'latest':      r['latest'].isoformat() if r['latest'] else None,
            'age_s':       int(r['age_s']) if r['age_s'] is not None else None,
        }

    return jsonify({
        'satellites': status,
        'operational': len(status) > 0,
    })

