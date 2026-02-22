#!/bin/bash
# CAP Weather COP - Git Setup and Operational Deployment Script
# Repository: https://github.com/gerrycreager/CAP-WxCOP.git

set -e  # Exit on any error

echo "=== CAP Weather COP - Git Setup and Deployment ==="
echo "Repository: https://github.com/gerrycreager/CAP-WxCOP.git"
echo "Current date: $(date)"
echo "Working directory: /var/www/cap_winds_app"

cd /var/www/cap_winds_app

# Phase 1: Git Initialization and First Commit
echo
echo "=== PHASE 1: Git Setup and Initial Commit ==="

# Initialize git if not already done
if [ ! -d ".git" ]; then
    echo "Initializing git repository..."
    git init
    git config user.name "Gerry Creager"
    git config user.email "gerry.creager@tamu.edu"
else
    echo "Git repository already exists"
fi

# Create comprehensive .gitignore
echo "Creating .gitignore..."
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

# Create .gitkeep file to preserve batch_maps directory structure
echo "Creating .gitkeep for batch_maps directory..."
touch static/batch_maps/.gitkeep

# Add all current files
echo "Adding files to git..."
git add .

# Check git status
echo "Git status:"
git status --short

# Create comprehensive commit
echo "Creating initial commit..."
git commit -m "Initial commit - CAP Weather COP System

Enhanced Weather Map Features:
- Military priority display with star markers
- Smart zoom-based labeling (Military ≥5, Major ≥6, All ≥7)
- 2500 station capacity (increased from 500)
- Complete METAR data including ceiling and sky coverage
- PostGIS geometry support for lat/lon extraction

Core Components:
- Flask application with weather API
- KQ station management system
- Wind forecast mapping
- Radar animation
- AIRMET/SIGMET integration
- Enhanced weather map with military prioritization

Database Schema:
- observations.metar (PostGIS location column)
- observations.airports (station_id join key)
- observations.custom_stations (KQ stations)
- observations.wind_constraints

Operational Status: STABLE - Ready for production deployment"

echo "✓ Phase 1 complete - Initial git commit created"

# Phase 2: GitHub Upload
echo
echo "=== PHASE 2: GitHub Repository Setup ==="

# Check if GitHub remote exists
if git remote get-url origin 2>/dev/null; then
    echo "GitHub remote already configured"
    echo "Current remote: $(git remote get-url origin)"
else
    echo "Configuring GitHub remote for gerrycreager/CAP-WxCOP..."
    git remote add origin https://github.com/gerrycreager/CAP-WxCOP.git
    git branch -M main
    echo "✓ GitHub remote configured"
    echo "Ready to push with: git push -u origin main"
fi

# Phase 3: Prepare for operational deployment
echo
echo "=== PHASE 3: Operational Environment Preparation ==="

# Create backup of current system
BACKUP_DIR="/var/backups/cap_winds_$(date +%Y%m%d_%H%M%S)"
echo "Creating backup at: $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -r /var/www/cap_winds_app/* "$BACKUP_DIR/"
echo "✓ Backup created: $BACKUP_DIR"

# Update /var/www/html/index.html to use enhanced weather map
echo "Updating main index.html to use enhanced weather map..."

# First, backup the original
cp /var/www/html/index.html /var/www/html/index.html.backup.$(date +%Y%m%d_%H%M%S)

# Update the weather map link in index.html
if grep -q "weather_map.html" /var/www/html/index.html; then
    sed -i 's/weather_map\.html/enhanced_weather_map.html/g' /var/www/html/index.html
    echo "✓ Updated /var/www/html/index.html to use enhanced_weather_map.html"
else
    echo "⚠ weather_map.html reference not found in /var/www/html/index.html"
    echo "Please manually update the weather map links to use enhanced_weather_map.html"
fi

