#!/bin/bash
# CAP Weather COP Development Environment Setup
# Creates safe testing environment for new features

set -e

DEV_DIR="/var/www/cap_winds_dev"
PROD_DIR="/var/www/cap_winds_app"

echo "=== Setting up CAP Weather COP Development Environment ==="
echo ""

# Create development directory
echo "1. Creating development directory..."
sudo mkdir -p $DEV_DIR
sudo chown www-data:www-data $DEV_DIR

# Sync from production (excluding transient data)
echo "2. Syncing code from production..."
sudo rsync -av --exclude='static/batch_maps/' \
              --exclude='model_data/' \
              --exclude='.cache/' \
              --exclude='archive/' \
              --exclude='__pycache__/' \
              --exclude='*.pyc' \
              --exclude='.git/' \
              $PROD_DIR/ $DEV_DIR/

# Create development-specific config
echo "3. Creating development configuration..."
sudo tee $DEV_DIR/dev_config.py > /dev/null << 'EOF'
# Development Configuration
DEBUG = True
TESTING = True
DEVELOPMENT = True

# Short cache times for rapid testing
CACHE_TIMEOUT = 60  # 1 minute vs 15 minutes in production

# Development-specific settings
LOG_LEVEL = 'DEBUG'
ENABLE_PROFILING = True

# Database connection (same as production for now)
# Could point to dev database if needed

print("🚧 DEVELOPMENT MODE ACTIVE 🚧")
EOF

# Update app.py for development detection
echo "4. Updating app.py for development mode..."
sudo tee -a $DEV_DIR/app.py > /dev/null << 'EOF'

# Development mode detection
import os
if os.path.exists('/var/www/cap_winds_dev/dev_config.py'):
    from dev_config import *
    print("🚧 Running in DEVELOPMENT mode")
    app.config.update(
        DEBUG=DEBUG,
        TESTING=TESTING
    )
EOF

# Fix KQ trailing slash issue
echo "5. Fixing KQ trailing slash issue..."
sudo sed -i 's|url_prefix='"'"'/admin/kq-stations'"'"'|url_prefix='"'"'/admin/kq-stations'"'"', strict_slashes=False|g' $DEV_DIR/app.py

# Create development Apache configuration
echo "6. Creating development Apache configuration..."
sudo tee /etc/apache2/sites-available/cap_winds_dev.conf > /dev/null << 'EOF'
<VirtualHost *:80>
    ServerName 209.248.90.253
    
    # Development Environment - CAP WxCOP DEV
    WSGIDaemonProcess CAP_WxCOP_DEV \
        user=www-data \
        group=www-data \
        python-home=/var/www/cap_winds_dev/venv \
        python-path=/var/www/cap_winds_dev \
        processes=1 \
        threads=5

    WSGIScriptAlias /CAP_WxCOP_DEV /var/www/cap_winds_dev/cap_winds.wsgi

    <Directory /var/www/cap_winds_dev>
        WSGIProcessGroup CAP_WxCOP_DEV
        WSGIApplicationGroup %{GLOBAL}
        Require all granted
        
        # Development headers
        Header always set X-Environment "DEVELOPMENT"
        Header always set X-Debug "Enabled"
    </Directory>

    # Static files for dev
    Alias /CAP_WxCOP_DEV/static /var/www/cap_winds_dev/static
    <Directory /var/www/cap_winds_dev/static>
        Require all granted
        ExpiresActive On
        ExpiresDefault "access plus 1 minute"
    </Directory>

    ErrorLog ${APACHE_LOG_DIR}/cap-wxcop-dev-error.log
    CustomLog ${APACHE_LOG_DIR}/cap-wxcop-dev-access.log combined
    LogLevel debug

</VirtualHost>
EOF

# Create development WSGI file
echo "7. Creating development WSGI configuration..."
sudo cp $PROD_DIR/cap_winds.wsgi $DEV_DIR/
sudo sed -i 's|/var/www/cap_winds_app|/var/www/cap_winds_dev|g' $DEV_DIR/cap_winds.wsgi

# Set permissions
echo "8. Setting development permissions..."
sudo chown -R www-data:www-data $DEV_DIR
sudo chmod -R 755 $DEV_DIR

# Enable development site (but don't activate yet)
sudo a2ensite cap_winds_dev.conf

echo ""
echo "=== Development Environment Ready ==="
echo ""
echo "🚧 Development URL: http://209.248.90.253/CAP_WxCOP_DEV/"
echo "📁 Development Path: $DEV_DIR"
echo "📋 Features:"
echo "   - Separate Apache process (CAP_WxCOP_DEV)"
echo "   - Debug mode enabled"
echo "   - Short cache timeouts (1 minute)"
echo "   - Separate logs"
echo "   - KQ trailing slash issue fixed"
echo ""
echo "To activate development environment:"
echo "   sudo systemctl reload apache2"
echo ""
echo "To work on development:"
echo "   cd $DEV_DIR"
echo "   # Make changes to templates, static files, etc."
echo "   # Test at /CAP_WxCOP_DEV/"
echo "   # When ready, copy changes back to production"
echo ""
echo "✅ Safe to test new features without affecting production!"

