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

// Cross-platform mutex
#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION s_csv_mutex;
static int s_csv_mutex_initialized = 0;

static void csv_mutex_init(void) {
  if (!s_csv_mutex_initialized) {
    InitializeCriticalSection(&s_csv_mutex);
    s_csv_mutex_initialized = 1;
  }
}
static void csv_mutex_lock(void) { EnterCriticalSection(&s_csv_mutex); }
static void csv_mutex_unlock(void) { LeaveCriticalSection(&s_csv_mutex); }
#else
static pthread_mutex_t s_csv_mutex = PTHREAD_MUTEX_INITIALIZER;
static void csv_mutex_init(void) { (void)0; }
static void csv_mutex_lock(void) { pthread_mutex_lock(&s_csv_mutex); }
static void csv_mutex_unlock(void) { pthread_mutex_unlock(&s_csv_mutex); }
#endif

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

static int csv_init(const char *path) {
  csv_mutex_init();

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

static void csv_cleanup(void) {
  csv_mutex_lock();
  if (g_csv_path != NULL) {
    free(g_csv_path);
    g_csv_path = NULL;
  }
  csv_mutex_unlock();
}

static int csv_is_available(void) {
  csv_mutex_lock();
  int result = 0;
  if (g_csv_path == NULL) {
    csv_mutex_unlock();
    return 0;
  }
  FILE *fp = fopen(g_csv_path, "rb");
  if (fp == NULL) {
    csv_mutex_unlock();
    return 0;
  }
  fclose(fp);
  result = 1;
  csv_mutex_unlock();
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

static int csv_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (g_csv_path == NULL || query == NULL || result == NULL) return -1;
  csv_mutex_lock();

  if (load_csv_cache() != 0) {
    csv_mutex_unlock();
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
    csv_mutex_unlock();
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

  csv_mutex_unlock();
  return 0;
}

static int csv_update_nodes(struct ds_node *nodes, int count) {
  if (g_csv_path == NULL || nodes == NULL || count <= 0) return -1;
  csv_mutex_lock();

  FILE *fp_in = fopen(g_csv_path, "rb");
  if (fp_in == NULL) {
    csv_mutex_unlock();
    return -1;
  }

  char edited_path[256];
  snprintf(edited_path, sizeof(edited_path), "%s.tmp", g_csv_path);
  FILE *fp_out = fopen(edited_path, "w");
  if (fp_out == NULL) {
    fclose(fp_in);
    csv_mutex_unlock();
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

  csv_mutex_unlock();
  return 0;
}

const struct ds_driver csv_driver = {
    "csv",
    csv_init,
    csv_cleanup,
    csv_is_available,
    csv_get_nodes,
    csv_update_nodes,
};
