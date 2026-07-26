import sqlite3

conn = sqlite3.connect(r'c:\s\device-dashboard\device_dashboard.db')
cursor = conn.cursor()

# 测试SQLite模式保存的数据
print("=== SQLite数据库数据 ===")
cursor.execute("SELECT id, operation, customOperation FROM nodes WHERE id IN ('N52885', 'N52886')")
rows = cursor.fetchall()
for row in rows:
    print(f"id: {row[0]}, operation: '{row[1]}', customOperation: '{row[2]}'")

# 测试CSV模式迁移过来的数据
print("\n=== CSV迁移数据验证 ===")
cursor.execute("SELECT id, operation, customOperation FROM nodes WHERE id = 'N52875'")
row = cursor.fetchone()
if row:
    print(f"id: {row[0]}, operation: '{row[1]}', customOperation: '{row[2]}'")

conn.close()
