#!/bin/bash
# satellite_pipe_writer.sh — LDM PIPE action handler, atomic stdin-to-file writer
#
# Used for ABI L1b archiving (RadC/RadF, both satellites). FILE -close showed
# ~43-67% HDF5 corruption on these products (2026-07-10 investigation, root
# cause unconfirmed -- reproduced on data1's local disk, ruling out NFS).
# PIPE -close through this script tested 32/32 clean at production volume
# before being promoted from a side-rule to the live pqact_satellite.conf rule.
#
# Writes stdin to a temp file then renames atomically, same pattern
# mrms_render_pipe.sh uses.
#
# Called by pqact PIPE action:
#   satellite_pipe_writer.sh <dest_path>
#
# stdin: raw bytes of the LDM product

set -euo pipefail

DEST="${1:?Usage: satellite_pipe_writer.sh <dest_path>}"
LOG=/var/www/cap_winds_app/logs/satellite_pipe.log

mkdir -p "$(dirname "$DEST")"
TMPFILE="${DEST}.tmp.$$"

cat > "$TMPFILE"

if [ ! -s "$TMPFILE" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR empty stdin for ${DEST}" >> "$LOG"
    rm -f "$TMPFILE"
    exit 1
fi

mv "$TMPFILE" "$DEST"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) OK wrote ${DEST} ($(stat -c%s "$DEST") bytes)" >> "$LOG"
