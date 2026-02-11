import os
import sys
import getpass
import traceback
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import xarray as xr
from scipy.interpolate import RegularGridInterpolator
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.patheffects as path_effects  # noqa: F401

import geopandas as gpd
from shapely.geometry import Point, Polygon, LineString, mapping as geom_mapping
import fiona
from fiona.crs import from_epsg

# ---------------------------------------------------------------------
# Base paths and cache/config dirs
# ---------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_CACHE_DIR = os.path.join(APP_DIR, ".cache")
APP_CONFIG_DIR = os.path.join(APP_DIR, ".config")

os.makedirs(APP_CACHE_DIR, exist_ok=True)
os.makedirs(APP_CONFIG_DIR, exist_ok=True)

os.environ.setdefault("MPLCONFIGDIR", os.path.join(APP_CONFIG_DIR, "matplotlib"))
os.environ.setdefault("CARTOPY_CACHE_DIR", os.path.join(APP_CACHE_DIR, "cartopy"))

MODEL_ROOT = "/var/www/cap_winds_app/model_data"
WEB_OUTPUT_DIR = "/var/www/html/cap_winds"
SHAPE_OUTPUT_DIR = "/var/www/html/cap_winds_shp"

MAX_OPERATIONAL_WIND = 30  # kts
CAUTION_WIND = 20          # kts

# ---------------------------------------------------------------------
# Logging / diagnostics
# ---------------------------------------------------------------------

def _log(msg):
    sys.stderr.write(f"[states_service] {msg}\n")
    sys.stderr.flush()


