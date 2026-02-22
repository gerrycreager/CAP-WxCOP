#!/bin/bash
# Automatic Phase 2C Deployment
# Adds AIRMET/SIGMET overlay to wind_forecast_map.html using sed

set -e  # Exit on error

TARGET="/var/www/cap_winds_app/templates/wind_forecast_map.html"
BACKUP="/var/www/cap_winds_app/templates/wind_forecast_map.html.backup.$(date +%Y%m%d_%H%M%S)"

echo "=========================================="
echo "  PHASE 2C AUTO-DEPLOYMENT"
echo "  AIRMET/SIGMET Overlay Integration"
echo "=========================================="
echo ""

# Verify file exists
if [ ! -f "$TARGET" ]; then
    echo "❌ Error: $TARGET not found"
    exit 1
fi

# Backup
echo "📦 Backing up to: $BACKUP"
sudo cp "$TARGET" "$BACKUP"

# Step 1: Add CSS before @media section
echo "✏️  Adding CSS styles..."
sudo sed -i '/@media (max-width: 768px) {/i\
        .hazard-toggle {\
            display: flex;\
            align-items: center;\
            gap: 0.5rem;\
            padding: 0.5rem 1rem;\
            background: white;\
            border: 2px solid #667eea;\
            border-radius: 4px;\
            cursor: pointer;\
            transition: all 0.3s ease;\
        }\
        \
        .hazard-toggle:hover {\
            background: #f0f0f0;\
        }\
        \
        .hazard-toggle input[type="checkbox"] {\
            width: 18px;\
            height: 18px;\
            cursor: pointer;\
        }\
        \
        .hazard-legend {\
            background: white;\
            padding: 10px;\
            border-radius: 4px;\
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);\
            font-size: 0.85rem;\
            max-width: 250px;\
        }\
        \
        .hazard-legend h4 {\
            margin: 0 0 8px 0;\
            font-size: 0.95rem;\
            border-bottom: 2px solid #667eea;\
            padding-bottom: 5px;\
        }\
        \
        .hazard-legend-item {\
            display: flex;\
            align-items: center;\
            margin: 5px 0;\
        }\
        \
        .hazard-legend-color {\
            width: 20px;\
            height: 15px;\
            display: inline-block;\
            margin-right: 8px;\
            border: 1px solid #666;\
            opacity: 0.6;\
        }\
' "$TARGET"

# Step 2: Add checkbox toggle after "Show Airport Labels"
echo "✏️  Adding hazards toggle checkbox..."
sudo sed -i '/<label for="showLabels">Show Airport Labels<\/label>/a\
        \
        <label class="hazard-toggle">\
            <input type="checkbox" id="showHazards" onchange="toggleHazards()">\
            <span>☁️ Show AIRMETs\/SIGMETs<\/span>\
        <\/label>' "$TARGET"

# Step 3: Add hazard legend after windLegend.addTo(map)
echo "✏️  Adding hazard legend..."
sudo sed -i '/windLegend\.addTo(map);/a\
\
            \/\/ Weather Hazards Legend\
            const hazardLegend = L.control({position: '\''topright'\''});\
            hazardLegend.onAdd = function(map) {\
                const div = L.DomUtil.create('\''div'\'', '\''hazard-legend'\'');\
                div.style.display = '\''none'\'';\
                div.id = '\''hazardLegend'\'';\
                div.innerHTML = `\
                    <h4>Weather Hazards<\/h4>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #FFA500;"><\/div>\
                        <span><strong>TURB<\/strong> - Turbulence<\/span>\
                    <\/div>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #87CEEB;"><\/div>\
                        <span><strong>ICE<\/strong> - Icing<\/span>\
                    <\/div>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #808080;"><\/div>\
                        <span><strong>IFR<\/strong> - IFR\/Vis<\/span>\
                    <\/div>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #696969;"><\/div>\
                        <span><strong>MTN<\/strong> - Mtn Obsc<\/span>\
                    <\/div>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #FFD700;"><\/div>\
                        <span><strong>WIND<\/strong> - Sfc Wind<\/span>\
                    <\/div>\
                    <div class="hazard-legend-item">\
                        <div class="hazard-legend-color" style="background: #FF0000;"><\/div>\
                        <span><strong>CONV<\/strong> - Convective<\/span>\
                    <\/div>\
                    <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #ddd; font-size: 0.75rem; color: #666;">\
                        Click polygon for details\
                    <\/div>\
                `;\
                return div;\
            };\
            hazardLegend.addTo(map);' "$TARGET"

