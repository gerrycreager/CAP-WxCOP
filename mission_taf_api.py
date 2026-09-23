"""
mission_taf_api.py - Serves model-driven mission-window TAFs (KBAK cluster)
as plain text for Gerry's laptop-side Mission Execution Forecast (MEF) tool
to scrape, added 2026-09-21. See scripts/mission_taf_extract.py for the
generation logic and its documented fidelity limits (single-layer sky
condition; these are NOT official NWS TAFs).

On-demand only -- no cron, no DB writes. Each request calls
generate_mission_tafs(), which fetches/decodes at most 3 RRFS files
(cached 6h by rrfs_wind_fetch.py), regardless of repeated scraping within
that window. This matches Gerry's explicit intent to keep server load
minimal since the actual MEF computation happens on his laptop.
"""

from flask import Blueprint, Response

from scripts.mission_taf_extract import generate_mission_tafs, AIRPORTS

mission_taf_api = Blueprint('mission_taf_api', __name__)


@mission_taf_api.route('/text')
def mission_tafs_text():
    """Plain-text bulletin: all 9 airports' TAFs, blank-line separated,
    each ending in '=' -- matches real NWS TAF bulletin formatting so any
    scraper already written against real TAF text needs no special-casing."""
    date, cyc, targets, tafs = generate_mission_tafs()
    header = (
        f'MISSION-WINDOW MODEL TAFS -- NOT AN OFFICIAL NWS PRODUCT\n'
        f'Source: RRFS {date}/{cyc}Z' if date else
        'MISSION-WINDOW MODEL TAFS -- NOT AN OFFICIAL NWS PRODUCT\nSource: RRFS (no cycle available)'
    )
    body = '\n\n'.join(tafs[icao] for icao in AIRPORTS)
    return Response(f'{header}\n\n{body}\n', mimetype='text/plain')


@mission_taf_api.route('/json')
def mission_tafs_json():
    """Same data as /text, structured -- for a scraper that'd rather parse
    JSON than split raw bulletin text."""
    from flask import jsonify
    date, cyc, targets, tafs = generate_mission_tafs()
    return jsonify({
        'source': 'RRFS',
        'cycle': f'{date}{cyc}' if date else None,
        'target_times_utc': [t.isoformat() + 'Z' for t in targets],
        'tafs': tafs,
    })
