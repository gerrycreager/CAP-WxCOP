#!/usr/bin/env python3
"""
hrrr_winds_api.py — Serve pre-rendered HRRR wind GeoJSON files.

Files are pre-rendered by render_hrrr_winds.py (cron, no WSGI).
This module just reads and returns the JSON from disk — no cfgrib, no numpy.

Endpoints:
  GET /api/winds/hrrr?level=SFC&fhr=0   — wind vectors for level/fhr
  GET /api/winds/hrrr/index             — available cycles and levels
  GET /api/winds/hrrr/levels            — level definitions
"""
import os
import json
import glob
import logging
from flask import Blueprint, jsonify, request, Response

log = logging.getLogger(__name__)

hrrr_winds_api = Blueprint('hrrr_winds_api', __name__)

OUTPUT_BASE = '/LDM/models/hrrr_winds'

VALID_LEVELS = {'SFC', '925', '850', '700', '600', '500'}

LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    '925': '~3,000 ft MSL',
    '850': '~6,000 ft MSL',
    '700': '~10,000 ft MSL',
    '600': '~12,000 ft MSL',
    '500': '~18,000 ft MSL',
}


def find_latest_dir():
    """Return path to latest rendered cycle directory via symlink or scan."""
    latest = os.path.join(OUTPUT_BASE, 'latest')
    if os.path.islink(latest) and os.path.isdir(latest):
        return os.path.realpath(latest)
    # Fallback: scan for newest dir
    dirs = sorted(glob.glob(os.path.join(OUTPUT_BASE, '*', '*z')))
    return dirs[-1] if dirs else None


def find_wind_file(level, fhr):
    """Return path to pre-rendered JSON file or None."""
    latest = find_latest_dir()
    if not latest:
        return None
    fname = f'winds_{level}_f{int(fhr):03d}.json'
    path = os.path.join(latest, fname)
    return path if os.path.exists(path) else None


@hrrr_winds_api.route('/hrrr')
def hrrr_winds():
    level = request.args.get('level', 'SFC').upper()
    fhr   = request.args.get('fhr', 0, type=int)
    fhr   = max(0, min(fhr, 18))

    if level not in VALID_LEVELS:
        return jsonify({'error': f'Unknown level: {level}. '
                                 f'Use: {", ".join(sorted(VALID_LEVELS))}'}), 400

    wind_file = find_wind_file(level, fhr)
    if not wind_file:
        return jsonify({'error': f'No pre-rendered data for level={level} fhr={fhr}. '
                                 f'Run render_hrrr_winds.py to generate.'}), 404

    # Serve the JSON file directly — fastest possible path
    try:
        with open(wind_file, 'r') as f:
            content = f.read()
        return Response(content, mimetype='application/json')
    except Exception as e:
        log.error(f'hrrr_winds read error: {e}')
        return jsonify({'error': str(e)}), 500


@hrrr_winds_api.route('/hrrr/index')
def hrrr_index():
    """Return available cycles and rendered files."""
    latest = find_latest_dir()
    if not latest:
        return jsonify({'error': 'No rendered wind data found. '
                                 'Run render_hrrr_winds.py first.'}), 404
    index_file = os.path.join(latest, 'index.json')
    if os.path.exists(index_file):
        try:
            with open(index_file, 'r') as f:
                return Response(f.read(), mimetype='application/json')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    # Build index on the fly if index.json missing
    files = glob.glob(os.path.join(latest, 'winds_*.json'))
    return jsonify({
        'cycle':  os.path.basename(latest),
        'n_files': len(files),
        'levels': [{'key': k, 'label': v} for k, v in LEVEL_LABELS.items()],
    })


@hrrr_winds_api.route('/hrrr/levels')
def hrrr_levels():
    return jsonify({'levels': [
        {'key': k, 'label': v} for k, v in LEVEL_LABELS.items()
    ]})
