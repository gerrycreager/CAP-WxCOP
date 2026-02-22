-- TFR Complete SQL Ingestion Script - CORRECTED VERSION
-- Fixed to match actual database schema
-- Run with: psql -U avwx_user -d avwx_data -f tfr_complete_fixed.sql

-- First, let's check and add missing columns if needed
ALTER TABLE observations.tfr ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE observations.tfr ADD COLUMN IF NOT EXISTS effective_start TIMESTAMP;
ALTER TABLE observations.tfr ADD COLUMN IF NOT EXISTS effective_end TIMESTAMP;
ALTER TABLE observations.tfr ADD COLUMN IF NOT EXISTS geometry GEOMETRY(GEOMETRY, 4326);

-- Add spatial index if geometry column was added
CREATE INDEX IF NOT EXISTS idx_tfr_geometry ON observations.tfr USING GIST (geometry);

-- Deactivate all existing TFRs
UPDATE observations.tfr SET active = FALSE;

-- Insert all active TFRs (using minimal columns to avoid schema issues)
INSERT INTO observations.tfr
(tfr_number, notam_id, facility, state, type, city, description, active, raw_data)
VALUES
('6/7321', '6/7321', 'ZAB', 'AZ', 'SECURITY', 'AZ', 'Libby AAF, AZ, Friday, February 20, 2026 through Sunday, February 22, 2026 Local', TRUE, '{"notam_id": "6/7321", "type": "SECURITY", "facility": "ZAB", "state": "AZ", "description": "Libby AAF, AZ, Friday, February 20, 2026 through Sunday, February 22, 2026 Local", "creation_date": "02/20/2026"}'),
('6/7255', '6/7255', 'ZKC', 'MO', 'HAZARDS', 'MO', '10NM NE OF WAYNESVILLE, MO, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC', TRUE, '{"notam_id": "6/7255", "type": "HAZARDS", "facility": "ZKC", "state": "MO", "description": "10NM NE OF WAYNESVILLE, MO, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC", "creation_date": "02/19/2026"}'),
('6/7254', '6/7254', 'ZKC', 'MO', 'HAZARDS', 'MO', '14 NM SE OF WAYNESVILLE, MO, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC', TRUE, '{"notam_id": "6/7254", "type": "HAZARDS", "facility": "ZKC", "state": "MO", "description": "14 NM SE OF WAYNESVILLE, MO, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC", "creation_date": "02/19/2026"}'),
('6/6628', '6/6628', 'ZHU', 'TX', 'HAZARDS', 'TX', '15NM SW OF HUNTSVILLE, TX, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC', TRUE, '{"notam_id": "6/6628", "type": "HAZARDS", "facility": "ZHU", "state": "TX", "description": "15NM SW OF HUNTSVILLE, TX, Friday, February 20, 2026 through Saturday, February 21, 2026 UTC", "creation_date": "02/19/2026"}'),
('6/6516', '6/6516', 'ZMA', 'FL', 'SPACE OPERATIONS', 'FL', 'Cape Canaveral, FL, Saturday, February 21, 2026 Local', TRUE, '{"notam_id": "6/6516", "type": "SPACE OPERATIONS", "facility": "ZMA", "state": "FL", "description": "Cape Canaveral, FL, Saturday, February 21, 2026 Local", "creation_date": "02/19/2026"}'),
('6/5649', '6/5649', 'ZDC', 'DC', 'VIP', 'DC', 'Washington, DC, Tuesday, February 24, 2026 Local', TRUE, '{"notam_id": "6/5649", "type": "VIP", "facility": "ZDC", "state": "DC", "description": "Washington, DC, Tuesday, February 24, 2026 Local", "creation_date": "02/18/2026"}')

ON CONFLICT (notam_id) DO UPDATE SET
    active = TRUE,
    facility = EXCLUDED.facility,
    state = EXCLUDED.state,
    type = EXCLUDED.type,
    description = EXCLUDED.description,
    raw_data = EXCLUDED.raw_data;

-- Verification queries
SELECT COUNT(*), type FROM observations.tfr WHERE active = TRUE GROUP BY type ORDER BY COUNT(*) DESC;
SELECT COUNT(*) as total_active_tfrs FROM observations.tfr WHERE active = TRUE;

