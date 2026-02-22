# 1. Copy all scripts to scripts directory
mkdir -p /var/www/cap_winds_app/scripts
# Download and copy all 4 scripts

# 2. Set permissions
chmod +x /var/www/cap_winds_app/scripts/*.py
chmod +x /var/www/cap_winds_app/scripts/*.sh

# 3. Install dependencies
cd /var/www/cap_winds_app
source venv/bin/activate
pip install python-metar psycopg2-binary

# 4. Test
sudo -u www-data ./scripts/ingest_metar.py --today
sudo -u www-data ./scripts/ingest_taf.py --today

# 5. Set up cron
sudo crontab -u www-data -e
# Add METAR/TAF ingest jobs (see guide)

sudo crontab -e  
# Add cleanup job (see guide)
