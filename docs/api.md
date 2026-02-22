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
