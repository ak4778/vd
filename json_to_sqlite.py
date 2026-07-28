import sqlite3
import json
import os
import sys

json_path = 'leaf_nodes.json'
db_path = 'device_dashboard.db'

if not os.path.exists(json_path):
    print(f"Error: JSON file not found at {json_path}")
    sys.exit(1)

with open(json_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

nodes = data.get('nodes', [])
if not nodes:
    print("Error: No nodes found in JSON")
    sys.exit(1)

total_json = len(nodes)
print(f"Read {total_json} nodes from JSON")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('DROP TABLE IF EXISTS nodes')
cursor.execute('''
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT,
    channelCode TEXT,
    isOnline TEXT,
    cameraType TEXT,
    operation TEXT,
    customOperation TEXT,
    P1 TEXT,
    P3 TEXT,
    P4 TEXT
)
''')
print("Table created")

BATCH_SIZE = 500
batch = []
total = 0

for node in nodes:
    batch.append((
        str(node.get('id', '')),
        str(node.get('name', '')),
        str(node.get('channelCode', '')),
        str(node.get('isOnline', '')),
        str(node.get('cameraType', '')),
        str(node.get('operation', '')) if node.get('operation') not in (None, '') else '',
        str(node.get('customOperation', '')) if node.get('customOperation') not in (None, '') else '',
        str(node.get('P1', '')),
        str(node.get('P3', '')),
        str(node.get('P4', ''))
    ))
    
    if len(batch) >= BATCH_SIZE:
        cursor.executemany('''
            INSERT INTO nodes 
            (id, name, channelCode, isOnline, cameraType, operation, customOperation, P1, P3, P4)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', batch)
        total += len(batch)
        batch = []
        if total % 5000 == 0:
            print(f"Inserted {total} rows...")

if batch:
    cursor.executemany('''
        INSERT INTO nodes 
        (id, name, channelCode, isOnline, cameraType, operation, customOperation, P1, P3, P4)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', batch)
    total += len(batch)

conn.commit()

cursor.execute('SELECT COUNT(*) FROM nodes')
db_count = cursor.fetchone()[0]

print(f"JSON nodes: {total_json}")
print(f"DB rows: {db_count}")
if total_json == db_count:
    print("✓ Row count matches!")
else:
    print(f"✗ MISMATCH: JSON has {total_json} but DB has {db_count}")

cursor.execute("PRAGMA table_info(nodes)")
cols = [c[1] for c in cursor.fetchall()]
print(f"Table columns: {cols}")

cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p1 ON nodes(P1)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p3 ON nodes(P3)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p4 ON nodes(P4)")
conn.commit()
print("Indexes created")

cursor.execute("SELECT id, P1, P3, P4 FROM nodes WHERE P1 != '' LIMIT 3")
samples = cursor.fetchall()
print(f"\nSample P1/P3/P4 data:")
for s in samples:
    print(f"  {s[0]}: P1={s[1]}, P3={s[2]}, P4={s[3]}")

conn.close()
print("Done!")