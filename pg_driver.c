#include "data_source.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#else
#include <pthread.h>
#endif

#include "libpq-fe.h"

#define MAX_SQL_LEN           2048
#define MAX_WHERE_LEN         1024
#define MAX_BIND_PARAMS        64
#define MAX_TOKENS_PER_FILTER  16
#define MAX_NODE_BUFFER_SIZE  1024

// Cross-platform mutex (same model as sqlite_driver: one global connection
// guarded by a single mutex; libpq connections are not thread-safe)
#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION s_pg_mutex;
static int s_mutexes_initialized = 0;

static void pg_mutex_init(void) {
  if (!s_mutexes_initialized) {
    InitializeCriticalSection(&s_pg_mutex);
    s_mutexes_initialized = 1;
  }
}
static void pg_mutex_lock(void) { EnterCriticalSection(&s_pg_mutex); }
static void pg_mutex_unlock(void) { LeaveCriticalSection(&s_pg_mutex); }
#else
static pthread_mutex_t s_pg_mutex = PTHREAD_MUTEX_INITIALIZER;
static void pg_mutex_init(void) { (void)0; }
static void pg_mutex_lock(void) { pthread_mutex_lock(&s_pg_mutex); }
static void pg_mutex_unlock(void) { pthread_mutex_unlock(&s_pg_mutex); }
#endif

static PGconn *s_conn = NULL;

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

// 返回本次参数的 1-based 序号（即 $n 的 n），失败返回 -1。
// 参数添加顺序与占位符编号天然单调一致，调用处用返回值拼 "$n"。
static int plist_add_text(struct param_list *pl, const char *text) {
  if (pl->count >= MAX_BIND_PARAMS) return -1;
  pl->values[pl->count] = text ? strdup(text) : NULL;
  pl->lens[pl->count] = text ? (int) strlen(text) : 0;
  pl->is_null[pl->count] = (text == NULL);
  pl->count++;
  return pl->count;
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

// PQexecParams 的参数值直接来自 param_list（text 格式，NULL 元素即 SQL NULL）
static PGresult *pg_exec_params_locked(const char *sql,
                                       const struct param_list *pl) {
  return PQexecParams(s_conn, sql, pl->count, NULL,
                      (const char *const *) pl->values, NULL, NULL, 0);
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
    int idx = plist_add_text(params, values);
    if (idx < 0) return pos;
    pos += snprintf(where + pos, (size_t)(where_len - pos), "%s%s = $%d",
                    pos > 0 ? " AND " : "", col, idx);
  } else {
    pos += snprintf(where + pos, (size_t)(where_len - pos), "%s%s IN (",
                    pos > 0 ? " AND " : "", col);
    char *copy = strdup(values);
    char *saveptr = NULL;
    char *t = strtok_r(copy, ",", &saveptr);
    int idx = 0;
    while (t != NULL && idx < n) {
      int pidx = plist_add_text(params, t);
      if (pidx > 0) {
        if (idx > 0) pos += snprintf(where + pos, (size_t)(where_len - pos), ", ");
        pos += snprintf(where + pos, (size_t)(where_len - pos), "$%d", pidx);
        idx++;
      }
      t = strtok_r(NULL, ",", &saveptr);
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
          int pidx = plist_add_text(params, t);
          if (pidx > 0) {
            pos += snprintf(where + pos, (size_t)(where_len - pos), "operation = $%d", pidx);
          }
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

    int base = params->count + 1;
    plist_add_text(params, like_pattern);
    plist_add_text(params, like_pattern);
    plist_add_text(params, like_pattern);
    plist_add_text(params, like_pattern);
    plist_add_text(params, like_pattern);

    pos += snprintf(where + pos, (size_t)(where_len - pos),
                     "%s(id LIKE $%d OR name LIKE $%d OR channelCode LIKE $%d"
                     " OR P1 LIKE $%d OR P4 LIKE $%d)",
                     pos > 0 ? " AND " : "",
                     base, base + 1, base + 2, base + 3, base + 4);
  }

  return pos;
}

static int pg_init(const char *conninfo) {
  pg_mutex_init();

  pg_mutex_lock();
  // 空连接串 → libpq 回退标准 PG* 环境变量 / 默认（localhost:5432, 同名库和用户）
  s_conn = PQconnectdb(conninfo != NULL ? conninfo : "");
  if (s_conn == NULL || PQstatus(s_conn) != CONNECTION_OK) {
    fprintf(stderr, "Cannot connect to PostgreSQL: %s\n",
            s_conn != NULL ? PQerrorMessage(s_conn) : "out of memory");
    if (s_conn != NULL) PQfinish(s_conn);
    s_conn = NULL;
    pg_mutex_unlock();
    return -1;
  }

  // 服务器 JSON/SQLite 路径均为 UTF-8，统一客户端编码避免 CJK 乱码
  PGresult *res = PQexec(s_conn, "SET client_encoding TO 'UTF8'");
  if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
    fprintf(stderr, "SET client_encoding failed: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    PQfinish(s_conn);
    s_conn = NULL;
    pg_mutex_unlock();
    return -1;
  }
  PQclear(res);

  // 表结构与 sqlite_driver 完全同构（10 列 TEXT，id 主键）。
  // 不带引号的标识符被 PG 折叠为小写——所有查询同样不带引号，
  // 读取按索引取列，大小写无影响。
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

  res = PQexec(s_conn, create_table);
  if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
    fprintf(stderr, "CREATE TABLE failed: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    PQfinish(s_conn);
    s_conn = NULL;
    pg_mutex_unlock();
    return -1;
  }
  PQclear(res);

  static const char *indexes[] = {
      "CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_p1 ON nodes(P1)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_p3 ON nodes(P3)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_p4 ON nodes(P4)",
      "CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)",
  };
  for (size_t i = 0; i < sizeof(indexes) / sizeof(indexes[0]); i++) {
    res = PQexec(s_conn, indexes[i]);
    if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
      fprintf(stderr, "CREATE INDEX failed: %s (%s)\n",
              PQerrorMessage(s_conn), indexes[i]);
      PQclear(res);
      PQfinish(s_conn);
      s_conn = NULL;
      pg_mutex_unlock();
      return -1;
    }
    PQclear(res);
  }

  pg_mutex_unlock();
  return 0;
}

