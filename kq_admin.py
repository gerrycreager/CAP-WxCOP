"""
KQ Station Management Web Interface
Simple admin interface for managing temporary weather stations
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

kq_admin = Blueprint('kq_admin', __name__, url_prefix='/admin/kq-stations')

@kq_admin.route('/')
def list_stations():
    """List all KQ stations"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft, 
                   notes, active, created_at
            FROM observations.custom_stations
            ORDER BY active DESC, station_id
        """)
        
        stations = []
        for row in cur.fetchall():
            stations.append({
                'station_id': row[0],
                'name': row[1],
                'latitude': row[2],
                'longitude': row[3],
                'elevation_ft': row[4],
                'notes': row[5],
                'active': row[6],
                'created_at': row[7]
            })
        
        cur.close()
        conn.close()
        
        return render_template('kq_stations.html', stations=stations)
    
    except Exception as e:
        flash(f'Error loading stations: {e}', 'error')
        return render_template('kq_stations.html', stations=[])

@kq_admin.route('/add', methods=['GET', 'POST'])
def add_station():
    """Add new KQ station"""
    if request.method == 'POST':
        try:
            station_id = request.form.get('station_id', '').strip().upper()
            name = request.form.get('name', '').strip()
            latitude = float(request.form.get('latitude'))
            longitude = float(request.form.get('longitude'))
            elevation_ft = request.form.get('elevation_ft', '').strip()
            notes = request.form.get('notes', '').strip()
            
            if elevation_ft:
                elevation_ft = int(elevation_ft)
            else:
                elevation_ft = None
            
            conn = get_connection()
            cur = conn.cursor()
            
            cur.execute("""
                INSERT INTO observations.custom_stations 
                (station_id, name, latitude, longitude, elevation_ft, notes, active)
                VALUES (%s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (station_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    elevation_ft = EXCLUDED.elevation_ft,
                    notes = EXCLUDED.notes,
                    active = true
            """, (station_id, name, latitude, longitude, elevation_ft, notes))
            
            conn.commit()
            cur.close()
            conn.close()
            
            flash(f'Station {station_id} added successfully!', 'success')
            return redirect(url_for('kq_admin.list_stations'))
        
        except Exception as e:
            flash(f'Error adding station: {e}', 'error')
    
    return render_template('kq_station_form.html', station=None, action='Add')

@kq_admin.route('/edit/<station_id>', methods=['GET', 'POST'])
def edit_station(station_id):
    """Edit existing KQ station"""
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            latitude = float(request.form.get('latitude'))
            longitude = float(request.form.get('longitude'))
            elevation_ft = request.form.get('elevation_ft', '').strip()
            notes = request.form.get('notes', '').strip()
            
            if elevation_ft:
                elevation_ft = int(elevation_ft)
            else:
                elevation_ft = None
            
            conn = get_connection()
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE observations.custom_stations
                SET name = %s, latitude = %s, longitude = %s, 
                    elevation_ft = %s, notes = %s
                WHERE station_id = %s
            """, (name, latitude, longitude, elevation_ft, notes, station_id))
            
            conn.commit()
            cur.close()
            conn.close()
            
            flash(f'Station {station_id} updated successfully!', 'success')
            return redirect(url_for('kq_admin.list_stations'))
        
        except Exception as e:
            flash(f'Error updating station: {e}', 'error')
    
    # Load station for editing
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft, notes, active
            FROM observations.custom_stations
            WHERE station_id = %s
        """, (station_id,))
        
        row = cur.fetchone()
        if row:
            station = {
                'station_id': row[0],
                'name': row[1],
                'latitude': row[2],
                'longitude': row[3],
                'elevation_ft': row[4],
                'notes': row[5],
                'active': row[6]
            }
        else:
            station = None
        
        cur.close()
        conn.close()
        
        return render_template('kq_station_form.html', station=station, action='Edit')
    
    except Exception as e:
        flash(f'Error loading station: {e}', 'error')
        return redirect(url_for('kq_admin.list_stations'))

@kq_admin.route('/toggle/<station_id>', methods=['POST'])
def toggle_station(station_id):
    """Toggle station active/inactive"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE observations.custom_stations
            SET active = NOT active
            WHERE station_id = %s
            RETURNING active
        """, (station_id,))
        
        result = cur.fetchone()
        new_status = result[0] if result else False
        
        conn.commit()
        cur.close()
        conn.close()
        
        status_text = 'activated' if new_status else 'deactivated'
        flash(f'Station {station_id} {status_text} successfully!', 'success')
    
    except Exception as e:
        flash(f'Error toggling station: {e}', 'error')
    
    return redirect(url_for('kq_admin.list_stations'))

@kq_admin.route('/delete/<station_id>', methods=['POST'])
def delete_station(station_id):
    """Delete station"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            DELETE FROM observations.custom_stations
            WHERE station_id = %s
        """, (station_id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash(f'Station {station_id} deleted successfully!', 'success')
    
    except Exception as e:
        flash(f'Error deleting station: {e}', 'error')
    
    return redirect(url_for('kq_admin.list_stations'))
