#include "data_source.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <sys/stat.h>
#include <time.h>

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#else
#include <pthread.h>
#endif

#if !defined(CSV_MODE)
#include "sqlite3.h"
#endif

#define MAX_SQL_LEN           2048
#define MAX_WHERE_LEN         1024
#define MAX_BIND_PARAMS        64
#define MAX_TOKENS_PER_FILTER  16
#define MAX_NODE_BUFFER_SIZE  1024

// Cross-platform mutex
#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION s_sqlite_mutex;
static CRITICAL_SECTION s_csv_mutex;
static int s_mutexes_initialized = 0;

static void mutex_init(CRITICAL_SECTION *m) {
  (void) m;
  if (!s_mutexes_initialized) {
    InitializeCriticalSection(&s_sqlite_mutex);
    InitializeCriticalSection(&s_csv_mutex);
    s_mutexes_initialized = 1;
  }
}
static void mutex_lock(CRITICAL_SECTION *m) { EnterCriticalSection(m); }
static void mutex_unlock(CRITICAL_SECTION *m) { LeaveCriticalSection(m); }
#else
static pthread_mutex_t s_sqlite_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t s_csv_mutex = PTHREAD_MUTEX_INITIALIZER;
static void mutex_init(pthread_mutex_t *m) { (void)m; }
static void mutex_lock(pthread_mutex_t *m) { pthread_mutex_lock(m); }
static void mutex_unlock(pthread_mutex_t *m) { pthread_mutex_unlock(m); }
#endif

#if !defined(CSV_MODE)
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

#endif

#if defined(CSV_MODE)
static void csv_escape_field(const char *src, char *dst, int dst_len) {
  if (src == NULL || dst == NULL || dst_len <= 0) return;
  int needs_quote = 0;
  for (const char *s = src; *s != '\0'; s++) {
    if (*s == ',' || *s == '"' || *s == '\n' || *s == '\r') {
      needs_quote = 1;
      break;
    }
  }
  if (!needs_quote) {
    strncpy(dst, src, dst_len - 1);
    dst[dst_len - 1] = '\0';
    return;
  }
  int pos = 0;
  if (pos < dst_len - 1) dst[pos++] = '"';
  for (const char *s = src; *s != '\0' && pos < dst_len - 1; s++) {
    if (*s == '"') {
      if (pos < dst_len - 1) dst[pos++] = '"';
      if (pos < dst_len - 1) dst[pos++] = '"';
    } else {
      dst[pos++] = *s;
    }
  }
  if (pos < dst_len - 1) dst[pos++] = '"';
  dst[pos] = '\0';
}
#endif

#if !defined(CSV_MODE)
static int file_exists(const char *path) {
  FILE *f = fopen(path, "rb");
  if (f) {
    fclose(f);
    return 1;
  }
  return 0;
}

int ds_init(const char *path) {
  mutex_init(&s_sqlite_mutex);
  mutex_init(&s_csv_mutex);

  mutex_lock(&s_sqlite_mutex);
  int rc = sqlite3_open(path, &s_db);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "Cannot open database: %s\n", sqlite3_errmsg(s_db));
    sqlite3_close(s_db);
    s_db = NULL;
    mutex_unlock(&s_sqlite_mutex);
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
    mutex_unlock(&s_sqlite_mutex);
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

  mutex_unlock(&s_sqlite_mutex);
  return 0;
}

void ds_cleanup(void) {
  mutex_lock(&s_sqlite_mutex);
  if (s_db != NULL) {
    sqlite3_close(s_db);
    s_db = NULL;
  }
  mutex_unlock(&s_sqlite_mutex);
}

int ds_is_available(void) {
  mutex_lock(&s_sqlite_mutex);
  int result = 0;
  if (s_db == NULL) {
    mutex_unlock(&s_sqlite_mutex);
    return 0;
  }

  sqlite3_stmt *stmt;
  int rc = sqlite3_prepare_v2(s_db, "SELECT COUNT(*) FROM nodes", -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    mutex_unlock(&s_sqlite_mutex);
    return 0;
  }

  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(stmt);
    mutex_unlock(&s_sqlite_mutex);
    return 0;
  }

  int count = sqlite3_column_int(stmt, 0);
  sqlite3_finalize(stmt);
  result = count > 0;
  mutex_unlock(&s_sqlite_mutex);
  return result;
}

