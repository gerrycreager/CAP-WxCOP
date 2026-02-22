#!/var/www/cap_winds_app/venv/bin/python3
"""
TFR JSON Ingestion Script
Uses provided TFR JSON data
"""
import sys
import json
import logging
from datetime import datetime
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def ingest_tfr_json(json_file_path):
    """Ingest TFR data from JSON file"""
    try:
        with open(json_file_path, 'r') as f:
            tfrs = json.load(f)
            
        log.info(f"Loaded {len(tfrs)} TFRs from JSON")
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Deactivate existing TFRs
        cur.execute("UPDATE observations.tfr SET active = FALSE")
        
        inserted = 0
        for tfr in tfrs:
            try:
                cur.execute("""
                    INSERT INTO observations.tfr 
                    (tfr_number, notam_id, facility, state, type, city, description, active, raw_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                    ON CONFLICT (notam_id) DO UPDATE SET
                        active = TRUE,
                        facility = EXCLUDED.facility,
                        state = EXCLUDED.state,
                        type = EXCLUDED.type,
                        description = EXCLUDED.description,
                        raw_data = EXCLUDED.raw_data
                """, (
                    tfr['notam_id'], tfr['notam_id'], tfr['facility'], 
                    tfr['state'], tfr['type'], tfr['state'], 
                    tfr['description'], json.dumps(tfr)
                ))
                inserted += 1
            except Exception as e:
                log.error(f"Failed to insert TFR {tfr['notam_id']}: {e}")
                
        conn.commit()
        cur.close()
        conn.close()
        
        log.info(f"Successfully ingested {inserted} TFRs")
        return inserted
        
    except Exception as e:
        log.error(f"Failed to ingest TFR JSON: {e}")
        return 0

if __name__ == '__main__':
    json_path = '/mnt/user-data/uploads/TFR_list.json'
    result = ingest_tfr_json(json_path)
    print(f"Ingested {result} TFRs")
