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
