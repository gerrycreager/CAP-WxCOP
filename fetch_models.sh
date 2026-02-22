#!/bin/bash
# HRRR/GFS Model Data Manager - ALL FROM AWS S3
# Downloads new model data from AWS S3 and maintains 30-hour retention
# Backup for LDM failures

set -euo pipefail

# Configuration
ROOT="/var/www/cap_winds_app/model_data"
LOG="${ROOT}/fetch_models.log"
RETENTION_HOURS=30
MIN_FREE_SPACE_GB=50  # Abort if less than this available

# Ensure directories exist
mkdir -p "$ROOT"

# Logging function
log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] $1" | tee -a "$LOG"
}

# Check disk space before proceeding
check_disk_space() {
    local mount_point=$(df "$ROOT" | tail -1 | awk '{print $6}')
    local available_gb=$(df -BG "$ROOT" | tail -1 | awk '{print $4}' | sed 's/G//')
    
    log "Available space on $mount_point: ${available_gb}GB"
    
    if [ "$available_gb" -lt "$MIN_FREE_SPACE_GB" ]; then
        log "ERROR: Insufficient disk space (${available_gb}GB < ${MIN_FREE_SPACE_GB}GB minimum)"
        log "Running emergency cleanup..."
        cleanup_old_data
        
        # Check again after cleanup
        available_gb=$(df -BG "$ROOT" | tail -1 | awk '{print $4}' | sed 's/G//')
        if [ "$available_gb" -lt "$MIN_FREE_SPACE_GB" ]; then
            log "FATAL: Still insufficient space after cleanup. Aborting download."
            exit 1
        fi
    fi
}

# Cleanup old data (>30 hours)
cleanup_old_data() {
    log "=== CLEANUP: Starting removal of data older than ${RETENTION_HOURS} hours ==="
    
    # Count before cleanup
    local before_count=$(find "$ROOT" -type f \( -name "*.grib2" -o -name "gfs.t*" \) 2>/dev/null | wc -l)
    local before_size=$(du -sh "$ROOT" 2>/dev/null | cut -f1)
    
    log "Before cleanup: $before_count files, total size: $before_size"
    
    # Delete HRRR files older than retention period
    local hrrr_deleted=0
    if [ -d "$ROOT" ]; then
        hrrr_deleted=$(find "$ROOT" -type f -name "hrrr.t*.grib2" -mmin +$((RETENTION_HOURS * 60)) -delete -print 2>/dev/null | wc -l)
    fi
    log "Deleted $hrrr_deleted old HRRR files"
    
    # Delete GFS files older than retention period
    local gfs_deleted=0
    if [ -d "$ROOT" ]; then
        gfs_deleted=$(find "$ROOT" -type f -name "gfs.t*.grib2" -mmin +$((RETENTION_HOURS * 60)) -delete -print 2>/dev/null | wc -l)
    fi
    log "Deleted $gfs_deleted old GFS files"
    
    # Remove empty directories
    local empty_dirs=$(find "$ROOT" -type d -empty -delete -print 2>/dev/null | wc -l)
    log "Removed $empty_dirs empty directories"
    
    # Report after cleanup
    local after_count=$(find "$ROOT" -type f \( -name "*.grib2" -o -name "gfs.t*" \) 2>/dev/null | wc -l)
    local after_size=$(du -sh "$ROOT" 2>/dev/null | cut -f1)
    
    log "After cleanup: $after_count files, total size: $after_size"
    log "=== CLEANUP: Complete ==="
}

