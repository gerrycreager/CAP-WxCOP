#!/bin/bash
# CAP Weather Diagnostic and Fix Script
set -e

echo "=== CAP WEATHER SYSTEM DIAGNOSTICS ==="
echo

# Test Python syntax for all key modules
echo "1. Testing Python module syntax..."

cd /var/www/cap_winds_app

echo -n "  app.py: "
if python3 -m py_compile app.py 2>/dev/null; then
    echo "✓ OK"
else
    echo "❌ SYNTAX ERROR"
    python3 -m py_compile app.py
fi

echo -n "  weather_api.py: "
if python3 -m py_compile weather_api.py 2>/dev/null; then
    echo "✓ OK"
else
    echo "❌ SYNTAX ERROR"
    python3 -m py_compile weather_api.py
fi

echo -n "  weather_enhanced_api.py: "
if python3 -m py_compile weather_enhanced_api.py 2>/dev/null; then
    echo "✓ OK"
else
    echo "❌ SYNTAX ERROR (or file missing)"
    python3 -m py_compile weather_enhanced_api.py 2>/dev/null || echo "    File may not exist yet"
fi

echo -n "  kq_admin.py: "
if python3 -m py_compile kq_admin.py 2>/dev/null; then
    echo "✓ OK"
else
    echo "❌ SYNTAX ERROR"
    echo "    Checking line 290..."
    sed -n '285,295p' kq_admin.py | nl -ba -v285
    echo
    echo "    Attempting to identify the issue..."
    python3 -c "
import ast
try:
    with open('kq_admin.py', 'r') as f:
        content = f.read()
    ast.parse(content)
    print('    Syntax is actually OK - may be import issue')
except SyntaxError as e:
    print(f'    Syntax Error: {e.msg} at line {e.lineno}')
    print(f'    Text: {e.text}')
"
fi

echo

# Test database connectivity
echo "2. Testing database connectivity..."
python3 -c "
try:
    import sys
    sys.path.insert(0, '/var/www/cap_winds_app')
    from db_config import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT 1')
    cur.close()
    conn.close()
    print('  ✓ Database connection OK')
except Exception as e:
    print(f'  ❌ Database error: {e}')
"

echo

# Test weather API functionality specifically
echo "3. Testing weather API functionality..."
python3 -c "
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
try:
    from weather_api import weather_api
    print('  ✓ weather_api module loads successfully')
    print(f'    Blueprint name: {weather_api.name}')
    print(f'    URL prefix: {weather_api.url_prefix}')
    print(f'    Routes: {[rule.rule for rule in weather_api.url_map.iter_rules()]}')
except Exception as e:
    print(f'  ❌ weather_api import error: {e}')
    import traceback
    traceback.print_exc()
"

echo

# Check if weather data exists
echo "4. Testing weather data availability..."
python3 -c "
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
try:
    from db_config import get_connection
    conn = get_connection()
    cur = conn.cursor()
    
    # Check for recent METAR data
    cur.execute('''
        SELECT COUNT(*), MAX(observation_time) 
        FROM metar_observations 
        WHERE observation_time >= NOW() - INTERVAL '2 hours'
    ''')
    count, latest = cur.fetchone()
    print(f'  Recent METAR observations: {count}')
    print(f'  Latest observation: {latest}')
    
    # Test airports table
    cur.execute('SELECT COUNT(*) FROM airports WHERE ident LIKE %s', ('K%',))
    airport_count = cur.fetchone()[0]
    print(f'  US airports in database: {airport_count}')
    
    cur.close()
    conn.close()
    
    if count == 0:
        print('  ⚠️  WARNING: No recent weather data found')
    else:
        print('  ✓ Weather data is available')
        
except Exception as e:
    print(f'  ❌ Database query error: {e}')
"

echo

# Test individual weather API endpoint
echo "5. Testing weather API endpoint directly..."
python3 -c "
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
try:
    from weather_api import weather_api
    from flask import Flask
    
    # Create minimal test app
    app = Flask(__name__)
    app.register_blueprint(weather_api, url_prefix='/api/weather')
    
    with app.test_client() as client:
        response = client.get('/api/weather/metar/recent?bounds=-100,25,-80,45&limit=10')
        print(f'  Status: {response.status_code}')
        if response.status_code == 200:
            data = response.get_json()
            print(f'  Response type: {type(data)}')
            if isinstance(data, dict) and 'metars' in data:
                print(f'  METAR count: {len(data[\"metars\"])}')
                print('  ✓ Weather API endpoint working')
            else:
                print(f'  ⚠️  Unexpected response format: {data}')
        else:
            print(f'  ❌ Error response: {response.get_data(as_text=True)}')
            
except Exception as e:
    print(f'  ❌ Endpoint test error: {e}')
    import traceback
    traceback.print_exc()
"

echo
echo "=== DIAGNOSTIC COMPLETE ==="
echo
echo "Next steps based on results above:"
echo "1. Fix any syntax errors identified"
echo "2. Ensure database connectivity"
echo "3. Verify weather data is current"
echo "4. Test API endpoints individually"
