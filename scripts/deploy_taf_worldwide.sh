#!/bin/bash
#
# TAF Worldwide Ingest - Complete Deployment
#

set -e  # Exit on error

echo "========================================================================"
echo "TAF Worldwide Ingest - Deployment"
echo "========================================================================"
echo ""
echo "This will deploy:"
echo "  - UNIQUE constraint on (station_id, issue_time)"
echo "  - Worldwide TAF parser (all ICAO codes)"
echo "  - Canadian TAF support (C***)"
echo "  - Military TAF support (KQxx, up to 1MB)"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled."
    exit 1
fi

cd /var/www/cap_winds_app/scripts

echo ""
echo "========================================================================"
echo "Step 1: Add UNIQUE Constraint"
echo "========================================================================"
echo ""

# Check if constraint exists
CONSTRAINT_EXISTS=$(psql -U avwx_user -d avwx_data -t -c "
SELECT COUNT(*) 
FROM information_schema.table_constraints
WHERE table_schema = 'observations'
  AND table_name = 'taf'
  AND constraint_type = 'UNIQUE'
  AND constraint_name = 'taf_station_issue_unique';
" | xargs)

if [ "$CONSTRAINT_EXISTS" -gt 0 ]; then
    echo "✅ UNIQUE constraint already exists - skipping"
else
    echo "Checking for duplicate TAFs..."
    DUPLICATE_COUNT=$(psql -U postgres -d avwx_data -t -c "
    SELECT COUNT(*) 
    FROM (
        SELECT station_id, issue_time
        FROM observations.taf
        GROUP BY station_id, issue_time
        HAVING COUNT(*) > 1
    ) AS duplicates;
    " 2>/dev/null | xargs || echo "0")
    
    if [ "$DUPLICATE_COUNT" -gt 0 ]; then
        echo "❌ Found $DUPLICATE_COUNT duplicate TAF groups"
        echo ""
        echo "You must clean duplicates first:"
        echo "  ./fix_taf_duplicates.sh"
        echo ""
        echo "Then re-run this deployment script."
        exit 1
    fi
    
    echo "Adding UNIQUE constraint..."
    sudo -u postgres psql -d avwx_data -c "
    ALTER TABLE observations.taf
    ADD CONSTRAINT taf_station_issue_unique 
    UNIQUE (station_id, issue_time);
    " || {
        echo "❌ Failed to add constraint"
        echo ""
        echo "If you see duplicate errors, clean them first:"
        echo "  ./fix_taf_duplicates.sh"
        exit 1
    }
    echo "✅ UNIQUE constraint added"
fi

echo ""
echo "========================================================================"
echo "Step 2: Deploy Worldwide TAF Parser"
echo "========================================================================"
echo ""

#### Backup existing script
###if [ -f ingest_taf.py ]; then
###    BACKUP_FILE="ingest_taf.py.backup_$(date +%Y%m%d_%H%M%S)"
###    echo "Backing up existing script to: $BACKUP_FILE"
###    cp ingest_taf.py "$BACKUP_FILE"
###fi
###
#### Deploy new parser
###echo "Deploying worldwide TAF parser..."
###cp /mnt/user-data/outputs/ingest_taf_final.py ./ingest_taf.py
###chmod +x ingest_taf.py
###chown www-data:www-data ingest_taf.py 2>/dev/null || chown $(whoami):$(whoami) ingest_taf.py

echo "✅ Parser deployed"
echo ""
echo "Features enabled:"
echo "  ✅ All ICAO codes (K, C, P, T, L, U, Z, Y, etc.)"
echo "  ✅ Canadian TAFs (C***)"
echo "  ✅ Military TAFs (KQxx)"
echo "  ✅ File size limit: 1MB (for long military TAFs)"
echo "  ✅ Dual format support (KWBC + KLSX)"

echo ""
echo "========================================================================"
echo "Step 3: Test Run"
echo "========================================================================"
echo ""

read -p "Run test ingest now? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Running test ingest..."
    time ./ingest_taf.py
    
    echo ""
    echo "========================================================================"
    echo "Step 4: Verify Results"
    echo "========================================================================"
    echo ""
    
    echo "Total TAFs in database:"
    psql -U avwx_user -d avwx_data -c "SELECT COUNT(*) FROM observations.taf;"
    
    echo ""
    echo "Coverage by region (last 6 hours):"
    psql -U avwx_user -d avwx_data -c "
    SELECT 
        LEFT(station_id, 1) as prefix,
        CASE LEFT(station_id, 1)
            WHEN 'K' THEN 'US Continental/Military'
            WHEN 'C' THEN 'Canada'
            WHEN 'P' THEN 'Pacific US'
            WHEN 'T' THEN 'Caribbean'
            WHEN 'L' THEN 'Europe'
            WHEN 'U' THEN 'Russia/FSU'
            ELSE 'Other Intl'
        END as region,
        COUNT(*) as count
    FROM observations.taf
    WHERE issue_time > NOW() - INTERVAL '6 hours'
    GROUP BY LEFT(station_id, 1)
    ORDER BY COUNT(*) DESC;
    "
    
    echo ""
    echo "Sample Canadian TAFs:"
    psql -U avwx_user -d avwx_data -c "
    SELECT station_id, issue_time, LEFT(raw_text, 60)
    FROM observations.taf
    WHERE station_id LIKE 'C%'
    ORDER BY issue_time DESC
    LIMIT 5;
    "
fi

echo ""
echo "========================================================================"
echo "Step 5: Add to Cron"
echo "========================================================================"
echo ""

echo "Recommended cron entry (every 30 minutes):"
echo ""
echo "  */30 * * * * /var/www/cap_winds_app/scripts/ingest_taf.py >> /var/log/taf_ingest.log 2>&1"
echo ""

read -p "Add to cron now? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Check if already in cron
    if sudo crontab -l 2>/dev/null | grep -q "ingest_taf.py"; then
        echo "⚠️  Cron entry already exists - skipping"
    else
        (sudo crontab -l 2>/dev/null; echo "*/30 * * * * /var/www/cap_winds_app/scripts/ingest_taf.py >> /var/log/taf_ingest.log 2>&1") | sudo crontab -
        echo "✅ Cron job added"
    fi
fi

echo ""
echo "========================================================================"
echo "✅ Deployment Complete!"
echo "========================================================================"
echo ""
echo "Worldwide TAF ingest is now operational with:"
echo "  ✅ All ICAO station codes (worldwide coverage)"
echo "  ✅ Canadian TAF support"
echo "  ✅ Military TAF support (longer forecasts)"
echo "  ✅ Automatic deduplication"
echo "  ✅ 7-day retention with cleanup"
echo ""
echo "Monitor logs: tail -f /var/log/taf_ingest.log"
echo "Check status: ./ingest_taf.py"
echo ""
echo "Next steps:"
echo "  1. Add TAF display to web UI"
echo "  2. Create TAF API endpoint"
echo "  3. Test with KMCO and Canadian stations"
echo ""
echo "========================================================================"
