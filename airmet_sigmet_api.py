#!/usr/bin/env python3
"""
AIRMET/SIGMET API Endpoints for CAP Winds Application
Phase 2B: Flask Blueprint for serving weather hazard polygons
"""
from flask import Blueprint, jsonify, request
from datetime import datetime, timezone
from airmet_sigmet_api_fetcher import AviationWeatherAPI

# Create blueprint
airmet_sigmet_api = Blueprint('airmet_sigmet_api', __name__)

# Initialize API fetcher (singleton)
api = AviationWeatherAPI()


@airmet_sigmet_api.route('/airmets')
def get_airmets():
    """Get active AIRMETs as GeoJSON (polygons only, no FZLVL contours)"""
    valid_at = _parse_valid_at()
    if isinstance(valid_at, tuple):  # error response
        return valid_at
    try:
        airmets = api.get_active_airmets(timestamp=valid_at)
        # Exclude FZLVL contours from the polygon layer
        airmets = [a for a in airmets if a.get('type') != 'FZLVL_CONTOUR']
        return jsonify(api.to_geojson(airmets))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@airmet_sigmet_api.route('/sigmets')
def get_sigmets():
    """Get active SIGMETs as GeoJSON"""
    valid_at = _parse_valid_at()
    if isinstance(valid_at, tuple):
        return valid_at
    try:
        sigmets = api.get_active_sigmets(timestamp=valid_at)
        return jsonify(api.to_geojson(sigmets))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@airmet_sigmet_api.route('/fzlvl')
def get_fzlvl():
    """Get active FZLVL contour lines as GeoJSON LineString features"""
    valid_at = _parse_valid_at()
    if isinstance(valid_at, tuple):
        return valid_at
    try:
        all_items = api.get_active_airmets(timestamp=valid_at)
        contours = [a for a in all_items if a.get('type') == 'FZLVL_CONTOUR']
        return jsonify(api.to_geojson(contours))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@airmet_sigmet_api.route('/weather-hazards')
def get_all_hazards():
    """
    Get AIRMETs and SIGMETs combined as GeoJSON polygons.
    FZLVL contours are excluded here — use /fzlvl for those.
    """
    valid_at = _parse_valid_at()
    if isinstance(valid_at, tuple):
        return valid_at
    try:
        airmets = api.get_active_airmets(timestamp=valid_at)
        sigmets = api.get_active_sigmets(timestamp=valid_at)
        # Exclude FZLVL contours from polygon layer
        polygons = [a for a in airmets if a.get('type') != 'FZLVL_CONTOUR'] + sigmets
        return jsonify(api.to_geojson(polygons))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _parse_valid_at():
    """Parse optional valid_at query param. Returns datetime or None, or error tuple."""
    valid_at_str = request.args.get('valid_at')
    if valid_at_str:
        try:
            return datetime.fromisoformat(valid_at_str.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid timestamp format. Use ISO 8601.'}), 400
    return None

