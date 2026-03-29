-- =============================================================================
-- CAP WxCOP — GLM Flash Table
-- Stores recent GLM LCFA flash-level data for lightning proximity detection.
-- Retention: ~2 hours of granules (purged by ingest_glm.py on every run).
--
-- Source: GOES-16/17/18/19 GLM Level-2 Lightning Cluster-Filter Algorithm
--         LCFA granules — 20-second cadence, flash-level records
--
-- Usage: cadet_wx_api.py queries this table for flashes within a
--        configurable radius and time window around each cadet site.
-- =============================================================================

CREATE TABLE IF NOT EXISTS observations.glm_flashes (
    id              BIGSERIAL PRIMARY KEY,
    satellite       CHAR(3) NOT NULL,           -- 'G19' or 'G18'
    flash_id        INTEGER NOT NULL,            -- flash_id from LCFA file
    flash_time      TIMESTAMPTZ NOT NULL,        -- flash_time_offset_of_first_event
    lat             REAL NOT NULL,               -- flash_lat
    lon             REAL NOT NULL,               -- flash_lon
    energy          REAL,                        -- flash_energy (J)
    area            REAL,                        -- flash_area (km²)
    granule_file    TEXT NOT NULL,               -- source filename (for dedup)
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Spatial index for proximity queries (lat/lon box filter)
CREATE INDEX IF NOT EXISTS idx_glm_flashes_latlon
    ON observations.glm_flashes (lat, lon);

-- Time index for recency queries and purge
CREATE INDEX IF NOT EXISTS idx_glm_flashes_time
    ON observations.glm_flashes (flash_time DESC);

-- Dedup index: don't re-insert same flash from same granule
CREATE UNIQUE INDEX IF NOT EXISTS uix_glm_flashes_granule_flash
    ON observations.glm_flashes (granule_file, flash_id);

-- Ingested_at index for purge efficiency
CREATE INDEX IF NOT EXISTS idx_glm_flashes_ingested
    ON observations.glm_flashes (ingested_at);

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'observations'
  AND table_name   = 'glm_flashes'
ORDER BY ordinal_position;

