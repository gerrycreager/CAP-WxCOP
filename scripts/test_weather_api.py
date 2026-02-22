#!/usr/bin/env python3
"""
Weather API Diagnostic Script
Tests the weather API directly to identify the 500 error cause
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

def test_weather_api():
    """Test the weather API functionality directly"""
    print("=== WEATHER API DIAGNOSTIC TEST ===")
    
    try:
        # Test database connection
        print("1. Testing database connection...")
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT 1")
        print("   ✓ Database connection successful")
        
        # Test observations.metar table exists
        print("2. Testing observations.metar table...")
        cur.execute("""
            SELECT COUNT(*) FROM observations.metar 
            WHERE observation_time >= NOW() - INTERVAL '2 hours'
        """)
        metar_count = cur.fetchone()[0]
        print(f"   ✓ Found {metar_count} recent METAR observations")
        
        # Test observations.airports table exists
        print("3. Testing observations.airports table...")
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        airport_count = cur.fetchone()[0]
        print(f"   ✓ Found {airport_count} airports in database")
        
        # Test the actual query from weather API
        print("4. Testing weather API query...")
        test_bounds = (25, 50, -125, -70)  # south, north, west, east
        
        query = """
            WITH recent_observations AS (
                SELECT DISTINCT ON (m.station_id)
                    m.station_id,
                    m.latitude,
                    m.longitude,
                    m.observation_time,
                    m.temp_c,
                    m.dewpoint_c,
                    m.wind_dir,
                    m.wind_speed_kts,
                    m.wind_gust_kts,
                    m.altimeter_hg,
                    m.visibility_sm,
                    m.present_weather,
                    m.sky_conditions,
                    m.flight_category,
                    m.raw_text,
                    m.is_speci,
                    a.name as airport_name,
                    a.municipality,
                    a.is_military,
                    a.airport_type
                FROM observations.metar m
                LEFT JOIN observations.airports a ON m.station_id = a.ident
                WHERE m.latitude BETWEEN %s AND %s
                  AND m.longitude BETWEEN %s AND %s
                  AND m.observation_time >= NOW() - INTERVAL '2 hours'
                ORDER BY m.station_id, m.observation_time DESC
            )
            SELECT 
                station_id,
                latitude,
                longitude,
                observation_time,
                temp_c,
                dewpoint_c,
                wind_dir,
                wind_speed_kts,
                wind_gust_kts,
                altimeter_hg,
                visibility_sm,
                present_weather,
                sky_conditions,
                flight_category,
                raw_text,
                is_speci,
                airport_name,
                municipality,
                is_military,
                airport_type
            FROM recent_observations
            ORDER BY observation_time DESC
            LIMIT 10
        """
        
        cur.execute(query, test_bounds)
        results = cur.fetchall()
        
        print(f"   ✓ Query executed successfully, returned {len(results)} results")
        
        if results:
            sample = results[0]
            print(f"   Sample result: {sample[0]} at ({sample[1]}, {sample[2]})")
        
        # Test the Flask import
        print("5. Testing Flask weather API import...")
        from weather_api import weather_api
        print(f"   ✓ Weather API blueprint imported: {weather_api.name}")
        
        cur.close()
        conn.close()
        
        print("\n=== ALL TESTS PASSED ===")
        print("The weather API should be working. The 500 error may be:")
        print("1. A Flask app registration issue")  
        print("2. A URL routing problem")
        print("3. An Apache/WSGI configuration issue")
        
        return True
        
    except Exception as e:
        print(f"❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_weather_api()
