# Deployment Guide

## Production Deployment

### 1. System Requirements
- Ubuntu 24 LTS server
- PostgreSQL 14+ with PostGIS extension
- Python 3.8+ with pip
- Apache2 with mod_wsgi
- Git for version control

### 2. Database Setup
```bash
sudo -u postgres createdb cap_weather
sudo -u postgres psql cap_weather -c "CREATE EXTENSION postgis;"
sudo -u postgres psql cap_weather -c "CREATE SCHEMA observations;"
```

### 3. Application Installation
```bash
# Clone repository
git clone https://github.com/YOUR-USERNAME/CAP-WxCOP.git /var/www/cap_winds_app
cd /var/www/cap_winds_app

# Install Python dependencies
pip3 install -r requirements.txt --break-system-packages

# Set permissions
chown -R www-data:www-data /var/www/cap_winds_app
chmod 755 /var/www/cap_winds_app/*.py
```

### 4. Apache Configuration
```apache
<VirtualHost *:80>
    ServerName your-server.com
    
    WSGIScriptAlias /CAP_WxCOP /var/www/cap_winds_app/app.wsgi
    <Directory "/var/www/cap_winds_app">
        WSGIProcessGroup cap_winds_app
        WSGIApplicationGroup %{GLOBAL}
        Order allow,deny
        Allow from all
    </Directory>
    
    WSGIDaemonProcess cap_winds_app python-path=/var/www/cap_winds_app
    
    ErrorLog ${APACHE_LOG_DIR}/cap_winds_error.log
    CustomLog ${APACHE_LOG_DIR}/cap_winds_access.log combined
</VirtualHost>
```

### 5. Testing
```bash
# Test database connection
python3 -c "from db_config import get_connection; print('DB OK')"

# Test weather API
curl "http://localhost/CAP_WxCOP/api/weather/health"

# Test enhanced weather map
curl "http://localhost/CAP_WxCOP/enhanced_weather_map.html"
```

## Development Environment

### Create Development Environment
```bash
./create_dev_environment.sh
```

This creates a separate development instance at `/var/www/cap_winds_dev` accessible via `/CAP_WxCOP_DEV/` URL path.

## Troubleshooting

### Common Issues
1. **Database connection errors**: Check PostgreSQL service and credentials
2. **Apache 500 errors**: Check error logs and Python import paths  
3. **Missing weather data**: Verify LDM data ingestion
4. **PostGIS errors**: Ensure PostGIS extension is installed

### Log Files
- Apache errors: `/var/log/apache2/cap_winds_error.log`
- Application logs: Check Flask debug output
- Database logs: PostgreSQL system logs
