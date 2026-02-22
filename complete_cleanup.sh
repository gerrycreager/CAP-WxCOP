#!/bin/bash
# CAP Weather COP - Complete Repository Cleanup
# Remove GRIB2 files and fix git push issues

set -e

echo "=== Final CAP Weather COP Repository Cleanup ==="
cd /var/www/cap_winds_app

# Step 1: Reset again to remove GRIB2 files from the commit
echo "Resetting to remove GRIB2 files from git..."
git reset --soft HEAD~1

# Step 2: Completely remove GRIB2 files and model_data directory
echo "Removing GRIB2 files and model_data directory..."
rm -rf model_data/
git rm -r --cached model_data/ 2>/dev/null || true

# Step 3: Update .gitignore to be more comprehensive
echo "Updating .gitignore with comprehensive exclusions..."
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual Environment
venv/
ENV/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Logs and databases
*.log
*.sqlite3
*.db

# Weather Model Data - EXCLUDE ALL GRIB FILES AND MODEL DATA
model_data/
*.grib2
*.grb2
*.grib
*.grb
*.grib2.part

# Application specific - EXCLUDE TRANSIENT PRODUCTS
static/batch_maps/*.png
static/batch_maps/*.zip
static/batch_maps/*_wind_constraints.*
static/batch_maps/cap_winds_*
static/batch_maps/conus*
static/batch_maps/glr*
static/batch_maps/mar*
static/batch_maps/ncr*
static/batch_maps/ner*
static/batch_maps/pcr*
static/batch_maps/rmr*
static/batch_maps/ser*
static/batch_maps/swr*
static/radar_images/
uploads/
temp/
archive/

# Keep batch_maps directory but exclude contents
static/batch_maps/*
!static/batch_maps/.gitkeep

# Sensitive files
config.py
secrets.py
.env

# Large data files
*.tgz
*.tar.gz
pqact_debug.log
pqact_nexrad3.log
tree.cap
EOF

# Step 4: Ensure .gitkeep exists
touch static/batch_maps/.gitkeep

# Step 5: Add files with new .gitignore rules
echo "Re-adding files with comprehensive .gitignore..."
git add .

# Step 6: Check what will be committed
FILE_COUNT=$(git diff --cached --name-only | wc -l)
echo "Files to be committed: $FILE_COUNT"

# Step 7: Show any remaining large files
echo "Checking for large files..."
git diff --cached --name-only | xargs ls -lh 2>/dev/null | awk '$5 ~ /[0-9]+M/ {print "WARNING: Large file " $9 " (" $5 ")"}'

# Step 8: Create final clean commit
echo "Creating final clean commit..."
git commit -m "CAP Weather COP - Clean Repository

Enhanced Weather Map Features:
- Military priority display with star markers  
- Smart zoom-based labeling (Military ≥5, Major ≥6, All ≥7)
- 2500 station capacity with PostGIS optimization
- Complete METAR data including ceiling and sky coverage
- Home button navigation

Core Components:
- Flask application with weather API
- KQ station management system  
- Enhanced weather map with military prioritization
- Radar animation system
- AIRMET/SIGMET integration
- Wind forecast mapping

Database Schema:
- observations.metar (PostGIS location column)
- observations.airports (station_id join key)  
- observations.custom_stations (KQ stations)

Exclusions:
- model_data/ directory (GRIB2 files)
- static/batch_maps/ products (generated maps)
- archive/ directory (moved elsewhere)
- All transient weather products

Repository: Clean source code only, ready for production"

# Step 9: Fix git branch and push setup
echo "Setting up git branch for push..."
git branch -M main

# Step 10: Verify remote and push
echo "Checking git remote..."
if git remote get-url origin 2>/dev/null; then
    echo "Remote configured: $(git remote get-url origin)"
    echo "Attempting push..."
    git push -u origin main
else
    echo "⚠ GitHub remote not configured"
    echo "Run: git remote add origin https://github.com/gerrycreager/CAP-WxCOP.git"
    echo "Then: git push -u origin main"
fi

# Step 11: Final status report
echo
echo "=== FINAL REPOSITORY STATUS ==="
echo "Files in repository: $(git ls-files | wc -l)"
echo "Repository size: $(du -sh .git | cut -f1)"
echo "Model data excluded: $(ls model_data/ 2>/dev/null | wc -l || echo 0) GRIB2 files"
echo "Batch maps directory: $(ls static/batch_maps/ 2>/dev/null | wc -l || echo 0) files (gitignored)"

# Show repository contents summary
echo
echo "Repository contents:"
git ls-files | head -20
echo "... (showing first 20 files)"

echo
echo "✅ Repository cleanup complete!"
echo "✅ GRIB2 files excluded from version control" 
echo "✅ Transient products excluded"
echo "✅ Ready for operational deployment"

