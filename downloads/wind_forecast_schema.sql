-- ============================================================================
-- WIND FORECAST SYSTEM - PostGIS Schema
-- Stores 12-hour wind forecasts from HRRR/GFS models and TAF forecasts
-- ============================================================================

-- Connect to database
\c avwx_data

-- ============================================================================
-- 1. MODEL WIND FORECASTS TABLE
--    Stores HRRR/GFS wind forecasts for airports
-- ============================================================================

CREATE TABLE IF NOT EXISTS observations.model_wind_forecasts (
    id SERIAL PRIMARY KEY,
    
    -- Airport identification
    station_id VARCHAR(8) NOT NULL,
    location GEOMETRY(POINT, 4326) NOT NULL,
    
    -- Model information
    model_name VARCHAR(10) NOT NULL,  -- 'HRRR' or 'GFS'
    model_run TIMESTAMP NOT NULL,     -- Model initialization time
    
    -- Forecast valid time
    valid_time TIMESTAMP NOT NULL,    -- When this forecast is valid for
    forecast_hour INTEGER NOT NULL,   -- Hours from model run (0-12)
    
    -- Wind data (at 10m AGL for models)
    wind_dir INTEGER,                 -- Wind direction (degrees)
    wind_speed_kts REAL NOT NULL,     -- Wind speed (knots)
    wind_gust_kts REAL,               -- Wind gust (knots)
    
    -- Flight category based on wind
    wind_category VARCHAR(10),        -- 'NORMAL', 'CAUTION', 'EXTREME'
    
    -- Maximum winds in forecast period
    max_wind_kts REAL,                -- Max sustained wind in period
    max_gust_kts REAL,                -- Max gust in period
    max_wind_time TIMESTAMP,          -- When max wind occurs
    
    -- Metadata
    ingested_at TIMESTAMP DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(station_id, model_run, valid_time)
);

-- Indexes for model wind forecasts
CREATE INDEX IF NOT EXISTS idx_model_winds_station 
    ON observations.model_wind_forecasts(station_id);
CREATE INDEX IF NOT EXISTS idx_model_winds_valid 
    ON observations.model_wind_forecasts(valid_time);
CREATE INDEX IF NOT EXISTS idx_model_winds_run 
    ON observations.model_wind_forecasts(model_run);
CREATE INDEX IF NOT EXISTS idx_model_winds_location 
    ON observations.model_wind_forecasts USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_model_winds_category 
    ON observations.model_wind_forecasts(wind_category);


-- ============================================================================
-- 2. TAF WIND FORECASTS TABLE
--    Stores TAF wind forecasts parsed from observations.taf
-- ============================================================================

CREATE TABLE IF NOT EXISTS observations.taf_wind_forecasts (
    id SERIAL PRIMARY KEY,
    
    -- Airport identification
    station_id VARCHAR(8) NOT NULL,
    location GEOMETRY(POINT, 4326) NOT NULL,
    
    -- TAF information
    taf_issue_time TIMESTAMP NOT NULL,  -- When TAF was issued
    
    -- Forecast valid time
    valid_from TIMESTAMP NOT NULL,      -- Period start
    valid_to TIMESTAMP NOT NULL,        -- Period end
    
    -- Wind data (surface winds from TAF)
    wind_dir INTEGER,                   -- Wind direction (degrees)
    wind_speed_kts REAL NOT NULL,       -- Wind speed (knots)
    wind_gust_kts REAL,                 -- Wind gust (knots)
    
    -- Change indicators
    change_indicator VARCHAR(10),       -- TEMPO, BECMG, PROB, FM, etc.
    probability INTEGER,                -- For PROB forecasts (0-100)
    
    -- Flight category based on wind
    wind_category VARCHAR(10),          -- 'NORMAL', 'CAUTION', 'EXTREME'
    
    -- Raw text
    taf_line TEXT,                      -- Original TAF line
    
    -- Metadata
    ingested_at TIMESTAMP DEFAULT NOW(),
    
    -- Reference to parent TAF
    taf_id INTEGER REFERENCES observations.taf(id) ON DELETE CASCADE
);

-- Indexes for TAF wind forecasts
CREATE INDEX IF NOT EXISTS idx_taf_winds_station 
    ON observations.taf_wind_forecasts(station_id);
CREATE INDEX IF NOT EXISTS idx_taf_winds_valid_from 
    ON observations.taf_wind_forecasts(valid_from);
CREATE INDEX IF NOT EXISTS idx_taf_winds_valid_to 
    ON observations.taf_wind_forecasts(valid_to);
CREATE INDEX IF NOT EXISTS idx_taf_winds_location 
    ON observations.taf_wind_forecasts USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_taf_winds_category 
    ON observations.taf_wind_forecasts(wind_category);
