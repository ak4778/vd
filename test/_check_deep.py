import sqlite3
c = sqlite3.connect('c:/s/vd/device_dashboard.db')
total = c.execute('SELECT COUNT(1) FROM nodes').fetchone()[0]
schema = c.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name").fetchall()
fts = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'").fetchall()
c.close()
print(f'{total}|{len(schema)}|{len(fts)}')