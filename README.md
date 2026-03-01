# CAP Weather Common Operating Picture (WxCOP)

A web-based weather operations system for Civil Air Patrol, providing real-time
weather data, radar visualization, and wind constraint analysis for aviation
and search-and-rescue mission planning.

**Production:** http://209.248.90.253/CAP_WxCOP/  
**Development:** http://209.248.90.253/CAP_WxCOP_DEV/

---

## Architecture

```
Apache2 (mod_wsgi)
├── /CAP_WxCOP       → /var/www/cap_winds_app/   (production)
└── /CAP_WxCOP_DEV   → /var/www/cap_winds_dev/   (development)

Flask Application
├── app.py           ← main routes
├── cap_winds.wsgi   ← WSGI entry point (touch this to reload, NOT app.wsgi)
├── weather_api.py   ← METAR/TAF blueprint
├── wind_forecast_api.py ← HRRR wind constraint analysis
├── radar_api.py     ← NEXRAD animation
└── templates/       ← Jinja2 HTML templates

LDM Pipeline
├── /home/ldm/etc/pqact_mrms.conf  ← product ingest patterns
├── /home/ldm/scripts/mrms_render_pipe.sh ← pqact PIPE wrapper
├── mrms_tile_renderer.py  ← GRIB2 → PNG tile pyramid renderer
└── /LDM/radar/mrms_tiles/ ← rendered tile output (NOT in git)
```

---

## Pages

| URL | Description |
|-----|-------------|
| `/CAP_WxCOP/` | Landing page |
| `/CAP_WxCOP/weather-map` | Weather COP — METARs, flight categories, TFRs, MRMS overlay |
| `/CAP_WxCOP/wind-map` | Wind constraint map — HRRR-based airport wind analysis |
| `/CAP_WxCOP/mrms` | MRMS radar — composite reflectivity, MESH, lightning, azimuthal shear |

---

## MRMS Radar System

Real-time MRMS GRIB2 data ingested via Unidata LDM, rendered to Leaflet PNG
tile pyramids every 2 minutes, animated in browser with 4-minute poll cycle.

**Products (Phase 1):**
- Composite Reflectivity (dBZ) — z3–z8
- MESH Hail Size (mm)
- Lightning Probability 60min (%)
- Azimuthal Shear 0-2km AGL (s⁻¹) — bipolar cyclonic/anticyclonic

**Frame retention:** 30 frames (fixed count, ~60 min at 2-min cycle)  
**Zoom:** z3–z8 (z8 ≈ 10km/tile; MRMS native resolution is 1km)  
**Tile path:** `/LDM/radar/mrms_tiles/<product>/CONUS/<YYYYMMDD-HHMM>/{z}/{x}/{y}.png`

### LDM Products

| Product key | GRIB2 pattern |
|-------------|---------------|
| composite | `MRMS_MergedReflectivityQComposite_*.grib2.gz` |
| mesh | `MRMS_MESH_[0-9]*.grib2.gz` |
| lightning | `MRMS_LightningProbabilityNext60minGrid_*.grib2.gz` |
| azshear | `MRMS_MergedAzShear01kmAGL_*.grib2.gz` *(FILE only until product name verified)* |

### Renderer

```bash
# Manual render test
/var/www/cap_winds_app/venv/bin/python3 mrms_tile_renderer.py \
    composite CONUS /path/to/file.grib2.gz

# Check render log
tail -50 /var/www/cap_winds_app/logs/mrms_renderer.log

# Verify tile output
ls /LDM/radar/mrms_tiles/composite/CONUS/
cat /LDM/radar/mrms_tiles/composite/CONUS/index.json | python3 -m json.tool | head -20
```

### pqact reload

```bash
ldmadmin pqactHup
```

---

## Deployment

### DEV → Production promotion

```bash
# 1. Test in DEV first
curl -s -o /dev/null -w "%{http_code}" http://209.248.90.253/CAP_WxCOP_DEV/mrms

# 2. Backup production
cp -r /var/www/cap_winds_app/templates /var/www/cap_winds_app/templates.bak.$(date +%Y%m%d)
cp /var/www/cap_winds_app/app.py /var/www/cap_winds_app/app.py.bak.$(date +%Y%m%d)

# 3. Copy files
cp /var/www/cap_winds_dev/templates/radar_map.html \
   /var/www/cap_winds_app/templates/
cp /var/www/cap_winds_dev/templates/enhanced_weather_map_complete.html \
   /var/www/cap_winds_app/templates/
cp /var/www/cap_winds_dev/mrms_tile_renderer.py \
   /var/www/cap_winds_app/mrms_tile_renderer.py

# 4. Reload (touch cap_winds.wsgi, NOT app.wsgi — app.wsgi is a decoy)
touch /var/www/cap_winds_app/cap_winds.wsgi

# 5. Smoke test
curl -s -o /dev/null -w "/mrms → %{http_code}\n" http://209.248.90.253/CAP_WxCOP/mrms
```

### Apache reload (config changes only)

```bash
apache2ctl configtest && systemctl reload apache2
```

---

## Known Gotchas

- **Touch `cap_winds.wsgi` not `app.wsgi`** — `app.wsgi` is zero-byte and unused. Apache's `WSGIScriptAlias` points to `cap_winds.wsgi`.
- **Jinja2 vs JavaScript `{{ }}`** — tile URL patterns like `{z}/{x}/{y}` in templates must use `{% raw %}` blocks or Jinja2 will try to interpret them. All current templates handle this correctly.
- **`error.html` must exist** — the `@app.errorhandler(500)` calls `render_template('error.html')`. If that template is missing, error handling double-faults and you get a bare Apache 500 with no useful traceback in the log.
- **MRMS tile Alias** — `Alias /CAP_WxCOP/static/mrms_tiles /LDM/radar/mrms_tiles` must be in the Apache vhost *before* the WSGIScriptAlias block, otherwise Apache serves 404 for all tile requests.
- **AzShear product name** — `MRMS_MergedAzShear01kmAGL_` is the expected LDM identifier but must be verified against actual queue before enabling the PIPE render action. Check: `ls /LDM/radar/mrms/$(date +%Y/%m/%d)/AzShear/`

---

## Development Workflow

```bash
# Work in DEV
vim /var/www/cap_winds_dev/templates/radar_map.html
touch /var/www/cap_winds_dev/cap_winds.wsgi
curl -s -o /dev/null -w "%{http_code}" http://209.248.90.253/CAP_WxCOP_DEV/mrms

# Commit to dev branch
git checkout dev
git add -A
git commit -m "description of change"
git push origin dev

# Promote to production
git checkout main
git merge dev
git push origin main
# then run deployment steps above
```

---

## Phase 2 Roadmap

See `CAP_WxCOP_Phase2_Requirements.md` for the full prioritized list.

Top items:
1. AzShear PIPE activation (verify product name, uncomment pqact entry)
2. Zoom > z8 with automatic transition to single-site NEXRAD Level III
3. Mid-level azimuthal shear (0-6km AGL)
4. Rotation track accumulation products (30min, 60min)
5. No-METAR airports on radar map (Tier 4)

---

## Server Info

- **OS:** Ubuntu 24.04
- **Web:** Apache 2.4 + mod_wsgi 5.0
- **Python:** 3.12 (venv at `/var/www/cap_winds_app/venv/`)
- **LDM:** Unidata LDM, upstream `idd.unidata.ucar.edu`
- **Data root:** `/LDM/`
- **Logs:** `/var/www/cap_winds_app/logs/`, `/var/log/apache2/`

