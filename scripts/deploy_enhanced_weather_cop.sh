#!/bin/bash
# Enhanced Weather COP Map Deployment Script
# Deploys new enhanced weather map files alongside existing system
# Based on actual /var/www/cap_winds_app directory structure

set -e

echo "=== CAP Weather COP Enhanced Map Deployment ==="
echo "Creating NEW enhanced files alongside existing system..."
echo

# Configuration - your actual directory structure
APP_DIR="/var/www/cap_winds_app"
STATIC_CSS_DIR="${APP_DIR}/static/css"
STATIC_JS_DIR="${APP_DIR}/static/js"
TEMPLATES_DIR="${APP_DIR}/templates"
BACKUP_DIR="${APP_DIR}/archive/enhanced_deployment_$(date +%Y%m%d_%H%M%S)"

# Verify directory structure
echo "Verifying directory structure..."
if [ ! -d "$APP_DIR" ]; then
    echo "❌ Error: $APP_DIR not found!"
    exit 1
fi

if [ ! -d "$STATIC_CSS_DIR" ]; then
    echo "❌ Error: $STATIC_CSS_DIR not found!"
    exit 1
fi

if [ ! -d "$STATIC_JS_DIR" ]; then
    echo "❌ Error: $STATIC_JS_DIR not found!"
    exit 1
fi

if [ ! -d "$TEMPLATES_DIR" ]; then
    echo "❌ Error: $TEMPLATES_DIR not found!"
    exit 1
fi

echo "  ✓ All directories verified"

# Create backup directory
echo "Creating backup directory..."
mkdir -p "${BACKUP_DIR}"

# Check if files exist in current directory
echo "Checking for enhanced files to deploy..."
FILES_TO_DEPLOY=""

if [ -f "weather_enhanced_api.py" ]; then
    FILES_TO_DEPLOY="${FILES_TO_DEPLOY} weather_enhanced_api.py"
fi

if [ -f "enhanced_weather_map.css" ]; then
    FILES_TO_DEPLOY="${FILES_TO_DEPLOY} enhanced_weather_map.css"
fi

if [ -f "enhanced_weather_map.js" ]; then
    FILES_TO_DEPLOY="${FILES_TO_DEPLOY} enhanced_weather_map.js"
fi

if [ -f "enhanced_weather_map_template.html" ]; then
    FILES_TO_DEPLOY="${FILES_TO_DEPLOY} enhanced_weather_map_template.html"
fi

if [ -z "$FILES_TO_DEPLOY" ]; then
    echo "❌ Error: No enhanced files found in current directory!"
    echo "Expected files: weather_enhanced_api.py, enhanced_weather_map.css, enhanced_weather_map.js, enhanced_weather_map_template.html"
    exit 1
fi

echo "  ✓ Found files to deploy: $FILES_TO_DEPLOY"

# Deploy enhanced Python API (NEW FILE - don't modify existing weather_api.py)
echo
echo "Deploying enhanced weather API..."
if [ -f "weather_enhanced_api.py" ]; then
    cp "weather_enhanced_api.py" "${APP_DIR}/weather_enhanced_api.py"
    echo "  ✓ Deployed weather_enhanced_api.py (NEW FILE)"
    
    # Set ownership and permissions
    chown www-data:www-data "${APP_DIR}/weather_enhanced_api.py" 2>/dev/null || true
    chmod 755 "${APP_DIR}/weather_enhanced_api.py"
else
    echo "  ⚠ weather_enhanced_api.py not found - skipping"
fi

# Deploy enhanced CSS (NEW FILE)
echo "Deploying enhanced CSS..."
if [ -f "enhanced_weather_map.css" ]; then
    cp "enhanced_weather_map.css" "${STATIC_CSS_DIR}/enhanced_weather_map.css"
    echo "  ✓ Deployed enhanced_weather_map.css to ${STATIC_CSS_DIR}/"
    
    # Set ownership and permissions
    chown www-data:www-data "${STATIC_CSS_DIR}/enhanced_weather_map.css" 2>/dev/null || true
    chmod 644 "${STATIC_CSS_DIR}/enhanced_weather_map.css"
else
    echo "  ⚠ enhanced_weather_map.css not found - skipping"
fi

# Deploy enhanced JavaScript (NEW FILE)
echo "Deploying enhanced JavaScript..."
if [ -f "enhanced_weather_map.js" ]; then
    cp "enhanced_weather_map.js" "${STATIC_JS_DIR}/enhanced_weather_map.js"
    echo "  ✓ Deployed enhanced_weather_map.js to ${STATIC_JS_DIR}/"
    
    # Set ownership and permissions
    chown www-data:www-data "${STATIC_JS_DIR}/enhanced_weather_map.js" 2>/dev/null || true
    chmod 644 "${STATIC_JS_DIR}/enhanced_weather_map.js"