int ds_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (s_db == NULL || query == NULL || result == NULL) return -1;
  mutex_lock(&s_sqlite_mutex);

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
    mutex_unlock(&s_sqlite_mutex);
    return -1;
  }

  plist_bind(stmt, &params);
  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    fprintf(stderr, "SQL step error: %s (sql: %s)\n", sqlite3_errmsg(s_db), sql);
    sqlite3_finalize(stmt);
    plist_free(&params);
    mutex_unlock(&s_sqlite_mutex);
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
    mutex_unlock(&s_sqlite_mutex);
    return -1;
  }

  plist_bind(stmt, &params);
  sqlite3_bind_int(stmt, bind_idx + 1, query->pageSize);
  sqlite3_bind_int(stmt, bind_idx + 2, offset);

  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * (size_t) query->pageSize);
  if (result->nodes == NULL) {
    sqlite3_finalize(stmt);
    plist_free(&params);
    mutex_unlock(&s_sqlite_mutex);
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
  mutex_unlock(&s_sqlite_mutex);
  return 0;
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (s_db == NULL || nodes == NULL || count <= 0) return -1;
  mutex_lock(&s_sqlite_mutex);

  int rc = sqlite3_exec(s_db, "BEGIN TRANSACTION", NULL, NULL, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "BEGIN TRANSACTION failed: %s\n", sqlite3_errmsg(s_db));
    mutex_unlock(&s_sqlite_mutex);
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
    mutex_unlock(&s_sqlite_mutex);
    return -1;
  }
  mutex_unlock(&s_sqlite_mutex);
  return 0;

fail:
  if (stmt_both) sqlite3_finalize(stmt_both);
  if (stmt_op) sqlite3_finalize(stmt_op);
  if (stmt_cop) sqlite3_finalize(stmt_cop);
  sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
  mutex_unlock(&s_sqlite_mutex);
  return -1;
}

#else

static char *g_csv_path = NULL;
static int get_csv_field(char *line, int index, char *buf, int buf_len);
static void csv_escape_field(const char *src, char *dst, int dst_len);

#define MAX_CACHE_SIZE 50000
#define CACHE_FIELD_SIZE 512

typedef struct {
  char id[CACHE_FIELD_SIZE];
  char name[CACHE_FIELD_SIZE];
  char channelCode[CACHE_FIELD_SIZE];
  char isOnline[CACHE_FIELD_SIZE];
  char cameraType[CACHE_FIELD_SIZE];
  char operation[CACHE_FIELD_SIZE];
  char customOperation[CACHE_FIELD_SIZE];
  char P4[CACHE_FIELD_SIZE];
} csv_node_t;

static csv_node_t *g_cache = NULL;
static int g_cache_count = 0;
static time_t g_cache_mtime = 0;

