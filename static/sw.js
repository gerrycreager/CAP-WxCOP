/**
 * CAP WxCOP Service Worker v2
 * Network-first for all HTML — never cache pages.
 * Cache-first only for icons (static, never change).
 * Everything else: network only.
 */

const CACHE_NAME = 'cap-wxcop-v3';

// Only cache truly static assets — icons don't change
const PRECACHE = [
    '/CAP_WxCOP/static/icons/icon-192x192.png',
    '/CAP_WxCOP/static/icons/icon-512x512.png',
];

// Never cache these — always network
const NETWORK_ONLY = [
    /\/api\//,
    /\/LDM\//,
    /mosaic/,
    /mrms_tiles/,
    /tstm_tiles/,
    /hrrr_winds/,
    /geojson/,
    /unpkg\.com/,
    /cartodb/,
    /openstreetmap/,
    /\.html$/,
    /\.json$/,
];

self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(function(cache) { return cache.addAll(PRECACHE); })
            .then(function() { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(
                keys.filter(function(k) { return k !== CACHE_NAME; })
                    .map(function(k) { return caches.delete(k); })
            );
        }).then(function() { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function(event) {
    var url = event.request.url;

    // Network only for everything except icons
    for (var i = 0; i < NETWORK_ONLY.length; i++) {
        if (NETWORK_ONLY[i].test(url)) {
            event.respondWith(fetch(event.request));
            return;
        }
    }

    // Navigation (HTML pages) — always network first, no fallback cache
    if (event.request.mode === 'navigate') {
        event.respondWith(fetch(event.request));
        return;
    }

    // Icons — cache first (they never change)
    event.respondWith(
        caches.match(event.request).then(function(cached) {
            return cached || fetch(event.request);
        })
    );
});
