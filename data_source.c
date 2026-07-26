#include "data_source.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#ifdef USE_SQLITE
#include "sqlite3.h"
#endif

#ifdef USE_SQLITE
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

int ds_init(const char *path) {
  // Check if database file exists before opening
  FILE *fp = fopen(path, "r");
  int file_exists = (fp != NULL);
  if (file_exists) fclose(fp);
  
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
      "customOperation TEXT"
      ");";
  
  if (exec_sql(create_table) != 0) {
    sqlite3_close(s_db);
    s_db = NULL;
    return -1;
  }
  
  // Store file existence status
  s_db_file_exists = file_exists;

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
  
  // SQLite creates the file automatically when opened, so we check if it existed before
  if (!s_db_file_exists) return 0;
  
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

  char sql[1024];
  char where[512] = " WHERE ";
  int first = 1;

  if (query->isOnline != NULL && query->isOnline[0] != '\0') {
    if (!first) strcat(where, " AND ");
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
    first = 0;
  }

  if (query->cameraType != NULL && query->cameraType[0] != '\0') {
    if (!first) strcat(where, " AND ");
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
    first = 0;
  }

  if (query->operation != NULL && query->operation[0] != '\0') {
    if (!first) strcat(where, " AND ");
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
    first = 0;
  }

  if (first) {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes");
  } else {
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM nodes%s", where);
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
  if (first) {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes LIMIT %d OFFSET %d", query->pageSize, offset);
  } else {
    snprintf(sql, sizeof(sql), "SELECT * FROM nodes%s LIMIT %d OFFSET %d", where, query->pageSize, offset);
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

    strncpy(node->id, id ? id : "", sizeof(node->id) - 1);
    strncpy(node->name, name ? name : "", sizeof(node->name) - 1);
    strncpy(node->channelCode, channelCode ? channelCode : "", sizeof(node->channelCode) - 1);
    strncpy(node->isOnline, isOnline ? isOnline : "", sizeof(node->isOnline) - 1);
    strncpy(node->cameraType, cameraType ? cameraType : "", sizeof(node->cameraType) - 1);
    strncpy(node->operation, operation ? operation : "", sizeof(node->operation) - 1);
    strncpy(node->customOperation, customOperation ? customOperation : "", sizeof(node->customOperation) - 1);

    result->count++;
  }

  sqlite3_finalize(stmt);
  return 0;
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (s_db == NULL || nodes == NULL || count <= 0) return -1;

  sqlite3_exec(s_db, "BEGIN TRANSACTION", NULL, NULL, NULL);

  const char *sql = 
      "INSERT OR REPLACE INTO nodes (id, name, channelCode, isOnline, cameraType, operation, customOperation) "
      "VALUES (?, ?, ?, ?, ?, ?, ?)";

  sqlite3_stmt *stmt;
  int rc = sqlite3_prepare_v2(s_db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK) {
    fprintf(stderr, "SQL prepare error: %s\n", sqlite3_errmsg(s_db));
    sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
    return -1;
  }

  for (int i = 0; i < count; i++) {
    struct ds_node *node = &nodes[i];
    sqlite3_bind_text(stmt, 1, node->id, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 2, node->name, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 3, node->channelCode, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 4, node->isOnline, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 5, node->cameraType, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 6, node->operation, -1, SQLITE_STATIC);
    sqlite3_bind_text(stmt, 7, node->customOperation, -1, SQLITE_STATIC);

    rc = sqlite3_step(stmt);
    if (rc != SQLITE_DONE) {
      fprintf(stderr, "SQL step error: %s\n", sqlite3_errmsg(s_db));
      sqlite3_finalize(stmt);
      sqlite3_exec(s_db, "ROLLBACK", NULL, NULL, NULL);
      return -1;
    }
    sqlite3_reset(stmt);
  }

  sqlite3_finalize(stmt);
  sqlite3_exec(s_db, "COMMIT", NULL, NULL, NULL);

  return 0;
}

#else

static char *g_csv_path = NULL;

static int get_csv_field(char *line, int index, char *buf, int buf_len) {
  char *p = line;
  for (int i = 0; i < index; i++) {
    if (*p == '"') {
      p++;
      while (*p != '\0' && *p != '"') p++;
      if (*p == '"') p++;
    } else {
      while (*p != '\0' && *p != ',' && *p != '\n' && *p != '\r') p++;
    }
    if (*p == ',') p++;
    if (*p == '\0' || *p == '\n' || *p == '\r') return -1;
  }
  int len = 0;
  if (*p == '"') {
    p++;
    while (*p != '\0' && *p != '"' && *p != '\n' && *p != '\r' && len < buf_len - 1) {
      if ((unsigned char)*p >= 0x20) buf[len++] = *p;
      p++;
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

int ds_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (g_csv_path == NULL || query == NULL || result == NULL) return -1;

  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) return -1;

  char line[4096];
  char headers[32][64] = {""};
  int header_count = 0;
  int id_idx = -1, name_idx = -1, channelCode_idx = -1;
  int isOnline_idx = -1, cameraType_idx = -1, operation_idx = -1, customOperation_idx = -1;

  if (fgets(line, sizeof(line), fp) == NULL) {
    fclose(fp);
    return -1;
  }

  char *h = line;
  if ((unsigned char) h[0] == 0xEF && (unsigned char) h[1] == 0xBB && (unsigned char) h[2] == 0xBF) {
    h += 3;
  }
  while (*h != '\0' && *h != '\n' && header_count < 32) {
    char *end = h;
    while (*end != '\0' && *end != ',' && *end != '\n' && *end != '\r') end++;
    int len = (int) (end - h);
    if (len > 0 && (size_t) len < sizeof(headers[0])) {
      strncpy(headers[header_count], h, len);
      headers[header_count][len] = '\0';
      if (strcmp(headers[header_count], "id") == 0) id_idx = header_count;
      if (strcmp(headers[header_count], "name") == 0) name_idx = header_count;
      if (strcmp(headers[header_count], "channelCode") == 0) channelCode_idx = header_count;
      if (strcmp(headers[header_count], "isOnline") == 0) isOnline_idx = header_count;
      if (strcmp(headers[header_count], "cameraType") == 0) cameraType_idx = header_count;
      if (strcmp(headers[header_count], "operation") == 0) operation_idx = header_count;
      if (strcmp(headers[header_count], "customOperation") == 0) customOperation_idx = header_count;
      header_count++;
    }
    while ((*end == ',' || *end == '\n' || *end == '\r') && *end != '\0') end++;
    h = end;
  }

  if (id_idx < 0 || name_idx < 0 || channelCode_idx < 0 || 
      isOnline_idx < 0 || cameraType_idx < 0) {
    fclose(fp);
    return -1;
  }

  int skip_count = (query->page - 1) * query->pageSize;
  long page_start_offset = -1;
  int total_count = 0;

  while (1) {
    long line_offset = ftell(fp);
    if (fgets(line, sizeof(line), fp) == NULL) break;

    // Remove trailing newline characters
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
      line[--len] = '\0';
    }

    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0') continue;

    if (query->isOnline != NULL && query->isOnline[0] != '\0' && isOnline_idx >= 0) {
      char val[32];
      if (get_csv_field(p, isOnline_idx, val, sizeof(val)) != 0 || 
          !is_value_in_list(val, query->isOnline)) continue;
    }

    if (query->cameraType != NULL && query->cameraType[0] != '\0' && cameraType_idx >= 0) {
      char val[32];
      if (get_csv_field(p, cameraType_idx, val, sizeof(val)) != 0 || 
          !is_value_in_list(val, query->cameraType)) continue;
    }

    if (query->operation != NULL && query->operation[0] != '\0' && operation_idx >= 0) {
      char val[64];
      if (get_csv_field(p, operation_idx, val, sizeof(val)) != 0) continue;
      if (is_value_in_list("0", query->operation) && val[0] == '\0') {
      } else if (!is_value_in_list(val, query->operation)) {
        continue;
      }
    }

    if (total_count == skip_count) {
      page_start_offset = line_offset;
    }
    total_count++;
  }

  result->total = total_count;
  result->nodes = (struct ds_node *) malloc(sizeof(struct ds_node) * query->pageSize);
  if (result->nodes == NULL) {
    fclose(fp);
    return -1;
  }

  result->count = 0;
  if (page_start_offset >= 0) {
    fseek(fp, page_start_offset, SEEK_SET);

    while (fgets(line, sizeof(line), fp) != NULL && result->count < query->pageSize) {
      // Remove trailing newline characters
      size_t len = strlen(line);
      while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) {
        line[--len] = '\0';
      }

      char *line_start = line;
      while (*line_start == ' ' || *line_start == '\t') line_start++;
      if (*line_start == '\0') continue;

      if (query->isOnline != NULL && query->isOnline[0] != '\0' && isOnline_idx >= 0) {
        char val[32];
        if (get_csv_field(line_start, isOnline_idx, val, sizeof(val)) != 0 || 
            !is_value_in_list(val, query->isOnline)) continue;
      }

      if (query->cameraType != NULL && query->cameraType[0] != '\0' && cameraType_idx >= 0) {
        char val[32];
        if (get_csv_field(line_start, cameraType_idx, val, sizeof(val)) != 0 || 
            !is_value_in_list(val, query->cameraType)) continue;
      }

      if (query->operation != NULL && query->operation[0] != '\0' && operation_idx >= 0) {
        char val[64];
        if (get_csv_field(line_start, operation_idx, val, sizeof(val)) != 0) continue;
        if (is_value_in_list("0", query->operation) && val[0] == '\0') {
        } else if (!is_value_in_list(val, query->operation)) {
          continue;
        }
      }

      struct ds_node *node = &result->nodes[result->count];
      get_csv_field(line_start, id_idx, node->id, sizeof(node->id));
      get_csv_field(line_start, name_idx, node->name, sizeof(node->name));
      get_csv_field(line_start, channelCode_idx, node->channelCode, sizeof(node->channelCode));
      get_csv_field(line_start, isOnline_idx, node->isOnline, sizeof(node->isOnline));
      get_csv_field(line_start, cameraType_idx, node->cameraType, sizeof(node->cameraType));
      if (operation_idx >= 0) get_csv_field(line_start, operation_idx, node->operation, sizeof(node->operation));
      else node->operation[0] = '\0';
      if (customOperation_idx >= 0) get_csv_field(line_start, customOperation_idx, node->customOperation, sizeof(node->customOperation));
      else node->customOperation[0] = '\0';

      result->count++;
    }
  }

  fclose(fp);
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

  if (fgets(line, sizeof(line), fp_in) != NULL) {
    // Remove trailing newline characters from header
    size_t header_len = strlen(line);
    while (header_len > 0 && (line[header_len-1] == '\n' || line[header_len-1] == '\r')) {
      line[--header_len] = '\0';
    }
    fprintf(fp_out, "%s\n", line);

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
        if (strcmp(header, "operation") == 0) operation_idx = header_count;
        if (strcmp(header, "customOperation") == 0) custom_operation_idx = header_count;
        header_count++;
      }
      while ((*end == ',' || *end == '\n' || *end == '\r') && *end != '\0') end++;
      h = end;
    }
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
        if (i == operation_idx) {
          strcat(new_line, nodes[match_idx].operation);
        } else if (i == custom_operation_idx) {
          strcat(new_line, nodes[match_idx].customOperation);
        } else {
          char field_val[512] = "";
          get_csv_field(p, i, field_val, sizeof(field_val));
          // Remove any trailing newline characters
          size_t fv_len = strlen(field_val);
          while (fv_len > 0 && (field_val[fv_len-1] == '\n' || field_val[fv_len-1] == '\r')) {
            field_val[--fv_len] = '\0';
          }
          strcat(new_line, field_val);
        }
      }
      fprintf(fp_out, "%s\n", new_line);
    } else {
      fprintf(fp_out, "%s\n", line);
    }
  }

  fclose(fp_in);
  fclose(fp_out);

  remove(g_csv_path);
  rename(edited_path, g_csv_path);

  return 0;
}

#endif
