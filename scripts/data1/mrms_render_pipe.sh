#!/bin/bash
# mrms_render_pipe.sh — LDM PIPE action handler for MRMS products
# Last modified: 2026-11-13
#
# Called by pqact PIPE action:
#   mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>
#
# pqact EXP pattern isolates date (\1) and time (\2) from product ID:
#   ProductName_HH.HH_([0-9]{8})-([0-9]{6})\.grib2\.gz$
#
# stdin: raw grib2.gz bytes from LDM product queue
#
# REDESIGNED 2026-11-13 to remove a real data-loss race condition. The
# old version used a bounded-retry mkdir lock (5 tries, 2s apart = 10s
# total) per (product,sector), and on giveup DROPPED the incoming
# product entirely (`cat > /dev/null`). Two live attempts to add a
# concurrency cap to mrms_tile_renderer.py (a hard serializing lock,
# then a 2-slot semaphore) both made this worse: any added queueing
# delay on top of an already-running render could push the *next*
# arrival for that same product past the 10s giveup, and that frame
# was gone for good -- confirmed live, real frames lost both times.
#
# This version never drops a received file. Every invocation always
# writes its stdin into a durable per-(product,sector) spool directory
# first (an ordinary file write, never blocks, never fails short of a
# real I/O error) -- that part alone can no longer race. Separately,
# exactly one process per (product,sector) becomes that key's "drainer"
# (a non-blocking flock(1) on a dedicated lock file) and processes the
# spool oldest-file-first, in a loop, until the spool is empty *while
# still holding the lock* -- the classic queue-drain pattern, safe
# against the lost-wakeup race because the lock is never released
# between "check if anything's left" and "give up." A file that arrives
# while a drain is in progress is simply picked up on that same drainer's
# next loop iteration; a file that arrives in the narrow window right as
# a drainer is exiting just waits, undeleted, for the next arrival's
# invocation to open the now-free lock and drain it -- worst case it
# renders one cycle late, it is never silently lost.

set -euo pipefail

PRODUCT="${1:?Usage: mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>}"
SECTOR="${2:?Usage: mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>}"
DATE="${3:-}"     # YYYYMMDD from pqact \1
TIME="${4:-}"     # HHMMSS   from pqact \2

VENV_PYTHON=/var/www/cap_winds_app/venv/bin/python3
RENDERER=/home/ldm/bin/mrms_tile_renderer.py
LOG=/var/www/cap_winds_app/logs/mrms_renderer.log
SPOOL_BASE=/tmp/mrms_render/spool
LOCK_DIR=/tmp/mrms_locks

KEY="${PRODUCT}_${SECTOR}"
SPOOL_DIR="${SPOOL_BASE}/${KEY}"
DRAIN_LOCK="${LOCK_DIR}/${KEY}.drain.lock"

mkdir -p "$SPOOL_DIR" "$LOCK_DIR"

# Build a filename guaranteed unique to this invocation ($$ = this
# script's own PID) so two near-simultaneous arrivals for the exact
# same product/sector/timestamp can never collide or overwrite
# each other in the spool -- each gets rendered as its own frame.
if [[ -n "$DATE" && -n "$TIME" ]]; then
    STAMP="${DATE}-${TIME}"
else
    STAMP=$(date -u +%Y%m%d-%H%M%S)
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN ${PRODUCT}/${SECTOR}: no timestamp args, using wallclock" \
        >> "$LOG"
fi
SPOOLFILE="${SPOOL_DIR}/${STAMP}.$$.grib2.gz"
trap 'rm -f "$SPOOLFILE"' EXIT

cat > "$SPOOLFILE"

if [ ! -s "$SPOOLFILE" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR ${PRODUCT}/${SECTOR}: empty stdin, discarding" \
        >> "$LOG"
    exit 1
fi
# From here on the file must survive even after this invocation exits
# (it may not be the one that renders it) -- drop the delete-on-exit trap.
trap - EXIT

# Try to become this key's drainer. Non-blocking: if another
# invocation is already draining, our job is done -- the file is
# durably spooled and that drainer's loop (or whichever invocation
# next acquires this lock) will pick it up. Nothing to retry, nothing
# to time out.
exec 9>"$DRAIN_LOCK"
if ! flock -n 9; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) QUEUED ${PRODUCT}/${SECTOR}: spooled $(basename "$SPOOLFILE"), another drainer is active" \
        >> "$LOG"
    exit 0
fi

# We're the drainer. Loop oldest-first until the spool is empty --
# re-list it fresh each pass (inside the held lock) so anything that
# lands mid-drain still gets picked up this same pass.
while :; do
    NEXT=$(ls -1 "$SPOOL_DIR" 2>/dev/null | sort | head -1 || true)
    [ -z "$NEXT" ] && break

    FRAME="${SPOOL_DIR}/${NEXT}"
    SIZE=$(stat -c%s "$FRAME" 2>/dev/null || echo 0)
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START ${PRODUCT}/${SECTOR}: rendering ${NEXT} (${SIZE} bytes)" \
        >> "$LOG"

    if ! "$VENV_PYTHON" "$RENDERER" "$PRODUCT" "$SECTOR" "$FRAME"; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR ${PRODUCT}/${SECTOR}: renderer failed on ${NEXT}" \
            >> "$LOG"
    fi
    rm -f "$FRAME"
done

flock -u 9