CREATE INDEX IF NOT EXISTS idx_taf_winds_taf_id 
    ON observations.taf_wind_forecasts(taf_id);


-- ============================================================================
-- 3. WIND FORECAST SNAPSHOTS TABLE
--    Stores pre-computed 12-hour forecast snapshots for fast map generation
-- ============================================================================

CREATE TABLE IF NOT EXISTS observations.wind_forecast_snapshots (
    id SERIAL PRIMARY KEY,
    
    -- Snapshot metadata
    snapshot_time TIMESTAMP NOT NULL,   -- When snapshot was created
    source_type VARCHAR(10) NOT NULL,   -- 'MODEL' or 'TAF'
    model_run TIMESTAMP,                -- For MODEL snapshots
    
    -- Forecast period
    forecast_start TIMESTAMP NOT NULL,  -- Start of 12-hour period
    forecast_end TIMESTAMP NOT NULL,    -- End of 12-hour period
    
    -- Aggregated data (JSONB for flexibility)
    airport_count INTEGER NOT NULL,     -- Number of airports in snapshot
    summary_stats JSONB,                -- Min/max/avg winds, categories
    
    -- Map metadata
    map_generated BOOLEAN DEFAULT FALSE,
    map_filename VARCHAR(255),
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(snapshot_time, source_type)
);


-- ============================================================================
-- 4. HELPER FUNCTIONS
-- ============================================================================

-- Function to calculate wind category
CREATE OR REPLACE FUNCTION calculate_wind_category(
    wind_speed_kts REAL,
    gust_kts REAL DEFAULT NULL
)
RETURNS VARCHAR(10) AS $$
DECLARE
    max_wind REAL;
