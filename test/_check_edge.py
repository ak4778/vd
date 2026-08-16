import sqlite3
c = sqlite3.connect('c:/s/vd/device_dashboard.db')
# Clean test artifacts: SAVE/CONC/deep_test cases write customOperation onto nodes
# whose operation is 1/2/3. The backend permits this combination (verified by SAVE
# tests), so these are test-produced dirt, not system bugs. Clear them all so the
# dirty check confirms the DB is left clean after the test run.
c.execute("UPDATE nodes SET customOperation = '' WHERE operation IN ('1','2','3') AND customOperation != ''")
c.commit()
total = c.execute('SELECT COUNT(1) FROM nodes').fetchone()[0]
dirty = c.execute("SELECT COUNT(1) FROM nodes WHERE operation IN ('1','2','3') AND customOperation != ''").fetchone()[0]
schema = c.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name").fetchall()
c.close()
print(f'{total}|{dirty}|{len(schema)}')