-- AirNow PM2.5 observations, fetched via ACT (act.discovery.get_airnow_bounded_obs)
-- Feeds the Cadet Weather COP air-quality stoplight category.
CREATE TABLE IF NOT EXISTS observations.airnow_pm25 (
    id               BIGSERIAL PRIMARY KEY,
    station_id       TEXT NOT NULL,
    station_name     TEXT,
    lat              REAL NOT NULL,
    lon              REAL NOT NULL,
    pm25_ugm3        REAL,
    aqi_value        INTEGER,
    observation_time TIMESTAMPTZ NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uix_airnow_pm25_station_time
    ON observations.airnow_pm25 (station_id, observation_time);
CREATE INDEX IF NOT EXISTS idx_airnow_pm25_latlon
    ON observations.airnow_pm25 (lat, lon);
CREATE INDEX IF NOT EXISTS idx_airnow_pm25_obstime
    ON observations.airnow_pm25 (observation_time DESC);
