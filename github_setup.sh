#!/bin/bash
# CAP Weather COP - GitHub Repository Setup
# Creates comprehensive documentation and uploads to GitHub

set -e

echo "=== CAP Weather COP - GitHub Repository Setup ==="

cd /var/www/cap_winds_app

# Create comprehensive README.md
echo "Creating README.md..."
cat > README.md << 'EOF'
# CAP Weather COP (Common Operating Picture)

A comprehensive weather system for Civil Air Patrol search and rescue operations, providing real-time meteorological data with military airfield prioritization.

## 🎯 Features

### Enhanced Weather Map
- **Military Priority Display**: Star markers and red labels for military airfields
- **Smart Labeling**: Zoom-based station labels (Military ≥5, Major ≥6, All ≥7)  
- **High Capacity**: 2500 station display capability
- **Complete METAR Data**: Including ceiling, sky coverage, and decoded weather
- **Flight Categories**: VFR, MVFR, IFR, LIFR color coding

### Core Components
- **Weather API**: Real-time METAR data with PostGIS spatial queries
- **KQ Station Management**: Temporary weather station administration
- **Wind Forecast Mapping**: CAPR 70-1 compliant wind constraint analysis
- **Radar Animation**: NEXRAD Level III radar display
- **AIRMET/SIGMET Integration**: Aviation weather hazards

## 🏗️ Architecture

### Database Schema
- **observations.metar**: Weather observations (PostGIS location column)
- **observations.airports**: Airport database (station_id join key)  
- **observations.custom_stations**: KQ temporary stations
- **observations.wind_constraints**: CAPR 70-1 wind analysis

### Technology Stack
- **Backend**: Python Flask with PostGIS/PostgreSQL
- **Frontend**: Leaflet maps with custom weather symbology
- **Data Sources**: NOAA LDM feed, FAA airport database
- **Server**: Apache2 with mod_wsgi

## 🚀 Deployment

### Production Environment
```bash
# Main application
http://209.248.90.253/CAP_WxCOP/

# Enhanced weather map
http://209.248.90.253/CAP_WxCOP/enhanced_weather_map.html
```

### Development Environment
```bash
# Create development environment
./create_dev_environment.sh

# Development URL
http://209.248.90.253/CAP_WxCOP_DEV/
```

## 📊 API Endpoints

### Weather API
- `GET /api/weather/metar/recent?bounds=west,south,east,north&limit=2500`
- `GET /api/weather/stations?bounds=west,south,east,north`
- `GET /api/weather/wind-constraints?bounds=west,south,east,north`
- `GET /api/weather/health`

### Wind Forecast API  
- `GET /api/wind-forecast/current?location=state_code`
- `GET /api/wind-forecast/constraints?region=region_code`

## 🛠️ Configuration

### Station Display Limits
```javascript
// ### STATION CONFIGURATION ###
const MAX_STATIONS = 2500; // Change this to adjust station limit
// ### END STATION CONFIGURATION ###
```

### Military Prioritization
Stations are automatically sorted by priority:
1. **Military airfields** (is_military = true)
2. **Large airports** (major hubs) 
3. **Medium airports** (regional)
4. **Small airports** (local)

## 📋 Requirements

### System Requirements
- Ubuntu 24 LTS
- PostgreSQL 14+ with PostGIS
- Python 3.8+
- Apache2 with mod_wsgi
- NOAA LDM for weather data ingestion

### Python Dependencies
- Flask
- psycopg2
- requests
- python-dateutil

## 🔧 Installation

### Database Setup
```sql
CREATE SCHEMA observations;
CREATE EXTENSION postgis;

-- Create tables (automated via setup scripts)
```

### Application Deployment
```bash
# Clone repository
git clone https://github.com/YOUR-USERNAME/CAP-WxCOP.git /var/www/cap_winds_app

# Install dependencies
cd /var/www/cap_winds_app
pip3 install -r requirements.txt

# Configure Apache
cp cap_winds.conf /etc/apache2/sites-available/
a2ensite cap_winds
systemctl reload apache2
```

## 📈 Development Workflow

1. **Make changes** in development environment (`/var/www/cap_winds_dev`)
2. **Test thoroughly** at development URL
3. **Copy stable changes** to production environment
4. **Commit and push** to GitHub repository
5. **Document changes** in commit messages

## 🎖️ Military Features

### Airfield Prioritization
- **Visual Priority**: Star markers and distinct styling
- **Label Priority**: Military labels appear first (zoom ≥5)
- **Data Priority**: Military stations sorted to top of results

### CAP-Specific Features
- **CAPR 70-1 Compliance**: Wind constraint analysis for CAP aircraft
- **SAR Operations**: Optimized for search and rescue mission planning
- **KQ Stations**: Temporary station management for incident operations

## 📚 Documentation

### API Documentation
See `/docs/api.md` for complete API reference

### Database Schema
See `/docs/database.md` for schema documentation

### Deployment Guide
See `/docs/deployment.md` for detailed setup instructions

## 🤝 Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open Pull Request

## 📧 Contact

**CAP Weather Operations Team**
- Email: weather@cap.gov
- System: CAP Weather COP
- Version: 1.0.0

## 📄 License

This project is developed for the Civil Air Patrol and contains operational weather systems. 

---

**🎯 CAP Weather COP - Enhanced Weather Intelligence for Search and Rescue Operations**
EOF

