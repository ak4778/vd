#include "data_source.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#else
#include <pthread.h>
#endif

#include "sqlite3.h"

#define MAX_SQL_LEN           2048
#define MAX_WHERE_LEN         1024
#define MAX_BIND_PARAMS        64
#define MAX_TOKENS_PER_FILTER  16
#define MAX_NODE_BUFFER_SIZE  1024

// Cross-platform mutex
#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION s_sqlite_mutex;
static int s_mutexes_initialized = 0;

static void sqlite_mutex_init(void) {
  if (!s_mutexes_initialized) {
    InitializeCriticalSection(&s_sqlite_mutex);
    s_mutexes_initialized = 1;
  }
}
static void sqlite_mutex_lock(void) { EnterCriticalSection(&s_sqlite_mutex); }
static void sqlite_mutex_unlock(void) { LeaveCriticalSection(&s_sqlite_mutex); }
#else
static pthread_mutex_t s_sqlite_mutex = PTHREAD_MUTEX_INITIALIZER;
static void sqlite_mutex_init(void) { (void)0; }
static void sqlite_mutex_lock(void) { pthread_mutex_lock(&s_sqlite_mutex); }
static void sqlite_mutex_unlock(void) { pthread_mutex_unlock(&s_sqlite_mutex); }
#endif

static sqlite3 *s_db = NULL;
static int s_db_file_exists = 0;

static int exec_sql(const char *sql) {
  char *errmsg;
  int rc = sqlite3_exec(s_db, sql, NULL, NULL, &errmsg);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL error: %s\n", errmsg);
    sqlite3_free(errmsg);
    return -1;
  }
  return 0;
}

static int count_commas(const char *str) {
  if (str == NULL || *str == '\0') return 0;
  int count = 1;
  for (const char *p = str; *p != '\0'; p++) {
    if (*p == ',') count++;
  }
  return count;
}

struct param_list {
  char *values[MAX_BIND_PARAMS];
  int lens[MAX_BIND_PARAMS];
  int is_null[MAX_BIND_PARAMS];
  int count;
};

static void plist_init(struct param_list *pl) {
  memset(pl, 0, sizeof(*pl));
}

static int plist_add_text(struct param_list *pl, const char *text) {
  if (pl->count >= MAX_BIND_PARAMS) return -1;
  pl->values[pl->count] = text ? strdup(text) : NULL;
  pl->lens[pl->count] = text ? (int) strlen(text) : 0;
  pl->is_null[pl->count] = (text == NULL);
  pl->count++;
  return 0;
}

static void plist_bind(sqlite3_stmt *stmt, struct param_list *pl) {
  for (int i = 0; i < pl->count; i++) {
    int idx = i + 1;
    if (pl->is_null[i]) {
      sqlite3_bind_null(stmt, idx);
    } else {
      sqlite3_bind_text(stmt, idx, pl->values[i], pl->lens[i], SQLITE_TRANSIENT);
    }
  }
}

static void plist_free(struct param_list *pl) {
  for (int i = 0; i < pl->count; i++) {
    if (pl->values[i] != NULL) {
      free(pl->values[i]);
      pl->values[i] = NULL;
    }
  }
  pl->count = 0;
}

static int add_filter_in(char *where, int where_len, int pos, const char *col,
                        const char *values, int has_value, struct param_list *params) {
  // has_value=0：参数未出现在 URL，跳过过滤
  if (!has_value) return pos;
  // has_value=1 且 values 为空字符串：用户取消了全部选择，返回 0 条（1=0 永假条件）
  if (values == NULL || *values == '\0') {
    pos += snprintf(where + pos, (size_t)(where_len - pos), "%s1=0",
                    pos > 0 ? " AND " : "");
    return pos;
  }

  int n = count_commas(values);
  if (n > MAX_TOKENS_PER_FILTER) n = MAX_TOKENS_PER_FILTER;

  if (n == 1) {
    pos += snprintf(where + pos, (size_t)(where_len - pos), "%s%s = ?",
                    pos > 0 ? " AND " : "", col);
    plist_add_text(params, values);
  } else {
    pos += snprintf(where + pos, (size_t)(where_len - pos), "%s%s IN (",
                    pos > 0 ? " AND " : "", col);
    char *copy = strdup(values);
    char *saveptr = NULL;
    char *t = strtok_r(copy, ",", &saveptr);
    int idx = 0;
    while (t != NULL && idx < n) {
      if (idx > 0) pos += snprintf(where + pos, (size_t)(where_len - pos), ", ");
      pos += snprintf(where + pos, (size_t)(where_len - pos), "?");
      plist_add_text(params, t);
      t = strtok_r(NULL, ",", &saveptr);
      idx++;
    }
    free(copy);
    pos += snprintf(where + pos, (size_t)(where_len - pos), ")");
  }
  return pos;
}

