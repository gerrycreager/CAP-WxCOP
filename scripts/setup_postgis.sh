#!/bin/bash
#
# PostGIS Setup Script for Aviation Weather System
# Creates database, extensions, schemas, and initial tables
#
# Usage: sudo ./setup_postgis.sh
#

set -e  # Exit on error

# Configuration
DB_NAME="avwx_data"
DB_USER="avwx_user"
DB_PASS="change_me_in_production"  # Change this!
POSTGRES_USER="postgres"

echo "========================================="
echo "Aviation Weather PostGIS Setup"
echo "========================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (sudo)"
    exit 1
fi

# Install PostGIS if not already installed
echo "Checking for PostGIS installation..."
if ! dpkg -l | grep -q postgresql-16-postgis; then
    echo "Installing PostGIS..."
    apt-get update
    apt-get install -y postgresql-16-postgis-3 postgresql-16-postgis-3-scripts
else
    echo "✓ PostGIS already installed"
fi

# Create database user
echo ""
echo "Creating database user: $DB_USER"
sudo -u $POSTGRES_USER psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || echo "User already exists"

# Create database
echo "Creating database: $DB_NAME"
sudo -u $POSTGRES_USER createdb -O $DB_USER $DB_NAME 2>/dev/null || echo "Database already exists"

# Enable PostGIS extensions
echo "Enabling PostGIS extensions..."
sudo -u $POSTGRES_USER psql -d $DB_NAME <<EOF
-- PostGIS extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Grant permissions
GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
GRANT ALL ON SCHEMA public TO $DB_USER;

-- Create schemas
CREATE SCHEMA IF NOT EXISTS model_data;
CREATE SCHEMA IF NOT EXISTS observations;
CREATE SCHEMA IF NOT EXISTS hazards;
CREATE SCHEMA IF NOT EXISTS radar;
CREATE SCHEMA IF NOT EXISTS satellite;
CREATE SCHEMA IF NOT EXISTS products;

-- Grant permissions on schemas
GRANT ALL ON SCHEMA model_data TO $DB_USER;
GRANT ALL ON SCHEMA observations TO $DB_USER;
GRANT ALL ON SCHEMA hazards TO $DB_USER;
GRANT ALL ON SCHEMA radar TO $DB_USER;
GRANT ALL ON SCHEMA satellite TO $DB_USER;
GRANT ALL ON SCHEMA products TO $DB_USER;

-- Ensure future tables get permissions
ALTER DEFAULT PRIVILEGES IN SCHEMA model_data GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA observations GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA hazards GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA radar GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA satellite GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA products GRANT ALL ON TABLES TO $DB_USER;

EOF

echo "✓ Database and extensions created"

# Create initial tables
echo ""
echo "Creating tables..."
sudo -u $POSTGRES_USER psql -d $DB_NAME <<EOF

-- ============================================================
-- MODEL DATA TABLES
-- ============================================================

