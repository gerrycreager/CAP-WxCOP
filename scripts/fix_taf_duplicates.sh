#!/bin/bash
#
# Clean TAF duplicates and add UNIQUE constraint
#

echo "========================================================================"
echo "TAF Table - Clean Duplicates and Add Constraint"
echo "========================================================================"
echo ""

echo "Step 1: Check for duplicates"
echo "------------------------------------------------------------------------"
psql -U postgres -d avwx_data -c "
SELECT station_id, issue_time, COUNT(*) as duplicate_count
FROM observations.taf
GROUP BY station_id, issue_time
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
LIMIT 20;
"

echo ""
echo "Total duplicate groups:"
DUPLICATE_COUNT=$(psql -U postgres -d avwx_data -t -c "
SELECT COUNT(*) 
FROM (
    SELECT station_id, issue_time
    FROM observations.taf
    GROUP BY station_id, issue_time
    HAVING COUNT(*) > 1
) AS duplicates;
" | xargs)

echo "Found $DUPLICATE_COUNT groups with duplicates"
echo ""

if [ "$DUPLICATE_COUNT" -eq 0 ]; then
    echo "✅ No duplicates found - adding constraint..."
else
    echo "Step 2: Remove duplicates (keeping newest by created_at)"
    echo "------------------------------------------------------------------------"
    
    read -p "Remove $DUPLICATE_COUNT duplicate groups? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled - no changes made"
        exit 1
    fi
    
    echo "Removing duplicates..."
    
    psql -U postgres -d avwx_data << 'EOF'
-- Create temporary table with row numbers
WITH ranked_tafs AS (
    SELECT 
        id,
        station_id,
        issue_time,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY station_id, issue_time 
            ORDER BY created_at DESC NULLS LAST, id DESC
        ) as rn
    FROM observations.taf
)
-- Delete all but the most recent (rn=1) for each duplicate group
DELETE FROM observations.taf
WHERE id IN (
    SELECT id 
    FROM ranked_tafs 
    WHERE rn > 1
);
EOF
    
    DELETED=$(psql -U postgres -d avwx_data -t -c "SELECT lastval();" 2>/dev/null || echo "done")
    echo "✅ Duplicates removed"
fi

echo ""
echo "Step 3: Verify no duplicates remain"
echo "------------------------------------------------------------------------"
REMAINING=$(psql -U postgres -d avwx_data -t -c "
SELECT COUNT(*) 
FROM (
    SELECT station_id, issue_time
    FROM observations.taf
    GROUP BY station_id, issue_time
    HAVING COUNT(*) > 1
) AS duplicates;
" | xargs)

if [ "$REMAINING" -gt 0 ]; then
    echo "❌ Still have $REMAINING duplicate groups - cannot add constraint"
    exit 1
fi

echo "✅ No duplicates - ready to add constraint"

echo ""
echo "Step 4: Add UNIQUE constraint"
echo "------------------------------------------------------------------------"

psql -U postgres -d avwx_data << 'EOF'
-- Add the constraint
ALTER TABLE observations.taf
ADD CONSTRAINT taf_station_issue_unique 
UNIQUE (station_id, issue_time);

-- Verify it was added
SELECT 
    constraint_name, 
    constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'observations'
  AND table_name = 'taf'
  AND constraint_type = 'UNIQUE';
EOF

echo ""
echo "Step 5: Test the constraint"
echo "------------------------------------------------------------------------"

psql -U avwx_user -d avwx_data << 'EOF'
-- Test insert with ON CONFLICT
INSERT INTO observations.taf 
    (station_id, issue_time, valid_from, valid_to, raw_text)
VALUES 
    ('TEST', '2026-01-19 10:00:00', '2026-01-19 11:00:00', '2026-01-19 23:00:00', 'TEST TAF 1')
ON CONFLICT (station_id, issue_time)
DO UPDATE SET raw_text = EXCLUDED.raw_text;

-- Insert duplicate (should update, not error)
INSERT INTO observations.taf 
    (station_id, issue_time, valid_from, valid_to, raw_text)
VALUES 
    ('TEST', '2026-01-19 10:00:00', '2026-01-19 11:00:00', '2026-01-19 23:00:00', 'TEST TAF 2 - UPDATED')
ON CONFLICT (station_id, issue_time)
DO UPDATE SET raw_text = EXCLUDED.raw_text;

-- Verify update worked
SELECT station_id, issue_time, raw_text 
FROM observations.taf 
WHERE station_id = 'TEST';

-- Clean up
DELETE FROM observations.taf WHERE station_id = 'TEST';
EOF

echo ""
echo "========================================================================"
echo "✅ Success!"
echo "========================================================================"
echo ""
echo "Database is now ready with:"
echo "  ✅ No duplicate TAFs"
echo "  ✅ UNIQUE constraint on (station_id, issue_time)"
echo "  ✅ ON CONFLICT DO UPDATE working"
echo ""
echo "Next step: Deploy worldwide TAF ingest"
echo "  ./deploy_taf_worldwide.sh"
echo ""
echo "========================================================================"