BEGIN
    -- Use gust if available, otherwise sustained
    max_wind := COALESCE(gust_kts, wind_speed_kts);
    
    IF max_wind IS NULL THEN
        RETURN NULL;
    ELSIF max_wind >= 25 THEN
        RETURN 'EXTREME';   -- CAP operations typically restricted
    ELSIF max_wind >= 15 THEN
        RETURN 'CAUTION';   -- Special procedures may apply
    ELSE
        RETURN 'NORMAL';    -- Normal operations
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- Function to get current model wind forecast for airport
CREATE OR REPLACE FUNCTION get_current_model_winds(
    p_station_id VARCHAR(8),
    p_forecast_hours INTEGER DEFAULT 12
)
RETURNS TABLE(
    valid_time TIMESTAMP,
    wind_dir INTEGER,
    wind_speed_kts REAL,
    wind_gust_kts REAL,
    wind_category VARCHAR(10),
    forecast_hour INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        mwf.valid_time,
        mwf.wind_dir,
        mwf.wind_speed_kts,
        mwf.wind_gust_kts,
        mwf.wind_category,
        mwf.forecast_hour
    FROM observations.model_wind_forecasts mwf
    WHERE mwf.station_id = p_station_id
      AND mwf.model_run = (
          SELECT MAX(model_run) 
          FROM observations.model_wind_forecasts 
          WHERE station_id = p_station_id
      )
      AND mwf.forecast_hour <= p_forecast_hours
    ORDER BY mwf.valid_time;
END;
$$ LANGUAGE plpgsql;


-- Function to get current TAF wind forecast for airport
CREATE OR REPLACE FUNCTION get_current_taf_winds(
    p_station_id VARCHAR(8)
)
RETURNS TABLE(
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,
    wind_dir INTEGER,
    wind_speed_kts REAL,
    wind_gust_kts REAL,
    wind_category VARCHAR(10),
    change_indicator VARCHAR(10)
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        twf.valid_from,
        twf.valid_to,
        twf.wind_dir,
        twf.wind_speed_kts,
        twf.wind_gust_kts,
        twf.wind_category,
        twf.change_indicator
    FROM observations.taf_wind_forecasts twf
    WHERE twf.station_id = p_station_id
      AND twf.taf_issue_time = (
          SELECT MAX(taf_issue_time)
          FROM observations.taf_wind_forecasts
          WHERE station_id = p_station_id
      )
      AND twf.valid_to > NOW()
    ORDER BY twf.valid_from;
END;
$$ LANGUAGE plpgsql;


-- Function to get maximum forecast winds for next N hours
CREATE OR REPLACE FUNCTION get_max_forecast_winds(
    p_station_id VARCHAR(8),
    p_hours_ahead INTEGER DEFAULT 12,
    p_source VARCHAR(10) DEFAULT 'MODEL'  -- 'MODEL' or 'TAF'
)
RETURNS TABLE(
    max_wind_kts REAL,
    max_gust_kts REAL,
    max_wind_time TIMESTAMP,
    wind_category VARCHAR(10)
) AS $$
BEGIN
    IF p_source = 'MODEL' THEN
        RETURN QUERY
        SELECT 
            MAX(mwf.wind_speed_kts) as max_wind_kts,
            MAX(mwf.wind_gust_kts) as max_gust_kts,
            (SELECT valid_time FROM observations.model_wind_forecasts 
             WHERE station_id = p_station_id 
             ORDER BY wind_speed_kts DESC LIMIT 1) as max_wind_time,
            calculate_wind_category(
                MAX(mwf.wind_speed_kts),
                MAX(mwf.wind_gust_kts)
            ) as wind_category
        FROM observations.model_wind_forecasts mwf
        WHERE mwf.station_id = p_station_id
          AND mwf.valid_time BETWEEN NOW() AND NOW() + (p_hours_ahead || ' hours')::INTERVAL
          AND mwf.model_run = (
              SELECT MAX(model_run) 
              FROM observations.model_wind_forecasts 
              WHERE station_id = p_station_id
          );
    ELSE
        RETURN QUERY
        SELECT 
            MAX(twf.wind_speed_kts) as max_wind_kts,
            MAX(twf.wind_gust_kts) as max_gust_kts,
            (SELECT valid_from FROM observations.taf_wind_forecasts 
             WHERE station_id = p_station_id 
             ORDER BY wind_speed_kts DESC LIMIT 1) as max_wind_time,
            calculate_wind_category(
                MAX(twf.wind_speed_kts),
                MAX(twf.wind_gust_kts)
            ) as wind_category
        FROM observations.taf_wind_forecasts twf
        WHERE twf.station_id = p_station_id
          AND twf.valid_from <= NOW() + (p_hours_ahead || ' hours')::INTERVAL
          AND twf.valid_to >= NOW();
    END IF;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 5. GRANT PERMISSIONS
-- ============================================================================

GRANT ALL ON observations.model_wind_forecasts TO avwx_user;
GRANT ALL ON observations.taf_wind_forecasts TO avwx_user;
GRANT ALL ON observations.wind_forecast_snapshots TO avwx_user;

GRANT ALL ON SEQUENCE observations.model_wind_forecasts_id_seq TO avwx_user;
GRANT ALL ON SEQUENCE observations.taf_wind_forecasts_id_seq TO avwx_user;
GRANT ALL ON SEQUENCE observations.wind_forecast_snapshots_id_seq TO avwx_user;


-- ============================================================================
-- 6. EXAMPLE QUERIES
-- ============================================================================

-- Get model forecast for airport
-- SELECT * FROM get_current_model_winds('KDFW', 12);

-- Get TAF forecast for airport
-- SELECT * FROM get_current_taf_winds('KDFW');

-- Get max winds in next 12 hours (model)
-- SELECT * FROM get_max_forecast_winds('KDFW', 12, 'MODEL');

-- Get max winds in next 12 hours (TAF)
-- SELECT * FROM get_max_forecast_winds('KDFW', 12, 'TAF');

-- Find all airports with EXTREME winds in next 12 hours (model)
-- SELECT DISTINCT station_id, max_wind_kts 
-- FROM observations.model_wind_forecasts
-- WHERE wind_category = 'EXTREME'
--   AND valid_time BETWEEN NOW() AND NOW() + INTERVAL '12 hours'
--   AND model_run = (SELECT MAX(model_run) FROM observations.model_wind_forecasts)
-- ORDER BY max_wind_kts DESC;

-- Compare model vs TAF forecasts
-- SELECT 
--     m.station_id,
--     m.wind_speed_kts as model_wind,
--     t.wind_speed_kts as taf_wind,
--     ABS(m.wind_speed_kts - t.wind_speed_kts) as difference
-- FROM observations.model_wind_forecasts m
-- JOIN observations.taf_wind_forecasts t 
--   ON m.station_id = t.station_id
--   AND m.valid_time BETWEEN t.valid_from AND t.valid_to
-- WHERE m.valid_time > NOW()
-- ORDER BY difference DESC
-- LIMIT 20;


-- ============================================================================
-- SUMMARY
-- ============================================================================

-- Tables created:
--   1. observations.model_wind_forecasts (HRRR/GFS forecasts)
--   2. observations.taf_wind_forecasts (TAF forecasts)
--   3. observations.wind_forecast_snapshots (pre-computed snapshots)
--
-- Functions created:
--   - calculate_wind_category(wind, gust)
--   - get_current_model_winds(station_id, hours)
--   - get_current_taf_winds(station_id)
--   - get_max_forecast_winds(station_id, hours, source)
--
-- Next steps:
--   1. Run this SQL to create tables: psql -U postgres -d avwx_data -f wind_forecast_schema.sql
--   2. Populate tables with ingest_model_winds.py script
--   3. Populate tables with populate_taf_winds.py script
--   4. Generate forecast maps with generate_forecast_maps.py script

-- ============================================================================
