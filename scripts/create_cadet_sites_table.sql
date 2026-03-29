-- ============================================================
-- CAP WxCOP: cadet_sites table
-- Schema + seed data for Cadet Ops Weather Map
-- Run as: psql -U avwx_user -d avwx_data -f create_cadet_sites_table.sql
-- ============================================================

SET search_path = observations, public;

-- ------------------------------------------------------------
-- Drop and recreate (safe for dev / initial deploy)
-- ------------------------------------------------------------
DROP TABLE IF EXISTS observations.cadet_sites CASCADE;

CREATE TABLE observations.cadet_sites (
    id                  SERIAL PRIMARY KEY,
    site_name           TEXT NOT NULL,              -- Display name: "NESA / Camp Atterbury"
    unit                TEXT,                       -- CAP unit string: "GLR-IL-001", "NHQ", etc.
    site_type           TEXT NOT NULL DEFAULT 'ground',
                        -- 'airfield'  = associated with an ICAO airport
                        -- 'ground'    = coordinate-only ground activity site
                        -- 'kq'        = CAP encampment / KQ activity site
    station_id          CHAR(4),                    -- ICAO of associated airfield (nullable)
    wx_station_override CHAR(4),                    -- Force a specific METAR source station.
                        --   NULL = use station_id if has METAR, else nearest <=25nm search
                        --   set  = always use this station regardless of proximity/station_id
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    elevation_ft        INTEGER,
    description         TEXT,                       -- Shown in site detail popup
    cap_region          TEXT,                       -- NER/MAR/SER/GLR/NCR/RMR/SWR/PCR
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cadet_sites_active    ON observations.cadet_sites (is_active);
CREATE INDEX idx_cadet_sites_region    ON observations.cadet_sites (cap_region);
CREATE INDEX idx_cadet_sites_station   ON observations.cadet_sites (station_id);
CREATE INDEX idx_cadet_sites_wx_ovride ON observations.cadet_sites (wx_station_override);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION observations.cadet_sites_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

CREATE TRIGGER trg_cadet_sites_updated_at
    BEFORE UPDATE ON observations.cadet_sites
    FOR EACH ROW EXECUTE FUNCTION observations.cadet_sites_set_updated_at();

-- ============================================================
-- SEED DATA
-- ============================================================

-- Site 1: NESA / Camp Atterbury IN
--   Himsel AAF (KHBE) has no active METAR.
--   wx_station_override = KBAK (Columbus Muni, ~25nm E) — CAP wx ops personnel on site.
INSERT INTO observations.cadet_sites
    (site_name, unit, site_type, station_id, wx_station_override,
     lat, lon, elevation_ft, description, cap_region)
VALUES (
    'NESA / Camp Atterbury',
    'NHQ',
    'airfield',
    'KHBE',
    'KBAK',
    39.3417, -86.0305,
    710,
    'National Emergency Services Academy at Camp Atterbury, IN. '
    || 'Himsel AAF (KHBE) — no METAR currently active. '
    || 'Weather from KBAK (Columbus Municipal, ~25nm E) '
    || 'where CAP weather operations personnel are stationed during NESA.',
    'GLR'
);

-- Site 2: Hawk Mountain Ranger School, Kempton PA
--   No airfield. Coordinate-only ground site. Nearest METAR auto-selected.
INSERT INTO observations.cadet_sites
    (site_name, unit, site_type, station_id, wx_station_override,
     lat, lon, elevation_ft, description, cap_region)
VALUES (
    'Hawk Mountain Ranger School',
    'PA-001',
    'ground',
    NULL,
    NULL,
    40.6257, -75.9395,
    1100,
    'Col. Phillip Neuweiler Memorial Training Center, Kempton PA. '
    || 'CAP Ranger SAR training facility on the Blue Mountain Ridge, Berks County. '
    || 'Weather sourced from nearest reporting station within 25nm.',
    'MAR'
);

-- Site 3: NESA Southeast / Maxwell AFB AL
--   KMXF has METAR. Use directly.
INSERT INTO observations.cadet_sites
    (site_name, unit, site_type, station_id, wx_station_override,
     lat, lon, elevation_ft, description, cap_region)
VALUES (
    'NESA Southeast / Maxwell AFB',
    'NHQ',
    'airfield',
    'KMXF',
    NULL,
    32.3829, -86.3658,
    171,
    'National Emergency Services Academy Southeast at Maxwell-Gunter AFB, Montgomery AL. '
    || 'HQ Civil Air Patrol. Weather from KMXF ASOS (augmented by 26th OWS).',
    'SER'
);

-- ============================================================
-- Verify
-- ============================================================
SELECT id, site_name, site_type, station_id, wx_station_override,
       lat, lon, cap_region, is_active
FROM observations.cadet_sites
ORDER BY id;