def _log_env_diagnostic():
    try:
        _log("ENV DIAGNOSTIC BEGIN")
        _log(f"  USER: {getpass.getuser()}")
        _log(f"  HOME: {os.environ.get('HOME')}")
        _log(f"  MPLCONFIGDIR: {os.environ.get('MPLCONFIGDIR')}")
        _log(f"  CARTOPY_CACHE_DIR: {os.environ.get('CARTOPY_CACHE_DIR')}")
        _log(f"  APP_DIR: {APP_DIR}")
        _log(f"  CWD: {os.getcwd()}")
        _log("ENV DIAGNOSTIC END")
    except Exception as e:
        sys.stderr.write(f"[states_service] ENV DIAGNOSTIC ERROR: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()

# ---------------------------------------------------------------------
# Static configuration (from original)
# ---------------------------------------------------------------------

STATE_BOUNDARIES = {
    'AL': {'name': 'Alabama', 'bounds': [-88.5, -84.8, 30.1, 35.1], 'region': 'SER'},
    'AK': {'name': 'Alaska', 'bounds': [-180, -129, 51, 72], 'region': 'PCR'},
    'AZ': {'name': 'Arizona', 'bounds': [-114.8, -109.0, 31.3, 37.1], 'region': 'SWR'},
    'AR': {'name': 'Arkansas', 'bounds': [-94.6, -89.6, 33.0, 36.5], 'region': 'SWR'},
    'CA': {'name': 'California', 'bounds': [-124.5, -114.1, 32.5, 42.1], 'region': 'PCR'},
    'CO': {'name': 'Colorado', 'bounds': [-109.1, -102.0, 36.9, 41.1], 'region': 'RMR'},
    'CT': {'name': 'Connecticut', 'bounds': [-73.8, -71.8, 40.9, 42.1], 'region': 'NER'},
    'DE': {'name': 'Delaware', 'bounds': [-75.8, -75.0, 38.4, 39.9], 'region': 'MAR'},
    'FL': {'name': 'Florida', 'bounds': [-87.7, -80.0, 24.4, 31.1], 'region': 'SER'},
    'GA': {'name': 'Georgia', 'bounds': [-85.6, -80.8, 30.3, 35.1], 'region': 'SER'},
    'HI': {'name': 'Hawaii', 'bounds': [-160.3, -154.7, 18.9, 22.3], 'region': 'PCR'},
    'ID': {'name': 'Idaho', 'bounds': [-117.3, -111.0, 41.9, 49.1], 'region': 'RMR'},
    'IL': {'name': 'Illinois', 'bounds': [-91.5, -87.5, 36.9, 42.6], 'region': 'GLR'},
    'IN': {'name': 'Indiana', 'bounds': [-88.1, -84.8, 37.7, 41.8], 'region': 'GLR'},
    'IA': {'name': 'Iowa', 'bounds': [-96.7, -90.1, 40.3, 43.6], 'region': 'NCR'},
    'KS': {'name': 'Kansas', 'bounds': [-102.1, -94.6, 36.9, 40.1], 'region': 'NCR'},
    'KY': {'name': 'Kentucky', 'bounds': [-89.6, -81.9, 36.5, 39.2], 'region': 'GLR'},
    'LA': {'name': 'Louisiana', 'bounds': [-94.1, -88.8, 28.9, 33.1], 'region': 'SWR'},
    'ME': {'name': 'Maine', 'bounds': [-71.1, -66.9, 43.0, 47.5], 'region': 'NER'},
    'MD': {'name': 'Maryland', 'bounds': [-79.5, -75.0, 37.9, 39.8], 'region': 'MAR'},
    'MA': {'name': 'Massachusetts', 'bounds': [-73.5, -69.9, 41.2, 42.9], 'region': 'NER'},
    'MI': {'name': 'Michigan', 'bounds': [-90.5, -82.1, 41.6, 48.3], 'region': 'GLR'},
    'MN': {'name': 'Minnesota', 'bounds': [-97.3, -89.5, 43.5, 49.4], 'region': 'NCR'},
    'MS': {'name': 'Mississippi', 'bounds': [-91.7, -88.1, 30.1, 35.1], 'region': 'SER'},
    'MO': {'name': 'Missouri', 'bounds': [-95.8, -89.1, 35.9, 40.7], 'region': 'NCR'},
    'MT': {'name': 'Montana', 'bounds': [-116.1, -104.0, 44.3, 49.1], 'region': 'RMR'},
    'NE': {'name': 'Nebraska', 'bounds': [-104.1, -95.3, 39.9, 43.1], 'region': 'NCR'},
    'NV': {'name': 'Nevada', 'bounds': [-120.1, -114.0, 35.0, 42.1], 'region': 'PCR'},
    'NH': {'name': 'New Hampshire', 'bounds': [-72.6, -70.6, 42.7, 45.4], 'region': 'NER'},
    'NJ': {'name': 'New Jersey', 'bounds': [-75.6, -73.9, 38.9, 41.4], 'region': 'MAR'},
    'NM': {'name': 'New Mexico', 'bounds': [-109.1, -103.0, 31.3, 37.1], 'region': 'SWR'},
    'NY': {'name': 'New York', 'bounds': [-79.8, -71.8, 40.5, 45.1], 'region': 'NER'},
    'NC': {'name': 'North Carolina', 'bounds': [-84.4, -75.4, 33.8, 36.6], 'region': 'MAR'},
    'ND': {'name': 'North Dakota', 'bounds': [-104.1, -96.5, 45.9, 49.1], 'region': 'NCR'},
    'OH': {'name': 'Ohio', 'bounds': [-84.9, -80.5, 38.4, 42.0], 'region': 'GLR'},
    'OK': {'name': 'Oklahoma', 'bounds': [-103.1, -94.4, 33.6, 37.1], 'region': 'SWR'},
    'OR': {'name': 'Oregon', 'bounds': [-124.7, -116.5, 41.9, 46.3], 'region': 'PCR'},
    'PA': {'name': 'Pennsylvania', 'bounds': [-80.6, -74.7, 39.7, 42.3], 'region': 'MAR'},
    'RI': {'name': 'Rhode Island', 'bounds': [-71.9, -71.1, 41.1, 42.1], 'region': 'NER'},
    'SC': {'name': 'South Carolina', 'bounds': [-83.4, -78.5, 32.0, 35.3], 'region': 'SER'},
    'SD': {'name': 'South Dakota', 'bounds': [-104.1, -96.4, 42.5, 45.9], 'region': 'NCR'},
    'TN': {'name': 'Tennessee', 'bounds': [-90.4, -81.6, 34.9, 36.7], 'region': 'SER'},
    'TX': {'name': 'Texas', 'bounds': [-106.7, -93.5, 25.8, 36.6], 'region': 'SWR'},
    'UT': {'name': 'Utah', 'bounds': [-114.1, -109.0, 37.0, 42.1], 'region': 'RMR'},
    'VT': {'name': 'Vermont', 'bounds': [-73.5, -71.5, 42.7, 45.1], 'region': 'NER'},
    'VA': {'name': 'Virginia', 'bounds': [-83.7, -75.2, 36.5, 39.5], 'region': 'MAR'},
    'WA': {'name': 'Washington', 'bounds': [-124.9, -116.9, 45.5, 49.1], 'region': 'PCR'},
    'WV': {'name': 'West Virginia', 'bounds': [-82.7, -77.7, 37.2, 40.7], 'region': 'MAR'},
    'WI': {'name': 'Wisconsin', 'bounds': [-92.9, -86.2, 42.5, 47.3], 'region': 'GLR'},
    'WY': {'name': 'Wyoming', 'bounds': [-111.1, -104.0, 40.9, 45.1], 'region': 'RMR'},
    'PR': {'name': 'Puerto Rico', 'bounds': [-67.3, -65.2, 17.9, 18.6], 'region': 'SER'},
    'VI': {'name': 'US Virgin Islands', 'bounds': [-65.1, -64.6, 17.6, 18.5], 'region': 'SER'},
    'GU': {'name': 'Guam', 'bounds': [144.6, 144.9, 13.2, 13.7], 'region': 'PCR'},
}

CAP_REGIONS = {
    'NER': {'name': 'Northeast Region', 'states': ['CT', 'ME', 'MA', 'NH', 'NY', 'RI', 'VT']},
    'MAR': {'name': 'Mid-Atlantic Region', 'states': ['DE', 'MD', 'NJ', 'NC', 'PA', 'VA', 'WV']},
    'SER': {'name': 'Southeast Region', 'states': ['AL', 'FL', 'GA', 'MS', 'SC', 'TN', 'PR', 'VI']},
    'GLR': {'name': 'Great Lakes Region', 'states': ['IL', 'IN', 'KY', 'MI', 'OH', 'WI']},
    'NCR': {'name': 'North Central Region', 'states': ['IA', 'KS', 'MN', 'MO', 'ND', 'NE', 'SD']},
    'RMR': {'name': 'Rocky Mountain Region', 'states': ['CO', 'ID', 'MT', 'UT', 'WY']},
    'SWR': {'name': 'Southwest Region', 'states': ['AR', 'AZ', 'LA', 'NM', 'OK', 'TX']},
    'PCR': {'name': 'Pacific Region', 'states': ['AK', 'CA', 'HI', 'NV', 'OR', 'WA']},
}

PCR_SUBMAPS = {
    'PCR-AK': {'name': 'Alaska', 'states': ['AK'], 'bounds': [-180, -129, 51, 72]},
    'PCR-HI': {'name': 'Hawaii', 'states': ['HI'], 'bounds': [-160.3, -154.7, 18.9, 22.3]},
    'PCR-WC': {'name': 'West Coast', 'states': ['CA', 'NV', 'OR', 'WA'], 'bounds': [-125, -114, 32, 49]},
}

CONUS_BOUNDS = [-125, -65, 24, 50]

# ---------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------

def ensure_output_directory():
    try:
        os.makedirs(WEB_OUTPUT_DIR, exist_ok=True)
        return WEB_OUTPUT_DIR
    except PermissionError:
        fallback = os.path.join(APP_DIR, "cap_winds_output")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _get_cycle():
    now_utc = datetime.utcnow()
    cycle_time = now_utc - timedelta(hours=2)
    cycle_date = cycle_time.strftime("%Y%m%d")
    cycle_hour = cycle_time.strftime("%H")
    return now_utc, cycle_time, cycle_date, cycle_hour


def _find_latest_hrrr_cycle_dir(cycle_date, cycle_hour):
    target_dir = os.path.join(MODEL_ROOT, f"hrrr.{cycle_date}", f"{cycle_hour}z")
    if os.path.isdir(target_dir):
        return target_dir

    if not os.path.isdir(MODEL_ROOT):
        return None

    latest = None
    try:
        for d in sorted(os.listdir(MODEL_ROOT), reverse=True):
            if not d.startswith("hrrr."):
                continue
            date_dir = os.path.join(MODEL_ROOT, d)
            if not os.path.isdir(date_dir):
                continue
            for hdir in sorted(os.listdir(date_dir), reverse=True):
                if not hdir.endswith("z"):
                    continue
                cand = os.path.join(date_dir, hdir)
                if os.path.isdir(cand):
                    latest = cand
                    break
            if latest:
                break
    except Exception as e:
        _log(f"_find_latest_hrrr_cycle_dir error: {e}")
    return latest


def _download_grib(hour, base_url, cycle_date, cycle_hour, local_dir):
    os.makedirs(local_dir, exist_ok=True)
    fhour = f"{hour:02d}"
    filename = f"hrrr.t{cycle_hour}z.wrfsfcf{fhour}.grib2"
    local_path = os.path.join(local_dir, filename)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        _log(f"Using existing HRRR: {local_path}")
        return local_path

    if base_url is None:
        base_url = f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{cycle_date}/conus"
    url = f"{base_url}/{filename}"
    _log(f"Downloading HRRR {url} -> {local_path}")
    try:
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        tmp = local_path + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, local_path)
        return local_path
    except Exception as e:
        _log(f"ERROR downloading {url}: {e}")
        return None


