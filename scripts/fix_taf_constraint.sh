#!/bin/bash
#
# Check and Fix TAF Database Constraint
#

echo "========================================================================"
echo "TAF Database Constraint Check and Fix"
echo "========================================================================"
echo ""

echo "Step 1: Check current TAF table structure"
echo "------------------------------------------------------------------------"
psql -U avwx_user -d avwx_data -c "\d observations.taf"

echo ""
echo "------------------------------------------------------------------------"
echo "Step 2: Check for UNIQUE constraint"
echo "------------------------------------------------------------------------"
psql -U avwx_user -d avwx_data -c "
SELECT constraint_name, constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'observations'
  AND table_name = 'taf'
  AND constraint_type = 'UNIQUE';
"

echo ""
echo "------------------------------------------------------------------------"
echo "Step 3: Add UNIQUE constraint if missing"
echo "------------------------------------------------------------------------"
psql -U avwx_user -d avwx_data -c "
-- Add UNIQUE constraint on (station_id, issue_time)
-- Use DO to avoid error if constraint already exists
DO \$\$
BEGIN
    -- Try to add the constraint
    ALTER TABLE observations.taf
    ADD CONSTRAINT taf_station_issue_unique 
    UNIQUE (station_id, issue_time);
    
    RAISE NOTICE 'UNIQUE constraint added successfully';
EXCEPTION
    WHEN duplicate_table THEN
        RAISE NOTICE 'UNIQUE constraint already exists';
    WHEN OTHERS THEN
        RAISE NOTICE 'Error adding constraint: %', SQLERRM;
END;
\$\$ LANGUAGE plpgsql;
"

echo ""
echo "------------------------------------------------------------------------"
echo "Step 4: Verify constraint was added"
echo "------------------------------------------------------------------------"
psql -U avwx_user -d avwx_data -c "
SELECT constraint_name, constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'observations'
  AND table_name = 'taf';
"

echo ""
echo "------------------------------------------------------------------------"
echo "Step 5: Test insert with ON CONFLICT"
echo "------------------------------------------------------------------------"
psql -U avwx_user -d avwx_data -c "
-- Test insert
INSERT INTO observations.taf 
    (station_id, issue_time, valid_from, valid_to, raw_text)
VALUES 
    ('TEST', '2026-01-19 08:00:00', '2026-01-19 09:00:00', '2026-01-19 21:00:00', 'TEST TAF')
ON CONFLICT (station_id, issue_time)
DO UPDATE SET raw_text = EXCLUDED.raw_text;

-- Check it worked
SELECT station_id, issue_time FROM observations.taf WHERE station_id = 'TEST';

-- Clean up test
DELETE FROM observations.taf WHERE station_id = 'TEST';
"

echo ""
echo "========================================================================"
echo "Constraint Fix Complete!"
echo "========================================================================"
echo ""
echo "The UNIQUE constraint should now exist on (station_id, issue_time)"
echo "This prevents duplicate TAFs and allows ON CONFLICT to work"
echo ""
echo "Next step: Re-run ingest_taf.py"
echo "========================================================================"

