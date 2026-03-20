"""
KQ Station Management Web Interface
Simple admin interface for managing temporary weather stations
Automatically syncs KQ stations to the main airports table for TAF/METAR integration

Authentication: view (list) is public; add/edit/delete/toggle/sync require login.
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from auth import login_required

kq_admin = Blueprint('kq_admin', __name__, url_prefix='/admin/kq-stations')


def sync_station_to_airports(cur, station_id, sync_type='upsert'):
    """
    Sync a KQ station to the main airports table.
    sync_type: 'upsert' to add/update, 'delete' to remove
    """
    try:
        if sync_type == 'delete':
            cur.execute("""
                DELETE FROM observations.airports
                WHERE station_id = %s
            """, (station_id,))
            return

        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft
            FROM observations.custom_stations
            WHERE station_id = %s AND active = true
        """, (station_id,))

        station = cur.fetchone()
        if not station:
            cur.execute("""
                DELETE FROM observations.airports
                WHERE station_id = %s
            """, (station_id,))
            return

        cur.execute("""
            INSERT INTO observations.airports
                (station_id, name, location, elevation_ft, has_reporting, is_military)
            VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, true, true)
            ON CONFLICT (station_id) DO UPDATE SET
                name          = EXCLUDED.name,
                location      = EXCLUDED.location,
                elevation_ft  = EXCLUDED.elevation_ft,
                has_reporting = true,
                is_military   = true
        """, (station[0], station[1], station[3], station[2], station[4]))

    except Exception as e:
        print(f"Warning: Failed to sync station {station_id} to airports table: {e}")


def sync_all_active_stations_to_airports(cur):
    """Sync all active KQ stations to airports table."""
    try:
        cur.execute("""
            SELECT station_id FROM observations.custom_stations
            WHERE active = true
        """)
        for row in cur.fetchall():
            sync_station_to_airports(cur, row[0], 'upsert')
    except Exception as e:
        print(f"Warning: Failed to sync all stations: {e}")


# ---------------------------------------------------------------------------
# PUBLIC route — no login required
# ---------------------------------------------------------------------------

@kq_admin.route('/', strict_slashes=False)
def list_stations():
    """List all KQ stations — public view."""
    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft,
                   notes, active, created_at
            FROM observations.custom_stations
            ORDER BY active DESC, station_id
        """)

        stations = []
        for row in cur.fetchall():
            stations.append({
                'station_id':  row[0],
                'name':        row[1],
                'latitude':    row[2],
                'longitude':   row[3],
                'elevation_ft':row[4],
                'notes':       row[5],
                'active':      row[6],
                'created_at':  row[7],
            })

        cur.close()
        conn.close()

        return render_template('kq_stations.html', stations=stations)

    except Exception as e:
        flash(f'Error loading stations: {e}', 'error')
        return render_template('kq_stations.html', stations=[])


# ---------------------------------------------------------------------------
# PROTECTED routes — login required
# ---------------------------------------------------------------------------

@kq_admin.route('/add', methods=['GET', 'POST'])
@login_required
def add_station():
    """Add new KQ station."""
    if request.method == 'POST':
        try:
            station_id   = request.form.get('station_id', '').strip().upper()
            name         = request.form.get('name', '').strip()
            latitude     = float(request.form.get('latitude'))
            longitude    = float(request.form.get('longitude'))
            elevation_ft = request.form.get('elevation_ft', '').strip()
            notes        = request.form.get('notes', '').strip()

            elevation_ft = int(elevation_ft) if elevation_ft else None

            conn = get_connection()
            cur  = conn.cursor()

            cur.execute("""
                INSERT INTO observations.custom_stations
                    (station_id, name, latitude, longitude, elevation_ft, notes, active)
                VALUES (%s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (station_id) DO UPDATE SET
                    name         = EXCLUDED.name,
                    latitude     = EXCLUDED.latitude,
                    longitude    = EXCLUDED.longitude,
                    elevation_ft = EXCLUDED.elevation_ft,
                    notes        = EXCLUDED.notes,
                    active       = true
            """, (station_id, name, latitude, longitude, elevation_ft, notes))

            sync_station_to_airports(cur, station_id, 'upsert')
            conn.commit()
            cur.close()
            conn.close()

            flash(f'Station {station_id} added successfully and synced to airports table!', 'success')
            return redirect(url_for('kq_admin.list_stations'))

        except Exception as e:
            flash(f'Error adding station: {e}', 'error')

    return render_template('kq_station_form.html', station=None, action='Add')


@kq_admin.route('/edit/<station_id>', methods=['GET', 'POST'])
@login_required
def edit_station(station_id):
    """Edit existing KQ station."""
    if request.method == 'POST':
        try:
            name         = request.form.get('name', '').strip()
            latitude     = float(request.form.get('latitude'))
            longitude    = float(request.form.get('longitude'))
            elevation_ft = request.form.get('elevation_ft', '').strip()
            notes        = request.form.get('notes', '').strip()

            elevation_ft = int(elevation_ft) if elevation_ft else None

            conn = get_connection()
            cur  = conn.cursor()

            cur.execute("""
                UPDATE observations.custom_stations
                SET name = %s, latitude = %s, longitude = %s,
                    elevation_ft = %s, notes = %s
                WHERE station_id = %s
            """, (name, latitude, longitude, elevation_ft, notes, station_id))

            sync_station_to_airports(cur, station_id, 'upsert')
            conn.commit()
            cur.close()
            conn.close()

            flash(f'Station {station_id} updated and synced to airports table!', 'success')
            return redirect(url_for('kq_admin.list_stations'))

        except Exception as e:
            flash(f'Error updating station: {e}', 'error')

    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft, notes, active
            FROM observations.custom_stations
            WHERE station_id = %s
        """, (station_id,))

        row = cur.fetchone()
        station = {
            'station_id':  row[0],
            'name':        row[1],
            'latitude':    row[2],
            'longitude':   row[3],
            'elevation_ft':row[4],
            'notes':       row[5],
            'active':      row[6],
        } if row else None

        cur.close()
        conn.close()

        return render_template('kq_station_form.html', station=station, action='Edit')

    except Exception as e:
        flash(f'Error loading station: {e}', 'error')
        return redirect(url_for('kq_admin.list_stations'))