def _open_hrrr_wind_fields(grib_file):
    if grib_file is None:
        return (None,) * 6
    try:
        ds_wind = xr.open_dataset(
            grib_file,
            engine="cfgrib",
            backend_kwargs={"filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 10}},
        )
        ds_gust = xr.open_dataset(
            grib_file,
            engine="cfgrib",
            backend_kwargs={"filter_by_keys": {"typeOfLevel": "surface", "stepType": "max"}},
        )

        wind_vars = list(ds_wind.data_vars)
        gust_vars = list(ds_gust.data_vars)
        uvar = next((v for v in wind_vars if "u" in v.lower()), None)
        vvar = next((v for v in wind_vars if "v" in v.lower()), None)
        gvar = next((v for v in gust_vars if "gust" in v.lower()), None)
        if uvar is None or vvar is None:
            return (None,) * 6

        u10 = ds_wind[uvar]
        v10 = ds_wind[vvar]
        gust10 = ds_gust[gvar] if gvar else None
        time = pd.to_datetime(u10["time"].values)

        if "latitude" in u10.coords:
            lats = u10["latitude"].values
            lons = u10["longitude"].values
        elif "lat" in u10.coords:
            lats = u10["lat"].values
            lons = u10["lon"].values
        else:
            return (None,) * 6

        return time, lats, lons, u10.values, v10.values, (gust10.values if gust10 is not None else None)
    except Exception as e:
        _log(f"ERROR opening HRRR fields: {e}")
        return (None,) * 6

# ---------------------------------------------------------------------
# Shapefile export helpers
# ---------------------------------------------------------------------

def _export_airports_shp(location_airports, shapefile_path):
    if location_airports is None or len(location_airports) == 0:
        _log("No airports to export to shapefile.")
        return

    out_dir = os.path.dirname(shapefile_path)
    os.makedirs(out_dir, exist_ok=True)

    gdf = gpd.GeoDataFrame(
        location_airports.copy(),
        geometry=[
            Point(lon, lat)
            for lon, lat in zip(location_airports["longitude_deg"], location_airports["latitude_deg"])
        ],
        crs="EPSG:4326",
    )
    cols = [
        "ident", "gps_code", "iata_code", "name", "iso_region",
        "type", "max_wind_kts", "status",
    ]
    cols = [c for c in cols if c in gdf.columns]
    gdf = gdf[cols + ["geometry"]]

    _log(f"Writing airport shapefile: {shapefile_path}")
    gdf.to_file(shapefile_path, driver="ESRI Shapefile")


def _export_contour_polygons(cf, shapefile_path):
    os.makedirs(os.path.dirname(shapefile_path), exist_ok=True)
    schema = {
        "geometry": "Polygon",
        "properties": {"level_min": "float", "level_max": "float"},
    }

    levels = cf.levels
    level_lookup = {}
    for i, col in enumerate(cf.collections):
        if i == 0:
            level_lookup[col] = (float("-inf"), float(levels[0]))
        elif i == len(cf.collections) - 1:
            level_lookup[col] = (float(levels[-1]), float("inf"))
        else:
            level_lookup[col] = (float(levels[i - 1]), float(levels[i]))

    with fiona.open(
        shapefile_path, "w", driver="ESRI Shapefile", schema=schema, crs=from_epsg(4326)
    ) as dst:
        for col in cf.collections:
            lvl_min, lvl_max = level_lookup[col]
            for path in col.get_paths():
                polys = path.to_polygons()
                if not polys:
                    continue
                outer = polys[0]
                holes = polys[1:] if len(polys) > 1 else []
                try:
                    poly = Polygon(shell=outer, holes=holes if holes else None)
                    if not poly.is_valid or poly.is_empty:
                        continue
                except Exception:
                    continue
                dst.write(
                    {
                        "geometry": geom_mapping(poly),
                        "properties": {"level_min": lvl_min, "level_max": lvl_max},
                    }
                )


def _export_contour_lines(cs, shapefile_path):
    os.makedirs(os.path.dirname(shapefile_path), exist_ok=True)
    schema = {"geometry": "LineString", "properties": {"level": "float"}}
    with fiona.open(
        shapefile_path, "w", driver="ESRI Shapefile", schema=schema, crs=from_epsg(4326)
    ) as dst:
        for level, col in zip(cs.levels, cs.collections):
            for path in col.get_paths():
                vertices = path.vertices
                if vertices.shape[0] < 2:
                    continue
                line = LineString(vertices)
                if line.is_empty:
                    continue
                dst.write(
                    {
                        "geometry": geom_mapping(line),
                        "properties": {"level": float(level)},
                    }
                )

# ---------------------------------------------------------------------
# Map creation
# ---------------------------------------------------------------------

def _create_map(
    map_name,
    map_bounds,
    airports_df,
    primary_airport,
    lons,
    lats,
    max_wind_kts,
    cycle_date,
    cycle_hour,
    forecast_hours,
    is_conus_map=False,
    contour_poly_shp=None,
    contour_line_shp=None,
):
    fig = plt.figure(figsize=(20, 12))
    ax = plt.axes([0.1, 0.1, 0.7, 0.8], projection=ccrs.PlateCarree())

    ax.add_feature(cfeature.STATES, linewidth=1.0, edgecolor="black")
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.coastlines("50m", linewidth=0.8)
    try:
        ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor="gray")
    except Exception:
        pass

    levels = np.arange(0, 65, 5)

    # 1) Data-space contours (no Cartopy) for shapefile export
    cf_data = plt.contourf(
        lons,
        lats,
        max_wind_kts,
        levels=levels,
        cmap="YlOrRd",
    )
    cs_data = plt.contour(
        lons,
        lats,
        max_wind_kts,
        levels=levels,
        colors="none",
    )

    if contour_poly_shp is not None:
        _log(f"Exporting contour polygons: {contour_poly_shp}")
        _export_contour_polygons(cf_data, contour_poly_shp)

    if contour_line_shp is not None:
        _log(f"Exporting contour lines: {contour_line_shp}")
        _export_contour_lines(cs_data, contour_line_shp)

    # Remove temporary data-space contours so they don't appear on figure
    for c in cf_data.collections:
        c.remove()
    for c in cs_data.collections:
        c.remove()

    # 2) Cartopy-aware contours for the actual map
    cf = ax.contourf(
        lons,
        lats,
        max_wind_kts,
        levels=levels,
        cmap="YlOrRd",
        transform=ccrs.PlateCarree(),
        extend="max",
        alpha=0.6,
    )

    cs_cap1 = ax.contour(
        lons,
        lats,
        max_wind_kts,
        levels=[CAUTION_WIND],
        colors="orange",
        linewidths=2,
        transform=ccrs.PlateCarree(),
    )
    ax.clabel(cs_cap1, inline=True, fontsize=10, fmt=f"{CAUTION_WIND} kts")

    cs_cap2 = ax.contour(
        lons,
        lats,
        max_wind_kts,
        levels=[MAX_OPERATIONAL_WIND],
        colors="red",
        linewidths=3,
        transform=ccrs.PlateCarree(),
    )
    ax.clabel(cs_cap2, inline=True, fontsize=12, fmt=f"{MAX_OPERATIONAL_WIND} kts")

    # Airports
    if airports_df is not None and len(airports_df) > 0:
        airports_df = airports_df.copy()
        airports_df["priority"] = 4
        airports_df.loc[airports_df["type"] == "medium_airport", "priority"] = 3
        airports_df.loc[airports_df["type"] == "large_airport", "priority"] = 2

        military_keywords = [
            "AFB", "AIR FORCE BASE", "AIR FORCE", "NAVAL", "NAS ", "MCAS",
            "ARMY", "MILITARY", "JOINT BASE", "AIR STATION", "ANGB",
            "AIR NATIONAL GUARD", "COAST GUARD", "USAF", "USAFE",
            "SPACE FORCE", "SPACEPORT", "NATIONAL GUARD", "RESERVE",
            "FIELD AAF", "ARMY AIRFIELD", "NAVY", "MARINE CORPS",
        ]
        for idx, row in airports_df.iterrows():
            name_upper = str(row["name"]).upper()
            keywords_upper = str(row.get("keywords", "")).upper()
            combined = name_upper + " " + keywords_upper
            for kw in military_keywords:
                if kw in combined:
                    airports_df.at[idx, "priority"] = 1
                    break

        if primary_airport:
            primary_matches = airports_df[
                (airports_df["gps_code"] == primary_airport)
                | (airports_df["ident"] == primary_airport)
                | (airports_df["iata_code"] == primary_airport)
            ]
            if len(primary_matches) > 0:
                airports_df.loc[primary_matches.index, "priority"] = 0

        for status, color, size, marker in [
            ("Normal", "green", 25, "o"),
            ("Caution", "orange", 40, "^"),
            ("Out of Limits", "red", 60, "X"),
        ]:
            subset = airports_df[airports_df["status"] == status]
            if len(subset) > 0:
                ax.scatter(
                    subset["longitude_deg"],
                    subset["latitude_deg"],
                    c=color,
                    s=size,
                    marker=marker,
                    alpha=0.8,
                    transform=ccrs.PlateCarree(),
                    label=f"{status} ({len(subset)})",
                    edgecolors="black",
                    linewidths=0.7,
                    zorder=5,
                )

        airports_sorted = airports_df.sort_values("priority")
        total_airports = len(airports_df)

        if is_conus_map:
            high_priority = airports_sorted[airports_sorted["priority"] <= 2]
            target = int(len(high_priority) * 0.7)
            max_labels = max(30, target)
        else:
            if total_airports <= 10:
                max_labels = total_airports
            elif total_airports <= 20:
                max_labels = total_airports
            elif total_airports <= 30:
                max_labels = 25
            elif total_airports <= 50:
                max_labels = 35
            elif total_airports <= 100:
                max_labels = 50
            else:
                max_labels = 60
            military_and_large = airports_sorted[airports_sorted["priority"] <= 2]
            num_priority = len(military_and_large)
            if num_priority > max_labels:
                max_labels = num_priority

        labeled_count = 0
        for _, row in airports_sorted.iterrows():
            if is_conus_map and row["priority"] > 2:
                continue
            if labeled_count >= max_labels:
                break

            label = row["gps_code"] if pd.notna(row["gps_code"]) else row["ident"]
            if row["priority"] == 0:
                fontsize, fontweight, color = 9, "bold", "darkred"
            elif row["priority"] == 1:
                fontsize, fontweight, color = 8, "bold", "darkblue"
            elif row["priority"] == 2:
                fontsize, fontweight, color = 8, "bold", "black"
            elif row["priority"] == 3:
                fontsize, fontweight, color = 7, "normal", "black"
            else:
                fontsize, fontweight, color = 7, "normal", "dimgray"

            ax.text(
                row["longitude_deg"] + 0.05,
                row["latitude_deg"] + 0.05,
                label,
                fontsize=fontsize,
                fontweight=fontweight,
                color=color,
                transform=ccrs.PlateCarree(),
                ha="left",
                va="bottom",
                zorder=6,
                path_effects=[plt.matplotlib.patheffects.withStroke(
                    linewidth=2, foreground="white"
                )],
            )
            labeled_count += 1

        legend = ax.legend(
            loc="upper left",
            bbox_to_anchor=(1.05, 1.0),
            fontsize=11,
            framealpha=0.95,
            title="Airport Status",
        )
        legend.get_title().set_fontsize(12)
        legend.get_title().set_fontweight("bold")

    cbar_ax = fig.add_axes([0.15, 0.05, 0.5, 0.02])
    cbar = plt.colorbar(cf, cax=cbar_ax, orientation="horizontal")
    cbar.set_label("Maximum Wind Speed (kts)", fontsize=12)

    padding = 0.5
    ax.set_extent(
        [
            map_bounds[0] - padding,
            map_bounds[1] + padding,
            map_bounds[2] - padding,
            map_bounds[3] + padding,
        ],
        crs=ccrs.PlateCarree(),
    )

    gl = ax.gridlines(
        draw_labels=True,
        linewidth=0.5,
        color="gray",
        alpha=0.5,
        linestyle="--",
    )
    gl.top_labels = False
    gl.right_labels = False

    init_time = pd.to_datetime(f"{cycle_date} {cycle_hour}:00")
    plt.suptitle(
        f"CAP Aircraft Wind Constraint Analysis - {map_name}\n"
        f"Init: {init_time.strftime('%Y-%m-%d %H:%M')} UTC | "
        f"Max Winds: Forecast Hours 0-{forecast_hours.stop - 1}\n"
        f"Airports: Paved runways ≥ 2500 ft",
        fontsize=14,
        fontweight="bold",
        y=0.95,
    )

    textstr = (
        f"CAP Wind Limits\n(CAPR 70-1):\n\n"
        f"≤ {CAUTION_WIND} kts:\nNormal operations\n\n"
        f"> {CAUTION_WIND} kts:\nCaution\n\n"
        f"> {MAX_OPERATIONAL_WIND} kts:\nRequires SFRO +\n"
        f"Wing Commander\napproval"
    )
    text_ax = fig.add_axes([0.82, 0.15, 0.16, 0.35])
    text_ax.axis("off")
    text_ax.text(
        0.5,
        0.5,
        textstr,
        transform=text_ax.transAxes,
        fontsize=10,
        va="center",
        ha="center",
        bbox=dict(
            boxstyle="round",
            facecolor="wheat",
            alpha=0.95,
            edgecolor="black",
            linewidth=1,
        ),
    )

    return fig

