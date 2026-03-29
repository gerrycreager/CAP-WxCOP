# =============================================================================
# GLM Lightning Flash API
# =============================================================================

@weather_enhanced_api.route('/glm/flashes', methods=['GET'])
def get_glm_flashes():
    """
    Return GLM lightning flashes as GeoJSON FeatureCollection.

    Query params:
      minutes  : lookback window in minutes (default 20, max 60)
      min_lat, max_lat, min_lon, max_lon : optional bbox filter
    """
    try:
        minutes = min(int(request.args.get('minutes', 20)), 60)
        min_lat = request.args.get('min_lat', type=float)
        max_lat = request.args.get('max_lat', type=float)
        min_lon = request.args.get('min_lon', type=float)
        max_lon = request.args.get('max_lon', type=float)

        cutoff = datetime.utcnow() - timedelta(minutes=minutes)

        conn = get_connection()
        cur  = conn.cursor()

        # Base query — bbox filter optional
        if all(v is not None for v in [min_lat, max_lat, min_lon, max_lon]):
            cur.execute("""
                SELECT flash_time, lat, lon, energy, satellite
                FROM observations.glm_flashes
                WHERE flash_time >= %s
                  AND lat  BETWEEN %s AND %s
                  AND lon  BETWEEN %s AND %s
                ORDER BY flash_time DESC
                LIMIT 50000
            """, (cutoff, min_lat, max_lat, min_lon, max_lon))
        else:
            cur.execute("""
                SELECT flash_time, lat, lon, energy, satellite
                FROM observations.glm_flashes
                WHERE flash_time >= %s
                ORDER BY flash_time DESC
                LIMIT 50000
            """, (cutoff,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        now = datetime.utcnow()

        features = []
        for flash_time, lat, lon, energy, satellite in rows:
            # Age in minutes (flash_time may be tz-aware)
            ft = flash_time.replace(tzinfo=None) if flash_time.tzinfo else flash_time
            age_min = (now - ft).total_seconds() / 60.0

            # Age bucket: 0=red (<5 min), 1=yellow (5-15 min), 2=green (>15 min)
            if age_min < 5:
                age_bucket = 0
            elif age_min < 15:
                age_bucket = 1
            else:
                age_bucket = 2

            features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [lon, lat]
                },
                'properties': {
                    'flash_time': ft.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'age_min':    round(age_min, 1),
                    'age_bucket': age_bucket,   # 0=red,1=yellow,2=green
                    'energy':     float(energy) if energy else None,
                    'satellite':  satellite.strip() if satellite else None,
                }
            })

        return jsonify({
            'type':        'FeatureCollection',
            'features':    features,
            'count':       len(features),
            'minutes':     minutes,
            'generated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500
