/**
 * CAP Winds Radar Animation Controller
 * Handles radar data loading, animation, and map interaction
 */

// Global state
let map, radarOverlay, radarMarker;
let frames = [];
let currentFrame = 0;
let isPlaying = false;
let playInterval;
let currentSite = null;
let currentProduct = null;

/**
 * Initialize the map
 */
function initMap() {
    map = L.map('map').setView([39.8, -98.6], 4); // Center US
    
    // Add base map
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 10
    }).addTo(map);
    
    loadRadarSites();
}

/**
 * Load all radar sites into dropdown
 */
async function loadRadarSites() {
    try {
        const response = await fetch('/cap_winds_app/radar/api/sites');
        const data = await response.json();
        
        const select = document.getElementById('siteSelect');
        Object.keys(data.sites).sort().forEach(site => {
            const opt = document.createElement('option');
            opt.value = site;
            opt.textContent = `${site} - ${data.sites[site].name}`;
            select.appendChild(opt);
        });
        
        updateStatus('Ready. ' + data.count + ' radar sites available.', 'success');
    } catch (err) {
        updateStatus('Error loading sites: ' + err.message, 'error');
    }
}

/**
 * Find nearest radar to an airport
 */
async function findNearestRadar() {
    const airport = document.getElementById('airportInput').value.toUpperCase().trim();
    if (!airport) {
        updateStatus('Please enter an airport identifier (e.g., KMCO)', 'error');
        return;
    }
    
    updateStatus('Finding nearest radar to ' + airport + '...', 'loading');
    
    try {
        const response = await fetch(`/cap_winds_app/radar/api/nearest/${airport}`);
        const data = await response.json();
        
        if (data.error) {
            updateStatus('Error: ' + data.error, 'error');
            return;
        }
        
        if (data.nearest && data.nearest.length > 0) {
            const nearest = data.nearest[0];
            document.getElementById('siteSelect').value = nearest.site_id;
            updateStatus(`Nearest: ${nearest.site_id} (${nearest.distance_nm} nm away)`, 'success');
            map.setView([nearest.lat, nearest.lon], 7);
            
            // Add marker for radar site
            if (radarMarker) {
                map.removeLayer(radarMarker);
            }
            radarMarker = L.marker([nearest.lat, nearest.lon])
                .bindPopup(`<b>${nearest.site_id}</b><br>${nearest.name}`)
                .addTo(map);
        }
    } catch (error) {
        updateStatus('Error: ' + error.message, 'error');
    }
}

/**
 * Load radar data
 */
async function loadRadar() {
    currentSite = document.getElementById('siteSelect').value;
    currentProduct = document.getElementById('productSelect').value;
    const hours = document.getElementById('hoursSelect').value;
    
    if (!currentSite) {
        updateStatus('Please select a radar site', 'error');
        return;
    }
    
    // Stop any current animation
    if (isPlaying) {
        togglePlay();
    }
    
    // Clear existing overlay
    if (radarOverlay) {
        map.removeLayer(radarOverlay);
        radarOverlay = null;
    }
    
    updateStatus(`Loading radar images for ${currentSite} ${currentProduct}...`, 'loading');
    
    try {
        const response = await fetch(
            `/cap_winds_app/radar/api/images/${currentSite}/${currentProduct}?hours=${hours}`
        );
        const data = await response.json();
        
        if (data.error) {
            updateStatus('Error: ' + data.error, 'error');
            return;
        }
        
        frames = data.images;
        
        if (frames.length === 0) {
            updateStatus('No radar data available for this site/product', 'error');
            return;
        }
        
        // Enable controls
        document.getElementById('timeline').max = frames.length - 1;
        document.getElementById('timeline').disabled = false;
        document.getElementById('playBtn').disabled = false;
        document.getElementById('prevBtn').disabled = false;
        document.getElementById('nextBtn').disabled = false;
        
        updateStatus(`Loaded ${frames.length} frames. Ready to animate.`, 'success');
        
        // Show first frame
        showFrame(0);
        
    } catch (error) {
        updateStatus('Error loading radar: ' + error.message, 'error');
    }
}

/**
 * Display a specific frame
 */
function showFrame(index) {
    if (index < 0 || index >= frames.length) return;
    
    currentFrame = index;
    const frame = frames[index];
    
    // Remove old overlay
    if (radarOverlay) {
        map.removeLayer(radarOverlay);
    }
    
    // Check if bounds are available
    if (frame.bounds) {
        const b = frame.bounds;
        radarOverlay = L.imageOverlay(
            frame.url,
            [[b.south, b.west], [b.north, b.east]],
            {
                opacity: 0.7,
                zIndex: 1000
            }
        ).addTo(map);
    } else {
        console.warn('No bounds available for frame', index);
    }
    
    // Update timeline slider
    document.getElementById('timeline').value = index;
    
    // Update time label
    const time = new Date(frame.time);
    document.getElementById('timeLabel').textContent = 
        time.toLocaleTimeString('en-US', { 
            hour: '2-digit', 
            minute: '2-digit',
            timeZone: 'UTC'
        }) + ' UTC';
}

/**
 * Toggle play/pause
 */
function togglePlay() {
    isPlaying = !isPlaying;
    const btn = document.getElementById('playBtn');
    
    if (isPlaying) {
        btn.textContent = '⏸ Pause';
        btn.classList.add('playing');
        playAnimation();
    } else {
        btn.textContent = '▶ Play';
        btn.classList.remove('playing');
        if (playInterval) clearInterval(playInterval);
    }
}

/**
 * Start animation loop
 */
function playAnimation() {
    const speed = parseInt(document.getElementById('speedSelect').value);
    
    playInterval = setInterval(() => {
        if (currentFrame >= frames.length - 1) {
            // Loop back to start
            currentFrame = 0;
        } else {
            currentFrame++;
        }
        showFrame(currentFrame);
    }, speed);
}

/**
 * Previous frame
 */
function prevFrame() {
    if (currentFrame > 0) {
        showFrame(currentFrame - 1);
    }
}

/**
 * Next frame
 */
function nextFrame() {
    if (currentFrame < frames.length - 1) {
        showFrame(currentFrame + 1);
    }
}

/**
 * Update status message
 */
function updateStatus(msg, type = '') {
    const status = document.getElementById('status');
    status.textContent = 'Status: ' + msg;
    
    // Remove all status classes
    status.classList.remove('status-loading', 'status-error', 'status-success');
    
    // Add appropriate class
    if (type) {
        status.classList.add('status-' + type);
    }
}

/**
 * Initialize on page load
 */
document.addEventListener('DOMContentLoaded', function() {
    // Initialize map
    initMap();
    
    // Timeline scrubber
    document.getElementById('timeline').addEventListener('input', (e) => {
        showFrame(parseInt(e.target.value));
    });
    
    // URL parameters support
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('airport')) {
        document.getElementById('airportInput').value = urlParams.get('airport');
        // Auto-load after a short delay
        setTimeout(() => {
            findNearestRadar().then(() => {
                setTimeout(loadRadar, 1000);
            });
        }, 500);
    }
    
    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (frames.length === 0) return;
        
        switch(e.key) {
            case ' ':
            case 'k':
                e.preventDefault();
                togglePlay();
                break;
            case 'ArrowLeft':
            case 'j':
                e.preventDefault();
                prevFrame();
                break;
            case 'ArrowRight':
            case 'l':
                e.preventDefault();
                nextFrame();
                break;
        }
    });
});