# Create requirements.txt
echo "Creating requirements.txt..."
cat > requirements.txt << 'EOF'
Flask==2.3.3
psycopg2-binary==2.9.7
python-dateutil==2.8.2
requests==2.31.0
Werkzeug==2.3.7
Jinja2==3.1.2
MarkupSafe==2.1.3
itsdangerous==2.1.2
click==8.1.7
gunicorn==21.2.0
EOF

# Create docs directory structure
mkdir -p docs

# Create API documentation
cat > docs/api.md << 'EOF'
# CAP Weather COP - API Documentation

## Weather API Endpoints

### Get Recent METAR Data
```
GET /api/weather/metar/recent
```

**Parameters:**
- `bounds`: Required. Format: `west,south,east,north` (decimal degrees)
- `limit`: Optional. Maximum stations to return (default: 500, max: 2500)

**Response:**
```json
{
  "metars": [
    {
      "station_id": "KORD",
      "latitude": 41.9786,
      "longitude": -87.9048,
      "observation_time": "2026-02-21T16:50:00",
      "temp_c": 8.0,
      "dewpoint_c": -3.0,
      "wind_dir": 350,
      "wind_speed_kts": 11,
      "wind_gust_kts": null,
      "altimeter_hg": 30.21,
      "visibility_sm": 10.0,
      "flight_category": "VFR",
      "raw_text": "METAR KORD 211650Z 35011KT 10SM CLR 08/M03 A3021",
      "sky_conditions": [{"cover": "CLR"}],
      "is_military": false,
      "airport_name": "Chicago O'Hare International Airport"
    }
  ],
  "count": 1,
  "bounds": {"west": -100, "south": 25, "east": -80, "north": 45}
}
```

### Health Check
```
GET /api/weather/health
```

Returns system status and database connectivity.

## Wind Forecast API

### Current Wind Constraints
```
GET /api/wind-forecast/current?location=IL
```

Returns CAPR 70-1 compliant wind constraint analysis for specified location.
EOF

# Create database documentation
cat > docs/database.md << 'EOF'
# Database Schema Documentation

## Core Tables

### observations.metar
Primary weather observation table.

**Columns:**
- `station_id` VARCHAR(8) - Airport identifier (PRIMARY KEY)
- `observation_time` TIMESTAMP - Observation time (UTC)
- `location` GEOMETRY(POINT,4326) - PostGIS point (lat/lon)
- `temp_c` NUMERIC - Temperature (Celsius)
- `dewpoint_c` NUMERIC - Dewpoint (Celsius)  
- `wind_dir` INTEGER - Wind direction (degrees)
- `wind_speed_kts` INTEGER - Wind speed (knots)
- `altimeter_hg` NUMERIC - Altimeter setting (inches Hg)
- `visibility_sm` NUMERIC - Visibility (statute miles)
- `flight_category` VARCHAR(10) - VFR/MVFR/IFR/LIFR
- `sky_conditions` JSONB - Sky coverage layers
- `raw_text` TEXT - Raw METAR text

### observations.airports
Airport reference data.

**Columns:**
- `station_id` VARCHAR(8) - Airport identifier (PRIMARY KEY)
- `name` TEXT - Airport name
- `location` GEOMETRY(POINT,4326) - PostGIS point
- `elevation_ft` INTEGER - Elevation (feet MSL)
- `is_military` BOOLEAN - Military airfield flag
- `longest_runway_ft` INTEGER - Longest runway length
- `airport_type` VARCHAR(20) - large/medium/small classification

## Spatial Queries

### Bounding Box Query
```sql
SELECT * FROM observations.metar
WHERE ST_Y(location) BETWEEN south AND north
  AND ST_X(location) BETWEEN west AND east
```

### Distance Query  
```sql  
SELECT * FROM observations.airports
WHERE ST_DWithin(location, ST_SetSRID(ST_MakePoint(lon, lat), 4326), 50000)
```
EOF

# Create deployment documentation
cat > docs/deployment.md << 'EOF'
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
EOF

echo "✓ Documentation created"
echo "✓ Requirements.txt created"
echo "✓ README.md created"

# Add documentation to git
git add .
git commit -m "Add comprehensive documentation and project structure

Documentation Added:
- README.md: Complete project overview and setup
- requirements.txt: Python dependencies  
- docs/api.md: API endpoint documentation
- docs/database.md: Database schema reference
- docs/deployment.md: Installation and deployment guide

Project Structure:
- Enhanced weather map with military prioritization
- 2500 station capacity with PostGIS optimization
- Complete METAR data including ceiling and sky coverage
- Development environment setup scripts

Ready for GitHub upload and operational deployment."

echo "✓ Documentation committed to git"
echo
echo "🎯 GitHub Repository Ready!"
echo
echo "NEXT STEPS:"
echo "1. Create GitHub repository: https://github.com/new"
echo "2. Repository name: CAP-WxCOP" 
echo "3. Run these commands:"
echo "   cd /var/www/cap_winds_app"
echo "   git remote add origin https://github.com/gerrycreager/CAP-WxCOP.git"
echo "   git branch -M main"
echo "   git push -u origin main"
echo
echo "4. Then run: ./setup_git_and_deploy.sh"
EOF

chmod +x setup_git_and_deploy.sh create_dev_environment.sh