static int build_where_sql(struct ds_query *query, char *where, int where_len,
                           struct param_list *params) {
  int pos = 0;

  pos = add_filter_in(where, where_len, pos, "isOnline", query->isOnline,
                      query->has_isOnline, params);
  pos = add_filter_in(where, where_len, pos, "cameraType", query->cameraType,
                      query->has_cameraType, params);

  // has_operation=0：参数未出现，跳过
  if (query->has_operation) {
    // has_operation=1 且值为空：用户取消了全部选择，返回 0 条
    if (query->operation == NULL || query->operation[0] == '\0') {
      pos += snprintf(where + pos, (size_t)(where_len - pos), "%s1=0",
                      pos > 0 ? " AND " : "");
    } else {
      char *copy = strdup(query->operation);
      char *saveptr = NULL;
      char *t = strtok_r(copy, ",", &saveptr);

      pos += snprintf(where + pos, (size_t)(where_len - pos), "%s(",
                      pos > 0 ? " AND " : "");

      int idx = 0;
      while (t != NULL) {
        if (idx > 0) pos += snprintf(where + pos, (size_t)(where_len - pos), " OR ");
        if (strcmp(t, "0") == 0) {
          pos += snprintf(where + pos, (size_t)(where_len - pos),
                           "(operation IS NULL OR operation = '')");
        } else {
          pos += snprintf(where + pos, (size_t)(where_len - pos), "operation = ?");
          plist_add_text(params, t);
        }
        t = strtok_r(NULL, ",", &saveptr);
        idx++;
      }
      free(copy);
      pos += snprintf(where + pos, (size_t)(where_len - pos), ")");
    }
  }

  if (query->keyword != NULL && query->keyword[0] != '\0') {
    char like_pattern[MAX_NODE_BUFFER_SIZE];
    snprintf(like_pattern, sizeof(like_pattern), "%%%s%%", query->keyword);

    pos += snprintf(where + pos, (size_t)(where_len - pos),
                     "%s(name LIKE ? OR P4 LIKE ?)", pos > 0 ? " AND " : "");
    plist_add_text(params, like_pattern);
    plist_add_text(params, like_pattern);
  }

  return pos;
}

static int file_exists(const char *path) {
  FILE *f = fopen(path, "rb");
  if (f) {
    fclose(f);
    return 1;
  }
  return 0;
}

static int sqlite_init(const char *path) {
  sqlite_mutex_init();

  sqlite_mutex_lock();
  int rc = sqlite3_open(path, &s_db);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "Cannot open database: %s\n", sqlite3_errmsg(s_db));
    sqlite3_close(s_db);
    s_db = NULL;
    sqlite_mutex_unlock();
    return -1;
  }

  sqlite3_busy_timeout(s_db, 5000);

  exec_sql("PRAGMA journal_mode=WAL");

  const char *create_table =
      "CREATE TABLE IF NOT EXISTS nodes ("
      "id TEXT PRIMARY KEY,"
      "name TEXT,"
      "channelCode TEXT,"
      "isOnline TEXT,"
      "cameraType TEXT,"
      "operation TEXT,"
      "customOperation TEXT,"
      "P1 TEXT,"
      "P3 TEXT,"
      "P4 TEXT"
      ");";

  if (exec_sql(create_table) != 0) {
    sqlite3_close(s_db);
    s_db = NULL;
    sqlite_mutex_unlock();
    return -1;
  }

  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_p1 ON nodes(P1)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_p3 ON nodes(P3)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_p4 ON nodes(P4)");
  exec_sql("CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)");

  /* Clean up orphaned FTS triggers/table left by older builds that used FTS.
   * The current schema does not use FTS; leftover triggers referencing a
   * missing nodes_fts table make every UPDATE/INSERT/DELETE fail at prepare
   * time ("no such table: main.nodes_fts" -> HTTP 503 "Update failed").
   * Drop them so the on-disk schema matches the current code. */
  exec_sql("DROP TRIGGER IF EXISTS nodes_au");
  exec_sql("DROP TRIGGER IF EXISTS nodes_ai");
  exec_sql("DROP TRIGGER IF EXISTS nodes_ad");
  exec_sql("DROP TABLE IF EXISTS nodes_fts");

  s_db_file_exists = file_exists(path);

  sqlite_mutex_unlock();
  return 0;
}

