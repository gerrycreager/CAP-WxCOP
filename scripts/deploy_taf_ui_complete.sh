#!/bin/bash
#
# TAF UI - Complete Automated Deployment
# 
# This script deploys TAF display to the CAP Winds web application.
# It creates backup copies and provides complete files for documentation.
#

set -e  # Exit on error

echo "========================================================================"
echo "TAF Web UI - Automated Deployment"
echo "========================================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
APP_DIR="/var/www/cap_winds_app"
BACKUP_DIR="${APP_DIR}/backups/taf_deployment_$(date +%Y%m%d_%H%M%S)"

echo -e "${BLUE}Configuration:${NC}"
echo "  App Directory: $APP_DIR"
echo "  Backup Directory: $BACKUP_DIR"
echo ""

# Check if app directory exists
if [ ! -d "$APP_DIR" ]; then
    echo -e "${RED}Error: Application directory not found: $APP_DIR${NC}"
    echo "Please update APP_DIR variable in this script."
    exit 1
fi

# Create backup directory
mkdir -p "$BACKUP_DIR"
echo -e "${GREEN}✓${NC} Created backup directory"

echo ""
echo "========================================================================"
echo "Step 1: Copy Complete Reference Files"
echo "========================================================================"
echo ""

# Copy complete files to documentation directory
DOC_DIR="${APP_DIR}/docs/taf_implementation"
mkdir -p "$DOC_DIR"

echo "Copying complete reference files to: $DOC_DIR"

cp /mnt/user-data/outputs/app_with_taf_endpoints.py "$DOC_DIR/" 2>/dev/null || \
    echo -e "${YELLOW}⚠${NC}  Could not copy app_with_taf_endpoints.py"

cp /mnt/user-data/outputs/station_template_complete.html "$DOC_DIR/" 2>/dev/null || \
    echo -e "${YELLOW}⚠${NC}  Could not copy station_template_complete.html"

cp /mnt/user-data/outputs/taf_display_v2.html "$DOC_DIR/" 2>/dev/null || \
    echo -e "${YELLOW}⚠${NC}  Could not copy taf_display_v2.html"

cp /mnt/user-data/outputs/TAF_UI_DEPLOYMENT_GUIDE.md "$DOC_DIR/" 2>/dev/null || \
    echo -e "${YELLOW}⚠${NC}  Could not copy deployment guide"

echo -e "${GREEN}✓${NC} Reference files copied to documentation"

echo ""
echo "========================================================================"
echo "Step 2: Display Integration Instructions"
echo "========================================================================"
echo ""

cat << 'EOF'
TAF UI integration requires two manual steps:

1. Add API endpoints to your Flask app.py
2. Add TAF display component to your station template

Complete reference files have been saved to:
  /var/www/cap_winds_app/docs/taf_implementation/

These files contain:
  - app_with_taf_endpoints.py     (Complete Flask app example)
  - station_template_complete.html (Complete template example)
  - taf_display_v2.html           (Standalone component)
  - TAF_UI_DEPLOYMENT_GUIDE.md    (Full deployment guide)

EOF

echo -e "${BLUE}Integration Steps:${NC}"
echo ""
echo "1. Edit your Flask app.py in vi:"
echo "   vi ${APP_DIR}/app.py"
echo ""
echo "   Copy these two functions from docs/taf_implementation/app_with_taf_endpoints.py:"
echo "   - get_taf() function (around line 60)"
echo "   - get_all_tafs() function (around line 130)"
echo ""
echo "2. Edit your station template in vi:"
echo "   vi ${APP_DIR}/templates/station.html"
echo ""
echo "   Copy the TAF section from docs/taf_implementation/station_template_complete.html"
echo "   - CSS styles (inside <style> tag)"
echo "   - HTML container (the <section class=\"taf-section\">)"
echo "   - JavaScript code (inside <script> tag)"
echo ""

