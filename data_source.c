#include "data_source.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#if !defined(CSV_MODE)
#include "sqlite3.h"
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

#endif

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
  
  int rc = sqlite3_open(path, &s_db);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "Cannot open database: %s\n", sqlite3_errmsg(s_db));
    sqlite3_close(s_db);
    s_db = NULL;
    return -1;
  }

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
    return -1;
  }

  const char *create_index_online = "CREATE INDEX IF NOT EXISTS idx_nodes_online ON nodes(isOnline);";
  const char *create_index_camera = "CREATE INDEX IF NOT EXISTS idx_nodes_camera ON nodes(cameraType);";
  const char *create_index_operation = "CREATE INDEX IF NOT EXISTS idx_nodes_operation ON nodes(operation);";
  const char *create_index_p1 = "CREATE INDEX IF NOT EXISTS idx_nodes_p1 ON nodes(P1);";
  const char *create_index_p3 = "CREATE INDEX IF NOT EXISTS idx_nodes_p3 ON nodes(P3);";
  const char *create_index_p4 = "CREATE INDEX IF NOT EXISTS idx_nodes_p4 ON nodes(P4);";

  exec_sql(create_index_online);
  exec_sql(create_index_camera);
  exec_sql(create_index_operation);
  exec_sql(create_index_p1);
  exec_sql(create_index_p3);
  exec_sql(create_index_p4);
  
  s_db_file_exists = file_exists(path);

  return 0;
}

void ds_cleanup(void) {
  if (s_db != NULL) {
    sqlite3_close(s_db);
    s_db = NULL;
  }
}

int ds_is_available(void) {
  if (s_db == NULL) return 0;
  
  // Check if database has any data (count > 0)
  sqlite3_stmt *stmt;
  int rc = sqlite3_prepare_v2(s_db, "SELECT COUNT(*) FROM nodes", -1, &stmt, NULL);
  if (rc != SQLITE_OK) return 0;
  
  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(stmt);
    return 0;
  }
  
  int count = sqlite3_column_int(stmt, 0);
  sqlite3_finalize(stmt);
  
  return count > 0;
}