@kq_admin.route('/toggle/<station_id>', methods=['POST'])
@login_required
def toggle_station(station_id):
    """Toggle station active/inactive."""
    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("""
            UPDATE observations.custom_stations
            SET active = NOT active
            WHERE station_id = %s
            RETURNING active
        """, (station_id,))

        result     = cur.fetchone()
        new_status = result[0] if result else False

        if new_status:
            sync_station_to_airports(cur, station_id, 'upsert')
        else:
            sync_station_to_airports(cur, station_id, 'delete')

        conn.commit()
        cur.close()
        conn.close()

        status_text  = 'activated'   if new_status else 'deactivated'
        airports_text = 'added to'   if new_status else 'removed from'
        flash(f'Station {station_id} {status_text} and {airports_text} airports table!', 'success')

    except Exception as e:
        flash(f'Error toggling station: {e}', 'error')

    return redirect(url_for('kq_admin.list_stations'))


@kq_admin.route('/delete/<station_id>', methods=['POST'])
@login_required
def delete_station(station_id):
    """Delete station permanently."""
    try:
        conn = get_connection()
        cur  = conn.cursor()

        sync_station_to_airports(cur, station_id, 'delete')

        cur.execute("""
            DELETE FROM observations.model_wind_forecasts
            WHERE station_id = %s
        """, (station_id,))

        cur.execute("""
            DELETE FROM observations.custom_stations
            WHERE station_id = %s
        """, (station_id,))

        conn.commit()
        cur.close()
        conn.close()

        flash(f'Station {station_id} deleted and removed from airports table!', 'success')

    except Exception as e:
        flash(f'Error deleting station: {e}', 'error')

    return redirect(url_for('kq_admin.list_stations'))


@kq_admin.route('/sync-all', methods=['POST'])
@login_required
def sync_all_stations():
    """Sync all active KQ stations to airports table."""
    try:
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("""
            DELETE FROM observations.airports
            WHERE station_id LIKE 'KQ%'
        """)

        sync_all_active_stations_to_airports(cur)

        cur.execute("""
            SELECT COUNT(*) FROM observations.custom_stations
            WHERE active = true
        """)
        active_count = cur.fetchone()[0]

        conn.commit()
        cur.close()
        conn.close()

        flash(f'Successfully synced {active_count} active KQ stations to airports table!', 'success')

    except Exception as e:
        flash(f'Error syncing stations: {e}', 'error')

    return redirect(url_for('kq_admin.list_stations'))

