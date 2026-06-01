-- ============================================================
-- TDWR sites migration for radar.radar_sites
-- Generated from NCDC nexrad-stations.txt
-- Run on: data2 (192.168.0.60)
-- Command: psql -U avwx_user -h 192.168.0.60 avwx_data -f add_tdwr_sites.sql
-- ============================================================

BEGIN;

-- Step 1: Add radar_type column if not present
ALTER TABLE radar.radar_sites
    ADD COLUMN IF NOT EXISTS radar_type TEXT NOT NULL DEFAULT 'WSR-88D';

-- Step 2: Add range_km column for siteBounds() in the frontend
ALTER TABLE radar.radar_sites
    ADD COLUMN IF NOT EXISTS range_km INTEGER NOT NULL DEFAULT 230;

-- Step 3: Fix the 2 TJUA duplicate — keep only the authoritative one
-- (site_id='JSJ' was the bad duplicate from earlier sessions)
DELETE FROM radar.radar_sites WHERE site_id = 'JSJ';

-- Step 4: Update existing WSR-88D rows to set radar_type explicitly
UPDATE radar.radar_sites SET radar_type = 'WSR-88D', range_km = 230
    WHERE radar_type = 'WSR-88D';  -- no-op but makes intent clear

-- Step 5: Insert TDWR sites (47 sites, range 90km)
-- site_id uses ICAO directly (T prefix), icao same
INSERT INTO radar.radar_sites
    (site_id, icao, name, state, lat, lon, elevation_m, radar_type, range_km, geom)