int ds_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (s_db == NULL || query == NULL || result == NULL) return -1;

  char sql[2048];
  char where[1024] = "";
  int first = 1;

  if (query->isOnline != NULL) {
    if (!first) strcat(where, " AND ");
    if (query->isOnline[0] == '\0') {
      strcat(where, "0");
    } else {
      strcat(where, "isOnline IN (");
      char *token = strdup(query->isOnline);
      char *t = token;
      int tok_first = 1;
      while ((t = strtok(t, ",")) != NULL) {
        if (!tok_first) strcat(where, ",");
        strcat(where, "'");
        strcat(where, t);
        strcat(where, "'");
        tok_first = 0;
        t = NULL;
      }
      free(token);
      strcat(where, ")");
    }
    first = 0;
  }

  if (query->cameraType != NULL) {
    if (!first) strcat(where, " AND ");
    if (query->cameraType[0] == '\0') {
      strcat(where, "0");
    } else {
      strcat(where, "cameraType IN (");
      char *token = strdup(query->cameraType);
      char *t = token;
      int tok_first = 1;
      while ((t = strtok(t, ",")) != NULL) {
        if (!tok_first) strcat(where, ",");
        strcat(where, "'");
        strcat(where, t);
        strcat(where, "'");
        tok_first = 0;
        t = NULL;
      }
      free(token);
      strcat(where, ")");
    }
    first = 0;
  }

  if (query->operation != NULL) {
    if (!first) strcat(where, " AND ");
    if (query->operation[0] == '\0') {
      strcat(where, "0");
    } else {
      char *token = strdup(query->operation);
      char *t = token;
      int tok_first = 1;
      strcat(where, "(");
      while ((t = strtok(t, ",")) != NULL) {
        if (!tok_first) strcat(where, " OR ");
        if (strcmp(t, "0") == 0) {
          strcat(where, "operation IS NULL OR operation = ''");
        } else {
          strcat(where, "operation = '");
          strcat(where, t);
          strcat(where, "'");
        }
        tok_first = 0;
        t = NULL;
      }
      free(token);
      strcat(where, ")");
    }
    first = 0;
  }

  if (query->keyword != NULL && query->keyword[0] != '\0') {
    if (!first) strcat(where, " AND ");
    // 转义单引号
    char escaped[256];
    int ei = 0;
    for (int ki = 0; query->keyword[ki] != '\0' && ei < (int)sizeof(escaped) - 2; ki++) {
      if (query->keyword[ki] == '\'') {
        escaped[ei++] = '\'';
        escaped[ei++] = '\'';
      } else {
        escaped[ei++] = query->keyword[ki];
      }
    }
    escaped[ei] = '\0';
    
    snprintf(where + strlen(where), sizeof(where) - strlen(where),
             "(name LIKE '%%%s%%' OR P4 LIKE '%%%s%%')", escaped, escaped);
    first = 0;
  }

  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes");
  } else {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes WHERE %s", where);
  }

  sqlite3_stmt *stmt;
  int rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s\n", sqlite3_errmsg(s_db));
    return -1;
  }

  rc = sqlite3_step(stmt);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(stmt);
    return -1;
  }
  result->total = sqlite3_column_int(stmt, 0);
  sqlite3_finalize(stmt);

  int offset = (query->page - 1) * query->pageSize;
  if (where[0] == '\0') {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes LIMIT %d OFFSET %d", query->pageSize, offset);
  } else {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes WHERE %s LIMIT %d OFFSET %d", where, query->pageSize, offset);
  }

  rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s\n", sqlite3_errmsg(s_db));
    return -1;
  }

  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * query->pageSize);
  if (result->nodes == NULL) {
    sqlite3_finalize(stmt);
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

    strncpy(node->id, id ? id : "", sizeof(node->id) - 1);
    strncpy(node->name, name ? name : "", sizeof(node->name) - 1);
    strncpy(node->channelCode, channelCode ? channelCode : "", sizeof(node->channelCode) - 1);
    strncpy(node->isOnline, isOnline ? isOnline : "", sizeof(node->isOnline) - 1);
    strncpy(node->cameraType, cameraType ? cameraType : "", sizeof(node->cameraType) - 1);
    strncpy(node->operation, operation ? operation : "", sizeof(node->operation) - 1);
    strncpy(node->customOperation, customOperation ? customOperation : "", sizeof(node->customOperation) - 1);
    strncpy(node->P1, p1 ? p1 : "", sizeof(node->P1) - 1);
    strncpy(node->P3, p3 ? p3 : "", sizeof(node->P3) - 1);
    strncpy(node->P4, p4 ? p4 : "", sizeof(node->P4) - 1);

    result->count++;
  }

  sqlite3_finalize(stmt);
  return 0;
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (s_db == NULL || nodes == NULL || count <= 0) return -1;

  int rc = sqlite3_exec(s_db, "BEGIN TRANSACTION", NULL, NULL, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "BEGIN TRANSACTION failed: %s\n", sqlite3_errmsg(s_db));
    return -1;
  }

  const char *sql = 
      "UPDATE nodes SET operation = ?, customOperation = ? WHERE id = ?";

  sqlite3_stmt *stmt;
  rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s\n", sqlite3_errmsg(s_db));
    sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
    return -1;
  }

  for (int i = 0; i < count; i++) {
    struct ds_node *node = &nodes[i];
    sqlite3_bind_text(stmt, 1, node->operation, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 2, node->customOperation, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 3, node->id, -1, SQLITE_TRANSIENT);

    rc = sqlite3_step(stmt);
    if (rc != SQLITE_DONE) {
      fprintf(stderr, "SQL step error: %s\n", sqlite3_errmsg(s_db));
      sqlite3_finalize(stmt);
      sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
      return -1;
    }
    sqlite3_reset(stmt);
    sqlite3_clear_bindings(stmt);
  }

  sqlite3_finalize(stmt);
  sqlite3_exec(s_db, "COMMIT", NULL, NULL, NULL);

  return 0;
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
  if (path == NULL) return -1;
  g_csv_path = strdup(path);
  
  // Check if CSV file exists
  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) {
    fprintf(stderr, "Warning: CSV file %s not found\n", g_csv_path);
  } else {
    fclose(fp);
  }
  return 0;
}

