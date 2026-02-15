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

# Initialize parser (singleton)
api = AviationWeatherAPI()


@airmet_sigmet_api.route('/airmets')
def get_airmets():
    """
    Get active AIRMETs as GeoJSON
    
    Query params:
        valid_at: ISO timestamp (optional, default: now)
        region: CONUS|AK|HI|PR (optional, default: all)
    
    Returns:
        GeoJSON FeatureCollection
    """
    # Parse timestamp if provided
    valid_at_str = request.args.get('valid_at')
    
    if valid_at_str:
        try:
            valid_at = datetime.fromisoformat(valid_at_str.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid timestamp format. Use ISO 8601.'}), 400
    else:
        valid_at = None  # Use current time
    
    # Get active AIRMETs
    try:
        airmets = api.get_active_airmets(timestamp=valid_at)
        geojson = api.to_geojson(airmets)
        
        return jsonify(geojson)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@airmet_sigmet_api.route('/sigmets')
def get_sigmets():
    """
    Get active SIGMETs as GeoJSON
    
    Query params:
        valid_at: ISO timestamp (optional, default: now)
        region: CONUS|AK|HI|PR (optional, default: all)
    
    Returns:
        GeoJSON FeatureCollection
    """
    # Parse timestamp if provided
    valid_at_str = request.args.get('valid_at')
    
    if valid_at_str:
        try:
            valid_at = datetime.fromisoformat(valid_at_str.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid timestamp format. Use ISO 8601.'}), 400
    else:
        valid_at = None  # Use current time
    
    # Get active SIGMETs
    try:
        sigmets = api.get_active_sigmets(timestamp=valid_at)
        geojson = api.to_geojson(sigmets)
        
        return jsonify(geojson)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@airmet_sigmet_api.route('/weather-hazards')
def get_all_hazards():
    """
    Get both AIRMETs and SIGMETs combined
    
    Returns:
        GeoJSON FeatureCollection with both types
    """
    valid_at_str = request.args.get('valid_at')
    
    if valid_at_str:
        try:
            valid_at = datetime.fromisoformat(valid_at_str.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid timestamp format. Use ISO 8601.'}), 400
    else:
        valid_at = None
    
    try:
        airmets = api.get_active_airmets(timestamp=valid_at)
        sigmets = api.get_active_sigmets(timestamp=valid_at)
        
        # Combine both
        all_hazards = airmets + sigmets
        geojson = api.to_geojson(all_hazards)
        
        return jsonify(geojson)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