static int load_csv_cache(void) {
  if (g_csv_path == NULL) return -1;

  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) return -1;

  struct stat st;
  if (stat(g_csv_path, &st) == 0 && st.st_mtime == g_cache_mtime && g_cache != NULL) {
    fclose(fp);
    return 0;
  }

  if (g_cache != NULL) {
    free(g_cache);
    g_cache = NULL;
  }

  g_cache = malloc(MAX_CACHE_SIZE * sizeof(csv_node_t));
  if (g_cache == NULL) {
    fclose(fp);
    return -1;
  }

  char line[4096];
  int header_count = 0;
  int id_idx = -1, name_idx = -1, channelCode_idx = -1;
  int isOnline_idx = -1, cameraType_idx = -1, operation_idx = -1, customOperation_idx = -1;
  int P4_idx = -1;

  if (fgets(line, sizeof(line), fp) == NULL) {
    fclose(fp);
    return -1;
  }

  size_t header_len = strlen(line);
  while (header_len > 0 && (line[header_len-1] == '\n' || line[header_len-1] == '\r')) {
    line[--header_len] = '\0';
  }

  char *h = line;
  if ((unsigned char) h[0] == 0xEF && (unsigned char) h[1] == 0xBB && (unsigned char) h[2] == 0xBF) {
    h += 3;
  }

  while (*h != '\0' && *h != '\n' && header_count < 32) {
    char *end = h;
    while (*end != '\0' && *end != ',' && *end != '\n' && *end != '\r') end++;
    int len = (int) (end - h);
    if (len > 0) {
      char header[64];
      strncpy(header, h, len);
      header[len] = '\0';
      if (strcmp(header, "id") == 0) id_idx = header_count;
      else if (strcmp(header, "name") == 0) name_idx = header_count;
      else if (strcmp(header, "channelCode") == 0) channelCode_idx = header_count;
      else if (strcmp(header, "isOnline") == 0) isOnline_idx = header_count;
      else if (strcmp(header, "cameraType") == 0) cameraType_idx = header_count;
      else if (strcmp(header, "operation") == 0) operation_idx = header_count;
      else if (strcmp(header, "customOperation") == 0) customOperation_idx = header_count;
      else if (strcmp(header, "P4") == 0) P4_idx = header_count;
      header_count++;
    }
    while ((*end == ',' || *end == '\n' || *end == '\r') && *end != '\0') end++;
    h = end;
  }

  g_cache_count = 0;
  while (fgets(line, sizeof(line), fp) != NULL && g_cache_count < MAX_CACHE_SIZE) {
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
      line[--len] = '\0';
    }

    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0') continue;

    csv_node_t *node = &g_cache[g_cache_count];
    node->id[0] = '\0';
    node->name[0] = '\0';
    node->channelCode[0] = '\0';
    node->isOnline[0] = '\0';
    node->cameraType[0] = '\0';
    node->operation[0] = '\0';
    node->customOperation[0] = '\0';
    node->P4[0] = '\0';

    if (id_idx >= 0) get_csv_field(line, id_idx, node->id, sizeof(node->id));
    if (name_idx >= 0) get_csv_field(line, name_idx, node->name, sizeof(node->name));
    if (channelCode_idx >= 0) get_csv_field(line, channelCode_idx, node->channelCode, sizeof(node->channelCode));
    if (isOnline_idx >= 0) get_csv_field(line, isOnline_idx, node->isOnline, sizeof(node->isOnline));
    if (cameraType_idx >= 0) get_csv_field(line, cameraType_idx, node->cameraType, sizeof(node->cameraType));
    if (operation_idx >= 0) get_csv_field(line, operation_idx, node->operation, sizeof(node->operation));
    if (customOperation_idx >= 0) get_csv_field(line, customOperation_idx, node->customOperation, sizeof(node->customOperation));
    if (P4_idx >= 0) get_csv_field(line, P4_idx, node->P4, sizeof(node->P4));

    if (node->id[0] != '\0') g_cache_count++;
  }

  fclose(fp);
  stat(g_csv_path, &st);
  g_cache_mtime = st.st_mtime;

  return 0;
}

static int get_csv_field(char *line, int index, char *buf, int buf_len) {
  char *p = line;
  for (int i = 0; i < index; i++) {
    if (*p == '"') {
      p++;
      int in_quotes = 1;
      while (*p != '\0' && in_quotes) {
        if (*p == '"') {
          if (*(p + 1) == '"') {
            p += 2;
          } else {
            in_quotes = 0;
            p++;
          }
        } else {
          p++;
        }
      }
    } else {
      while (*p != '\0' && *p != ',' && *p != '\n' && *p != '\r') p++;
    }
    if (*p == ',') p++;
    if (*p == '\0' || *p == '\n' || *p == '\r') return -1;
  }
  int len = 0;
  if (*p == '"') {
    p++;
    while (*p != '\0' && *p != '\n' && *p != '\r' && len < buf_len - 1) {
      if (*p == '"') {
        if (*(p + 1) == '"') {
          if ((unsigned char)*p >= 0x20) buf[len++] = *p;
          p += 2;
        } else {
          p++;
          break;
        }
      } else {
        if ((unsigned char)*p >= 0x20) buf[len++] = *p;
        p++;
      }
    }
  } else {
    while (*p != '\0' && *p != ',' && *p != '\n' && *p != '\r' && len < buf_len - 1) {
      if ((unsigned char)*p >= 0x20) buf[len++] = *p;
      p++;
    }
  }
  buf[len] = '\0';
  return 0;
}

static int is_value_in_list(const char *value, const char *list) {
  if (value == NULL || list == NULL || *list == '\0') return 0;
  char *copy = strdup(list);
  if (copy == NULL) return 0;
  char *token = strtok(copy, ",");
  while (token != NULL) {
    if (strcmp(token, value) == 0) {
      free(copy);
      return 1;
    }
    token = strtok(NULL, ",");
  }
  free(copy);
  return 0;
}

