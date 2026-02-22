#!/bin/bash
# CAP Weather COP - Fix Git Repository (Remove Transient Files)
# Undo the massive commit and properly exclude batch_maps products

set -e

echo "=== Fixing CAP Weather COP Git Repository ==="
echo "Removing transient batch_maps files from git history"

cd /var/www/cap_winds_app

# Step 1: Reset to before the large commit (but keep working files)
echo "Resetting git to remove large commit..."
git reset --soft HEAD~1

# Step 2: Create proper .gitignore to exclude transient products
echo "Creating improved .gitignore..."
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
*.grib2
*.grb2
*.grib
*.grb
pqact_debug.log
pqact_nexrad3.log
tree.cap
EOF

# Step 3: Create .gitkeep file to preserve batch_maps directory structure
echo "Creating .gitkeep for batch_maps directory..."
touch static/batch_maps/.gitkeep

# Step 4: Remove the transient files from git tracking
echo "Removing transient files from git..."
git rm -r --cached static/batch_maps/*.png static/batch_maps/*.zip static/batch_maps/cap_winds_* 2>/dev/null || true
git rm -r --cached archive/ 2>/dev/null || true
git rm --cached *.grib2 *.grb2 *.grib *.grb 2>/dev/null || true

# Step 5: Re-add everything with new .gitignore rules
echo "Re-adding files with corrected .gitignore..."
git add .

# Step 6: Check what's being committed now
echo "Current git status:"
git status --short | head -20

# Count files to be committed
FILE_COUNT=$(git diff --cached --name-only | wc -l)
echo "Files to be committed: $FILE_COUNT (should be much less than 1774)"

if [ "$FILE_COUNT" -lt 200 ]; then
    echo "✓ File count looks reasonable"
else
    echo "⚠ Still too many files - manual review needed"
    exit 1
fi

# Step 7: Create clean commit
echo "Creating clean commit without transient files..."
git commit -m "Initial commit - CAP Weather COP System (Clean)

Enhanced Weather Map Features:
- Military priority display with star markers
- Smart zoom-based labeling (Military ≥5, Major ≥6, All ≥7)
- 2500 station capacity (increased from 500)
- Complete METAR data including ceiling and sky coverage
- PostGIS geometry support for lat/lon extraction

Core Components:
- Flask application with weather API
- KQ station management system
- Wind forecast mapping (batch_maps/ excluded as transient)
- Radar animation
- AIRMET/SIGMET integration
- Enhanced weather map with military prioritization

Database Schema:
- observations.metar (PostGIS location column)
- observations.airports (station_id join key)
- observations.custom_stations (KQ stations)
- observations.wind_constraints

.gitignore: Properly excludes transient batch_maps products
Operational Status: STABLE - Ready for production deployment"

echo
echo "✓ Git repository cleaned and recommitted"
echo "✓ Transient batch_maps files excluded from version control"
echo "✓ Ready for GitHub upload"
echo
echo "NEXT STEPS:"
echo "1. Push to GitHub:"
echo "   git push -u origin main"
echo
echo "2. Batch maps will be generated on-demand and ignored by git"
echo "3. Repository size now manageable for GitHub"

# Show final statistics
echo
echo "=== FINAL REPOSITORY STATUS ==="
echo "Files in commit: $(git diff --cached --name-only | wc -l)"
echo "Repository size: $(du -sh .git | cut -f1)"
echo "Batch maps directory preserved but contents ignored"
echo
ls -la static/batch_maps/ | head -5
echo "... (batch maps exist but are gitignored)"
EOF

chmod +x fix_git_repository.sh

echo "✓ Git cleanup script created"
echo
echo "EXECUTE THIS SCRIPT TO FIX THE REPOSITORY:"
echo "./fix_git_repository.sh"
echo
echo "This will:"
echo "1. Undo the large commit (keeping your working files)"
echo "2. Fix .gitignore to exclude transient batch_maps"
echo "3. Recommit with only essential files"
echo "4. Prepare for clean GitHub upload"

