import sqlite3
import csv
import os

csv_path = r'c:\s\device-dashboard\leaf_nodes.csv'
db_path = r'c:\s\device-dashboard\device_dashboard.db'

# Check if CSV exists
if not os.path.exists(csv_path):
    print(f"Error: CSV file not found at {csv_path}")
    exit(1)

# Connect to SQLite database (creates if not exists)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create table
cursor.execute('''
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT,
    channelCode TEXT,
    isOnline TEXT,
    cameraType TEXT,
    operation TEXT,
    customOperation TEXT
)
''')

# Read CSV and insert data
with open(csv_path, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    count = 0
    for row in reader:
        cursor.execute('''
            INSERT OR REPLACE INTO nodes 
            (id, name, channelCode, isOnline, cameraType, operation, customOperation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            row.get('id', ''),
            row.get('name', ''),
            row.get('channelCode', ''),
            row.get('isOnline', ''),
            row.get('cameraType', ''),
            row.get('operation', ''),
            row.get('customOperation', '')
        ))
        count += 1
        if count % 1000 == 0:
            print(f"Inserted {count} rows...")

conn.commit()
cursor.execute('SELECT COUNT(*) FROM nodes')
total = cursor.fetchone()[0]
print(f"Total rows inserted: {total}")

conn.close()
print("Done!")