int ds_init(const char *path) {
  mutex_init(&s_sqlite_mutex);
  mutex_init(&s_csv_mutex);

  if (path == NULL) return -1;
  g_csv_path = strdup(path);

  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) {
    fprintf(stderr, "Warning: CSV file %s not found\n", g_csv_path);
  } else {
    fclose(fp);
  }
  return 0;
}

void ds_cleanup(void) {
  mutex_lock(&s_csv_mutex);
  if (g_csv_path != NULL) {
    free(g_csv_path);
    g_csv_path = NULL;
  }
  mutex_unlock(&s_csv_mutex);
}

int ds_is_available(void) {
  mutex_lock(&s_csv_mutex);
  int result = 0;
  if (g_csv_path == NULL) {
    mutex_unlock(&s_csv_mutex);
    return 0;
  }
  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) {
    mutex_unlock(&s_csv_mutex);
    return 0;
  }
  fclose(fp);
  result = 1;
  mutex_unlock(&s_csv_mutex);
  return result;
}

static int contains_ignore_case(const char *haystack, const char *needle) {
  if (!haystack || !needle || !*needle) return 1;
  while (*haystack) {
    const char *h = haystack, *n = needle;
    while (*h && *n && (tolower((unsigned char)*h) == tolower((unsigned char)*n))) {
      h++; n++;
    }
    if (!*n) return 1;
    haystack++;
  }
  return 0;
}