static void pg_cleanup(void) {
  pg_mutex_lock();
  if (s_conn != NULL) {
    PQfinish(s_conn);
    s_conn = NULL;
  }
  pg_mutex_unlock();
}

static int pg_is_available(void) {
  pg_mutex_lock();
  int result = 0;
  if (s_conn == NULL) {
    pg_mutex_unlock();
    return 0;
  }

  PGresult *res = PQexec(s_conn, "SELECT COUNT(*) FROM nodes");
  if (res == NULL || PQresultStatus(res) != PGRES_TUPLES_OK ||
      PQntuples(res) < 1) {
    PQclear(res);
    pg_mutex_unlock();
    return 0;
  }

  int count = atoi(PQgetvalue(res, 0, 0));
  PQclear(res);
  result = count > 0;
  pg_mutex_unlock();
  return result;
}

static int pg_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (s_conn == NULL || query == NULL || result == NULL) return -1;
  pg_mutex_lock();

  char where[MAX_WHERE_LEN] = "";
  struct param_list params;
  plist_init(&params);

  build_where_sql(query, where, MAX_WHERE_LEN, &params);

  char sql[MAX_SQL_LEN];
  PGresult *res;
  int rc_ok;

  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes");
  } else {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes WHERE %s", where);
  }

  res = pg_exec_params_locked(sql, &params);
  rc_ok = (res != NULL && PQresultStatus(res) == PGRES_TUPLES_OK &&
           PQntuples(res) >= 1);
  if (!rc_ok) {
    fprintf(stderr, "PG count error: %s (sql: %s)\n",
            s_conn != NULL ? PQerrorMessage(s_conn) : "no connection", sql);
    PQclear(res);
    plist_free(&params);
    pg_mutex_unlock();
    return -1;
  }
  result->total = atoi(PQgetvalue(res, 0, 0));
  PQclear(res);

  int offset = (query->page - 1) * query->pageSize;

  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql),
             "SELECT id, name, channelCode, isOnline, cameraType, operation,"
             " customOperation, P1, P3, P4 FROM nodes"
             " ORDER BY id LIMIT $%d OFFSET $%d",
             params.count + 1, params.count + 2);
  } else {
    snprintf(sql, sizeof(sql),
             "SELECT id, name, channelCode, isOnline, cameraType, operation,"
             " customOperation, P1, P3, P4 FROM nodes WHERE %s"
             " ORDER BY id LIMIT $%d OFFSET $%d",
             where, params.count + 1, params.count + 2);
  }

  // 分页整数以文本格式绑定（libpq text 协议无整型直传）
  char limit_buf[16], offset_buf[16];
  snprintf(limit_buf, sizeof(limit_buf), "%d", query->pageSize);
  snprintf(offset_buf, sizeof(offset_buf), "%d", offset);
  plist_add_text(&params, limit_buf);
  plist_add_text(&params, offset_buf);

  res = pg_exec_params_locked(sql, &params);
  rc_ok = (res != NULL && PQresultStatus(res) == PGRES_TUPLES_OK);
  if (!rc_ok) {
    fprintf(stderr, "PG select error: %s (sql: %s)\n",
            s_conn != NULL ? PQerrorMessage(s_conn) : "no connection", sql);
    PQclear(res);
    plist_free(&params);
    pg_mutex_unlock();
    return -1;
  }

  int ntuples = PQntuples(res);
  if (ntuples > query->pageSize) ntuples = query->pageSize;

  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * (size_t) query->pageSize);
  if (result->nodes == NULL) {
    PQclear(res);
    plist_free(&params);
    pg_mutex_unlock();
    return -1;
  }

  result->count = 0;
  for (int row = 0; row < ntuples && result->count < query->pageSize; row++) {
    struct ds_node *node = &result->nodes[result->count];
    // 按显式列清单的索引读取（0-9），与 SELECT 列顺序一一对应
    const char *id = PQgetvalue(res, row, 0);
    const char *name = PQgetvalue(res, row, 1);
    const char *channelCode = PQgetvalue(res, row, 2);
    const char *isOnline = PQgetvalue(res, row, 3);
    const char *cameraType = PQgetvalue(res, row, 4);
    const char *operation = PQgetvalue(res, row, 5);
    const char *customOperation = PQgetvalue(res, row, 6);
    const char *p1 = PQgetvalue(res, row, 7);
    const char *p3 = PQgetvalue(res, row, 8);
    const char *p4 = PQgetvalue(res, row, 9);

    // libpq 对 NULL 值返回空串，与 sqlite 路径 NULL→"" 兜底语义一致
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

  PQclear(res);
  plist_free(&params);
  pg_mutex_unlock();
  return 0;
}