static void sqlite_cleanup(void) {
  sqlite_mutex_lock();
  if (s_db != NULL) {
    sqlite3_close(s_db);
    s_db = NULL;
  }
  sqlite_mutex_unlock();
}

static int sqlite_is_available(void) {
  sqlite_mutex_lock();
  int result = 0;
  if (s_db == NULL) {
    sqlite_mutex_unlock();
    return 0;
  }

  sqlite3_stmt *stmt;
  int rc = sqlite3_prepare_v2(s_db, "SELECT COUNT(*) FROM nodes", -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    sqlite_mutex_unlock();
    return 0;
  }

  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(stmt);
    sqlite_mutex_unlock();
    return 0;
  }

  int count = sqlite3_column_int(stmt, 0);
  sqlite3_finalize(stmt);
  result = count > 0;
  sqlite_mutex_unlock();
  return result;
}

static int sqlite_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (s_db == NULL || query == NULL || result == NULL) return -1;
  sqlite_mutex_lock();

  char where[MAX_WHERE_LEN] = "";
  struct param_list params;
  plist_init(&params);

  build_where_sql(query, where, MAX_WHERE_LEN, &params);

  char sql[MAX_SQL_LEN];
  sqlite3_stmt *stmt;
  int rc;

  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes");
  } else {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes WHERE %s", where);
  }

  rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s (sql: %s)\n", sqlite3_errmsg(s_db), sql);
    plist_free(&params);
    sqlite_mutex_unlock();
    return -1;
  }

  plist_bind(stmt, &params);
  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    fprintf(stderr, "SQL step error: %s (sql: %s)\n", sqlite3_errmsg(s_db), sql);
    sqlite3_finalize(stmt);
    plist_free(&params);
    sqlite_mutex_unlock();
    return -1;
  }
  result->total = sqlite3_column_int(stmt, 0);
  sqlite3_finalize(stmt);

  int offset = (query->page - 1) * query->pageSize;
  int bind_idx = params.count;

  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes ORDER BY id LIMIT ? OFFSET ?");
  } else {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes WHERE %s ORDER BY id LIMIT ? OFFSET ?", where);
  }

  rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s (sql: %s)\n", sqlite3_errmsg(s_db), sql);
    plist_free(&params);
    sqlite_mutex_unlock();
    return -1;
  }

  plist_bind(stmt, &params);
  sqlite3_bind_int(stmt, bind_idx + 1, query->pageSize);
  sqlite3_bind_int(stmt, bind_idx + 2, offset);

  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * (size_t) query->pageSize);
  if (result->nodes == NULL) {
    sqlite3_finalize(stmt);
    plist_free(&params);
    sqlite_mutex_unlock();
    return -1;
  }

  result->count = 0;
  while ((rc = sqlite3_step(stmt)) == SQLITE_ROW && result->count < query->pageSize) {
    struct ds_node *node = &result->nodes[result->count];
    const char *id = (const char *) sqlite3_column_text(stmt, 0);
    const char *name = (const char *) sqlite3_column_text(stmt, 1);
    const char *channelCode = (const char *) sqlite3_column_text(stmt, 2);
    const char *isOnline = (const char *) sqlite3_column_text(stmt, 3);
    const char *cameraType = (const char *) sqlite3_column_text(stmt, 4);
    const char *operation = (const char *) sqlite3_column_text(stmt, 5);
    const char *customOperation = (const char *) sqlite3_column_text(stmt, 6);
    const char *p1 = (const char *) sqlite3_column_text(stmt, 7);
    const char *p3 = (const char *) sqlite3_column_text(stmt, 8);
    const char *p4 = (const char *) sqlite3_column_text(stmt, 9);

    snprintf(node->id, sizeof(node->id), "%s", id ? id : "");
    snprintf(node->name, sizeof(node->name), "%s", name ? name : "");
    snprintf(node->channelCode, sizeof(node->channelCode), "%s", channelCode ? channelCode : "");
    snprintf(node->isOnline, sizeof(node->isOnline), "%s", isOnline ? isOnline : "");
    snprintf(node->cameraType, sizeof(node->cameraType), "%s", cameraType ? cameraType : "");
    snprintf(node->operation, sizeof(node->operation), "%s", operation ? operation : "");
    snprintf(node->customOperation, sizeof(node->customOperation), "%s", customOperation ? customOperation : "");
    snprintf(node->P1, sizeof(node->P1), "%s", p1 ? p1 : "");
    snprintf(node->P3, sizeof(node->P3), "%s", p3 ? p3 : "");
    snprintf(node->P4, sizeof(node->P4), "%s", p4 ? p4 : "");

    // 读取路径不使用这些标志位，初始化为 0 防止垃圾值
    node->has_operation = 0;
    node->has_customOperation = 0;

    result->count++;
  }

  sqlite3_finalize(stmt);
  plist_free(&params);
  sqlite_mutex_unlock();
  return 0;
}

