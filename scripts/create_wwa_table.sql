-- ============================================================
-- CAP WxCOP — observations.wwa table
-- NWS Watch/Warning/Advisory storage for cadet ops map
-- Run as: psql -U avwx_user -d avwx_data -f create_wwa_table.sql
-- ============================================================

-- Requires PostGIS extension (already present)
-- Run once in dev, then repeat in production when ready

CREATE TABLE IF NOT EXISTS observations.wwa (
    id              SERIAL PRIMARY KEY,

    -- VTEC identity fields
    wfo             CHAR(4)      NOT NULL,   -- 4-char WFO e.g. KBMX
    phenomena       CHAR(2)      NOT NULL,   -- e.g. SV, TO, FF, FL
    significance    CHAR(1)      NOT NULL,   -- W=Warning A=Watch Y=Advisory
    event_number    SMALLINT     NOT NULL,   -- ETN (Event Tracking Number)
    vtec_year       SMALLINT     NOT NULL,   -- year of issuance

    -- Action / product metadata
    vtec_action     CHAR(3)      NOT NULL,   -- NEW CON EXT EXA EXB UPG CAN EXP COR
    wmo_header      VARCHAR(16),             -- e.g. WUUS54
    product_id      VARCHAR(64),             -- pyiem product ID string
    issue_time      TIMESTAMPTZ  NOT NULL,   -- product issuance time
    begin_time      TIMESTAMPTZ,             -- VTEC begin (null if 000000T0000Z)
    end_time        TIMESTAMPTZ,             -- VTEC end   (null if 000000T0000Z)

    -- Content
    headline        TEXT,                    -- first headline from segment
    raw_segment     TEXT,                    -- raw segment text (4000 char max)

    -- Geometry — polygon warnings (TOR/SVR/FFW/FLW have LAT...LON)
    -- NULL for zone/county-based products (watches, winter warnings)
    geom            GEOMETRY(POLYGON, 4326),

    -- Zone/county list for non-polygon products
    -- e.g. {MOC099, MOC189} or {NMZ125, NMZ104}
    ugc_zones       TEXT[],

    -- State
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Prevent duplicate ingest of same issuance
    CONSTRAINT wwa_unique_event
        UNIQUE (wfo, phenomena, significance, event_number,
                vtec_action, vtec_year, issue_time)
);

-- ── Indexes ──────────────────────────────────────────────────────────────────

-- Spatial index for point-in-polygon site queries
CREATE INDEX IF NOT EXISTS idx_wwa_geom
    ON observations.wwa USING GIST (geom)
    WHERE geom IS NOT NULL;

-- Fast lookup of active events
CREATE INDEX IF NOT EXISTS idx_wwa_active
    ON observations.wwa (is_active, end_time)
    WHERE is_active = TRUE;

-- Phenomenon/significance filter (for map legend queries)
CREATE INDEX IF NOT EXISTS idx_wwa_phenom
    ON observations.wwa (phenomena, significance, is_active);

-- WFO + ETN lookup (for CAN/EXP updates)
CREATE INDEX IF NOT EXISTS idx_wwa_vtec_id
    ON observations.wwa (wfo, phenomena, significance, event_number, vtec_year);

-- ── Verification ─────────────────────────────────────────────────────────────

SELECT 'observations.wwa table created successfully' AS status;

\d observations.wwa