static int pg_update_nodes(struct ds_node *nodes, int count) {
  if (s_conn == NULL || nodes == NULL || count <= 0) return -1;
  pg_mutex_lock();

  PGresult *res = PQexec(s_conn, "BEGIN");
  if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
    fprintf(stderr, "BEGIN failed: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    pg_mutex_unlock();
    return -1;
  }
  PQclear(res);

  // 按字段组合选择 3 种语句（只更新实际提供的字段，避免覆盖未提供的字段）
  static const char *SQL_BOTH =
      "UPDATE nodes SET operation = $1, customOperation = $2 WHERE id = $3";
  static const char *SQL_OP   =
      "UPDATE nodes SET operation = $1 WHERE id = $2";
  static const char *SQL_COP  =
      "UPDATE nodes SET customOperation = $1 WHERE id = $2";

  for (int i = 0; i < count; i++) {
    struct ds_node *node = &nodes[i];
    int has_op  = node->has_operation;
    int has_cop = node->has_customOperation;

    // 两个字段都未提供，跳过该更新
    if (!has_op && !has_cop) continue;

    const char *vals[3];
    int nparams;
    const char *sql;

    if (has_op && has_cop) {
      vals[0] = node->operation;
      vals[1] = node->customOperation;
      vals[2] = node->id;
      nparams = 3;
      sql = SQL_BOTH;
    } else if (has_op) {
      vals[0] = node->operation;
      vals[1] = node->id;
      nparams = 2;
      sql = SQL_OP;
    } else {  // has_cop only
      vals[0] = node->customOperation;
      vals[1] = node->id;
      nparams = 2;
      sql = SQL_COP;
    }

    res = PQexecParams(s_conn, sql, nparams, NULL, vals, NULL, NULL, 0);
    if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
      fprintf(stderr, "PG update error: %s\n", PQerrorMessage(s_conn));
      PQclear(res);
      goto fail;
    }
    PQclear(res);
  }

  // 检查 COMMIT 返回值，失败时回滚以防止事务泄漏
  res = PQexec(s_conn, "COMMIT");
  if (res == NULL || PQresultStatus(res) != PGRES_COMMAND_OK) {
    fprintf(stderr, "COMMIT failed: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    PQexec(s_conn, "ROLLBACK");
    pg_mutex_unlock();
    return -1;
  }
  PQclear(res);
  pg_mutex_unlock();
  return 0;

fail:
  PQexec(s_conn, "ROLLBACK");
  pg_mutex_unlock();
  return -1;
}

