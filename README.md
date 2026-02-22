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
