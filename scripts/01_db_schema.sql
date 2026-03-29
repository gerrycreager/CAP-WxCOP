-- =============================================================================
-- CAP WxCOP — DB Schema Changes for Cadet Weather
-- Run on: avwx_data database as avwx_user or postgres
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Add weather/WBGT fields to model_wind_forecasts (airport forecasts)
--    These are NULL-able — populated only when HRRR/GFS contains the field.
--    Existing rows are unaffected; new ingest will populate them going forward.
-- -----------------------------------------------------------------------------
ALTER TABLE observations.model_wind_forecasts
    ADD COLUMN IF NOT EXISTS tmp_c        REAL,
    ADD COLUMN IF NOT EXISTS dpt_c        REAL,
    ADD COLUMN IF NOT EXISTS precip_mm    REAL,
    ADD COLUMN IF NOT EXISTS dswrf_wm2    REAL,
    ADD COLUMN IF NOT EXISTS wbgt_c       REAL;

-- -----------------------------------------------------------------------------
-- 2. Create model_site_wx — cadet site forecast table
--    Separate from model_wind_forecasts: no runway logic, different schema.
--    One row per (site_id, model_name, model_run, valid_time).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observations.model_site_wx (
    id              SERIAL PRIMARY KEY,
    site_id         INTEGER NOT NULL REFERENCES observations.cadet_sites(id) ON DELETE CASCADE,
    model_name      VARCHAR(20) NOT NULL,          -- 'HRRR' or 'GFS'
    model_run       TIMESTAMPTZ NOT NULL,           -- model initialization time
    valid_time      TIMESTAMPTZ NOT NULL,           -- valid forecast time
    forecast_hour   SMALLINT NOT NULL,              -- F01..F24
    -- Wind
    wind_dir        SMALLINT,                       -- degrees true
    wind_speed_kts  REAL,
    wind_gust_kts   REAL,
    -- Thermodynamics
    tmp_c           REAL,                           -- 2m temperature °C
    dpt_c           REAL,                           -- 2m dewpoint °C
    -- Derived
    heat_index_c    REAL,                           -- Rothfusz heat index °C (NULL if T < 27°C)
    wind_chill_c    REAL,                           -- NWS wind chill °C (NULL if T > 10°C or wind < 5 kt)
    -- Precip / solar
    precip_mm       REAL,                           -- accumulated precip mm (since model init)
    precip_rate_mmhr REAL,                          -- instantaneous precip rate mm/hr (derived from delta)
    dswrf_wm2       REAL,                           -- downward shortwave radiation W/m²
    -- WBGT
    wbgt_c          REAL,                           -- Liljegren outdoor WBGT °C (precomputed at ingest)
    -- Metadata
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unique constraint: one row per site/model/run/valid_time
CREATE UNIQUE INDEX IF NOT EXISTS uix_model_site_wx
    ON observations.model_site_wx (site_id, model_name, model_run, valid_time);

-- Index for API lookups by site + model + run
CREATE INDEX IF NOT EXISTS idx_model_site_wx_site_run
    ON observations.model_site_wx (site_id, model_run DESC, forecast_hour);

-- Index for purging old data
CREATE INDEX IF NOT EXISTS idx_model_site_wx_ingested
    ON observations.model_site_wx (ingested_at);

-- -----------------------------------------------------------------------------
-- 3. Verify
-- -----------------------------------------------------------------------------
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'observations'
  AND table_name   = 'model_wind_forecasts'
  AND column_name  IN ('tmp_c','dpt_c','precip_mm','dswrf_wm2','wbgt_c')
ORDER BY ordinal_position;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'observations'
  AND table_name   = 'model_site_wx'
ORDER BY ordinal_position;