// ---- 告警事件流水增量查询（wind_alarm，告警 WS 推送专用） ----
// 表由 monitor_fj_state.py 建表并写入，本模块只读。
static int s_alarm_table_ok = -1;  // -1 未探测 / 0 表不存在 / 1 表存在

int pg_alarm_supported(void) {
  pg_mutex_lock();
  if (s_conn == NULL) {
    pg_mutex_unlock();
    return 0;
  }
  if (s_alarm_table_ok < 0) {
    // 一次性探测表是否存在（monitor_fj_state.py 未启动时表尚未建）
    PGresult *res =
        PQexec(s_conn, "SELECT 1 FROM to_regclass('public.wind_alarm')");
    s_alarm_table_ok =
        (res != NULL && PQresultStatus(res) == PGRES_TUPLES_OK &&
         PQntuples(res) == 1);
    PQclear(res);
    if (!s_alarm_table_ok) {
      fprintf(stderr, "wind_alarm table not found - alarm push disabled\n");
    }
  }
  int ok = s_alarm_table_ok;
  pg_mutex_unlock();
  return ok;
}

static void alarm_set_str(char *dst, size_t cap, PGresult *res, int row,
                          int col) {
  // libpq 对 NULL 返回空串，与 get_nodes 的 NULL->"" 兜底语义一致
  snprintf(dst, cap, "%s", PQgetvalue(res, row, col));
  // UTF-8 安全截断：snprintf 按字节截断，可能切断多字节字符产生非法 UTF-8，
  // 导致严格 WS 客户端拒绝整帧。截断发生时回退到最近的完整字符边界。
  size_t len = strlen(dst);
  if (len == cap - 1 && len > 0) {
    size_t i = len;
    while (i > 0 && ((unsigned char) dst[i - 1] & 0xC0) == 0x80) i--;
    if (i > 0) {
      unsigned char lead = (unsigned char) dst[i - 1];
      int need = lead < 0x80 ? 1 : lead < 0xE0 ? 2 : lead < 0xF0 ? 3 : 4;
      if ((int) (len - (i - 1)) != need) dst[i - 1] = '\0';  // 序列不完整，砍掉首字节
    }
  }
}

// 单行结果填充（三处告警查询共用）
static void alarm_fill_row(struct ds_alarm *r, PGresult *res, int i) {
  memset(r, 0, sizeof(*r));
  r->id = strtoull(PQgetvalue(res, i, 0), NULL, 10);
  alarm_set_str(r->event, sizeof(r->event), res, i, 1);
  alarm_set_str(r->ts, sizeof(r->ts), res, i, 2);
  alarm_set_str(r->region_company_name, sizeof(r->region_company_name), res, i, 3);
  alarm_set_str(r->station_name, sizeof(r->station_name), res, i, 4);
  alarm_set_str(r->device_id, sizeof(r->device_id), res, i, 5);
  alarm_set_str(r->device_name, sizeof(r->device_name), res, i, 6);
  alarm_set_str(r->point_code, sizeof(r->point_code), res, i, 7);
  alarm_set_str(r->point_name, sizeof(r->point_name), res, i, 8);
  alarm_set_str(r->rule_code, sizeof(r->rule_code), res, i, 9);
  alarm_set_str(r->alarm_type, sizeof(r->alarm_type), res, i, 10);
  r->temperature = atof(PQgetvalue(res, i, 11));
  r->temp_rate = atof(PQgetvalue(res, i, 12));
  r->range_1h = atof(PQgetvalue(res, i, 13));
  r->range_20s = atof(PQgetvalue(res, i, 14));
  r->alarm_value = atof(PQgetvalue(res, i, 15));
  alarm_set_str(r->detail, sizeof(r->detail), res, i, 16);
}

