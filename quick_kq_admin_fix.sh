#!/bin/bash
# Simple Fix for kq_admin.py Line 290 Syntax Error
# The issue is an incomplete SQL query that needs proper closing

echo "=== FIXING KQ_ADMIN.PY LINE 290 SYNTAX ERROR ==="

cd /var/www/cap_winds_app

# Backup first
cp kq_admin.py kq_admin.py.backup.$(date +%Y%m%d_%H%M%S)
echo "✓ Created backup"

# The issue is around line 290 where there's an incomplete cur.execute() call
# Based on the error, line 290 starts a new cur.execute(""" but the previous one wasn't closed properly

# Create a simple sed-based fix for the missing query termination
sed -i '289s/$/""", (station_id,))/' kq_admin.py
sed -i '290s/^[ \t]*cur\.execute("""/        # Clean up related wind forecast data\n        cur.execute("""/' kq_admin.py

echo "Applied syntax fix to kq_admin.py"

# Test the fix
if python3 -m py_compile kq_admin.py 2>/dev/null; then
    echo "✓ Syntax error fixed successfully"
    
    # Set proper permissions
    chown www-data:www-data kq_admin.py 2>/dev/null || true
    chmod 755 kq_admin.py
    
else
    echo "❌ Fix didn't work, restoring backup"
    cp kq_admin.py.backup.$(date +%Y%m%d_%H%M%S) kq_admin.py
    echo "Manual fix required - the syntax error is more complex"
fi

echo "=== FIX COMPLETE ==="
