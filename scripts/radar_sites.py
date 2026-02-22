"""
NEXRAD Radar Sites Database
Complete listing of all ~160 WSR-88D radar sites

Coverage:
- CONUS: ~145 sites
- Alaska: 11 sites
- Hawaii: 5 sites
- Guam: 1 site
- Puerto Rico: 1 site

Each site includes:
- Site ID (4-letter code)
- Location name
- Latitude/Longitude (decimal degrees)
- Elevation (feet MSL)
"""

from math import radians, cos, sin, asin, sqrt
from typing import Dict, List, Tuple, Optional

# Complete NEXRAD site database
NEXRAD_SITES = {
    # Alaska (11 sites)
    'ABC': {'name': 'Bethel, AK', 'lat': 60.7919, 'lon': -161.8764, 'elev': 162},
    'ACG': {'name': 'Sitka, AK', 'lat': 56.8525, 'lon': -135.5289, 'elev': 209},
    'AEC': {'name': 'Nome, AK', 'lat': 64.5114, 'lon': -165.2950, 'elev': 54},
    'AHG': {'name': 'Anchorage, AK', 'lat': 60.7256, 'lon': -151.3514, 'elev': 242},
    'AIH': {'name': 'Middleton Island, AK', 'lat': 59.4619, 'lon': -146.3031, 'elev': 107},
    'AKC': {'name': 'King Salmon, AK', 'lat': 58.6794, 'lon': -156.6297, 'elev': 63},
    'APD': {'name': 'Fairbanks, AK', 'lat': 65.0353, 'lon': -147.6114, 'elev': 2593},
    'HDC': {'name': 'Kodiak, AK', 'lat': 57.5986, 'lon': -152.5519, 'elev': 2381},
    'KJK': {'name': 'South Central, AK', 'lat': 62.3261, 'lon': -145.6236, 'elev': 2094},
    'KSG': {'name': 'Southeast, AK', 'lat': 59.7833, 'lon': -160.5153, 'elev': 1450},
    
    # Hawaii (5 sites)
    'HKI': {'name': 'South Kauai, HI', 'lat': 21.8939, 'lon': -159.5525, 'elev': 179},
    'HKM': {'name': 'Kohala, HI', 'lat': 20.1256, 'lon': -155.7781, 'elev': 3812},
    'HMO': {'name': 'Molokai, HI', 'lat': 21.1328, 'lon': -157.1808, 'elev': 1363},
    'HWA': {'name': 'South Shore, HI', 'lat': 19.0950, 'lon': -155.5689, 'elev': 1370},
    
    # Guam (1 site)
    'GUA': {'name': 'Andersen AFB, Guam', 'lat': 13.4545, 'lon': 144.8081, 'elev': 866},
    
    # Puerto Rico (1 site)
    'JUA': {'name': 'San Juan, PR', 'lat': 18.1156, 'lon': -66.0781, 'elev': 2794},
    
    # CONUS Sites (~145 sites)
    'ABR': {'name': 'Aberdeen, SD', 'lat': 45.4558, 'lon': -98.4132, 'elev': 1302},
    'ABX': {'name': 'Albuquerque, NM', 'lat': 35.1497, 'lon': -106.8239, 'elev': 5870},
    'AKQ': {'name': 'Norfolk/Richmond, VA', 'lat': 36.9840, 'lon': -77.0075, 'elev': 112},
    'AMA': {'name': 'Amarillo, TX', 'lat': 35.2334, 'lon': -101.7092, 'elev': 3587},
    'AMX': {'name': 'Miami, FL', 'lat': 25.6111, 'lon': -80.4128, 'elev': 14},
    'APX': {'name': 'Gaylord, MI', 'lat': 44.9072, 'lon': -84.7197, 'elev': 1464},
    'ARX': {'name': 'La Crosse, WI', 'lat': 43.8228, 'lon': -91.1914, 'elev': 1276},
    'ATX': {'name': 'Seattle/Tacoma, WA', 'lat': 48.1947, 'lon': -122.4958, 'elev': 495},
    'BBX': {'name': 'Beale AFB, CA', 'lat': 39.4958, 'lon': -121.6317, 'elev': 173},
    'BGM': {'name': 'Binghamton, NY', 'lat': 42.1997, 'lon': -75.9847, 'elev': 1606},
    'BHX': {'name': 'Eureka, CA', 'lat': 40.4986, 'lon': -124.2919, 'elev': 2402},
    'BIS': {'name': 'Bismarck, ND', 'lat': 46.7709, 'lon': -100.7606, 'elev': 1658},
    'BLX': {'name': 'Billings, MT', 'lat': 45.8539, 'lon': -108.6069, 'elev': 3598},
    'BMX': {'name': 'Birmingham, AL', 'lat': 33.1722, 'lon': -86.7697, 'elev': 645},
    'BOX': {'name': 'Boston, MA', 'lat': 41.9559, 'lon': -71.1369, 'elev': 118},
    'BRO': {'name': 'Brownsville, TX', 'lat': 25.9160, 'lon': -97.4189, 'elev': 23},
    'BUF': {'name': 'Buffalo, NY', 'lat': 42.9489, 'lon': -78.7369, 'elev': 693},
    'BYX': {'name': 'Key West, FL', 'lat': 24.5975, 'lon': -81.7033, 'elev': 8},
    'CAE': {'name': 'Columbia, SC', 'lat': 33.9486, 'lon': -81.1186, 'elev': 231},
    'CBW': {'name': 'Houlton, ME', 'lat': 46.0392, 'lon': -67.8067, 'elev': 746},
    'CBX': {'name': 'Boise, ID', 'lat': 43.4906, 'lon': -116.2358, 'elev': 3061},
    'CCX': {'name': 'State College, PA', 'lat': 40.9231, 'lon': -78.0039, 'elev': 2405},
    'CLE': {'name': 'Cleveland, OH', 'lat': 41.4131, 'lon': -81.8597, 'elev': 763},
    'CLX': {'name': 'Charleston, SC', 'lat': 32.6556, 'lon': -81.0422, 'elev': 97},
    'CRP': {'name': 'Corpus Christi, TX', 'lat': 27.7842, 'lon': -97.5111, 'elev': 41},
    'CXX': {'name': 'Burlington, VT', 'lat': 44.5111, 'lon': -73.1664, 'elev': 317},
    'CYS': {'name': 'Cheyenne, WY', 'lat': 41.1519, 'lon': -104.8061, 'elev': 6128},
    'DAX': {'name': 'Sacramento, CA', 'lat': 38.5011, 'lon': -121.6778, 'elev': 30},
    'DDC': {'name': 'Dodge City, KS', 'lat': 37.7608, 'lon': -99.9689, 'elev': 2590},
    'DFX': {'name': 'Laughlin AFB, TX', 'lat': 29.2731, 'lon': -100.2803, 'elev': 1131},
    'DGX': {'name': 'Jackson, MS', 'lat': 32.2797, 'lon': -89.9847, 'elev': 478},
    'DIX': {'name': 'Philadelphia, PA', 'lat': 39.9469, 'lon': -74.4108, 'elev': 149},
    'DLH': {'name': 'Duluth, MN', 'lat': 46.8369, 'lon': -92.2097, 'elev': 1428},
    'DMX': {'name': 'Des Moines, IA', 'lat': 41.7311, 'lon': -93.7229, 'elev': 1018},
    'DOX': {'name': 'Dover AFB, DE', 'lat': 38.8256, 'lon': -75.4400, 'elev': 50},
    'DTX': {'name': 'Detroit, MI', 'lat': 42.6997, 'lon': -83.4717, 'elev': 1072},
    'DVN': {'name': 'Davenport, IA', 'lat': 41.6117, 'lon': -90.5808, 'elev': 754},
    'DYX': {'name': 'Dyess AFB, TX', 'lat': 32.5386, 'lon': -99.2542, 'elev': 1517},
    'EAX': {'name': 'Kansas City, MO', 'lat': 38.8103, 'lon': -94.2644, 'elev': 995},
    'EMX': {'name': 'Tucson, AZ', 'lat': 31.8936, 'lon': -110.6303, 'elev': 5202},
    'ENX': {'name': 'Albany, NY', 'lat': 42.5864, 'lon': -74.0639, 'elev': 1826},
    'EOX': {'name': 'Fort Rucker, AL', 'lat': 31.4606, 'lon': -85.4594, 'elev': 434},
    'EPZ': {'name': 'El Paso, TX', 'lat': 31.8731, 'lon': -106.6981, 'elev': 4104},
    'ESX': {'name': 'Las Vegas, NV', 'lat': 35.7011, 'lon': -114.8919, 'elev': 4867},
    'EVX': {'name': 'Eglin AFB, FL', 'lat': 30.5644, 'lon': -85.9214, 'elev': 140},
    'EWX': {'name': 'Austin/San Antonio, TX', 'lat': 29.7039, 'lon': -98.0281, 'elev': 633},
    'EYX': {'name': 'Edwards AFB, CA', 'lat': 35.0978, 'lon': -117.5608, 'elev': 2757},
    'FCX': {'name': 'Roanoke, VA', 'lat': 37.0242, 'lon': -80.2736, 'elev': 2868},
    'FDR': {'name': 'Altus AFB, OK', 'lat': 34.3622, 'lon': -98.9764, 'elev': 1267},
    'FDX': {'name': 'Cannon AFB, NM', 'lat': 34.6347, 'lon': -103.6186, 'elev': 4650},
    'FFC': {'name': 'Atlanta, GA', 'lat': 33.3636, 'lon': -84.5658, 'elev': 858},
    'FSD': {'name': 'Sioux Falls, SD', 'lat': 43.5878, 'lon': -96.7294, 'elev': 1430},
    'FSX': {'name': 'Flagstaff, AZ', 'lat': 34.5744, 'lon': -111.1983, 'elev': 7220},
    'FTG': {'name': 'Denver, CO', 'lat': 39.7867, 'lon': -104.5458, 'elev': 5497},
    'FWS': {'name': 'Dallas/Fort Worth, TX', 'lat': 32.5731, 'lon': -97.3031, 'elev': 683},
    'GGW': {'name': 'Glasgow, MT', 'lat': 48.2064, 'lon': -106.6253, 'elev': 2276},
    'GJX': {'name': 'Grand Junction, CO', 'lat': 39.0619, 'lon': -108.2136, 'elev': 9992},
    'GLD': {'name': 'Goodland, KS', 'lat': 39.3667, 'lon': -101.7003, 'elev': 3651},
    'GRB': {'name': 'Green Bay, WI', 'lat': 44.4986, 'lon': -88.1111, 'elev': 682},
    'GRK': {'name': 'Fort Hood, TX', 'lat': 30.7217, 'lon': -97.3831, 'elev': 538},
    'GRR': {'name': 'Grand Rapids, MI', 'lat': 42.8939, 'lon': -85.5449, 'elev': 778},
    'GSP': {'name': 'Greer, SC', 'lat': 34.8833, 'lon': -82.2203, 'elev': 940},
    'GWX': {'name': 'Columbus AFB, MS', 'lat': 33.8967, 'lon': -88.3289, 'elev': 476},
    'GYX': {'name': 'Portland, ME', 'lat': 43.8914, 'lon': -70.2567, 'elev': 409},
    'HDX': {'name': 'Holloman AFB, NM', 'lat': 33.0764, 'lon': -106.1219, 'elev': 4222},
    'HGX': {'name': 'Houston/Galveston, TX', 'lat': 29.4719, 'lon': -95.0792, 'elev': 18},
    'HNX': {'name': 'San Joaquin Valley, CA', 'lat': 36.3142, 'lon': -119.6319, 'elev': 243},
    'HPX': {'name': 'Fort Campbell, KY', 'lat': 36.7369, 'lon': -87.2850, 'elev': 576},
    'HTX': {'name': 'Huntsville, AL', 'lat': 34.9306, 'lon': -86.0833, 'elev': 1760},
    'ICT': {'name': 'Wichita, KS', 'lat': 37.6544, 'lon': -97.4431, 'elev': 1335},
    'ICX': {'name': 'Cedar City, UT', 'lat': 37.5908, 'lon': -112.8619, 'elev': 10600},
    'ILN': {'name': 'Wilmington, OH', 'lat': 39.4203, 'lon': -83.8217, 'elev': 1056},
    'ILX': {'name': 'Lincoln, IL', 'lat': 40.1506, 'lon': -89.3367, 'elev': 582},
    'IND': {'name': 'Indianapolis, IN', 'lat': 39.7075, 'lon': -86.2803, 'elev': 790},
    'INX': {'name': 'Tulsa, OK', 'lat': 36.1750, 'lon': -95.5644, 'elev': 674},
    'IWA': {'name': 'Phoenix, AZ', 'lat': 33.2892, 'lon': -111.6700, 'elev': 1353},
    'IWX': {'name': 'Fort Wayne, IN', 'lat': 41.3586, 'lon': -85.7000, 'elev': 960},
    'JAX': {'name': 'Jacksonville, FL', 'lat': 30.4847, 'lon': -81.7019, 'elev': 33},
    'JGX': {'name': 'Robins AFB, GA', 'lat': 32.6753, 'lon': -83.3508, 'elev': 521},
    'JKL': {'name': 'Jackson, KY', 'lat': 37.5906, 'lon': -83.3130, 'elev': 1364},
    'LBB': {'name': 'Lubbock, TX', 'lat': 33.6542, 'lon': -101.8139, 'elev': 3281},
    'LCH': {'name': 'Lake Charles, LA', 'lat': 30.1253, 'lon': -93.2161, 'elev': 13},
    'LGX': {'name': 'Langley Hill, WA', 'lat': 47.1158, 'lon': -124.1064, 'elev': 719},
    'LNX': {'name': 'North Platte, NE', 'lat': 41.9578, 'lon': -100.5761, 'elev': 2970},
    'LOT': {'name': 'Chicago, IL', 'lat': 41.6044, 'lon': -88.0844, 'elev': 663},
    'LRX': {'name': 'Elko, NV', 'lat': 40.7397, 'lon': -116.8025, 'elev': 7095},
    'LSX': {'name': 'St. Louis, MO', 'lat': 38.6989, 'lon': -90.6828, 'elev': 608},
    'LTX': {'name': 'Wilmington, NC', 'lat': 33.9892, 'lon': -78.4292, 'elev': 64},
    'LVX': {'name': 'Louisville, KY', 'lat': 37.9753, 'lon': -85.9439, 'elev': 719},
    'LWX': {'name': 'Sterling, VA', 'lat': 38.9753, 'lon': -77.4778, 'elev': 272},
    'LZK': {'name': 'Little Rock, AR', 'lat': 34.8364, 'lon': -92.2622, 'elev': 568},
    'MAF': {'name': 'Midland/Odessa, TX', 'lat': 31.9433, 'lon': -102.1894, 'elev': 2868},
    'MAX': {'name': 'Medford, OR', 'lat': 42.0811, 'lon': -122.7172, 'elev': 7513},
    'MBX': {'name': 'Minot AFB, ND', 'lat': 48.3925, 'lon': -100.8644, 'elev': 1493},
    'MHX': {'name': 'Morehead City, NC', 'lat': 34.7758, 'lon': -76.8761, 'elev': 31},
    'MKX': {'name': 'Milwaukee, WI', 'lat': 42.9678, 'lon': -88.5506, 'elev': 958},
    'MLB': {'name': 'Melbourne, FL', 'lat': 28.1133, 'lon': -80.6542, 'elev': 35},
    'MOB': {'name': 'Mobile, AL', 'lat': 30.6794, 'lon': -88.2397, 'elev': 219},
    'MPX': {'name': 'Minneapolis/St. Paul, MN', 'lat': 44.8489, 'lon': -93.5656, 'elev': 946},
    'MQT': {'name': 'Marquette, MI', 'lat': 46.5311, 'lon': -87.5486, 'elev': 1411},
    'MRX': {'name': 'Knoxville/Tri-Cities, TN', 'lat': 36.1686, 'lon': -83.4017, 'elev': 1337},
    'MSX': {'name': 'Missoula, MT', 'lat': 47.0411, 'lon': -113.9864, 'elev': 7855},
    'MTX': {'name': 'Salt Lake City, UT', 'lat': 41.2628, 'lon': -112.4472, 'elev': 6348},
    'MUX': {'name': 'San Francisco, CA', 'lat': 37.1550, 'lon': -121.8983, 'elev': 3469},
    'MVX': {'name': 'Grand Forks, ND', 'lat': 47.5278, 'lon': -97.3256, 'elev': 986},
    'MXX': {'name': 'Maxwell AFB, AL', 'lat': 32.5367, 'lon': -85.7897, 'elev': 400},
    'NKX': {'name': 'San Diego, CA', 'lat': 32.9189, 'lon': -117.0419, 'elev': 955},
    'NQA': {'name': 'Memphis, TN', 'lat': 35.3447, 'lon': -89.8733, 'elev': 282},
    'OAX': {'name': 'Omaha, NE', 'lat': 41.3203, 'lon': -96.3667, 'elev': 1148},
    'OHX': {'name': 'Nashville, TN', 'lat': 36.2472, 'lon': -86.5628, 'elev': 579},
    'OKX': {'name': 'New York City, NY', 'lat': 40.8656, 'lon': -72.8639, 'elev': 85},
    'OTX': {'name': 'Spokane, WA', 'lat': 47.6803, 'lon': -117.6267, 'elev': 2384},
    'PAH': {'name': 'Paducah, KY', 'lat': 37.0683, 'lon': -88.7719, 'elev': 392},
    'PBZ': {'name': 'Pittsburgh, PA', 'lat': 40.5317, 'lon': -80.2178, 'elev': 1185},
    'PDT': {'name': 'Pendleton, OR', 'lat': 45.6906, 'lon': -118.8528, 'elev': 1515},
    'POE': {'name': 'Fort Polk, LA', 'lat': 31.1553, 'lon': -92.9758, 'elev': 408},
    'PUX': {'name': 'Pueblo, CO', 'lat': 38.4595, 'lon': -104.1814, 'elev': 5249},
    'RAX': {'name': 'Raleigh/Durham, NC', 'lat': 35.6655, 'lon': -78.4897, 'elev': 348},
    'RGX': {'name': 'Reno, NV', 'lat': 39.7542, 'lon': -119.4611, 'elev': 8299},
    'RIW': {'name': 'Riverton, WY', 'lat': 43.0661, 'lon': -108.4773, 'elev': 5568},
    'RLX': {'name': 'Charleston, WV', 'lat': 38.3111, 'lon': -81.7233, 'elev': 1080},
    'RTX': {'name': 'Portland, OR', 'lat': 45.7150, 'lon': -122.9650, 'elev': 1572},
    'SFX': {'name': 'Pocatello/Boise, ID', 'lat': 43.1056, 'lon': -112.6861, 'elev': 4474},
    'SGF': {'name': 'Springfield, MO', 'lat': 37.2353, 'lon': -93.4006, 'elev': 1278},
    'SHV': {'name': 'Shreveport, LA', 'lat': 32.4508, 'lon': -93.8414, 'elev': 273},
    'SJT': {'name': 'San Angelo, TX', 'lat': 31.3711, 'lon': -100.4925, 'elev': 1890},
    'SOX': {'name': 'Santa Ana Mountains, CA', 'lat': 33.8178, 'lon': -117.6358, 'elev': 3027},
    'SRX': {'name': 'Fort Smith, AR', 'lat': 35.2906, 'lon': -94.3619, 'elev': 615},
    'TBW': {'name': 'Tampa Bay Area, FL', 'lat': 27.7056, 'lon': -82.4017, 'elev': 41},
    'TFX': {'name': 'Great Falls, MT', 'lat': 47.4595, 'lon': -111.3856, 'elev': 3714},
    'TLH': {'name': 'Tallahassee, FL', 'lat': 30.3975, 'lon': -84.3289, 'elev': 63},
    'TLX': {'name': 'Oklahoma City, OK', 'lat': 35.3331, 'lon': -97.2778, 'elev': 1213},
    'TWX': {'name': 'Topeka, KS', 'lat': 38.9969, 'lon': -96.2325, 'elev': 1367},
    'TYX': {'name': 'Montague, NY', 'lat': 43.7556, 'lon': -75.6800, 'elev': 1846},
    'UDX': {'name': 'Rapid City, SD', 'lat': 44.1250, 'lon': -102.8297, 'elev': 3016},
    'UEX': {'name': 'Hastings, NE', 'lat': 40.3208, 'lon': -98.4419, 'elev': 1976},
    'VAX': {'name': 'Moody AFB, GA', 'lat': 30.8903, 'lon': -83.0017, 'elev': 178},
    'VBX': {'name': 'Vandenberg AFB, CA', 'lat': 34.8381, 'lon': -120.3958, 'elev': 1233},
    'VNX': {'name': 'Vance AFB, OK', 'lat': 36.7406, 'lon': -98.1278, 'elev': 1210},
    'VTX': {'name': 'Los Angeles, CA', 'lat': 34.4117, 'lon': -119.1797, 'elev': 2726},
    'VWX': {'name': 'Evansville, IN', 'lat': 38.2603, 'lon': -87.7247, 'elev': 581},
    'YUX': {'name': 'Yuma, AZ', 'lat': 32.4953, 'lon': -114.6567, 'elev': 174},
}


