# =============================================================================
# AIRPORT TIERS ENDPOINT
# Add this to weather_api.py
# Returns tiered airport data within map bounds using PostGIS
#
# Tiers:
#   1 = Military          (is_military=true)          always show, red label
#   2 = Major carrier     (reporting + rwy >= 8000ft) zoom 4+,    blue label
#   3 = Regional          (paved rwy >= 5000ft)       zoom 7+,    gray label
#   4 = GA                (paved rwy >= 2500ft)       zoom 9+,    no label (tooltip only)
#
# US scope: all US-* states + GU, PR, VI
# =============================================================================

@weather_api.route('/airport-tiers')
def get_airport_tiers():
    """
    Get tiered airport data within bounds for label display.
    
    Query parameters:
    - bounds: west,south,east,north (required)
    - tier: max tier to return (default: 4, i.e. all)
    """
    try:
        bounds_str = request.args.get('bounds')
        max_tier   = int(request.args.get('tier', 4))

        if not bounds_str:
            return jsonify({'error': 'bounds parameter required'}), 400

        parts = bounds_str.split(',')
        if len(parts) != 4:
            return jsonify({'error': 'bounds must be west,south,east,north'}), 400

        west, south, east, north = map(float, parts)

        conn = get_connection()
        cur  = conn.cursor()

        # US scope filter - all states + territories
        US_REGIONS = (
            'US-AL','US-AK','US-AZ','US-AR','US-CA','US-CO','US-CT','US-DE',
            'US-FL','US-GA','US-HI','US-ID','US-IL','US-IN','US-IA','US-KS',
            'US-KY','US-LA','US-ME','US-MD','US-MA','US-MI','US-MN','US-MS',
            'US-MO','US-MT','US-NE','US-NV','US-NH','US-NJ','US-NM','US-NY',
            'US-NC','US-ND','US-OH','US-OK','US-OR','US-PA','US-RI','US-SC',
            'US-SD','US-TN','US-TX','US-UT','US-VT','US-VA','US-WA','US-WV',
            'US-WI','US-WY',
            'GU','PR','VI'
        )

        query = """
            SELECT
                station_id,
                name,
                ST_X(location) as lon,
                ST_Y(location) as lat,
                is_military,
                has_reporting,
                longest_runway_ft,
                CASE
                    WHEN is_military = true                                          THEN 1
                    WHEN has_reporting = true AND longest_runway_ft >= 8000          THEN 2
                    WHEN has_paved_runway = true AND longest_runway_ft >= 5000       THEN 3
                    WHEN has_paved_runway = true AND longest_runway_ft >= 2500       THEN 4
                    ELSE NULL
                END as tier
            FROM observations.airports
            WHERE
                iso_region = ANY(%s)
                AND has_paved_runway = true
                AND longest_runway_ft >= 2500
                AND location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                AND CASE
                    WHEN is_military = true                                          THEN 1
                    WHEN has_reporting = true AND longest_runway_ft >= 8000          THEN 2
                    WHEN has_paved_runway = true AND longest_runway_ft >= 5000       THEN 3
                    WHEN has_paved_runway = true AND longest_runway_ft >= 2500       THEN 4
                    ELSE 99
                END <= %s
            ORDER BY
                CASE
                    WHEN is_military = true                                          THEN 1
                    WHEN has_reporting = true AND longest_runway_ft >= 8000          THEN 2
                    WHEN has_paved_runway = true AND longest_runway_ft >= 5000       THEN 3
                    ELSE 4
                END,
                longest_runway_ft DESC
        """

        cur.execute(query, (list(US_REGIONS), west, south, east, north, max_tier))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        airports = []
        for row in rows:
            airports.append({
                'station_id':      row[0],
                'name':            row[1],
                'lon':             float(row[2]),
                'lat':             float(row[3]),
                'is_military':     row[4],
                'has_reporting':   row[5],
                'longest_runway':  row[6],
                'tier':            row[7]
            })

        return jsonify({
            'count':    len(airports),
            'airports': airports,
            'bounds':   {'west': west, 'south': south, 'east': east, 'north': north}
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