# ---------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------

def run_analysis(location_type, location_code, primary_airport=None):
    _log_env_diagnostic()
    _log(f"run_analysis start: type={location_type}, code={location_code}, primary={primary_airport}")

    is_conus = False
    is_region = False
    is_pcr = False

    location_code = location_code.upper()

    if location_type == "conus":
        states_to_process = [
            code for code in STATE_BOUNDARIES.keys()
            if code not in ["AK", "HI", "PR", "VI", "GU"]
        ]
        map_bounds = CONUS_BOUNDS
        location_name = "Continental United States"
        is_conus = True
    elif location_type == "state":
        if location_code not in STATE_BOUNDARIES:
            raise ValueError(f"Unknown state code: {location_code}")
        states_to_process = [location_code]
        map_bounds = STATE_BOUNDARIES[location_code]["bounds"]
        location_name = STATE_BOUNDARIES[location_code]["name"]
    elif location_type == "region":
        if location_code not in CAP_REGIONS:
            raise ValueError(f"Unknown region code: {location_code}")
        if location_code == "PCR":
            is_region = True
            is_pcr = True
            location_name = CAP_REGIONS[location_code]["name"]
            states_to_process = CAP_REGIONS[location_code]["states"]
            all_bounds = [STATE_BOUNDARIES[s]["bounds"] for s in states_to_process]
            map_bounds = [
                min(b[0] for b in all_bounds),
                max(b[1] for b in all_bounds),
                min(b[2] for b in all_bounds),
                max(b[3] for b in all_bounds),
            ]
        else:
            is_region = True
            location_name = CAP_REGIONS[location_code]["name"]
            states_to_process = CAP_REGIONS[location_code]["states"]
            all_bounds = [STATE_BOUNDARIES[s]["bounds"] for s in states_to_process]
            map_bounds = [
                min(b[0] for b in all_bounds),
                max(b[1] for b in all_bounds),
                min(b[2] for b in all_bounds),
                max(b[3] for b in all_bounds),
            ]
    else:
        raise ValueError(f"Unknown location_type: {location_type}")

    now_utc, cycle_time, cycle_date, cycle_hour = _get_cycle()
    forecast_hours = range(0, 13)

    hrrr_cycle_dir = _find_latest_hrrr_cycle_dir(cycle_date, cycle_hour)
    if hrrr_cycle_dir:
        _log(f"Using HRRR local cycle dir: {hrrr_cycle_dir}")
        local_dir = hrrr_cycle_dir
        base_url = None
    else:
        _log("No pre-downloaded HRRR; using APP_DIR/hrrr_data with remote fetch.")
        local_dir = os.path.join(APP_DIR, "hrrr_data")
        base_url = f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{cycle_date}/conus"
    os.makedirs(local_dir, exist_ok=True)

    wind_speeds = []
    wind_gusts = []
    times = []
    lats = None
    lons = None

    for fh in forecast_hours:
        grib_file = _download_grib(fh, base_url, cycle_date, cycle_hour, local_dir)
        if grib_file is None:
            continue
        time, lats_tmp, lons_tmp, u10, v10, gust10 = _open_hrrr_wind_fields(grib_file)
        if time is None:
            continue
        if lats is None:
            lats = lats_tmp
            lons = lons_tmp
        wspd = np.sqrt(u10 ** 2 + v10 ** 2)
        wind_speeds.append(wspd)
        if gust10 is not None:
            wind_gusts.append(gust10)
        times.append(time)

    if len(wind_speeds) == 0:
        raise RuntimeError("No valid HRRR data retrieved.")

    wind_speeds = np.array(wind_speeds)
    if len(wind_gusts) > 0:
        wind_gusts = np.array(wind_gusts)
        max_wind_total = np.maximum(np.nanmax(wind_speeds, axis=0), np.nanmax(wind_gusts, axis=0))
    else:
        max_wind_total = np.nanmax(wind_speeds, axis=0)
    max_wind_kts = max_wind_total * 1.944

    airport_url = "https://davidmegginson.github.io/ourairports-data/airports.csv"
    runway_url = "https://davidmegginson.github.io/ourairports-data/runways.csv"

    _log("Downloading airport list…")
    df_airports = pd.read_csv(airport_url)

    if location_type == "state":
        location_airports = df_airports[
            (df_airports["iso_country"] == "US")
            & (df_airports["iso_region"] == f"US-{location_code}")
        ].copy()
    elif location_type == "conus":
        conus_state_codes = ["US-" + s for s in states_to_process]
        location_airports = df_airports[
            (df_airports["iso_country"] == "US")
            & (df_airports["iso_region"].isin(conus_state_codes))
        ].copy()
    else:
        if is_pcr:
            location_airports = df_airports[
                (df_airports["iso_country"] == "US")
                & (df_airports["iso_region"].str.startswith("US-"))
            ].copy()
            pcr_states = ["US-" + s for s in CAP_REGIONS["PCR"]["states"]]
            location_airports = location_airports[
                location_airports["iso_region"].isin(pcr_states)
            ]
        else:
            region_state_codes = ["US-" + s for s in states_to_process]
            location_airports = df_airports[
                (df_airports["iso_country"] == "US")
                & (df_airports["iso_region"].isin(region_state_codes))
            ].copy()

    location_airports = location_airports[
        location_airports["type"].isin(["large_airport", "medium_airport", "small_airport"])
    ]
    _log(f"Airports before runway filter: {len(location_airports)}")

    dfrunways = pd.read_csv(runway_url)
    pavedsurfaces = ["ASP", "ASPH", "CON", "CONC", "concrete", "asphalt"]
    pavedrunways = dfrunways[
        (dfrunways["surface"].isin(pavedsurfaces))
        & (dfrunways["length_ft"] >= 2500)
    ]
    qualifying_airport_ids = pavedrunways["airport_ident"].unique()
    location_airports = location_airports[location_airports["ident"].isin(qualifying_airport_ids)]
    _log(f"Airports with paved runways ≥2500 ft: {len(location_airports)}")

    if location_airports is not None and len(location_airports) > 0:
        if lats.ndim == 2:
            lat_1d = lats[:, 0]
            lon_1d = lons[0, :]
        else:
            lat_1d = lats
            lon_1d = lons

        interp = RegularGridInterpolator(
            (lat_1d, lon_1d),
            max_wind_kts,
            bounds_error=False,
            fill_value=np.nan,
        )

        winds = []
        for _, row in location_airports.iterrows():
            lat = row["latitude_deg"]
            lon = row["longitude_deg"]
            if lon < 0 and lon_1d.min() > 0:
                lon += 360
            elif lon > 180 and lon_1d.max() < 180:
                lon -= 360
            try:
                winds.append(interp((lat, lon)))
            except Exception:
                winds.append(np.nan)

        location_airports["max_wind_kts"] = winds
        location_airports["status"] = "Normal"
        location_airports.loc[location_airports["max_wind_kts"] >= CAUTION_WIND, "status"] = "Caution"
        location_airports.loc[
            location_airports["max_wind_kts"] >= MAX_OPERATIONAL_WIND, "status"
        ] = "Out of Limits"

        _log(
            f"Airport status: "
            f"Normal={len(location_airports[location_airports['status']=='Normal'])}, "
            f"Caution={len(location_airports[location_airports['status']=='Caution'])}, "
            f"Out={len(location_airports[location_airports['status']=='Out of Limits'])}"
        )

    dtg = cycle_time.strftime("%d%H%M") + "Z" + cycle_time.strftime("%b%y").upper()
    output_dir = ensure_output_directory()
    results = []

    if location_type == "conus":
        shape_prefix = f"cap_wind_CONUS_{dtg}"
    else:
        shape_prefix = f"cap_wind_{location_code}_{dtg}"

    airports_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_airports.shp")
    _export_airports_shp(location_airports, airports_shp)

    if location_type == "state":
        contour_poly_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_poly.shp")
        contour_line_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_line.shp")
        fig = _create_map(
            location_name,
            map_bounds,
            location_airports,
            primary_airport,
            lons,
            lats,
            max_wind_kts,
            cycle_date,
            cycle_hour,
            forecast_hours,
            is_conus_map=False,
            contour_poly_shp=contour_poly_shp,
            contour_line_shp=contour_line_shp,
        )
        output_file = os.path.join(output_dir, f"cap_wind_{location_code}_{dtg}.png")
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        plt.close(fig)
        url = f"/cap_winds/{os.path.basename(output_file)}"
        results.append({"map_name": location_name, "filename": output_file, "url": url})

    elif location_type == "conus":
        contour_poly_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_poly.shp")
        contour_line_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_line.shp")
        fig = _create_map(
            location_name,
            map_bounds,
            location_airports,
            primary_airport,
            lons,
            lats,
            max_wind_kts,
            cycle_date,
            cycle_hour,
            forecast_hours,
            is_conus_map=True,
            contour_poly_shp=contour_poly_shp,
            contour_line_shp=contour_line_shp,
        )
        output_file = os.path.join(output_dir, f"cap_wind_CONUS_{dtg}.png")
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        plt.close(fig)
        url = f"/cap_winds/{os.path.basename(output_file)}"
        results.append({"map_name": location_name, "filename": output_file, "url": url})

    elif is_pcr:
        for submap_code, submap_data in PCR_SUBMAPS.items():
            sub_states = submap_data["states"]
            sub_state_codes = ["US-" + s for s in sub_states]
            sub_airports = location_airports[
                location_airports["iso_region"].isin(sub_state_codes)
            ].copy()
            sub_prefix = f"cap_wind_{submap_code}_{dtg}"
            contour_poly_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{sub_prefix}_contours_poly.shp")
            contour_line_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{sub_prefix}_contours_line.shp")
            _export_airports_shp(sub_airports, os.path.join(SHAPE_OUTPUT_DIR, f"{sub_prefix}_airports.shp"))
            fig = _create_map(
                submap_data["name"],
                submap_data["bounds"],
                sub_airports,
                primary_airport,
                lons,
                lats,
                max_wind_kts,
                cycle_date,
                cycle_hour,
                forecast_hours,
                is_conus_map=False,
                contour_poly_shp=contour_poly_shp,
                contour_line_shp=contour_line_shp,
            )
            output_file = os.path.join(output_dir, f"cap_wind_{submap_code}_{dtg}.png")
            plt.savefig(output_file, dpi=150, bbox_inches="tight")
            plt.close(fig)
            url = f"/cap_winds/{os.path.basename(output_file)}"
            results.append(
                {
                    "map_name": f"{submap_data['name']} ({submap_code})",
                    "filename": output_file,
                    "url": url,
                }
            )
    else:
        contour_poly_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_poly.shp")
        contour_line_shp = os.path.join(SHAPE_OUTPUT_DIR, f"{shape_prefix}_contours_line.shp")
        fig = _create_map(
            location_name,
            map_bounds,
            location_airports,
            primary_airport,
            lons,
            lats,
            max_wind_kts,
            cycle_date,
            cycle_hour,
            forecast_hours,
            is_conus_map=False,
            contour_poly_shp=contour_poly_shp,
            contour_line_shp=contour_line_shp,
        )
        output_file = os.path.join(output_dir, f"cap_wind_{location_code}_{dtg}.png")
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        plt.close(fig)
        url = f"/cap_winds/{os.path.basename(output_file)}"
        results.append({"map_name": location_name, "filename": output_file, "url": url})

    _log(f"run_analysis complete, generated {len(results)} map(s).")
    return results