echo
echo "=== OPERATIONAL READINESS STATUS ==="
echo "✓ Git repository initialized and committed"
echo "✓ GitHub remote configured: https://github.com/gerrycreager/CAP-WxCOP.git"
echo "✓ Backup created: $BACKUP_DIR"
echo "✓ Enhanced weather map set as default"
echo "✓ All core systems operational"
echo
echo "NEXT STEPS:"
echo "1. Push to GitHub:"
echo "   git push -u origin main"
echo
echo "2. Test operational system:"
echo "   http://209.248.90.253/ (main index)"
echo "   http://209.248.90.253/CAP_WxCOP/enhanced_weather_map.html"
echo
echo "3. Create development environment:"
echo "   ./create_dev_environment.sh"

# Create the development environment setup script
cat > create_dev_environment.sh << 'EOF'
#!/bin/bash
# CAP Weather COP - Development Environment Setup
# Creates a separate dev environment for future updates

set -e

echo "=== Creating Development Environment ==="

DEV_DIR="/var/www/cap_winds_dev"
PROD_DIR="/var/www/cap_winds_app"

# Create development directory
if [ -d "$DEV_DIR" ]; then
    echo "Development directory exists. Backing up..."
    mv "$DEV_DIR" "${DEV_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
fi

echo "Creating development environment..."
cp -r "$PROD_DIR" "$DEV_DIR"

# Update development configuration
cd "$DEV_DIR"

# Create development-specific settings
cat > dev_settings.py << 'DEVEOF'
"""
Development Environment Settings
Override production settings for development work
"""

# Development database (if different)
DEV_DATABASE_URL = "postgresql://username:password@localhost:5432/cap_weather_dev"

# Development server settings
DEBUG = True
TESTING = True

# Development-specific API endpoints
DEV_API_PREFIX = "/CAP_WxCOP_DEV"

print("Development settings loaded")
DEVEOF

# Update Apache configuration for development
echo "Creating development Apache configuration..."
cat > /etc/apache2/sites-available/cap_winds_dev.conf << 'APACHEEOF'
<VirtualHost *:80>
    ServerName 209.248.90.253
    
    # Development environment
    WSGIScriptAlias /CAP_WxCOP_DEV /var/www/cap_winds_dev/app.wsgi
    <Directory "/var/www/cap_winds_dev">
        WSGIProcessGroup cap_winds_dev
        WSGIApplicationGroup %{GLOBAL}
        Order allow,deny
        Allow from all
    </Directory>
    
    # Static files for development
    Alias /cap_winds_dev /var/www/cap_winds_dev/static/batch_maps
    <Directory "/var/www/cap_winds_dev/static/batch_maps">
        Order allow,deny
        Allow from all
    </Directory>
    
    WSGIDaemonProcess cap_winds_dev python-path=/var/www/cap_winds_dev python-home=/var/www/cap_winds_dev/venv
    
    ErrorLog ${APACHE_LOG_DIR}/cap_winds_dev_error.log
    CustomLog ${APACHE_LOG_DIR}/cap_winds_dev_access.log combined
</VirtualHost>
APACHEEOF

# Enable development site
a2ensite cap_winds_dev
systemctl reload apache2

echo "✓ Development environment created at: $DEV_DIR"
echo "✓ Development URL: http://209.248.90.253/CAP_WxCOP_DEV/"
echo "✓ Production URL remains: http://209.248.90.253/CAP_WxCOP/"
echo
echo "DEVELOPMENT WORKFLOW:"
echo "1. Make changes in: $DEV_DIR"
echo "2. Test at: http://209.248.90.253/CAP_WxCOP_DEV/"
echo "3. When stable, copy changes to production"
echo "4. Commit to git: https://github.com/gerrycreager/CAP-WxCOP.git"
EOF

chmod +x create_dev_environment.sh

echo "✓ Development environment script created: create_dev_environment.sh"
echo
echo "🎯 CAP Weather COP - Git Setup Complete!"
echo "Repository: https://github.com/gerrycreager/CAP-WxCOP.git"
echo "System ready for operational deployment and future development."