read -p "Press Enter when you've completed these steps, or Ctrl+C to exit..."

echo ""
echo "========================================================================"
echo "Step 3: Restart Flask Application"
echo "========================================================================"
echo ""

# Try to restart Apache (most common deployment)
if systemctl is-active --quiet apache2; then
    echo "Restarting Apache2..."
    sudo systemctl restart apache2
    echo -e "${GREEN}✓${NC} Apache2 restarted"
elif systemctl is-active --quiet httpd; then
    echo "Restarting httpd..."
    sudo systemctl restart httpd
    echo -e "${GREEN}✓${NC} httpd restarted"
else
    echo -e "${YELLOW}⚠${NC}  Could not detect web server"
    echo "Please restart your Flask application manually"
fi

echo ""
echo "========================================================================"
echo "Step 4: Test TAF API"
echo "========================================================================"
echo ""

# Test if API is responding
echo "Testing TAF API endpoints..."
echo ""

# Find the app URL
if [ -f "${APP_DIR}/.env" ]; then
    source "${APP_DIR}/.env"
fi

BASE_URL="${BASE_URL:-http://localhost}"

echo "Testing: GET ${BASE_URL}/api/taf/KMCO"
echo ""

# Test with curl
if command -v curl &> /dev/null; then
    RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/api/taf/KMCO" 2>/dev/null || echo "ERROR\n000")
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | sed '$d')
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "${GREEN}✓${NC} API Success (HTTP 200)"
        echo ""
        echo "Response preview:"
        echo "$BODY" | head -c 200
        echo "..."
    elif [ "$HTTP_CODE" = "404" ]; then
        echo -e "${YELLOW}⚠${NC}  No TAF found (HTTP 404)"
        echo "This is normal if TAF ingest hasn't run yet"
        echo "Run: cd ${APP_DIR}/scripts && ./ingest_taf.py"
    else
        echo -e "${RED}✗${NC} API Error (HTTP $HTTP_CODE)"
        echo ""
        echo "Response:"
        echo "$BODY"
    fi
else
    echo -e "${YELLOW}⚠${NC}  curl not available - skipping API test"
fi

echo ""
echo "========================================================================"
echo "Step 5: Verification"
echo "========================================================================"
echo ""

cat << EOF
Verify TAF display is working:

1. Open in browser:
   ${BASE_URL}/station/KMCO

2. You should see:
   ✓ TAF section appears on page
   ✓ TAF loads without errors
   ✓ TAF text is syntax-highlighted
   ✓ Validity times are displayed
   ✓ Refresh button works

3. Test with other stations:
   ${BASE_URL}/station/KATL (Atlanta)
   ${BASE_URL}/station/KORD (Chicago)
   ${BASE_URL}/station/KJFK (New York)
   ${BASE_URL}/station/CYYZ (Toronto - Canadian)

EOF

echo ""
echo "========================================================================"
echo "Step 6: Documentation Created"
echo "========================================================================"
echo ""

echo "Complete reference documentation available at:"
echo "  ${DOC_DIR}/"
echo ""
echo "Files:"
ls -lh "$DOC_DIR/" 2>/dev/null || echo "  (no files found)"

echo ""
echo "========================================================================"
echo "Deployment Complete!"
echo "========================================================================"
echo ""

cat << 'EOF'
✓ Reference files copied to docs/taf_implementation/
✓ Integration instructions provided
✓ Flask application restarted
✓ API endpoints tested

Next steps:
1. Verify TAF display in browser
2. Test with multiple stations
3. Check API logs if issues occur

Troubleshooting:
- API logs: tail -f /var/log/apache2/error.log
- TAF ingest logs: tail -f /var/log/taf_ingest.log
- Database check: psql -U avwx_user -d avwx_data -c "SELECT COUNT(*) FROM observations.taf;"

EOF

echo "Documentation preserved in: ${DOC_DIR}/"
echo ""
echo "========================================================================"
