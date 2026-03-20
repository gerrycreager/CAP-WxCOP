# ============================================================================
# STATIC WIND MAPS PAGE + SUPPORTING API ENDPOINTS
# Add this block to app.py just before the /legacy-wind-generator route.
# ============================================================================

@app.route('/static-maps')
def static_wind_maps():
    """Static wind constraint maps — CONUS, Region, Wing with PNG/shapefile export"""
    return render_template('static_wind_maps.html')


@app.route('/api/map-meta')
def map_meta():
    """Return metadata about latest map generation run"""
    import os
    import glob
    from datetime import datetime

    batch_dir = '/var/www/cap_winds_app/static/batch_maps'

    # Find most recent shapefile ZIP
    zips = sorted(glob.glob(os.path.join(batch_dir, 'cap_winds_all_territories_*.zip')))
    latest_zip = os.path.basename(zips[-1]) if zips else None

    # Parse model run timestamp from ZIP name: cap_winds_all_territories_YYYYMMDD_HHMM.zip
    model_run_str = None
    if latest_zip:
        try:
            ts = latest_zip.replace('cap_winds_all_territories_', '').replace('.zip', '')
            dt = datetime.strptime(ts, '%Y%m%d_%H%M')
            model_run_str = dt.strftime('%Y-%m-%d %H%MZ')
        except Exception:
            model_run_str = latest_zip

    # Generation time from CONUS PNG mtime
    conus_png = os.path.join(batch_dir, 'conus_wind_constraints.png')
    generated = None
    if os.path.exists(conus_png):
        mtime = os.path.getmtime(conus_png)
        generated = datetime.utcfromtimestamp(mtime).strftime('%Y-%m-%d %H%MZ')

    # Airport count from current model run in DB
    airport_count = None
    try:
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT station_id)
            FROM observations.model_wind_forecasts
            WHERE model_run = (
                SELECT MAX(model_run) FROM observations.model_wind_forecasts
            )
        """)
        row = cur.fetchone()
        if row:
            airport_count = row[0]
        cur.close()
        conn.close()
    except Exception:
        pass

    return jsonify({
        'model_run':     model_run_str,
        'airport_count': airport_count,
        'generated':     generated,
        'latest_zip':    latest_zip,
    })


@app.route('/api/latest-shapefile')
def latest_shapefile():
    """Return URL and filename of the most recent shapefile ZIP"""
    import os
    import glob

    batch_dir = '/var/www/cap_winds_app/static/batch_maps'
    zips = sorted(glob.glob(os.path.join(batch_dir, 'cap_winds_all_territories_*.zip')))

    if not zips:
        return jsonify({'error': 'No shapefiles available'}), 404

    latest = os.path.basename(zips[-1])
    return jsonify({
        'filename': latest,
        'url':      '/CAP_WxCOP/cap_winds/' + latest,
    })

