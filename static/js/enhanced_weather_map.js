/**
 * Enhanced Interactive Weather Map
 * Features:
 * - Station labels with military prioritization
 * - Increased default station limit (2500)
 * - Military airfield prioritization and highlighting
 * - Enhanced visual indicators for different airport types
 * - Improved popup information with airport details
 */

class EnhancedWeatherMap {
    constructor(mapContainerId) {
        this.mapContainer = mapContainerId;
        this.map = null;
        this.weatherLayer = null;
        this.tfrsLayer = null;
        this.ndaLayer = null;
        this.stationsLayer = null;
        this.labelsVisible = true;
        this.militaryHighlighted = true;
        this.currentBounds = null;
        
        // Station display limits
        this.defaultLimit = 2500; // Increased from 500
        this.maxStations = 5000;
        
        // Layer groups for better organization
        this.layerGroups = {
            military: L.layerGroup(),
            major: L.layerGroup(),
            regional: L.layerGroup(),
            small: L.layerGroup(),
            tfrs: L.layerGroup(),
            nda: L.layerGroup()
        };
        
        this.initializeMap();
        this.setupControls();
    }
    
    initializeMap() {
        // Initialize Leaflet map
        this.map = L.map(this.mapContainer, {
            center: [39.0, -98.0], // Center on CONUS
            zoom: 5,
            zoomControl: true,
            preferCanvas: true // Better performance for many markers
        });
        
        // Add base map layers
        const baseLayers = {
            "Streets": L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 18
            }),
            "Satellite": L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
                attribution: 'Esri, DigitalGlobe, GeoEye, Earthstar Geographics, CNES/Airbus DS, USDA, USGS, AeroGRID, IGN, and the GIS User Community',
                maxZoom: 18
            }),
            "Terrain": L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenTopoMap contributors',
                maxZoom: 17
            })
        };
        
        // Set default base layer
        baseLayers["Streets"].addTo(this.map);
        
        // Add all layer groups to map
        Object.values(this.layerGroups).forEach(group => {
            group.addTo(this.map);
        });
        
        // Setup layer control
        const overlayLayers = {
            "Military Stations": this.layerGroups.military,
            "Major Airports": this.layerGroups.major,  
            "Regional Airports": this.layerGroups.regional,
            "Small Airports": this.layerGroups.small,
            "Stadium TFRs": this.layerGroups.tfrs,
            "National Defense Airspace": this.layerGroups.nda
        };
        
        L.control.layers(baseLayers, overlayLayers, {
            position: 'topright',
            collapsed: false
        }).addTo(this.map);
        
        // Load initial data
        this.loadWeatherData();
        this.loadTFRData();
        this.loadNDAData();
        
        // Setup map event handlers
        this.map.on('moveend', () => {
            this.loadWeatherData();
        });
        
        this.map.on('zoomend', () => {
            this.updateLabelVisibility();
        });
    }
    
    setupControls() {
        // Add custom control panel
        const controlDiv = L.DomUtil.create('div', 'weather-map-controls');
        controlDiv.innerHTML = `
            <div class="control-panel">
                <h4>Weather COP Controls</h4>
                <label>
                    <input type="checkbox" id="showLabels" ${this.labelsVisible ? 'checked' : ''}> 
                    Show Station Labels
                </label>
                <label>
                    <input type="checkbox" id="highlightMilitary" ${this.militaryHighlighted ? 'checked' : ''}> 
                    Highlight Military
                </label>
                <div class="station-info">
                    <span id="stationCount">Loading stations...</span>
                </div>
                <div class="legend">
                    <h5>Station Types</h5>
                    <div class="legend-item">
                        <span class="legend-color military"></span> Military Airfields
                    </div>
                    <div class="legend-item">
                        <span class="legend-color major"></span> Major Airports
                    </div>
                    <div class="legend-item">
                        <span class="legend-color regional"></span> Regional Airports
                    </div>
                    <div class="legend-item">
                        <span class="legend-color small"></span> Small Airports
                    </div>
                </div>
            </div>
        `;
        
        const control = L.control({ position: 'topleft' });
        control.onAdd = () => controlDiv;
        control.addTo(this.map);
        
        // Setup control event listeners
        this.setupControlHandlers();
    }
    
    setupControlHandlers() {
        // Labels toggle
        document.getElementById('showLabels').addEventListener('change', (e) => {
            this.labelsVisible = e.target.checked;
            this.updateLabelVisibility();
        });
        
        // Military highlighting toggle
        document.getElementById('highlightMilitary').addEventListener('change', (e) => {
            this.militaryHighlighted = e.target.checked;
            this.updateMilitaryHighlighting();
        });
    }
    
    async loadWeatherData() {
        try {
            const bounds = this.map.getBounds();
            const boundsParam = `${bounds.getWest()},${bounds.getSouth()},${bounds.getEast()},${bounds.getNorth()}`;
            
            const response = await fetch(`/api/weather/metar/recent?bounds=${boundsParam}&limit=${this.defaultLimit}`);
            if (!response.ok) throw new Error('Weather data request failed');
            
            const data = await response.json();
            this.displayWeatherData(data);
            
            // Update station count display
            document.getElementById('stationCount').textContent = 
                `${data.count} stations (${data.military_count} military)`;
                
        } catch (error) {
            console.error('Weather data load error:', error);
            document.getElementById('stationCount').textContent = 'Error loading stations';
        }
    }
    
    displayWeatherData(data) {
        // Clear existing weather layers
        Object.values(this.layerGroups).forEach(group => {
            if (['military', 'major', 'regional', 'small'].includes(group.groupName)) {
                group.clearLayers();
            }
        });
        
        data.metars.forEach(metar => {
            const marker = this.createWeatherMarker(metar);
            const layerGroup = this.getLayerGroup(metar);
            marker.addTo(layerGroup);
        });
        
        this.updateLabelVisibility();
    }
    
    createWeatherMarker(metar) {
        const icon = this.createStationIcon(metar);
        const marker = L.marker([metar.latitude, metar.longitude], { icon });
        
        // Enhanced popup with airport information
        const popup = this.createEnhancedPopup(metar);
        marker.bindPopup(popup, { 
            maxWidth: 400,
            className: metar.is_military ? 'military-popup' : 'civilian-popup'
        });
        
        // Add station label if enabled
        if (this.labelsVisible && this.shouldShowLabel(metar)) {
            const label = this.createStationLabel(metar);
            marker.bindTooltip(label, {
                permanent: true,
                direction: 'bottom',
                offset: [0, 10],
                className: `station-label ${metar.label_priority}`
            });
        }
        
        return marker;
    }
    
    createStationIcon(metar) {
        const flightCategoryColors = {
            'VFR': '#00ff00',      // Green
            'MVFR': '#0099ff',     // Blue  
            'IFR': '#ff9900',      // Orange
            'LIFR': '#ff0000',     // Red
            'UNKNOWN': '#808080'    // Gray
        };
        
        const color = flightCategoryColors[metar.flight_category] || '#808080';
        
        // Different shapes for different airport types
        let iconHtml;
        if (metar.is_military) {
            // Star shape for military
            iconHtml = `<div class="military-station-marker" style="background-color: ${color};">
                           <i class="military-star">★</i>
                       </div>`;
        } else if (metar.airport_type === 'large_airport') {
            // Large square
            iconHtml = `<div class="large-station-marker" style="background-color: ${color};"></div>`;
        } else if (metar.airport_type === 'medium_airport') {
            // Medium circle  
            iconHtml = `<div class="medium-station-marker" style="background-color: ${color};"></div>`;
        } else {
            // Small circle
            iconHtml = `<div class="small-station-marker" style="background-color: ${color};"></div>`;
        }
        
        return L.divIcon({
            html: iconHtml,
            className: 'weather-station-icon',
            iconSize: metar.is_military ? [16, 16] : [12, 12],
            iconAnchor: metar.is_military ? [8, 8] : [6, 6]
        });
    }
    
    createEnhancedPopup(metar) {
        const obsTime = new Date(metar.observation_time).toLocaleString();
        const windDir = metar.wind_dir !== null ? metar.wind_dir : 'VRB';
        const windSpeed = metar.wind_speed_kts !== null ? metar.wind_speed_kts : 0;
        const gustInfo = metar.wind_gust_kts ? `G${metar.wind_gust_kts}` : '';
        const visibility = metar.visibility_sm !== null ? metar.visibility_sm : 'N/A';
        const ceiling = this.getCeilingFromSkyConditions(metar.sky_conditions);
        
        return `
            <div class="enhanced-weather-popup">
                <div class="station-header ${metar.is_military ? 'military' : 'civilian'}">
                    <h3>${metar.display_label}</h3>
                    ${metar.airport_name ? `<div class="airport-name">${metar.airport_name}</div>` : ''}
                    ${metar.municipality ? `<div class="municipality">${metar.municipality}</div>` : ''}
                </div>
                
                <div class="weather-details">
                    <div class="flight-category ${metar.flight_category.toLowerCase()}">
                        ${metar.flight_category}
                    </div>
                    
                    <div class="weather-grid">
                        <div class="weather-item">
                            <span class="label">Wind:</span>
                            <span class="value">${windDir}°@${windSpeed}kt ${gustInfo}</span>
                        </div>
                        <div class="weather-item">
                            <span class="label">Visibility:</span>
                            <span class="value">${visibility} SM</span>
                        </div>
                        <div class="weather-item">
                            <span class="label">Ceiling:</span>
                            <span class="value">${ceiling}</span>
                        </div>
                        <div class="weather-item">
                            <span class="label">Temperature:</span>
                            <span class="value">${metar.temp_c}°C</span>
                        </div>
                        <div class="weather-item">
                            <span class="label">Dewpoint:</span>
                            <span class="value">${metar.dewpoint_c}°C</span>
                        </div>
                        <div class="weather-item">
                            <span class="label">Altimeter:</span>
                            <span class="value">${metar.altimeter_hg}" Hg</span>
                        </div>
                    </div>
                    
                    <div class="observation-time">
                        <strong>Observed:</strong> ${obsTime} ${metar.is_speci ? '(SPECI)' : ''}
                    </div>
                    
                    <div class="raw-metar">
                        <details>
                            <summary>Raw METAR</summary>
                            <code>${metar.raw_text}</code>
                        </details>
                    </div>
                </div>
            </div>
        `;
    }
    
    createStationLabel(metar) {
        const labelText = metar.is_military ? 
            `${metar.station_id} (MIL)` : 
            metar.station_id;
        return labelText;
    }
    
    shouldShowLabel(metar) {
        const zoom = this.map.getZoom();
        
        // Always show military labels if highlighted
        if (metar.is_military && this.militaryHighlighted && zoom >= 6) {
            return true;
        }
        
        // Show major airport labels at medium zoom
        if (metar.airport_type === 'large_airport' && zoom >= 7) {
            return true;
        }
        
        // Show all labels at high zoom
        if (zoom >= 9) {
            return true;
        }
        
        return false;
    }
    
    getLayerGroup(metar) {
        if (metar.is_military) {
            if (!this.layerGroups.military.groupName) {
                this.layerGroups.military.groupName = 'military';
            }
            return this.layerGroups.military;
        } else if (metar.airport_type === 'large_airport') {
            if (!this.layerGroups.major.groupName) {
                this.layerGroups.major.groupName = 'major';
            }
            return this.layerGroups.major;
        } else if (metar.airport_type === 'medium_airport') {
            if (!this.layerGroups.regional.groupName) {
                this.layerGroups.regional.groupName = 'regional';
            }
            return this.layerGroups.regional;
        } else {
            if (!this.layerGroups.small.groupName) {
                this.layerGroups.small.groupName = 'small';
            }
            return this.layerGroups.small;
        }
    }
    
    updateLabelVisibility() {
        // Refresh all markers to update label visibility
        Object.values(this.layerGroups).forEach(group => {
            if (['military', 'major', 'regional', 'small'].includes(group.groupName)) {
                group.eachLayer(marker => {
                    if (marker.getTooltip) {
                        const tooltip = marker.getTooltip();
                        if (tooltip) {
                            marker.unbindTooltip();
                        }
                        
                        // Re-add tooltip based on current settings
                        if (this.labelsVisible) {
                            const metar = marker.metarData; // Need to store this when creating marker
                            if (metar && this.shouldShowLabel(metar)) {
                                const label = this.createStationLabel(metar);
                                marker.bindTooltip(label, {
                                    permanent: true,
                                    direction: 'bottom',
                                    offset: [0, 10],
                                    className: `station-label ${metar.label_priority}`
                                });
                            }
                        }
                    }
                });
            }
        });
    }
    
    updateMilitaryHighlighting() {
        // Update military marker styling
        if (this.layerGroups.military) {
            this.layerGroups.military.eachLayer(marker => {
                const element = marker.getElement();
                if (element) {
                    if (this.militaryHighlighted) {
                        element.classList.add('military-highlighted');
                    } else {
                        element.classList.remove('military-highlighted');
                    }
                }
            });
        }
        
        this.updateLabelVisibility();
    }
    
    getCeilingFromSkyConditions(skyConditions) {
        if (!skyConditions || skyConditions.length === 0) {
            return 'Clear';
        }
        
        // Find lowest broken or overcast layer
        let lowestCeiling = null;
        for (const condition of skyConditions) {
            if (condition.cover === 'BKN' || condition.cover === 'OVC') {
                if (!lowestCeiling || condition.height_ft < lowestCeiling) {
                    lowestCeiling = condition.height_ft;
                }
            }
        }
        
        return lowestCeiling ? `${lowestCeiling} ft` : 'None';
    }
    
    async loadTFRData() {
        try {
            const response = await fetch('/api/weather/stadium-tfrs');
            if (!response.ok) throw new Error('TFR data request failed');
            
            const data = await response.json();
            this.displayTFRData(data);
            
        } catch (error) {
            console.error('TFR load error:', error);
        }
    }
    
    displayTFRData(data) {
        this.layerGroups.tfrs.clearLayers();
        
        data.stadium_tfrs.forEach(tfr => {
            if (tfr.latitude && tfr.longitude && tfr.tfr_area) {
                const geoJson = L.geoJSON(tfr.tfr_area, {
                    style: {
                        color: '#ff6b35',
                        weight: 2,
                        opacity: 0.8,
                        fillColor: '#ff6b35',
                        fillOpacity: 0.2
                    }
                }).bindPopup(`
                    <div class="tfr-popup">
                        <h4>Stadium TFR</h4>
                        <p><strong>${tfr.name}</strong></p>
                        <p>${tfr.city}, ${tfr.state}</p>
                        <p>Status: ${tfr.status}</p>
                        <p><em>Potential TFR area during events</em></p>
                    </div>
                `);
                
                geoJson.addTo(this.layerGroups.tfrs);
            }
        });
    }
    
    async loadNDAData() {
        try {
            const response = await fetch('/api/weather/nda/active');
            if (!response.ok) throw new Error('NDA data request failed');
            
            const data = await response.json();
            this.displayNDAData(data);
            
        } catch (error) {
            console.error('NDA load error:', error);
        }
    }
    
    displayNDAData(data) {
        this.layerGroups.nda.clearLayers();
        
        data.nda_areas.forEach(nda => {
            if (nda.geometry) {
                const geoJson = L.geoJSON(nda.geometry, {
                    style: {
                        color: '#dc2626',
                        weight: 3,
                        opacity: 0.9,
                        fillColor: '#dc2626',
                        fillOpacity: 0.3
                    }
                }).bindPopup(`
                    <div class="nda-popup">
                        <h4>National Defense Airspace</h4>
                        <p><strong>${nda.name}</strong></p>
                        <p>${nda.city}, ${nda.state}</p>
                        <p>Type: ${nda.type_code} (${nda.local_type})</p>
                        <p>Hours: ${nda.work_hours_remark}</p>
                        <p><em>Permanent military restriction</em></p>
                    </div>
                `);
                
                geoJson.addTo(this.layerGroups.nda);
            }
        });
    }
}

