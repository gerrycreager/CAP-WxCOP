#!/usr/bin/env python3
"""
Add Home Button to index.html Header
Simple and reliable approach
"""

import os
import re
from datetime import datetime

# Paths
template_path = '/var/www/cap_winds_app/templates/index.html'

# Backup
backup_path = template_path + f'.backup_home_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
os.system(f'cp {template_path} {backup_path}')
print(f"[OK] Backup created: {backup_path}")
print()

# Read file
with open(template_path, 'r') as f:
    content = f.read()

# Check if button already exists
if '🏠 Home' in content or 'href="/"' in content and 'Home' in content:
    print("[INFO] Home button appears to already exist")
    print()
else:
    print("[INFO] Adding Home button to header...")
    
    # Find the header section and add a wrapper div with home button
    # Look for <header> tag and the h1
    
    # Strategy: Add right after opening <header> tag
    # Add a flex container with title on left, button on right
    
    header_addition = '''    <div style="display: flex; justify-content: space-between; align-items: center;">
        <div>'''
    
    # Add after opening header tag
    content = content.replace('<header>', '<header>\n' + header_addition, 1)
    
    # Close the wrapper and add button before </header>
    button_html = '''        </div>
        <a href="/" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 0.7rem 1.5rem; border-radius: 8px; text-decoration: none; font-weight: 600; box-shadow: 0 2px 4px rgba(0,0,0,0.2); transition: transform 0.2s, box-shadow 0.2s;" onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 8px rgba(0,0,0,0.3)';" onmouseout="this.style.transform=''; this.style.boxShadow='0 2px 4px rgba(0,0,0,0.2)';">
            🏠 Home
        </a>
    </div>'''
    
    content = content.replace('</header>', button_html + '\n    </header>', 1)
    
    # Write back
    with open(template_path, 'w') as f:
        f.write(content)
    
    print("[OK] Home button added to header")
    print()

# Restart Apache
print("[INFO] Restarting Apache...")
os.system('sudo systemctl restart apache2')
print("[OK] Apache restarted")
print()

print("=" * 70)
print("Home Button Added!")
print("=" * 70)
print()
print("Test at: http://209.248.90.253/cap_winds_app/")
print()
print("The Home button should appear in the top-right of the header")
print("=" * 70)

