import sqlite3
import csv
import os
import sys

csv_path = 'leaf_nodes.csv'
db_path = 'device_dashboard.db'

if not os.path.exists(csv_path):
    print(f"Error: CSV file not found at {csv_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

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

cursor.execute('DELETE FROM nodes')
print("Cleared existing data")

BATCH_SIZE = 500
batch = []
total = 0

with open(csv_path, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    
    for row in reader:
        batch.append((
            row.get('id', ''),
            row.get('name', ''),
            row.get('channelCode', ''),
            row.get('isOnline', ''),
            row.get('cameraType', ''),
            row.get('operation', ''),
            row.get('customOperation', '')
        ))
        
        if len(batch) >= BATCH_SIZE:
            cursor.executemany('''
                INSERT INTO nodes 
                (id, name, channelCode, isOnline, cameraType, operation, customOperation)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', batch)
            total += len(batch)
            batch = []
            if total % 5000 == 0:
                print(f"Inserted {total} rows...")
    
    if batch:
        cursor.executemany('''
            INSERT INTO nodes 
            (id, name, channelCode, isOnline, cameraType, operation, customOperation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', batch)
        total += len(batch)

conn.commit()

cursor.execute('SELECT COUNT(*) FROM nodes')
db_count = cursor.fetchone()[0]

with open(csv_path, 'r', encoding='utf-8-sig') as f:
    csv_count = sum(1 for _ in csv.DictReader(f))

print(f"CSV rows: {csv_count}")
print(f"DB rows: {db_count}")
if csv_count == db_count:
    print("✓ Row count matches!")
else:
    print(f"✗ MISMATCH: CSV has {csv_count} but DB has {db_count}")

cursor.execute("PRAGMA table_info(nodes)")
cols = [c[1] for c in cursor.fetchall()]
print(f"Table columns: {cols}")

cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation)")
conn.commit()
print("Indexes created")

conn.close()
print("Done!")