VALUES
    ('TADW', 'TADW', 'Andrews Afb', 'MD', 38.695, -76.845, 105, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-76.845, 38.695), 4326)),
    ('TATL', 'TATL', 'Atlanta', 'GA', 33.646944, -84.261944, 328, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-84.261944, 33.646944), 4326)),
    ('TBNA', 'TBNA', 'Nashville', 'TN', 35.98, -86.661944, 249, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-86.661944, 35.98), 4326)),
    ('TBOS', 'TBOS', 'Boston', 'MA', 42.158056, -70.933056, 80, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-70.933056, 42.158056), 4326)),
    ('TBWI', 'TBWI', 'Baltimore Washington', 'MD', 39.09, -76.63, 91, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-76.63, 39.09), 4326)),
    ('TCLT', 'TCLT', 'Charlotte', 'NC', 35.336944, -80.885, 265, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-80.885, 35.336944), 4326)),
    ('TCMH', 'TCMH', 'Columbus', 'OH', 40.006111, -82.715, 350, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-82.715, 40.006111), 4326)),
    ('TCVG', 'TCVG', 'Covington', 'KY', 38.898056, -84.58, 321, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-84.58, 38.898056), 4326)),
    ('TDAL', 'TDAL', 'Dallas Love Field', 'TX', 32.926111, -96.968056, 190, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-96.968056, 32.926111), 4326)),
    ('TDAY', 'TDAY', 'Dayton', 'OH', 40.021944, -84.123056, 311, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-84.123056, 40.021944), 4326)),
    ('TDCA', 'TDCA', 'Washington National', 'MD', 38.758889, -76.961944, 105, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-76.961944, 38.758889), 4326)),
    ('TDEN', 'TDEN', 'Denver', 'CO', 39.728056, -104.52611, 1738, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-104.52611, 39.728056), 4326)),
    ('TDFW', 'TDFW', 'Dallas Ft Worth', 'TX', 33.065, -96.918056, 178, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-96.918056, 33.065), 4326)),
    ('TDTW', 'TDTW', 'Detroit', 'MI', 42.111111, -83.515, 235, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-83.515, 42.111111), 4326)),
    ('TEWR', 'TEWR', 'Newark', 'NJ', 40.593056, -74.27, 41, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-74.27, 40.593056), 4326)),
    ('TFLL', 'TFLL', 'Ft Lauderdale', 'FL', 26.143056, -80.343889, 37, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-80.343889, 26.143056), 4326)),
    ('THOU', 'THOU', 'Houston Hobby', 'TX', 29.516111, -95.241944, 36, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-95.241944, 29.516111), 4326)),
    ('TIAD', 'TIAD', 'Washington Dulles', 'VA', 39.083889, -77.528889, 144, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-77.528889, 39.083889), 4326)),
    ('TIAH', 'TIAH', 'Houston International', 'TX', 30.065, -95.566944, 77, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-95.566944, 30.065), 4326)),
    ('TICH', 'TICH', 'Wichita', 'KS', 37.506944, -97.436944, 412, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-97.436944, 37.506944), 4326)),
    ('TIDS', 'TIDS', 'Indianapolis', 'IN', 39.636944, -86.436111, 258, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-86.436111, 39.636944), 4326)),
    ('TJBQ', 'TJBQ', 'Rafael Hernandez Airport', 'PR', 18.485, -67.143, 85, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-67.143, 18.485), 4326)),
    ('TJFK', 'TJFK', 'New York City Jfk', 'NY', 40.588889, -73.881111, 34, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-73.881111, 40.588889), 4326)),
    ('TJRV', 'TJRV', 'Jose Aponte De La Torre Airpor', 'PR', 18.256, -65.637, 15, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-65.637, 18.256), 4326)),
    ('TLAS', 'TLAS', 'Las Vegas', 'NV', 36.143889, -115.00694, 627, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-115.00694, 36.143889), 4326)),
    ('TLVE', 'TLVE', 'Cleveland', 'OH', 41.29, -82.008056, 284, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-82.008056, 41.29), 4326)),
    ('TMCI', 'TMCI', 'Kansas City', 'MO', 39.498056, -94.741944, 332, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-94.741944, 39.498056), 4326)),
    ('TMCO', 'TMCO', 'Orlando International', 'FL', 28.343889, -81.326111, 52, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-81.326111, 28.343889), 4326)),
    ('TMDW', 'TMDW', 'Chicago Midway', 'IL', 41.651111, -87.73, 233, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-87.73, 41.651111), 4326)),
    ('TMEM', 'TMEM', 'Memphis', 'MS', 34.896111, -89.993056, 147, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-89.993056, 34.896111), 4326)),
    ('TMIA', 'TMIA', 'Miami', 'FL', 25.758056, -80.491111, 38, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-80.491111, 25.758056), 4326)),
    ('TMKE', 'TMKE', 'Milwaukee', 'WI', 42.818889, -88.046111, 284, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-88.046111, 42.818889), 4326)),
    ('TMSP', 'TMSP', 'Minneapolis', 'MN', 44.871111, -92.933056, 342, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-92.933056, 44.871111), 4326)),
    ('TMSY', 'TMSY', 'New Orleans', 'LA', 30.021944, -90.403056, 30, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-90.403056, 30.021944), 4326)),
    ('TOKC', 'TOKC', 'Norman Wfo', 'OK', 35.276111, -97.51, 399, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-97.51, 35.276111), 4326)),
    ('TORD', 'TORD', 'Chicago Ohare', 'IL', 41.796944, -87.858056, 227, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-87.858056, 41.796944), 4326)),
    ('TPBI', 'TPBI', 'West Palm Beach', 'FL', 26.688056, -80.273056, 41, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-80.273056, 26.688056), 4326)),
    ('TPHL', 'TPHL', 'Philadelphia', 'NJ', 39.948889, -75.068889, 47, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-75.068889, 39.948889), 4326)),
    ('TPHX', 'TPHX', 'Phoenix', 'AZ', 33.421111, -112.16305, 332, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-112.16305, 33.421111), 4326)),
    ('TPIT', 'TPIT', 'Pittsburgh', 'PA', 40.501111, -80.486111, 422, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-80.486111, 40.501111), 4326)),
    ('TRDU', 'TRDU', 'Raleigh', 'NC', 36.001944, -78.696944, 157, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-78.696944, 36.001944), 4326)),
    ('TSDF', 'TSDF', 'Louisville', 'KY', 38.046111, -85.61, 223, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-85.61, 38.046111), 4326)),
    ('TSJU', 'TSJU', 'San Juan', 'PR', 18.473889, -66.178889, 48, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-66.178889, 18.473889), 4326)),
    ('TSLC', 'TSLC', 'Salt Lake City', 'UT', 40.966944, -111.93, 1309, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-111.93, 40.966944), 4326)),
    ('TSTL', 'TSTL', 'St Louis', 'MO', 38.805, -90.488889, 197, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-90.488889, 38.805), 4326)),
    ('TTPA', 'TTPA', 'Tampa', 'FL', 27.86, -82.518056, 28, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-82.518056, 27.86), 4326)),
    ('TTUL', 'TTUL', 'Tulsa', 'OK', 36.071111, -95.826944, 251, 'TDWR', 90, ST_SetSRID(ST_MakePoint(-95.826944, 36.071111), 4326))
ON CONFLICT (site_id) DO UPDATE SET
    icao        = EXCLUDED.icao,
    name        = EXCLUDED.name,
    state       = EXCLUDED.state,
    lat         = EXCLUDED.lat,
    lon         = EXCLUDED.lon,
    elevation_m = EXCLUDED.elevation_m,
    radar_type  = EXCLUDED.radar_type,
    range_km    = EXCLUDED.range_km,
    geom        = EXCLUDED.geom;

-- Step 6: Verify
SELECT radar_type, COUNT(*) as count, AVG(range_km)::int as avg_range_km
FROM radar.radar_sites
GROUP BY radar_type ORDER BY radar_type;

-- Spot-check a few TDWR
SELECT site_id, icao, name, state, lat, lon, range_km
FROM radar.radar_sites
WHERE radar_type = 'TDWR'
ORDER BY state, site_id LIMIT 10;

COMMIT;