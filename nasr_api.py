"""
NASR aviation-detail API: NavAids, fixes, airports/heliports, and
runways (depicted by real endpoint geometry, not a generic dot).

Built 2026-11-19 at Gerry's request, to give the EWMC map detail the
Esri VFR Sectional tile service can't -- that service tops out at
native zoom 10 with no maxNativeZoom set, so detail disappears when
zoomed in close. This is vector data instead (NASR 28-day subscription,
see scripts/ingest_nasr.py), so it has no resolution ceiling at all.

All endpoints take `bounds=west,south,east,north` and filter via
ST_MakeEnvelope + && against each table's GIST index -- same pattern
wind_forecast_api.py already uses. Fixes (70k) and airports (19k) are
dense enough that the frontend should zoom-gate them; this API itself
doesn't enforce a zoom minimum since "how dense is too dense" is a
frontend/UX call, not a backend one.
"""
from flask import Blueprint, request, jsonify
from db_config import get_connection

nasr_api = Blueprint('nasr_api', __name__)


def _parse_bounds():
    bounds_param = request.args.get('bounds', '')
    try:
        west, south, east, north = map(float, bounds_param.split(','))
    except (ValueError, TypeError):
        return None
    return west, south, east, north


@nasr_api.route('/airports')
def get_airports():
    """Airports + heliports (+ seaplane/glider/balloon/ultralight sites),
    with their runway endpoints nested so the frontend can draw real
    runway lines instead of a dot."""
    b = _parse_bounds()
    if not b:
        return jsonify({'error': 'bounds parameter required (west,south,east,north)'}), 400
    west, south, east, north = b
    site_types = request.args.get('site_types', 'A,H')  # default: airports + heliports

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.site_no, a.site_type_code, a.arpt_id, a.icao_id, a.arpt_name,
                   ST_X(a.geom) AS lon, ST_Y(a.geom) AS lat, a.elevation_ft
            FROM nasr.airports a
            WHERE a.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
              AND a.site_type_code = ANY(%s)
        """, (west, south, east, north, site_types.split(',')))
        airports = []
        site_nos = []
        for row in cur.fetchall():
            site_nos.append(row[0])
            airports.append({
                'site_no': row[0], 'site_type': row[1], 'arpt_id': row[2],
                'icao_id': row[3], 'name': row[4], 'lon': row[5], 'lat': row[6],
                'elevation_ft': row[7], 'runways': [],
            })
        by_site = {a['site_no']: a for a in airports}

        if site_nos:
            cur.execute("""
                SELECT e.site_no, e.rwy_id, e.rwy_end_id, ST_X(e.geom), ST_Y(e.geom),
                       r.rwy_len_ft, r.rwy_width_ft, r.surface_code
                FROM nasr.runway_ends e
                JOIN nasr.runways r ON r.site_no = e.site_no AND r.rwy_id = e.rwy_id
                WHERE e.site_no = ANY(%s)
            """, (site_nos,))
            rwy_ends = {}
            rwy_meta = {}
            for site_no, rwy_id, end_id, lon, lat, length_ft, width_ft, surface in cur.fetchall():
                key = (site_no, rwy_id)
                rwy_ends.setdefault(key, []).append({'end_id': end_id, 'lon': lon, 'lat': lat})
                rwy_meta[key] = {'length_ft': length_ft, 'width_ft': width_ft, 'surface': surface}
            for (site_no, rwy_id), ends in rwy_ends.items():
                if site_no not in by_site or len(ends) != 2:
                    continue
                by_site[site_no]['runways'].append({
                    'rwy_id': rwy_id, 'ends': ends, **rwy_meta[(site_no, rwy_id)],
                })
        cur.close()
        return jsonify({'count': len(airports), 'airports': airports})
    finally:
        conn.close()


@nasr_api.route('/navaids')
def get_navaids():
    b = _parse_bounds()
    if not b:
        return jsonify({'error': 'bounds parameter required (west,south,east,north)'}), 400
    west, south, east, north = b

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT nav_id, nav_type, name, ST_X(geom), ST_Y(geom), freq, chan
            FROM nasr.navaids
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
        """, (west, south, east, north))
        navaids = [{
            'nav_id': r[0], 'nav_type': r[1], 'name': r[2],
            'lon': r[3], 'lat': r[4], 'freq': r[5], 'chan': r[6],
        } for r in cur.fetchall()]
        cur.close()
        return jsonify({'count': len(navaids), 'navaids': navaids})
    finally:
        conn.close()


@nasr_api.route('/fixes')
def get_fixes():
    """Dense (70k nationwide) -- frontend should only call this at high
    zoom, same convention as NIDS' z>9 gate."""
    b = _parse_bounds()
    if not b:
        return jsonify({'error': 'bounds parameter required (west,south,east,north)'}), 400
    west, south, east, north = b
    limit = min(int(request.args.get('limit', 2000)), 5000)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fix_id, ST_X(geom), ST_Y(geom), fix_use_code
            FROM nasr.fixes
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            LIMIT %s
        """, (west, south, east, north, limit))
        fixes = [{'fix_id': r[0], 'lon': r[1], 'lat': r[2], 'use_code': r[3]} for r in cur.fetchall()]
        cur.close()
        return jsonify({'count': len(fixes), 'fixes': fixes})
    finally:
        conn.close()
