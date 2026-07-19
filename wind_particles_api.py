#!/usr/bin/env python3
"""
wind_particles_api.py — Serve pre-rendered animated wind-particle grids
(leaflet-velocity / wind-js format) for the Atlantic basin TC-tracking layer.

Files are pre-rendered by scripts/render_wind_particles.py (cron, no WSGI).
This module just reads and returns the JSON from disk — no cfgrib, no numpy.

The `source` param exists from day one even though only `gfs` is wired up
in Phase 1 -- Phase 2 (ECMWF IFS/AIFS) adds output dirs under the same
/LDM/models/wind_particles/{source}/ layout without a route redesign.

Endpoints:
  GET /api/wind-particles?source=gfs&level=SFC&fhr=0  — wind-js grid for level/fhr
  GET /api/wind-particles/index?source=gfs             — available cycle/levels/fhrs
  GET /api/wind-particles/levels?source=gfs             — level definitions
"""
import os
import glob
import logging
from flask import Blueprint, jsonify, request, Response

log = logging.getLogger(__name__)

wind_particles_api = Blueprint('wind_particles_api', __name__)

OUTPUT_ROOT = '/LDM/models/wind_particles'

VALID_SOURCES = {'gfs', 'ecmwf-ifs', 'ecmwf-aifs'}

VALID_LEVELS = {'SFC', '850', '700', '500', '200', 'DLM'}

LEVEL_LABELS = {
    'SFC': 'Surface (10m AGL)',
    '850': '850 hPa',
    '700': '700 hPa',
    '500': '500 hPa',
    '200': '200 hPa',
    'DLM': 'Deep-Layer Mean (850-700-500 hPa steering flow)',
}


def _source_base(source):
    return os.path.join(OUTPUT_ROOT, source)


def find_latest_dir(source):
    """Return path to latest rendered cycle directory via symlink or scan."""
    latest = os.path.join(_source_base(source), 'latest')
    if os.path.islink(latest) and os.path.isdir(latest):
        return os.path.realpath(latest)
    dirs = sorted(glob.glob(os.path.join(_source_base(source), '*', '*z')))
    return dirs[-1] if dirs else None


def find_particle_file(source, level, fhr):
    """Return path to pre-rendered JSON file or None."""
    latest = find_latest_dir(source)
    if not latest:
        return None
    fname = f'particles_{level}_f{int(fhr):03d}.json'
    path = os.path.join(latest, fname)
    return path if os.path.exists(path) else None


def _validated_source():
    source = request.args.get('source', 'gfs').lower()
    if source not in VALID_SOURCES:
        return None, jsonify({'error': f'Unknown source: {source}. '
                                        f'Use: {", ".join(sorted(VALID_SOURCES))}'}), 400
    return source, None, None


@wind_particles_api.route('')
def wind_particles():
    source, err, code = _validated_source()
    if err:
        return err, code

    level = request.args.get('level', 'SFC').upper()
    fhr   = request.args.get('fhr', 0, type=int)
    fhr   = max(0, fhr)

    if level not in VALID_LEVELS:
        return jsonify({'error': f'Unknown level: {level}. '
                                 f'Use: {", ".join(sorted(VALID_LEVELS))}'}), 400

    particle_file = find_particle_file(source, level, fhr)
    if not particle_file:
        return jsonify({'error': f'No pre-rendered data for source={source} level={level} '
                                 f'fhr={fhr}. Run render_wind_particles.py to generate.'}), 404

    try:
        with open(particle_file, 'r') as f:
            content = f.read()
        return Response(content, mimetype='application/json')
    except Exception as e:
        log.error(f'wind_particles read error: {e}')
        return jsonify({'error': str(e)}), 500


@wind_particles_api.route('/index')
def wind_particles_index():
    source, err, code = _validated_source()
    if err:
        return err, code

    latest = find_latest_dir(source)
    if not latest:
        return jsonify({'error': f'No rendered wind-particle data for source={source}. '
                                 f'Run render_wind_particles.py first.'}), 404
    index_file = os.path.join(latest, 'index.json')
    if os.path.exists(index_file):
        try:
            with open(index_file, 'r') as f:
                return Response(f.read(), mimetype='application/json')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    files = glob.glob(os.path.join(latest, 'particles_*.json'))
    return jsonify({
        'cycle':  os.path.basename(latest),
        'n_files': len(files),
        'levels': [{'key': k, 'label': v} for k, v in LEVEL_LABELS.items()],
    })


@wind_particles_api.route('/levels')
def wind_particles_levels():
    return jsonify({'levels': [
        {'key': k, 'label': v} for k, v in LEVEL_LABELS.items()
    ]})