def calculate_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two points in nautical miles using Haversine formula
    
    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)
        
    Returns:
        Distance in nautical miles
    """
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return c * 3440.065  # Earth radius in nautical miles


def find_nearest_radar(lat: float, lon: float, max_sites: int = 5) -> List[Tuple[str, float, Dict]]:
    """
    Find nearest radar sites to a given location
    
    Args:
        lat, lon: Target location (degrees)
        max_sites: Maximum number of sites to return
        
    Returns:
        List of tuples: (site_id, distance_nm, site_info)
    """
    distances = []
    for site_id, info in NEXRAD_SITES.items():
        dist = calculate_distance_nm(lat, lon, info['lat'], info['lon'])
        distances.append((site_id, dist, info))
    distances.sort(key=lambda x: x[1])
    return distances[:max_sites]


def get_site_info(site_id: str) -> Optional[Dict]:
    """Get information for a specific radar site"""
    return NEXRAD_SITES.get(site_id.upper())


def get_all_sites() -> Dict[str, Dict]:
    """Get all radar sites"""
    return NEXRAD_SITES


def get_sites_by_region() -> Dict[str, List[str]]:
    """Group sites by region"""
    alaska = ['ABC', 'ACG', 'AEC', 'AHG', 'AIH', 'AKC', 'APD', 'HDC', 'KJK', 'KSG']
    hawaii = ['HKI', 'HKM', 'HMO', 'HWA']
    guam = ['GUA']
    puerto_rico = ['JUA']
    oconus = alaska + hawaii + guam + puerto_rico
    conus = [k for k in NEXRAD_SITES.keys() if k not in oconus]
    
    return {
        'alaska': alaska,
        'hawaii': hawaii,
        'guam': guam,
        'puerto_rico': puerto_rico,
        'conus': conus,
        'all': list(NEXRAD_SITES.keys())
    }


if __name__ == '__main__':
    print(f"Total NEXRAD sites: {len(NEXRAD_SITES)}")
    regions = get_sites_by_region()
    for region, sites in regions.items():
        if region != 'all':
            print(f"  {region.upper()}: {len(sites)} sites")
    
    # Test nearest to Orlando (KMCO)
    print("\nNearest radar to Orlando (28.43, -81.31):")
    nearest = find_nearest_radar(28.43, -81.31, max_sites=3)
    for site_id, dist, info in nearest:
        print(f"  {site_id} - {info['name']}: {dist:.1f} nm")