void ds_cleanup(void) {
  if (g_csv_path != NULL) {
    free(g_csv_path);
    g_csv_path = NULL;
  }
}

int ds_is_available(void) {
  if (g_csv_path == NULL) return 0;
  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) return 0;
  fclose(fp);
  return 1;
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

  if (load_csv_cache() != 0) return -1;

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
  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * query->pageSize);
  if (result->nodes == NULL) return -1;

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
      strncpy(result_node->name, node->name, sizeof(result_node->name) - 1);
      strncpy(result_node->channelCode, node->channelCode, sizeof(result_node->channelCode) - 1);
      strncpy(result_node->isOnline, node->isOnline, sizeof(result_node->isOnline) - 1);
      strncpy(result_node->cameraType, node->cameraType, sizeof(result_node->cameraType) - 1);
      strncpy(result_node->operation, node->operation, sizeof(result_node->operation) - 1);
      strncpy(result_node->customOperation, node->customOperation, sizeof(result_node->customOperation) - 1);
      strncpy(result_node->P1, "", sizeof(result_node->P1) - 1);
      strncpy(result_node->P3, "", sizeof(result_node->P3) - 1);
      strncpy(result_node->P4, node->P4, sizeof(result_node->P4) - 1);

      result->count++;
    }
  }

  return 0;
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (g_csv_path == NULL || nodes == NULL || count <= 0) return -1;

  FILE *fp_in = fopen(g_csv_path, "rb");
  if (fp_in == NULL) return -1;

  char edited_path[256];
  snprintf(edited_path, sizeof(edited_path), "%s.tmp", g_csv_path);
  FILE *fp_out = fopen(edited_path, "w");
  if (fp_out == NULL) {
    fclose(fp_in);
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
        strcat(new_header, ",");
      }
      strcat(new_header, "operation");
      header_count++;
      operation_idx = header_count - 1;
    }
    if (!has_custom_col) {
      int cur_len = (int) strlen(new_header);
      if (cur_len > 0 && new_header[cur_len-1] != ',') {
        strcat(new_header, ",");
      }
      strcat(new_header, "customOperation");
      header_count++;
      custom_operation_idx = header_count - 1;
    }
    if (had_newline) {
      strcat(new_header, "\n");
    }
    fprintf(fp_out, "%s", new_header);
  }

  while (fgets(line, sizeof(line), fp_in) != NULL) {
    // Remove trailing newline characters
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
      line[--len] = '\0';
    }

    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0') {
      // Skip empty lines entirely
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
        if (!first) strcat(new_line, ",");
        first = 0;
        char escaped[512] = "";
        if (i == operation_idx) {
          csv_escape_field(nodes[match_idx].operation, escaped, sizeof(escaped));
        } else if (i == custom_operation_idx) {
          csv_escape_field(nodes[match_idx].customOperation, escaped, sizeof(escaped));
        } else {
          char field_val[512] = "";
          get_csv_field(p, i, field_val, sizeof(field_val));
          size_t fv_len = strlen(field_val);
          while (fv_len > 0 && (field_val[fv_len-1] == '\n' || field_val[fv_len-1] == '\r')) {
            field_val[--fv_len] = '\0';
          }
          csv_escape_field(field_val, escaped, sizeof(escaped));
        }
        strcat(new_line, escaped);
      }
      fprintf(fp_out, "%s\n", new_line);
    } else {
      if (!has_operation_col || !has_custom_col) {
        char new_line[4096] = "";
        int first = 1;
        for (int i = 0; i < header_count; i++) {
          if (!first) strcat(new_line, ",");
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
          strcat(new_line, escaped);
        }
        fprintf(fp_out, "%s\n", new_line);
      } else {
        fprintf(fp_out, "%s\n", line);
      }
    }
  }

  fclose(fp_in);
  fclose(fp_out);

  if (!MoveFileEx(edited_path, g_csv_path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
    remove(g_csv_path);
    rename(edited_path, g_csv_path);
  }

  if (g_cache != NULL) {
    free(g_cache);
    g_cache = NULL;
  }
  g_cache_count = 0;
  g_cache_mtime = 0;

  return 0;
}

#endif