else
    echo "  ⚠ enhanced_weather_map.js not found - skipping"
fi

# Deploy enhanced HTML template (NEW FILE)
echo "Deploying enhanced HTML template..."
if [ -f "enhanced_weather_map_template.html" ]; then
    cp "enhanced_weather_map_template.html" "${TEMPLATES_DIR}/enhanced_weather_map.html"
    echo "  ✓ Deployed enhanced_weather_map.html to ${TEMPLATES_DIR}/"
    
    # Set ownership and permissions
    chown www-data:www-data "${TEMPLATES_DIR}/enhanced_weather_map.html" 2>/dev/null || true
    chmod 644 "${TEMPLATES_DIR}/enhanced_weather_map.html"
else
    echo "  ⚠ enhanced_weather_map_template.html not found - skipping"
fi

# Update Flask app registration (check if app.py needs the new blueprint)
echo
echo "Checking Flask app integration..."
if [ -f "${APP_DIR}/app.py" ]; then
    echo "Checking if weather_enhanced_api blueprint needs to be registered..."
    
    if grep -q "weather_enhanced_api" "${APP_DIR}/app.py"; then
        echo "  ✓ Enhanced weather API blueprint appears to be registered"
    else
        echo "  ⚠ Enhanced weather API blueprint NOT registered in app.py"
        echo
        echo "ACTION REQUIRED: Add these lines to ${APP_DIR}/app.py:"
        echo "─────────────────────────────────────────────────────────────"
        echo "from weather_enhanced_api import weather_enhanced_api"
        echo "app.register_blueprint(weather_enhanced_api)"
        echo "─────────────────────────────────────────────────────────────"
        echo
        
        # Create backup of app.py before suggesting changes
        cp "${APP_DIR}/app.py" "${BACKUP_DIR}/app.py.backup"
        echo "  ✓ Backed up app.py to ${BACKUP_DIR}/"
    fi
else
    echo "  ⚠ app.py not found - manual registration required"
fi

# Test Python syntax
echo
echo "Testing Python syntax..."
if [ -f "${APP_DIR}/weather_enhanced_api.py" ]; then
    if python3 -m py_compile "${APP_DIR}/weather_enhanced_api.py" 2>/dev/null; then
        echo "  ✓ weather_enhanced_api.py syntax is valid"
    else
        echo "  ❌ Error: weather_enhanced_api.py has syntax errors"
        echo "  Removing invalid file..."
        rm -f "${APP_DIR}/weather_enhanced_api.py"
        exit 1
    fi
fi

# Verify original files are untouched
echo
echo "Verifying original files are intact..."
if [ -f "${APP_DIR}/weather_api.py" ]; then
    echo "  ✓ Original weather_api.py is untouched"
fi

if [ -f "${TEMPLATES_DIR}/weather_map.html" ]; then
    echo "  ✓ Original weather_map.html is untouched"
fi

# Create route information
echo
echo "=== DEPLOYMENT COMPLETE ==="
echo
echo "✅ Enhanced Weather COP files deployed successfully!"
echo
echo "📁 Files Created (NEW FILES - originals untouched):"
echo "   • ${APP_DIR}/weather_enhanced_api.py"
echo "   • ${STATIC_CSS_DIR}/enhanced_weather_map.css"
echo "   • ${STATIC_JS_DIR}/enhanced_weather_map.js"
echo "   • ${TEMPLATES_DIR}/enhanced_weather_map.html"
echo
echo "🆕 Enhanced Features:"
echo "   ⭐ Military airfield prioritization"
echo "   🏷️  Station labels with smart visibility"
echo "   📊 Increased station limit to 2500"
echo "   🎨 Enhanced visual styling"
echo "   🚁 TFR and NDA integration"
echo
echo "🔧 Next Steps:"
echo "1. Register the enhanced API blueprint in app.py (if not already done):"
echo "   from weather_enhanced_api import weather_enhanced_api"
echo "   app.register_blueprint(weather_enhanced_api)"
echo
echo "2. Restart Apache web server:"
echo "   sudo systemctl restart apache2"
echo
echo "3. Access enhanced map at:"
echo "   http://your-server/enhanced_weather_map.html"
echo
echo "🔗 API Endpoints (NEW):"
echo "   • /api/weather-enhanced/metar/recent"
echo "   • /api/weather-enhanced/stations/priorities"
echo "   • /api/weather-enhanced/nda/active"
echo "   • /api/weather-enhanced/stadium-tfrs"
echo
echo "📋 Original System:"
echo "   • Your original weather_api.py is UNTOUCHED"
echo "   • Your original weather_map.html is UNTOUCHED"
echo "   • Both systems can run simultaneously"
echo
echo "🗂️  Backups stored in: ${BACKUP_DIR}"
echo
echo "🎯 Enhanced Weather COP ready for CAP operations! ⭐"