// CSS Styles for enhanced map
const mapStyles = `
<style>
.weather-map-controls {
    background: white;
    padding: 10px;
    border-radius: 8px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    max-width: 250px;
}

.control-panel h4 {
    margin: 0 0 10px 0;
    font-size: 14px;
    font-weight: bold;
}

.control-panel label {
    display: block;
    margin: 8px 0;
    font-size: 12px;
    cursor: pointer;
}

.control-panel input[type="checkbox"] {
    margin-right: 6px;
}

.station-info {
    margin: 10px 0;
    padding: 8px;
    background: #f5f5f5;
    border-radius: 4px;
    font-size: 11px;
}

.legend {
    margin-top: 10px;
}

.legend h5 {
    margin: 0 0 8px 0;
    font-size: 12px;
    font-weight: bold;
}

.legend-item {
    display: flex;
    align-items: center;
    margin: 4px 0;
    font-size: 11px;
}

.legend-color {
    width: 12px;
    height: 12px;
    margin-right: 6px;
    border-radius: 2px;
}

.legend-color.military {
    background: #dc2626;
    position: relative;
}

.legend-color.military::after {
    content: "★";
    position: absolute;
    color: white;
    font-size: 8px;
    left: 2px;
    top: -1px;
}

.legend-color.major {
    background: #0066cc;
}

.legend-color.regional {
    background: #00cc66;
}

.legend-color.small {
    background: #999999;
}

/* Station markers */
.military-station-marker {
    width: 16px;
    height: 16px;
    border-radius: 2px;
    border: 2px solid #dc2626;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
}

.military-star {
    color: white;
    font-size: 10px;
    text-shadow: 1px 1px 1px rgba(0,0,0,0.5);
}

.large-station-marker {
    width: 14px;
    height: 14px;
    border-radius: 2px;
    border: 1px solid #333;
}

.medium-station-marker {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    border: 1px solid #333;
}

.small-station-marker {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    border: 1px solid #333;
}

.military-highlighted {
    box-shadow: 0 0 6px 2px #dc2626 !important;
    z-index: 1000 !important;
}

/* Station labels */
.station-label {
    background: rgba(255,255,255,0.9);
    border: 1px solid #ccc;
    border-radius: 3px;
    padding: 2px 4px;
    font-size: 10px;
    font-weight: bold;
    box-shadow: 1px 1px 2px rgba(0,0,0,0.2);
}

.station-label.military {
    background: rgba(220, 38, 38, 0.9);
    color: white;
    border-color: #dc2626;
}

/* Popups */
.enhanced-weather-popup {
    min-width: 300px;
}

.station-header {
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 2px solid #eee;
}

.station-header.military {
    border-bottom-color: #dc2626;
}

.station-header h3 {
    margin: 0;
    font-size: 16px;
}

.airport-name {
    font-size: 12px;
    font-weight: bold;
    color: #666;
}

.municipality {
    font-size: 11px;
    color: #888;
}

.flight-category {
    display: inline-block;
    padding: 4px 8px;
    border-radius: 4px;
    font-weight: bold;
    font-size: 12px;
    margin-bottom: 10px;
}

.flight-category.vfr { background: #00ff00; color: #000; }
.flight-category.mvfr { background: #0099ff; color: #fff; }
.flight-category.ifr { background: #ff9900; color: #fff; }
.flight-category.lifr { background: #ff0000; color: #fff; }
.flight-category.unknown { background: #808080; color: #fff; }

.weather-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin: 10px 0;
}

.weather-item {
    display: flex;
    justify-content: space-between;
}

.weather-item .label {
    font-weight: bold;
    font-size: 11px;
}

.weather-item .value {
    font-size: 11px;
}

.observation-time {
    margin-top: 10px;
    font-size: 11px;
    color: #666;
}

.raw-metar {
    margin-top: 10px;
}

.raw-metar code {
    font-size: 10px;
    background: #f5f5f5;
    padding: 4px;
    border-radius: 2px;
    display: block;
    margin-top: 4px;
}

.tfr-popup h4, .nda-popup h4 {
    margin: 0 0 10px 0;
    color: #dc2626;
}

.military-popup .leaflet-popup-content-wrapper {
    border: 2px solid #dc2626;
}
</style>
`;

// Initialize the enhanced weather map
document.addEventListener('DOMContentLoaded', function() {
    // Add styles to head
    document.head.insertAdjacentHTML('beforeend', mapStyles);
    
    // Initialize the map
    const weatherMap = new EnhancedWeatherMap('map');
    window.weatherMap = weatherMap; // Make available globally for debugging
});