# Download HRRR data from AWS S3
download_hrrr() {
    log "=== HRRR: Starting download from AWS S3 ==="
    
    # Target cycle ~2 hours ago (HRRR typically available 1-2 hours after cycle)
    local now_utc=$(date -u +%s)
    local cycle_time=$(( now_utc - 2*3600 ))
    local CYCLE_DATE=$(date -u -d "@${cycle_time}" +%Y%m%d)
    local CYCLE_HOUR=$(date -u -d "@${cycle_time}" +%H)
    
    local HRRR_DIR="${ROOT}/hrrr.${CYCLE_DATE}/${CYCLE_HOUR}z"
    mkdir -p "$HRRR_DIR"
    
    local BASE_HRRR_URL="https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.${CYCLE_DATE}/conus"
    
    log "HRRR target cycle: ${CYCLE_DATE} ${CYCLE_HOUR}Z"
    log "HRRR source: AWS S3"
    
    local downloaded=0
    local skipped=0
    local failed=0
    
    for FH in $(seq -w 00 12); do
        local FILE_REMOTE="hrrr.t${CYCLE_HOUR}z.wrfsfcf${FH}.grib2"
        local URL="${BASE_HRRR_URL}/${FILE_REMOTE}"
        local DEST="${HRRR_DIR}/${FILE_REMOTE}"
        
        # Skip if already exists and is non-zero size
        if [ -s "$DEST" ]; then
            ((skipped++))
            continue
        fi
        
        # Download with timeout and retries
        if timeout 300 curl -sS --fail --retry 3 --max-time 180 -o "$DEST.part" "$URL" 2>/dev/null; then
            mv "$DEST.part" "$DEST"
            ((downloaded++))
            log "HRRR: Downloaded f${FH} ($(du -h "$DEST" | cut -f1))"
        else
            ((failed++))
            log "HRRR: FAILED f${FH} - ${URL}"
            rm -f "$DEST.part"
        fi
    done
    
    log "HRRR: Downloaded=$downloaded, Skipped=$skipped, Failed=$failed"
    log "=== HRRR: Complete ==="
}

# Download GFS data from AWS S3
download_gfs() {
    log "=== GFS: Starting download from AWS S3 ==="
    
    # Target cycle ~6 hours ago (GFS available 4-5+ hours after cycle)
    # GFS runs at 00Z, 06Z, 12Z, 18Z
    local now_utc=$(date -u +%s)
    local gfs_time=$(( now_utc - 6*3600 ))
    local GFS_CYCLE_DATE=$(date -u -d "@${gfs_time}" +%Y%m%d)
    local GFS_CYCLE_HOUR=$(date -u -d "@${gfs_time}" +%H)
    
    # Round to nearest 6-hour cycle (00, 06, 12, 18)
    GFS_CYCLE_HOUR=$(( (GFS_CYCLE_HOUR / 6) * 6 ))
    GFS_CYCLE_HOUR=$(printf "%02d" $GFS_CYCLE_HOUR)
    
    local GFS_DIR="${ROOT}/gfs.${GFS_CYCLE_DATE}/${GFS_CYCLE_HOUR}z"
    mkdir -p "$GFS_DIR"
    
    # GFS on AWS S3: https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.YYYYMMDD/HH/atmos/
    local BASE_GFS_URL="https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.${GFS_CYCLE_DATE}/${GFS_CYCLE_HOUR}/atmos"
    
    log "GFS target cycle: ${GFS_CYCLE_DATE} ${GFS_CYCLE_HOUR}Z"
    log "GFS source: AWS S3"
    
    local downloaded=0
    local skipped=0
    local failed=0
    
    for FH in $(seq -w 000 012); do
        local FILE_REMOTE="gfs.t${GFS_CYCLE_HOUR}z.pgrb2.0p25.f${FH}"
        local URL="${BASE_GFS_URL}/${FILE_REMOTE}"
        local DEST="${GFS_DIR}/${FILE_REMOTE}"
        
        # Skip if already exists and is non-zero size
        if [ -s "$DEST" ]; then
            ((skipped++))
            continue
        fi
        
        # Download with timeout and retries
        if timeout 300 curl -sS --fail --retry 3 --max-time 180 -o "$DEST.part" "$URL" 2>/dev/null; then
            mv "$DEST.part" "$DEST"
            ((downloaded++))
            log "GFS: Downloaded f${FH} ($(du -h "$DEST" | cut -f1))"
        else
            ((failed++))
            log "GFS: FAILED f${FH} - ${URL}"
            rm -f "$DEST.part"
            
            # If S3 fails, try NOMADS as fallback
            log "GFS: Trying NOMADS fallback for f${FH}..."
            local NOMADS_URL="https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.${GFS_CYCLE_DATE}/${GFS_CYCLE_HOUR}/atmos/${FILE_REMOTE}"
            
            if timeout 300 curl -sS --fail --retry 2 --max-time 180 -o "$DEST.part" "$NOMADS_URL" 2>/dev/null; then
                mv "$DEST.part" "$DEST"
                ((downloaded++))
                ((failed--))
                log "GFS: Downloaded f${FH} from NOMADS fallback ($(du -h "$DEST" | cut -f1))"
            else
                log "GFS: NOMADS fallback also failed for f${FH}"
                rm -f "$DEST.part"
            fi
        fi
    done
    
    log "GFS: Downloaded=$downloaded, Skipped=$skipped, Failed=$failed"
    log "=== GFS: Complete ==="
}

