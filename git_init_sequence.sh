#!/bin/bash
# git_init_sequence.sh
# Run once on the server to initialize the CAP WxCOP git repository.
# Execute as root or the user that owns /var/www/cap_winds_app/
#
# Prerequisites:
#   1. Create a GitHub repo first (e.g. github.com/YOUR_ORG/cap-wxcop)
#   2. Set up SSH key or personal access token for GitHub auth
#   3. Edit GITHUB_REMOTE below before running
#
# Usage:
#   chmod +x git_init_sequence.sh
#   ./git_init_sequence.sh

set -euo pipefail

GITHUB_REMOTE="git@github.com:gerrycreager/CAP-WxCOP.git"   # ← edit this
APP_DIR="/var/www/cap_winds_app"
DEV_DIR="/var/www/cap_winds_dev"

echo "=== CAP WxCOP Git Repository Init ==="
echo ""

# ── Step 1: Init repo in production app dir ───────────────────────────────
cd "$APP_DIR"
echo "[1/8] Initializing git repo in $APP_DIR"
git init
git checkout -b main

# ── Step 2: Copy repo files into place ────────────────────────────────────
echo "[2/8] Placing .gitignore and README.md"
# These should already be here from the session outputs, but copy if needed:
# cp /home/claude/.gitignore .
# cp /home/claude/README.md .
# cp /home/claude/CAP_WxCOP_Phase2_Requirements.md .

# ── Step 3: Stage files for initial commit ────────────────────────────────
echo "[3/8] Staging files"
git add \
    app.py \
    cap_winds.wsgi \
    mrms_tile_renderer.py \
    README.md \
    .gitignore \
    CAP_WxCOP_Phase2_Requirements.md

# API modules (add whichever exist)
for f in weather_api.py wind_forecast_api.py radar_api.py \
          weather_pages.py kq_admin.py incident_archive.py \
          manual_taf.py airmet_sigmet_api.py weather_enhanced_api.py; do
    [ -f "$f" ] && git add "$f" && echo "  + $f"
done

# Templates
git add templates/

# LDM scripts and config
git add \
    /home/ldm/scripts/mrms_render_pipe.sh \
    /home/ldm/etc/pqact_mrms.conf 2>/dev/null || \
    echo "  ⚠ LDM scripts not found at expected paths — add manually"

# Apache vhost configs
git add \
    /etc/apache2/sites-available/cap_winds*.conf 2>/dev/null || \
    echo "  ⚠ Apache configs not found — add manually if desired"

# ── Step 4: Verify nothing sensitive is staged ────────────────────────────
echo ""
echo "[4/8] Staged files — review before committing:"
git status
echo ""
echo "  ← Press Ctrl-C now if anything above should NOT be committed"
echo "  ← Check especially for: credentials, .env files, private keys"
read -p "  Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }

# ── Step 5: Initial commit on main ────────────────────────────────────────
echo "[5/8] Creating initial commit on main"
git config user.email "wxcop@cap.gov"        # ← edit to your address
git config user.name  "CAP Weather Ops"       # ← edit to your name
git commit -m "Initial commit — Phase 1 radar operational

- MRMS composite reflectivity / MESH / lightning / azimuthal shear
- Animated tile pyramid renderer (z3-z8, 30-frame fixed window)
- 4-minute poll cycle, crossfade animation
- Weather COP map with MRMS overlay
- Airport symbol/label tiers, search/highlight
- LDM pqact pipeline for all four products
- Production deployment confirmed 2026-02-28"

# ── Step 6: Create and switch to dev branch ───────────────────────────────
echo "[6/8] Creating dev branch"
git checkout -b dev

# ── Step 7: Add remote and push both branches ─────────────────────────────
echo "[7/8] Adding remote: $GITHUB_REMOTE"
git remote add origin "$GITHUB_REMOTE"

echo "  Pushing main..."
git push -u origin main

echo "  Pushing dev..."
git push -u origin dev

# ── Step 8: Set dev as default working branch ─────────────────────────────
echo "[8/8] Setting HEAD to dev on remote (makes dev the default branch)"
echo "  → Also set this in GitHub repo Settings → Branches → Default branch → dev"

echo ""
echo "=== Done ==="
echo ""
echo "Repository layout:"
echo "  main  ← production-ready, matches /CAP_WxCOP"
echo "  dev   ← active development, matches /CAP_WxCOP_DEV"
echo ""
echo "Daily workflow:"
echo "  1. Edit files in /var/www/cap_winds_dev/"
echo "  2. git checkout dev && git add -A && git commit && git push"
echo "  3. When ready to promote: git checkout main && git merge dev && git push"
echo "  4. Deploy: cp files to cap_winds_app/, touch cap_winds.wsgi"
echo ""
echo "GitHub remote: $GITHUB_REMOTE"

