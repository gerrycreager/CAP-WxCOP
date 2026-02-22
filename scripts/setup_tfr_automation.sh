#!/bin/bash
# TFR Automated Update Setup Script
# Sets up cron jobs for regular TFR data updates from ESRI/FAA

set -e

# Configuration
SCRIPT_DIR="/var/www/cap_winds_app/scripts"
LOG_DIR="/var/log"
CRON_USER="root"

echo "Setting up automated TFR ingestion from ESRI/FAA..."

# Create log directory if it doesn't exist
mkdir -p ${LOG_DIR}

# Make the TFR ingestion script executable
chmod +x ${SCRIPT_DIR}/ingest_tfr_esri.py

# Backup existing crontab
crontab -l > /tmp/crontab_backup_$(date +%Y%m%d_%H%M%S) 2>/dev/null || echo "No existing crontab"

# Create new cron job for TFR updates every 30 minutes
echo "Adding TFR cron job..."
(crontab -l 2>/dev/null || echo "") | grep -v "ingest_tfr_esri" > /tmp/crontab_new

# Add TFR ingestion job
cat >> /tmp/crontab_new << EOF

# TFR Data Ingestion from ESRI/FAA - Every 30 minutes
*/30 * * * * ${SCRIPT_DIR}/ingest_tfr_esri.py >> ${LOG_DIR}/tfr_ingestion.log 2>&1

# TFR Log rotation - Daily at 2 AM
0 2 * * * find ${LOG_DIR} -name "tfr_ingestion.log*" -mtime +7 -delete
EOF

# Install new crontab
crontab /tmp/crontab_new

# Verify cron job was added
echo "Verifying cron job installation..."
crontab -l | grep -q "ingest_tfr_esri" && echo "✅ TFR cron job installed successfully" || echo "❌ Failed to install cron job"

# Test the TFR ingestion script
echo "Testing TFR ingestion script..."
if ${SCRIPT_DIR}/ingest_tfr_esri.py; then
    echo "✅ TFR ingestion test successful"
else
    echo "❌ TFR ingestion test failed - check logs"
fi

# Show current cron jobs
echo "Current cron jobs:"
crontab -l | grep -v "^#" | grep -v "^$"

echo "TFR automation setup complete!"
echo "Log file: ${LOG_DIR}/tfr_ingestion.log"
echo "Cron schedule: Every 30 minutes"
echo "Next run: $(date -d '+30 minutes' '+%Y-%m-%d %H:%M')"