int ds_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (g_csv_path == NULL || query == NULL || result == NULL) return -1;
  mutex_lock(&s_csv_mutex);

  if (load_csv_cache() != 0) {
    mutex_unlock(&s_csv_mutex);
    return -1;
  }

  int skip_count = (query->page - 1) * query->pageSize;
  int total_count = 0;
  int page_start_idx = -1;
  int has_keyword = (query->keyword != NULL && query->keyword[0] != '\0');

  for (int i = 0; i < g_cache_count; i++) {
    csv_node_t *node = &g_cache[i];

    if (query->isOnline != NULL) {
      if (query->isOnline[0] == '\0') continue;
      if (!is_value_in_list(node->isOnline, query->isOnline)) continue;
    }

    if (query->cameraType != NULL) {
      if (query->cameraType[0] == '\0') continue;
      if (!is_value_in_list(node->cameraType, query->cameraType)) continue;
    }

    if (query->operation != NULL) {
      if (query->operation[0] == '\0') continue;
      if (is_value_in_list("0", query->operation) && node->operation[0] == '\0') {
      } else if (!is_value_in_list(node->operation, query->operation)) {
        continue;
      }
    }

    if (has_keyword) {
      if (!contains_ignore_case(node->name, query->keyword) &&
          !contains_ignore_case(node->P4, query->keyword)) continue;
    }

    if (total_count == skip_count) {
      page_start_idx = i;
    }
    total_count++;
  }

  result->total = total_count;
  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * (size_t) query->pageSize);
  if (result->nodes == NULL) {
    mutex_unlock(&s_csv_mutex);
    return -1;
  }

  result->count = 0;
  if (page_start_idx >= 0) {
    for (int i = page_start_idx; i < g_cache_count && result->count < query->pageSize; i++) {
      csv_node_t *node = &g_cache[i];

      if (query->isOnline != NULL) {
        if (query->isOnline[0] == '\0') continue;
        if (!is_value_in_list(node->isOnline, query->isOnline)) continue;
      }

      if (query->cameraType != NULL) {
        if (query->cameraType[0] == '\0') continue;
        if (!is_value_in_list(node->cameraType, query->cameraType)) continue;
      }

      if (query->operation != NULL) {
        if (query->operation[0] == '\0') continue;
        if (is_value_in_list("0", query->operation) && node->operation[0] == '\0') {
        } else if (!is_value_in_list(node->operation, query->operation)) {
          continue;
        }
      }

      if (has_keyword) {
        if (!contains_ignore_case(node->name, query->keyword) &&
            !contains_ignore_case(node->P4, query->keyword)) continue;
      }

      struct ds_node *result_node = &result->nodes[result->count];
      strncpy(result_node->id, node->id, sizeof(result_node->id) - 1);
      result_node->id[sizeof(result_node->id) - 1] = '\0';
      strncpy(result_node->name, node->name, sizeof(result_node->name) - 1);
      result_node->name[sizeof(result_node->name) - 1] = '\0';
      strncpy(result_node->channelCode, node->channelCode, sizeof(result_node->channelCode) - 1);
      result_node->channelCode[sizeof(result_node->channelCode) - 1] = '\0';
      strncpy(result_node->isOnline, node->isOnline, sizeof(result_node->isOnline) - 1);
      result_node->isOnline[sizeof(result_node->isOnline) - 1] = '\0';
      strncpy(result_node->cameraType, node->cameraType, sizeof(result_node->cameraType) - 1);
      result_node->cameraType[sizeof(result_node->cameraType) - 1] = '\0';
      strncpy(result_node->operation, node->operation, sizeof(result_node->operation) - 1);
      result_node->operation[sizeof(result_node->operation) - 1] = '\0';
      strncpy(result_node->customOperation, node->customOperation, sizeof(result_node->customOperation) - 1);
      result_node->customOperation[sizeof(result_node->customOperation) - 1] = '\0';
      result_node->P1[0] = '\0';
      result_node->P3[0] = '\0';
      strncpy(result_node->P4, node->P4, sizeof(result_node->P4) - 1);
      result_node->P4[sizeof(result_node->P4) - 1] = '\0';

      // 读取路径不使用这些标志位，初始化为 0 防止垃圾值
      result_node->has_operation = 0;
      result_node->has_customOperation = 0;

      result->count++;
    }
  }

  mutex_unlock(&s_csv_mutex);
  return 0;
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (g_csv_path == NULL || nodes == NULL || count <= 0) return -1;
  mutex_lock(&s_csv_mutex);

  FILE *fp_in = fopen(g_csv_path, "rb");
  if (fp_in == NULL) {
    mutex_unlock(&s_csv_mutex);
    return -1;
  }

  char edited_path[256];
  snprintf(edited_path, sizeof(edited_path), "%s.tmp", g_csv_path);
  FILE *fp_out = fopen(edited_path, "w");
  if (fp_out == NULL) {
    fclose(fp_in);
    mutex_unlock(&s_csv_mutex);
    return -1;
  }

  char line[4096];
  int operation_idx = -1;
  int custom_operation_idx = -1;
  int header_count = 0;
  int has_operation_col = 0;
  int has_custom_col = 0;

  if (fgets(line, sizeof(line), fp_in) != NULL) {
    int had_newline = 0;
    size_t header_len = strlen(line);
    while (header_len > 0 && (line[header_len-1] == '\n' || line[header_len-1] == '\r')) {
      line[--header_len] = '\0';
      had_newline = 1;
    }

    char new_header[1024] = "";
    strncpy(new_header, line, sizeof(new_header) - 1);
    new_header[sizeof(new_header) - 1] = '\0';

    char *h = line;
    if ((unsigned char) h[0] == 0xEF && (unsigned char) h[1] == 0xBB && (unsigned char) h[2] == 0xBF) {
      h += 3;
    }
    while (*h != '\0' && *h != '\n' && header_count < 32) {
      char *end = h;
      while (*end != '\0' && *end != ',' && *end != '\n' && *end != '\r') end++;
      int len = (int) (end - h);
      if (len > 0) {
        char header[64];
        strncpy(header, h, len);
        header[len] = '\0';
        if (strcmp(header, "operation") == 0) {
          operation_idx = header_count;
          has_operation_col = 1;
        }
        if (strcmp(header, "customOperation") == 0) {
          custom_operation_idx = header_count;
          has_custom_col = 1;
        }
        header_count++;
      }
      while ((*end == ',' || *end == '\n' || *end == '\r') && *end != '\0') end++;
      h = end;
    }

    if (!has_operation_col) {
      int cur_len = (int) strlen(new_header);
      if (cur_len > 0 && new_header[cur_len-1] != ',') {
        strncat(new_header, ",", sizeof(new_header) - (size_t)cur_len - 1);
      }
      strncat(new_header, "operation", sizeof(new_header) - strlen(new_header) - 1);
      header_count++;
      operation_idx = header_count - 1;
    }
    if (!has_custom_col) {
      int cur_len = (int) strlen(new_header);
      if (cur_len > 0 && new_header[cur_len-1] != ',') {
        strncat(new_header, ",", sizeof(new_header) - (size_t)cur_len - 1);
      }
      strncat(new_header, "customOperation", sizeof(new_header) - strlen(new_header) - 1);
      header_count++;
      custom_operation_idx = header_count - 1;
    }
    if (had_newline) {
      strncat(new_header, "\n", sizeof(new_header) - strlen(new_header) - 1);
    }
    fprintf(fp_out, "%s", new_header);
  }

  while (fgets(line, sizeof(line), fp_in) != NULL) {
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
      line[--len] = '\0';
    }

    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0') {
      continue;
    }

    char row_id[64] = "";
    get_csv_field(p, 0, row_id, sizeof(row_id));

    int match_idx = -1;
    for (int i = 0; i < count; i++) {
      if (strcmp(row_id, nodes[i].id) == 0) {
        match_idx = i;
        break;
      }
    }

    if (match_idx >= 0) {
      char new_line[4096] = "";
      int first = 1;
      for (int i = 0; i < header_count; i++) {
        if (!first) strncat(new_line, ",", sizeof(new_line) - strlen(new_line) - 1);
        first = 0;
        char escaped[512] = "";
        if (i == operation_idx) {
          if (nodes[match_idx].has_operation) {
            csv_escape_field(nodes[match_idx].operation, escaped, sizeof(escaped));
          } else {
            char field_val[512] = "";
            get_csv_field(p, i, field_val, sizeof(field_val));
            csv_escape_field(field_val, escaped, sizeof(escaped));
          }
        } else if (i == custom_operation_idx) {
          if (nodes[match_idx].has_customOperation) {
            csv_escape_field(nodes[match_idx].customOperation, escaped, sizeof(escaped));
          } else {
            char field_val[512] = "";
            get_csv_field(p, i, field_val, sizeof(field_val));
            csv_escape_field(field_val, escaped, sizeof(escaped));
          }
        } else {
          char field_val[512] = "";
          get_csv_field(p, i, field_val, sizeof(field_val));
          size_t fv_len = strlen(field_val);
          while (fv_len > 0 && (field_val[fv_len-1] == '\n' || field_val[fv_len-1] == '\r')) {
            field_val[--fv_len] = '\0';
          }
          csv_escape_field(field_val, escaped, sizeof(escaped));
        }
        strncat(new_line, escaped, sizeof(new_line) - strlen(new_line) - 1);
      }
      fprintf(fp_out, "%s\n", new_line);
    } else {
      if (!has_operation_col || !has_custom_col) {
        char new_line[4096] = "";
        int first = 1;
        for (int i = 0; i < header_count; i++) {
          if (!first) strncat(new_line, ",", sizeof(new_line) - strlen(new_line) - 1);
          first = 0;
          char escaped[512] = "";
          if (i == operation_idx && !has_operation_col) {
            csv_escape_field("", escaped, sizeof(escaped));
          } else if (i == custom_operation_idx && !has_custom_col) {
            csv_escape_field("", escaped, sizeof(escaped));
          } else {
            char field_val[512] = "";
            get_csv_field(p, i, field_val, sizeof(field_val));
            size_t fv_len = strlen(field_val);
            while (fv_len > 0 && (field_val[fv_len-1] == '\n' || field_val[fv_len-1] == '\r')) {
              field_val[--fv_len] = '\0';
            }
            csv_escape_field(field_val, escaped, sizeof(escaped));
          }
          strncat(new_line, escaped, sizeof(new_line) - strlen(new_line) - 1);
        }
        fprintf(fp_out, "%s\n", new_line);
      } else {
        fprintf(fp_out, "%s\n", line);
      }
    }
  }

  fclose(fp_in);
  fclose(fp_out);

#if defined(_WIN32) || defined(_WIN64)
  if (!MoveFileEx(edited_path, g_csv_path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
    remove(g_csv_path);
    rename(edited_path, g_csv_path);
  }
#else
  remove(g_csv_path);
  rename(edited_path, g_csv_path);
#endif

  if (g_cache != NULL) {
    free(g_cache);
    g_cache = NULL;
  }
  g_cache_count = 0;
  g_cache_mtime = 0;

  mutex_unlock(&s_csv_mutex);
  return 0;
}

#endif