static int sqlite_update_nodes(struct ds_node *nodes, int count) {
  if (s_db == NULL || nodes == NULL || count <= 0) return -1;
  sqlite_mutex_lock();

  int rc = sqlite3_exec(s_db, "BEGIN TRANSACTION", NULL, NULL, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "BEGIN TRANSACTION failed: %s\n", sqlite3_errmsg(s_db));
    sqlite_mutex_unlock();
    return -1;
  }

  // 按字段组合懒编译 3 种语句（只更新实际提供的字段，避免覆盖未提供的字段）
  sqlite3_stmt *stmt_both = NULL;  // operation + customOperation
  sqlite3_stmt *stmt_op   = NULL;  // operation only
  sqlite3_stmt *stmt_cop  = NULL;  // customOperation only

  for (int i = 0; i < count; i++) {
    struct ds_node *node = &nodes[i];
    int has_op  = node->has_operation;
    int has_cop = node->has_customOperation;

    // 两个字段都未提供，跳过该更新
    if (!has_op && !has_cop) continue;

    sqlite3_stmt *stmt = NULL;

    if (has_op && has_cop) {
      if (stmt_both == NULL) {
        rc = sqlite3_prepare_v2(s_db,
            "UPDATE nodes SET operation = ?, customOperation = ? WHERE id = ?",
            -1, &stmt_both, NULL);
        if (rc != SQLITE_OK) {
          fprintf(stderr, "SQL prepare error (both): %s\n", sqlite3_errmsg(s_db));
          goto fail;
        }
      }
      stmt = stmt_both;
      sqlite3_bind_text(stmt, 1, node->operation, -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(stmt, 2, node->customOperation, -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(stmt, 3, node->id, -1, SQLITE_TRANSIENT);
    } else if (has_op) {
      if (stmt_op == NULL) {
        rc = sqlite3_prepare_v2(s_db,
            "UPDATE nodes SET operation = ? WHERE id = ?",
            -1, &stmt_op, NULL);
        if (rc != SQLITE_OK) {
          fprintf(stderr, "SQL prepare error (op): %s\n", sqlite3_errmsg(s_db));
          goto fail;
        }
      }
      stmt = stmt_op;
      sqlite3_bind_text(stmt, 1, node->operation, -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(stmt, 2, node->id, -1, SQLITE_TRANSIENT);
    } else {  // has_cop only
      if (stmt_cop == NULL) {
        rc = sqlite3_prepare_v2(s_db,
            "UPDATE nodes SET customOperation = ? WHERE id = ?",
            -1, &stmt_cop, NULL);
        if (rc != SQLITE_OK) {
          fprintf(stderr, "SQL prepare error (cop): %s\n", sqlite3_errmsg(s_db));
          goto fail;
        }
      }
      stmt = stmt_cop;
      sqlite3_bind_text(stmt, 1, node->customOperation, -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(stmt, 2, node->id, -1, SQLITE_TRANSIENT);
    }

    rc = sqlite3_step(stmt);
    if (rc != SQLITE_DONE) {
      fprintf(stderr, "SQL step error: %s\n", sqlite3_errmsg(s_db));
      goto fail;
    }
    sqlite3_reset(stmt);
    sqlite3_clear_bindings(stmt);
  }

  if (stmt_both) sqlite3_finalize(stmt_both);
  if (stmt_op) sqlite3_finalize(stmt_op);
  if (stmt_cop) sqlite3_finalize(stmt_cop);

  // 检查 COMMIT 返回值，失败时回滚以防止事务泄漏
  rc = sqlite3_exec(s_db, "COMMIT", NULL, NULL, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "COMMIT failed: %s\n", sqlite3_errmsg(s_db));
    sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
    sqlite_mutex_unlock();
    return -1;
  }
  sqlite_mutex_unlock();
  return 0;

fail:
  if (stmt_both) sqlite3_finalize(stmt_both);
  if (stmt_op) sqlite3_finalize(stmt_op);
  if (stmt_cop) sqlite3_finalize(stmt_cop);
  sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
  sqlite_mutex_unlock();
  return -1;
}

const struct ds_driver sqlite_driver = {
    "sqlite",
    sqlite_init,
    sqlite_cleanup,
    sqlite_is_available,
    sqlite_get_nodes,
    sqlite_update_nodes,
};
