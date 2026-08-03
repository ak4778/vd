import sqlite3
import json
import os
import sys

json_path = 'leaf_nodes.json'
db_path = 'device_dashboard.db'

if not os.path.exists(json_path):
    print(f"Error: JSON file not found at {json_path}")
    sys.exit(1)

if not os.path.exists(db_path):
    print(f"Error: DB file not found at {db_path}")
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

cursor.execute("PRAGMA table_info(nodes)")
existing_cols = [c[1] for c in cursor.fetchall()]
print(f"Existing table columns: {existing_cols}")

cursor.execute("SELECT id, operation, customOperation FROM nodes")
existing_rows = {}
for row in cursor.fetchall():
    existing_rows[row[0]] = (row[1], row[2])
print(f"DB has {len(existing_rows)} rows")

update_count = 0
insert_count = 0
skip_count = 0

for node in nodes:
    nid = str(node.get('id', ''))
    if not nid:
        skip_count += 1
        continue

    name = str(node.get('name', ''))
    channelCode = str(node.get('channelCode', ''))
    isOnline = str(node.get('isOnline', ''))
    cameraType = str(node.get('cameraType', ''))
    operation = str(node.get('operation', '')) if node.get('operation') not in (None, '') else ''
    customOperation = str(node.get('customOperation', '')) if node.get('customOperation') not in (None, '') else ''
    P1 = str(node.get('P1', ''))
    P3 = str(node.get('P3', ''))
    P4 = str(node.get('P4', ''))

    if nid in existing_rows:
        old_op, old_cust = existing_rows[nid]
        if old_op and old_op.strip():
            cursor.execute('''
                UPDATE nodes
                SET name=?, channelCode=?, isOnline=?, cameraType=?, P1=?, P3=?, P4=?
                WHERE id=?
            ''', (name, channelCode, isOnline, cameraType, P1, P3, P4, nid))
            update_count += 1
        else:
            cursor.execute('''
                UPDATE nodes
                SET name=?, channelCode=?, isOnline=?, cameraType=?,
                    operation=?, customOperation=?, P1=?, P3=?, P4=?
                WHERE id=?
            ''', (name, channelCode, isOnline, cameraType, operation, customOperation, P1, P3, P4, nid))
            update_count += 1
    else:
        cursor.execute('''
            INSERT INTO nodes
            (id, name, channelCode, isOnline, cameraType, operation, customOperation, P1, P3, P4)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (nid, name, channelCode, isOnline, cameraType, operation, customOperation, P1, P3, P4))
        insert_count += 1

conn.commit()

cursor.execute('SELECT COUNT(*) FROM nodes')
db_count = cursor.fetchone()[0]

print(f"\n===== Update Summary =====")
print(f"JSON nodes: {total_json}")
print(f"Updated (existing): {update_count}")
print(f"Inserted (new): {insert_count}")
print(f"Skipped (no id): {skip_count}")
print(f"DB rows now: {db_count}")

cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p1 ON nodes(P1)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p3 ON nodes(P3)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_p4 ON nodes(P4)")
conn.commit()
print("Indexes verified/created")

cursor.execute("SELECT id, name, operation, P4 FROM nodes WHERE operation != '' LIMIT 3")
samples = cursor.fetchall()
print(f"\nSample rows with operation set:")
for s in samples:
    print(f"  {s[0]}: name={s[1]}, operation={s[2]}, P4={s[3]}")

cursor.execute("SELECT id, name, operation FROM nodes WHERE operation = '' LIMIT 3")
samples2 = cursor.fetchall()
print(f"\nSample rows without operation:")
for s in samples2:
    print(f"  {s[0]}: name={s[1]}, operation=(empty)")

conn.close()
print("\nDone!")