-- Wind analysis (HRRR/GFS)
CREATE TABLE IF NOT EXISTS model_data.wind_analysis (
    id SERIAL PRIMARY KEY,
    model VARCHAR(20) NOT NULL,
    init_time TIMESTAMP NOT NULL,
    valid_time TIMESTAMP NOT NULL,
    cycle_hour INTEGER,
    forecast_hour INTEGER,
    max_wind_speed RASTER,
    wind_u10 RASTER,
    wind_v10 RASTER,
    wind_gust RASTER,
    coverage_area GEOMETRY(POLYGON, 4326),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wind_model_time 
    ON model_data.wind_analysis (model, init_time, forecast_hour);
CREATE INDEX IF NOT EXISTS idx_wind_coverage 
    ON model_data.wind_analysis USING GIST(coverage_area);

-- ============================================================
-- OBSERVATION TABLES
-- ============================================================

-- METAR observations
CREATE TABLE IF NOT EXISTS observations.metar (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(4) NOT NULL,
    observation_time TIMESTAMP NOT NULL,
    raw_text TEXT NOT NULL,
    
    -- Parsed fields
    temp_c FLOAT,
    dewpoint_c FLOAT,
    wind_dir INTEGER,
    wind_speed_kts INTEGER,
    wind_gust_kts INTEGER,
    visibility_sm FLOAT,
    altimeter_hg FLOAT,
    
    -- Flight category
    flight_category VARCHAR(10),
    
    -- Sky conditions and weather
    sky_conditions JSONB,
    present_weather TEXT[],
    
    -- Location
    location GEOMETRY(POINT, 4326),
    
    -- Metadata
    is_speci BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metar_station_time 
    ON observations.metar (station_id, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_metar_time 
    ON observations.metar (observation_time);
CREATE INDEX IF NOT EXISTS idx_metar_category 
    ON observations.metar (flight_category);
CREATE INDEX IF NOT EXISTS idx_metar_location 
    ON observations.metar USING GIST(location);

-- TAF forecasts
CREATE TABLE IF NOT EXISTS observations.taf (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(4) NOT NULL,
    issue_time TIMESTAMP NOT NULL,
    valid_from TIMESTAMP NOT NULL,
    valid_to TIMESTAMP NOT NULL,
    raw_text TEXT NOT NULL,
    forecast_periods JSONB,
    location GEOMETRY(POINT, 4326),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_taf_station_time 
    ON observations.taf (station_id, issue_time DESC);
CREATE INDEX IF NOT EXISTS idx_taf_valid 
    ON observations.taf (valid_from, valid_to);

-- Airport reference data
CREATE TABLE IF NOT EXISTS observations.airports (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(4) UNIQUE NOT NULL,
    name TEXT,
    location GEOMETRY(POINT, 4326),
    elevation_ft INTEGER,
    has_reporting BOOLEAN DEFAULT FALSE,
    has_paved_runway BOOLEAN DEFAULT FALSE,
    longest_runway_ft INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_airports_station 
    ON observations.airports (station_id);
CREATE INDEX IF NOT EXISTS idx_airports_location 
    ON observations.airports USING GIST(location);

-- ============================================================
-- HAZARDS TABLES
-- ============================================================

-- SIGMETs and AIRMETs
CREATE TABLE IF NOT EXISTS hazards.sigmets (
    id SERIAL PRIMARY KEY,
    product_type VARCHAR(20),
    hazard_type VARCHAR(50),
    issue_time TIMESTAMP,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,
    raw_text TEXT,
    area GEOMETRY(POLYGON, 4326),
    flight_levels VARCHAR(50),
    moving BOOLEAN,
    direction INTEGER,
    speed_kts INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sigmets_valid 
    ON hazards.sigmets (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_sigmets_area 
    ON hazards.sigmets USING GIST(area);

-- TFRs
CREATE TABLE IF NOT EXISTS hazards.tfrs (
    id SERIAL PRIMARY KEY,
    tfr_number VARCHAR(50) UNIQUE,
    notam_number VARCHAR(50),
    type VARCHAR(50),
    location_name TEXT,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,
    area GEOMETRY(POLYGON, 4326),
    lower_altitude INTEGER,
    upper_altitude INTEGER,
    description TEXT,
    raw_notam TEXT,
    faa_url TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tfrs_valid 
    ON hazards.tfrs (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_tfrs_area 
    ON hazards.tfrs USING GIST(area);

-- ============================================================
-- PRODUCTS TABLE (Map metadata)
-- ============================================================

CREATE TABLE IF NOT EXISTS products.generated_maps (
    id SERIAL PRIMARY KEY,
    product_type VARCHAR(50),
    location_type VARCHAR(50),
    location_code VARCHAR(10),
    init_time TIMESTAMP,
    valid_time TIMESTAMP,
    file_path TEXT,
    shapefile_path TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_maps_location 
    ON products.generated_maps (location_type, location_code, created_at DESC);

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Calculate flight category
CREATE OR REPLACE FUNCTION observations.calculate_flight_category(
    visibility_sm FLOAT,
    ceiling_agl INTEGER
) RETURNS VARCHAR AS \$\$
BEGIN
    IF ceiling_agl < 500 OR visibility_sm < 1 THEN
        RETURN 'LIFR';
    ELSIF ceiling_agl < 1000 OR visibility_sm < 3 THEN
        RETURN 'IFR';
    ELSIF ceiling_agl <= 3000 OR visibility_sm <= 5 THEN
        RETURN 'MVFR';
    ELSE
        RETURN 'VFR';
    END IF;
END;
\$\$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================
-- GRANT PERMISSIONS
-- ============================================================

GRANT ALL ON ALL TABLES IN SCHEMA model_data TO $DB_USER;
GRANT ALL ON ALL TABLES IN SCHEMA observations TO $DB_USER;
GRANT ALL ON ALL TABLES IN SCHEMA hazards TO $DB_USER;
GRANT ALL ON ALL TABLES IN SCHEMA radar TO $DB_USER;
GRANT ALL ON ALL TABLES IN SCHEMA satellite TO $DB_USER;
GRANT ALL ON ALL TABLES IN SCHEMA products TO $DB_USER;

GRANT ALL ON ALL SEQUENCES IN SCHEMA model_data TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA observations TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA hazards TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA radar TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA satellite TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA products TO $DB_USER;

EOF

echo "✓ Tables created"

# Create Python database connection config
echo ""
echo "Creating database connection config..."
cat > /var/www/cap_winds_app/db_config.py <<EOF
"""
Database configuration for Aviation Weather System
Auto-generated by setup_postgis.sh
"""

DATABASE_CONFIG = {
    'dbname': '$DB_NAME',
    'user': '$DB_USER',
    'password': '$DB_PASS',
    'host': 'localhost',
    'port': 5432
}

# Connection string for SQLAlchemy
SQLALCHEMY_DATABASE_URI = f"postgresql://{DATABASE_CONFIG['user']}:{DATABASE_CONFIG['password']}@{DATABASE_CONFIG['host']}:{DATABASE_CONFIG['port']}/{DATABASE_CONFIG['dbname']}"

# Connection string for psycopg2
def get_connection():
    """Get psycopg2 database connection"""
    import psycopg2
    return psycopg2.connect(**DATABASE_CONFIG)
EOF

chown www-data:www-data /var/www/cap_winds_app/db_config.py
echo "✓ Database config created: /var/www/cap_winds_app/db_config.py"

# Test connection
echo ""
echo "Testing database connection..."
python3 -c "
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
try:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT PostGIS_Version();')
    version = cur.fetchone()[0]
    print(f'✓ PostGIS connection successful: {version}')
    conn.close()
except Exception as e:
    print(f'✗ Connection failed: {e}')
    sys.exit(1)
" || echo "Install psycopg2-binary: pip install psycopg2-binary"

echo ""
echo "========================================="
echo "✓ Setup Complete!"
echo "========================================="
echo ""
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo "Password: $DB_PASS (CHANGE THIS IN PRODUCTION!)"
echo ""
echo "Next steps:"
echo "1. Update password in /var/www/cap_winds_app/db_config.py"
echo "2. Install Python dependencies:"
echo "   pip install psycopg2-binary sqlalchemy geoalchemy2"
echo "3. Test connection:"
echo "   python3 -c 'from db_config import get_connection; conn = get_connection(); print(conn)'"
echo ""