// 拉取 id > after_id 的新告警事件（按 id 升序，最多 max_rows 行）。
// 返回实际行数；连接不可用或查询失败返回 -1。
int pg_get_new_alarms(unsigned long long after_id, struct ds_alarm *rows,
                      int max_rows) {
  if (s_conn == NULL || rows == NULL || max_rows <= 0) return -1;
  pg_mutex_lock();
  if (s_conn == NULL) {
    pg_mutex_unlock();
    return -1;
  }

  // 主键范围扫描，单轮上限由调用方控制（100 行），避免一次拉爆
  char idbuf[32], limbuf[16];
  snprintf(idbuf, sizeof(idbuf), "%llu", after_id);
  snprintf(limbuf, sizeof(limbuf), "%d", max_rows);
  const char *vals[2] = {idbuf, limbuf};

  static const char *SQL =
      "SELECT id, event,"
      " COALESCE(to_char(ts, 'YYYY-MM-DD HH24:MI:SS'), ''),"
      " COALESCE(region_company_name, ''), COALESCE(station_name, ''),"
      " device_id, COALESCE(device_name, ''),"
      " point_code, COALESCE(point_name, ''),"
      " rule_code, COALESCE(alarm_type, ''),"
      " COALESCE(temperature, 0), COALESCE(temp_rate, 0),"
      " COALESCE(range_1h, 0), COALESCE(range_20s, 0),"
      " COALESCE(alarm_value, 0), COALESCE(detail, '')"
      " FROM wind_alarm WHERE id > $1 ORDER BY id ASC LIMIT $2";

  PGresult *res = PQexecParams(s_conn, SQL, 2, NULL, vals, NULL, NULL, 0);
  if (res == NULL || PQresultStatus(res) != PGRES_TUPLES_OK) {
    fprintf(stderr, "PG alarm query error: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    pg_mutex_unlock();
    return -1;
  }

  int n = PQntuples(res);
  if (n > max_rows) n = max_rows;
  for (int i = 0; i < n; i++) alarm_fill_row(&rows[i], res, i);
  PQclear(res);
  pg_mutex_unlock();
  return n;
}

// 按 id 倒序分页查询告警历史（最新在前），供 /api/alarms/get 使用。
// total_out 返回匹配总数（前端分页器用）；返回行数，空页 0，失败 -1。
int pg_get_alarms_page(int page, int page_size, unsigned long long *total_out,
                       struct ds_alarm *rows, int max_rows) {
  if (total_out != NULL) *total_out = 0;
  if (s_conn == NULL || rows == NULL || max_rows <= 0 ||
      page < 1 || page_size < 1) {
    return -1;
  }
  if (page_size > max_rows) page_size = max_rows;

  pg_mutex_lock();
  if (s_conn == NULL) {
    pg_mutex_unlock();
    return -1;
  }

  // 先取总数：wind_alarm 为只追加的告警流水表，规模远小于秒级明细表
  PGresult *cres = PQexec(s_conn, "SELECT count(*) FROM wind_alarm");
  if (cres == NULL || PQresultStatus(cres) != PGRES_TUPLES_OK ||
      PQntuples(cres) != 1) {
    fprintf(stderr, "PG alarm count error: %s\n", PQerrorMessage(s_conn));
    PQclear(cres);
    pg_mutex_unlock();
    return -1;
  }
  unsigned long long total = strtoull(PQgetvalue(cres, 0, 0), NULL, 10);
  PQclear(cres);
  if (total_out != NULL) *total_out = total;

  long long offset = (long long) (page - 1) * (long long) page_size;
  if (offset < 0 || offset >= (long long) total) {
    pg_mutex_unlock();
    return 0;  // 超出末页：合法空结果
  }

  char limitbuf[16], offsetbuf[32];
  snprintf(limitbuf, sizeof(limitbuf), "%d", page_size);
  snprintf(offsetbuf, sizeof(offsetbuf), "%lld", offset);
  const char *vals[2] = {limitbuf, offsetbuf};

  static const char *SQL =
      "SELECT id, event,"
      " COALESCE(to_char(ts, 'YYYY-MM-DD HH24:MI:SS'), ''),"
      " COALESCE(region_company_name, ''), COALESCE(station_name, ''),"
      " device_id, COALESCE(device_name, ''),"
      " point_code, COALESCE(point_name, ''),"
      " rule_code, COALESCE(alarm_type, ''),"
      " COALESCE(temperature, 0), COALESCE(temp_rate, 0),"
      " COALESCE(range_1h, 0), COALESCE(range_20s, 0),"
      " COALESCE(alarm_value, 0), COALESCE(detail, '')"
      " FROM wind_alarm ORDER BY id DESC LIMIT $1 OFFSET $2";

  PGresult *res = PQexecParams(s_conn, SQL, 2, NULL, vals, NULL, NULL, 0);
  if (res == NULL || PQresultStatus(res) != PGRES_TUPLES_OK) {
    fprintf(stderr, "PG alarm page query error: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    pg_mutex_unlock();
    return -1;
  }

  int n = PQntuples(res);
  if (n > page_size) n = page_size;
  for (int i = 0; i < n; i++) {
    struct ds_alarm *r = &rows[i];
    memset(r, 0, sizeof(*r));
    r->id = strtoull(PQgetvalue(res, i, 0), NULL, 10);
    alarm_set_str(r->event, sizeof(r->event), res, i, 1);
    alarm_set_str(r->ts, sizeof(r->ts), res, i, 2);
    alarm_set_str(r->region_company_name, sizeof(r->region_company_name), res, i, 3);
    alarm_set_str(r->station_name, sizeof(r->station_name), res, i, 4);
    alarm_set_str(r->device_id, sizeof(r->device_id), res, i, 5);
    alarm_set_str(r->device_name, sizeof(r->device_name), res, i, 6);
    alarm_set_str(r->point_code, sizeof(r->point_code), res, i, 7);
    alarm_set_str(r->point_name, sizeof(r->point_name), res, i, 8);
    alarm_set_str(r->rule_code, sizeof(r->rule_code), res, i, 9);
    alarm_set_str(r->alarm_type, sizeof(r->alarm_type), res, i, 10);
    r->temperature = atof(PQgetvalue(res, i, 11));
    r->temp_rate = atof(PQgetvalue(res, i, 12));
    r->range_1h = atof(PQgetvalue(res, i, 13));
    r->range_20s = atof(PQgetvalue(res, i, 14));
    r->alarm_value = atof(PQgetvalue(res, i, 15));
    alarm_set_str(r->detail, sizeof(r->detail), res, i, 16);
  }
  PQclear(res);
  pg_mutex_unlock();
  return n;
}

// 启动预热：拉取最近 max_rows 行（旧 -> 新返回），用于初始化快照缓存与增量
// 游标，避免服务重启后把历史告警当作新增事件重放给客户端。
int pg_get_recent_alarms(struct ds_alarm *rows, int max_rows) {
  if (s_conn == NULL || rows == NULL || max_rows <= 0) return -1;
  pg_mutex_lock();
  if (s_conn == NULL) {
    pg_mutex_unlock();
    return -1;
  }

  char limbuf[16];
  snprintf(limbuf, sizeof(limbuf), "%d", max_rows);
  const char *vals[1] = {limbuf};

  static const char *SQL =
      "SELECT id, event,"
      " COALESCE(to_char(ts, 'YYYY-MM-DD HH24:MI:SS'), ''),"
      " COALESCE(region_company_name, ''), COALESCE(station_name, ''),"
      " device_id, COALESCE(device_name, ''),"
      " point_code, COALESCE(point_name, ''),"
      " rule_code, COALESCE(alarm_type, ''),"
      " COALESCE(temperature, 0), COALESCE(temp_rate, 0),"
      " COALESCE(range_1h, 0), COALESCE(range_20s, 0),"
      " COALESCE(alarm_value, 0), COALESCE(detail, '')"
      " FROM wind_alarm ORDER BY id DESC LIMIT $1";

  PGresult *res = PQexecParams(s_conn, SQL, 1, NULL, vals, NULL, NULL, 0);
  if (res == NULL || PQresultStatus(res) != PGRES_TUPLES_OK) {
    fprintf(stderr, "PG alarm recent query error: %s\n", PQerrorMessage(s_conn));
    PQclear(res);
    pg_mutex_unlock();
    return -1;
  }

  int n = PQntuples(res);
  if (n > max_rows) n = max_rows;
  // DESC 取回，倒序填入调用方数组（旧 -> 新）
  for (int i = 0; i < n; i++) alarm_fill_row(&rows[i], res, n - 1 - i);
  PQclear(res);
  pg_mutex_unlock();
  return n;
}

const struct ds_driver pg_driver = {
    "pg",
    pg_init,
    pg_cleanup,
    pg_is_available,
    pg_get_nodes,
    pg_update_nodes,
};