# Step 4: Add JavaScript functions before </script> closing tag
echo "✏️  Adding JavaScript functions..."
sudo sed -i '/<\/script>/i\
\
        \/\/ ===== AIRMET\/SIGMET OVERLAY FUNCTIONS =====\
        let hazardsLayer = null;\
\
        function toggleHazards() {\
            const checkbox = document.getElementById('\''showHazards'\'');\
            const legend = document.getElementById('\''hazardLegend'\'');\
            if (checkbox.checked) {\
                legend.style.display = '\''block'\'';\
                loadWeatherHazards();\
            } else {\
                legend.style.display = '\''none'\'';\
                if (hazardsLayer) {\
                    map.removeLayer(hazardsLayer);\
                    hazardsLayer = null;\
                }\
            }\
        }\
\
        async function loadWeatherHazards() {\
            try {\
                const response = await fetch('\''/cap_winds_app/api/hazards/weather-hazards'\'');\
                const geojson = await response.json();\
                if (!geojson || !geojson.features) return;\
                if (hazardsLayer) map.removeLayer(hazardsLayer);\
                hazardsLayer = L.geoJSON(geojson, {\
                    style: function(feature) {\
                        return {\
                            fillColor: feature.properties.color,\
                            weight: 2,\
                            opacity: 1,\
                            color: feature.properties.color,\
                            fillOpacity: 0.3\
                        };\
                    },\
                    onEachFeature: function(feature, layer) {\
                        const props = feature.properties;\
                        const validFrom = new Date(props.valid_from);\
                        const validUntil = new Date(props.valid_until);\
                        const formatTime = (date) => {\
                            const hours = String(date.getUTCHours()).padStart(2, '\''0'\'');\
                            const minutes = String(date.getUTCMinutes()).padStart(2, '\''0'\'');\
                            const day = String(date.getUTCDate()).padStart(2, '\''0'\'');\
                            return `${day}\/${hours}${minutes}Z`;\
                        };\
                        const phenLabels = {\
                            '\''TURB'\'': '\''⚠️ Turbulence'\'',\
                            '\''ICE'\'': '\''❄️ Icing'\'',\
                            '\''IFR'\'': '\''🌫️ IFR\/Low Visibility'\'',\
                            '\''MTN_OBSC'\'': '\''🏔️ Mountain Obscuration'\'',\
                            '\''SFC_WND'\'': '\''💨 Strong Surface Winds'\'',\
                            '\''CONVECTIVE'\'': '\''⛈️ Convective Activity'\'',\
                            '\''NONCONVECTIVE'\'': '\''⚠️ SIGMET'\''\
                        };\
                        const phenLabel = phenLabels[props.phenomenon] || props.phenomenon;\
                        const popupContent = `\
                            <div style="min-width: 250px; max-width: 400px;">\
                                <h3 style="margin: 0 0 10px 0; color: ${props.color}; font-size: 1.1rem;">\
                                    ${phenLabel}\
                                <\/h3>\
                                <div style="margin-bottom: 8px;">\
                                    <strong>Type:<\/strong> ${props.type}<br>\
                                    <strong>Severity:<\/strong> ${props.severity}<br>\
                                    <strong>Flight Levels:<\/strong> ${props.flight_levels}\
                                <\/div>\
                                <div style="margin-bottom: 8px; padding: 8px; background: #f5f5f5; border-left: 3px solid ${props.color};">\
                                    <strong>Valid:<\/strong><br>\
                                    ${formatTime(validFrom)} to ${formatTime(validUntil)}\
                                <\/div>\
                                <div style="margin-top: 10px; padding: 10px; background: #f9f9f9; border-radius: 4px; max-height: 200px; overflow-y: auto;">\
                                    <strong>Full Text:<\/strong><br>\
                                    <pre style="white-space: pre-wrap; font-size: 0.85rem; margin: 5px 0 0 0; font-family: monospace;">${props.text}<\/pre>\
                                <\/div>\
                            <\/div>\
                        `;\
                        layer.bindPopup(popupContent, { maxWidth: 450 });\
                        layer.on('\''mouseover'\'', function() { this.setStyle({ weight: 3, fillOpacity: 0.5 }); });\
                        layer.on('\''mouseout'\'', function() { this.setStyle({ weight: 2, fillOpacity: 0.3 }); });\
                    }\
                });\
                hazardsLayer.addTo(map);\
                console.log(`Loaded ${geojson.features.length} weather hazards`);\
            } catch (error) {\
                console.error('\''Error loading weather hazards:'\'', error);\
            }\
        }\
\
        setInterval(() => {\
            const checkbox = document.getElementById('\''showHazards'\'');\
            if (checkbox && checkbox.checked) loadWeatherHazards();\
        }, 5 * 60 * 1000);\
        \/\/ ===== END AIRMET\/SIGMET OVERLAY =====' "$TARGET"

echo "✅ All changes applied successfully!"
echo ""
echo "=========================================="
echo "  DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Restart Apache:"
echo "   sudo systemctl restart apache2"
echo ""
echo "2. Test at:"
echo "   http://209.248.90.253/cap_winds_app/wind-map"
echo ""
echo "3. Check the '☁️ Show AIRMETs/SIGMETs' checkbox"
echo ""
echo "Backup saved to: $BACKUP"
echo ""