# Set proper permissions
fix_permissions() {
    log "Setting permissions (www-data:www-data, 750)..."
    chown -R www-data:www-data "$ROOT" 2>/dev/null || log "WARNING: Could not chown (may need sudo)"
    chmod -R 750 "$ROOT" 2>/dev/null || log "WARNING: Could not chmod (may need sudo)"
}

# Generate summary report
generate_summary() {
    log "=== SUMMARY ==="
    
    # Count files by type
    local hrrr_count=$(find "$ROOT" -name "hrrr.t*.grib2" -type f 2>/dev/null | wc -l)
    local gfs_count=$(find "$ROOT" -name "gfs.t*.grib2" -type f 2>/dev/null | wc -l)
    local total_size=$(du -sh "$ROOT" 2>/dev/null | cut -f1)
    
    # Find oldest and newest
    local oldest=$(find "$ROOT" -type f -name "*.grib2" -printf '%T+ %p\n' 2>/dev/null | sort | head -1 | cut -d' ' -f1)
    local newest=$(find "$ROOT" -type f -name "*.grib2" -printf '%T+ %p\n' 2>/dev/null | sort | tail -1 | cut -d' ' -f1)
    
    log "HRRR files: $hrrr_count"
    log "GFS files: $gfs_count"
    log "Total size: $total_size"
    log "Oldest file: ${oldest:-none}"
    log "Newest file: ${newest:-none}"
    
    # Disk space
    local mount_point=$(df "$ROOT" | tail -1 | awk '{print $6}')
    local available=$(df -h "$ROOT" | tail -1 | awk '{print $4}')
    local used_pct=$(df -h "$ROOT" | tail -1 | awk '{print $5}')
    
    log "Disk usage on $mount_point: $used_pct used, $available available"
    log "=== END SUMMARY ==="
}

# Main execution
main() {
    log "========================================"
    log "=== MODEL DATA MANAGER STARTING ==="
    log "Source: AWS S3 (with NOMADS fallback for GFS)"
    log "Retention policy: ${RETENTION_HOURS} hours"
    log "========================================"
    
    # Step 1: Cleanup old data FIRST (free space before download)
    cleanup_old_data
    
    # Step 2: Check if we have enough space
    check_disk_space
    
    # Step 3: Download new HRRR data from S3
    download_hrrr
    
    # Step 4: Download new GFS data from S3 (with NOMADS fallback)
    download_gfs
    
    # Step 5: Fix permissions
    fix_permissions
    
    # Step 6: Generate summary
    generate_summary
    
    log "========================================"
    log "=== MODEL DATA MANAGER COMPLETE ==="
    log "========================================"
}

# Run main function
main

# Exit with success
exit 0

