// Copyright (c) 2023 Cesanta Software Limited
// All rights reserved

#include "net.h"
#include "data_source.h"

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#include <process.h>
#else
#include <time.h>
#include <pthread.h>
#endif

#define MAX_FILTER_LEN          64
#define MAX_KEYWORD_LEN         256
#define MAX_NODE_UPDATES        256
#define MAX_FIELD_KEYS          32
#define MAX_FIELD_KEY_LEN       64
#define AVG_NODE_JSON_SIZE      2048
#define RESPONSE_OVERHEAD       4096

// Cross-platform thread
// Thread functions use void return type; on Linux a wrapper adapts to pthread's void* return.
#if defined(_WIN32) || defined(_WIN64)
static void start_thread(void (*f)(void *), void *p) {
  _beginthread(f, 0, p);
}
#else
struct thread_arg { void (*fn)(void *); void *arg; };
static void *thread_wrapper(void *p) {
  struct thread_arg *ta = (struct thread_arg *) p;
  ta->fn(ta->arg);
  free(ta);
  return NULL;
}
static void start_thread(void (*f)(void *), void *p) {
  struct thread_arg *ta = (struct thread_arg *) malloc(sizeof(*ta));
  if (ta == NULL) return;
  ta->fn = f;
  ta->arg = p;
  pthread_t thread_id;
  pthread_attr_t attr;
  pthread_attr_init(&attr);
  pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
  pthread_create(&thread_id, &attr, thread_wrapper, ta);
  pthread_attr_destroy(&attr);
}
#endif

// Forward declarations
static char *get_config_buf(void);
static int get_max_page_size(void);
static int get_default_page_size(void);

struct work_request {
  struct mg_mgr *mgr;
  unsigned long conn_id;

  int http_status;
  char *response_body;
  size_t response_len;

  char isOnline_buf[MAX_FILTER_LEN];
  char cameraType_buf[MAX_FILTER_LEN];
  char operation_buf[MAX_FILTER_LEN];
  char keyword_buf[MAX_KEYWORD_LEN];
  int page;
  int page_size;
  // 标志位：参数是否出现在 URL 中（用于区分 isOnline= 和 缺失 isOnline）
  int has_isOnline;
  int has_cameraType;
  int has_operation;

  struct ds_node *updates;
  int update_count;
};

static void free_work_request(struct work_request *wr);
static void push_result(unsigned long conn_id, int http_status, char *body, size_t len);

struct cfg_parsed {
  int defaultPageSize;
  int maxPageSize;
  char field_keys[MAX_FIELD_KEYS][MAX_FIELD_KEY_LEN];
  int field_key_count;
  size_t fields_tok_len;
  char *fields_json_copy;
};

static struct cfg_parsed g_cfg_parsed;
static int g_cfg_parsed_valid = 0;
static char s_global_api_token[256] = {0};  // Global fixed token for API tools like Postman

#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION g_cfg_mutex;
static int g_cfg_mutex_inited = 0;
static void cfg_mutex_init(void) {
  if (!g_cfg_mutex_inited) {
    InitializeCriticalSection(&g_cfg_mutex);
    g_cfg_mutex_inited = 1;
  }
}
static void cfg_lock(void) { EnterCriticalSection(&g_cfg_mutex); }
static void cfg_unlock(void) { LeaveCriticalSection(&g_cfg_mutex); }
#else
static pthread_mutex_t g_cfg_mutex = PTHREAD_MUTEX_INITIALIZER;
static void cfg_mutex_init(void) { (void) g_cfg_mutex; }
static void cfg_lock(void) { pthread_mutex_lock(&g_cfg_mutex); }
static void cfg_unlock(void) { pthread_mutex_unlock(&g_cfg_mutex); }
#endif

static void parse_config_fields(const char *cfg_buf) {
  struct cfg_parsed *cp = &g_cfg_parsed;
  memset(cp, 0, sizeof(*cp));

  cp->defaultPageSize = 50;
  cp->maxPageSize = 100;

  const char *dps_key = "\"defaultPageSize\"";
  char *pos = strstr(cfg_buf, dps_key);
  if (pos) {
    pos += strlen(dps_key);
    while (*pos == ' ' || *pos == ':') pos++;
    if (isdigit((unsigned char)*pos)) cp->defaultPageSize = atoi(pos);
  }

  const char *mps_key = "\"maxPageSize\"";
  pos = strstr(cfg_buf, mps_key);
  if (pos) {
    pos += strlen(mps_key);
    while (*pos == ' ' || *pos == ':') pos++;
    if (isdigit((unsigned char)*pos)) cp->maxPageSize = atoi(pos);
  }

  // Read global API token for Postman etc.
  const char *api_key = "\"apiToken\"";
  pos = strstr(cfg_buf, api_key);
  if (pos) {
    pos += strlen(api_key);
    while (*pos == ' ' || *pos == ':') pos++;
    if (*pos == '"') {
      pos++;
      char *end_pos = strchr(pos, '"');
      if (end_pos) {
        int len = (int) (end_pos - pos);
        if (len > 0 && len < (int)sizeof(s_global_api_token)) {
          memcpy(s_global_api_token, pos, (size_t) len);
          s_global_api_token[len] = '\0';
          MG_INFO(("Global API token loaded: %s", s_global_api_token));
        }
      }
    }
  }

  struct mg_str cfg_fields_tok = mg_json_get_tok(mg_str((char *)cfg_buf), "$.fields");
  cp->fields_tok_len = cfg_fields_tok.len;

  if (cfg_fields_tok.len > 0) {
    cp->fields_json_copy = (char *) malloc((size_t) cfg_fields_tok.len + 1);
    if (cp->fields_json_copy) {
      memcpy(cp->fields_json_copy, cfg_fields_tok.buf, (size_t) cfg_fields_tok.len);
      cp->fields_json_copy[cfg_fields_tok.len] = '\0';

      char *ptr = cp->fields_json_copy;
      while (*ptr != '\0' && cp->field_key_count < MAX_FIELD_KEYS) {
        char *key_ptr = strstr(ptr, "\"key\"");
        if (key_ptr == NULL) break;
        key_ptr += 5;
        while (*key_ptr == ' ' || *key_ptr == '\t' || *key_ptr == ':') key_ptr++;
        while (*key_ptr == ' ' || *key_ptr == '\t') key_ptr++;
        if (*key_ptr != '"') break;
        key_ptr++;
        char *end_ptr = strchr(key_ptr, '"');
        if (end_ptr == NULL) break;
        int len = (int) (end_ptr - key_ptr);
        if (len > 0 && len < MAX_FIELD_KEY_LEN) {
          memcpy(cp->field_keys[cp->field_key_count], key_ptr, (size_t) len);
          cp->field_keys[cp->field_key_count][len] = '\0';
          cp->field_key_count++;
        }
        ptr = end_ptr + 1;
      }
    }
  }

  g_cfg_parsed_valid = 1;
}

static int get_max_page_size(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.maxPageSize;
  cfg_unlock();
  return val;
}

static int get_default_page_size(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.defaultPageSize;
  cfg_unlock();
  return val;
}

// Append JSON-escaped `str` into out[pos..cap) using direct buffer writes.
// Replaces the per-character snprintf loop that dominated /nodes/get serialization
// (~15k snprintf calls per request at 50 nodes x ~10 fields x ~30 chars). Returns
// the new position; stops writing if the buffer would overflow (the response buffer
// is sized generously, so this never triggers in practice).
static int json_escape_append(char *out, int pos, int cap, const char *str) {
  if (str == NULL) return pos;
  static const char hex[] = "0123456789abcdef";
  for (const unsigned char *p = (const unsigned char *) str; *p != '\0'; p++) {
    unsigned char c = *p;
    const char *seq;
    int seqlen;
    switch (c) {
      case '"':  seq = "\\\""; seqlen = 2; break;
      case '\\': seq = "\\\\"; seqlen = 2; break;
      case '\n': seq = "\\n";  seqlen = 2; break;
      case '\r': seq = "\\r";  seqlen = 2; break;
      case '\t': seq = "\\t";  seqlen = 2; break;
      case '\b': seq = "\\b";  seqlen = 2; break;
      case '\f': seq = "\\f";  seqlen = 2; break;
      default:
        if (c < 0x20) {
          if (pos + 6 > cap) return pos;
          out[pos++] = '\\';
          out[pos++] = 'u';
          out[pos++] = '0';
          out[pos++] = '0';
          out[pos++] = hex[c >> 4];
          out[pos++] = hex[c & 0xf];
        } else {
          if (pos + 1 > cap) return pos;
          out[pos++] = (char) c;
        }
        continue;
    }
    if (pos + seqlen > cap) return pos;
    memcpy(out + pos, seq, (size_t) seqlen);
    pos += seqlen;
  }
  return pos;
}

static void build_nodes_get_response(struct work_request *wr) {
  struct ds_query query;
  memset(&query, 0, sizeof(query));
  query.page = wr->page;
  query.pageSize = wr->page_size;

  // has_isOnline=1：参数出现在 URL 中（即使值为空也设置空字符串，让后端返回 0 条）
  // has_isOnline=0：参数未出现，保持 NULL，跳过过滤
  if (wr->has_isOnline) {
    query.has_isOnline = 1;
    query.isOnline = wr->isOnline_buf;
  }
  if (wr->has_cameraType) {
    query.has_cameraType = 1;
    query.cameraType = wr->cameraType_buf;
  }
  if (wr->has_operation) {
    query.has_operation = 1;
    query.operation = wr->operation_buf;
  }
  if (wr->keyword_buf[0] != '\0') query.keyword = wr->keyword_buf;

  struct ds_result result = {0};
  int query_result = ds_get_nodes(&query, &result);

  if (query_result != 0) {
    wr->http_status = 503;
    wr->response_body = strdup("{\"error\":\"Database query failed\"}");
    wr->response_len = strlen(wr->response_body);
    return;
  }

  char *cfg_buf = get_config_buf();
  if (cfg_buf == NULL) {
    if (result.nodes != NULL) free(result.nodes);
    wr->http_status = 500;
    wr->response_body = strdup("{\"error\":\"Cannot read config\"}");
    wr->response_len = strlen(wr->response_body);
    return;
  }

  cfg_lock();
  if (!g_cfg_parsed_valid) parse_config_fields(cfg_buf);

  int field_key_count = g_cfg_parsed.field_key_count;
  char (*field_keys)[MAX_FIELD_KEY_LEN] = g_cfg_parsed.field_keys;
  size_t fields_tok_len = g_cfg_parsed.fields_tok_len;
  char *fields_json_copy = g_cfg_parsed.fields_json_copy;

  int cfg_dps = g_cfg_parsed.defaultPageSize;
  int cfg_mps = g_cfg_parsed.maxPageSize;
  cfg_unlock();

  size_t cfg_len = strlen(cfg_buf);
  long total_size = (long) cfg_len + (long) wr->page_size * AVG_NODE_JSON_SIZE + RESPONSE_OVERHEAD;
  if (total_size < 8192) total_size = 8192;

  char *response = (char *) malloc((size_t) total_size);
  if (response == NULL) {
    if (result.nodes != NULL) free(result.nodes);
    wr->http_status = 500;
    wr->response_body = strdup("{\"error\":\"Memory allocation failed\"}");
    wr->response_len = strlen(wr->response_body);
    return;
  }

  int pos = 0;
  pos += snprintf(response + pos, (size_t)(total_size - pos),
                   "{\"config\":{\"defaultPageSize\":%d,\"maxPageSize\":%d,\"fields\":",
                   cfg_dps, cfg_mps);

  if (fields_tok_len > 0 && fields_json_copy) {
    memcpy(response + pos, fields_json_copy, fields_tok_len);
    pos += (int) fields_tok_len;
  } else {
    pos += snprintf(response + pos, (size_t)(total_size - pos), "[]");
  }
  pos += snprintf(response + pos, (size_t)(total_size - pos), "},");
  pos += snprintf(response + pos, (size_t)(total_size - pos),
                   "\"data\":{\"total\":%d,\"nodes\":[", result.total);

  int first = 1;
  for (int i = 0; i < result.count; i++) {
    struct ds_node *node = &result.nodes[i];
    if (!first) pos += snprintf(response + pos, (size_t)(total_size - pos), ",");
    first = 0;
    pos += snprintf(response + pos, (size_t)(total_size - pos), "{");
    for (int j = 0; j < field_key_count; j++) {
      if (j > 0) pos += snprintf(response + pos, (size_t)(total_size - pos), ",");
      pos += snprintf(response + pos, (size_t)(total_size - pos), "\"%s\":\"", field_keys[j]);

      const char *field_val = "";
      if (strcmp(field_keys[j], "id") == 0) field_val = node->id;
      else if (strcmp(field_keys[j], "name") == 0) field_val = node->name;
      else if (strcmp(field_keys[j], "channelCode") == 0) field_val = node->channelCode;
      else if (strcmp(field_keys[j], "isOnline") == 0) field_val = node->isOnline;
      else if (strcmp(field_keys[j], "cameraType") == 0) field_val = node->cameraType;
      else if (strcmp(field_keys[j], "operation") == 0) field_val = node->operation;
      else if (strcmp(field_keys[j], "customOperation") == 0) field_val = node->customOperation;
      else if (strcmp(field_keys[j], "P1") == 0) field_val = node->P1;
      else if (strcmp(field_keys[j], "P3") == 0) field_val = node->P3;
      else if (strcmp(field_keys[j], "P4") == 0) field_val = node->P4;

      pos = json_escape_append(response, pos, (int) total_size, field_val);
      pos += snprintf(response + pos, (size_t)(total_size - pos), "\"");
    }
    pos += snprintf(response + pos, (size_t)(total_size - pos), "}");
  }
  pos += snprintf(response + pos, (size_t)(total_size - pos), "]}}");

  if (result.nodes != NULL) free(result.nodes);

  wr->http_status = 200;
  wr->response_body = response;
  wr->response_len = (size_t) pos;
}

static void build_nodes_queryCategory_response(struct work_request *wr) {
  struct ds_query query;
  memset(&query, 0, sizeof(query));
  query.has_operation = 1;
  query.operation = "1,2,3";

  // Return ALL matching data — ignore page/pageSize.
  // Phase 1: quick count query (pageSize=1) to learn how many rows match.
  query.page = 1;
  query.pageSize = 1;
  struct ds_result result = {0};
  int query_result = ds_get_nodes(&query, &result);
  if (query_result != 0) {
    wr->http_status = 503;
    wr->response_body = strdup("{\"error\":\"Database query failed\"}");
    wr->response_len = strlen(wr->response_body);
    return;
  }
  int total = result.total;
  if (result.nodes != NULL) { free(result.nodes); result.nodes = NULL; }

  // Phase 2: fetch every matching row in one shot.
  if (total > 0) {
    query.pageSize = total;
    memset(&result, 0, sizeof(result));
    query_result = ds_get_nodes(&query, &result);
    if (query_result != 0) {
      wr->http_status = 503;
      wr->response_body = strdup("{\"error\":\"Database query failed\"}");
      wr->response_len = strlen(wr->response_body);
      return;
    }
  }

  size_t total_size = (size_t) ((total > 0 ? total : 1) * 512 + 4096);
  if (total_size < 8192) total_size = 8192;
  char *response = (char *) malloc(total_size);
  if (response == NULL) {
    if (result.nodes != NULL) free(result.nodes);
    wr->http_status = 500;
    wr->response_body = strdup("{\"error\":\"Memory allocation failed\"}");
    wr->response_len = strlen(wr->response_body);
    return;
  }

  int pos = 0;
  pos += snprintf(response + pos, (size_t)(total_size - pos),
                   "{\"data\":{\"total\":%d,\"nodes\":[", result.total);

  int first = 1;
  const char *keys[] = {"name", "channelCode", "isOnline", "P1", "P4", "operation"};
  for (int i = 0; i < result.count; i++) {
    struct ds_node *node = &result.nodes[i];
    const char *fields[] = {node->name, node->channelCode, node->isOnline,
                            node->P1, node->P4, node->operation};
    if (!first) pos += snprintf(response + pos, (size_t)(total_size - pos), ",");
    first = 0;
    pos += snprintf(response + pos, (size_t)(total_size - pos), "{");

    for (int j = 0; j < 6; j++) {
      if (j > 0) pos += snprintf(response + pos, (size_t)(total_size - pos), ",");
      if (j == 3)
          pos += snprintf(response + pos, (size_t)(total_size - pos), "\"%s\":\"", "type");
      else if (j == 4)
          pos += snprintf(response + pos, (size_t)(total_size - pos), "\"%s\":\"", "company");
      else if (j == 5)
          pos += snprintf(response + pos, (size_t)(total_size - pos), "\"%s\":\"", "category");
      else
          pos += snprintf(response + pos, (size_t)(total_size - pos), "\"%s\":\"", keys[j]);
      const char *field_val = fields[j];
      pos = json_escape_append(response, pos, (int) total_size, field_val);
      pos += snprintf(response + pos, (size_t)(total_size - pos), "\"");
    }
    pos += snprintf(response + pos, (size_t)(total_size - pos), "}");
  }

  pos += snprintf(response + pos, (size_t)(total_size - pos), "]}}");

  if (result.nodes != NULL) free(result.nodes);
  wr->http_status = 200;
  wr->response_body = response;
  wr->response_len = (size_t) pos;
}

static void nodes_queryCategory_get_thread(void *param) {
  struct work_request *wr = (struct work_request *) param;
  build_nodes_queryCategory_response(wr);
  push_result(wr->conn_id, wr->http_status, wr->response_body, wr->response_len);
  wr->response_body = NULL;
  free_work_request(wr);
}

static void nodes_get_thread(void *param) {
  struct work_request *wr = (struct work_request *) param;
  build_nodes_get_response(wr);
  push_result(wr->conn_id, wr->http_status, wr->response_body, wr->response_len);
  wr->response_body = NULL;
  free_work_request(wr);
}

static void nodes_batchset_thread(void *param) {
  struct work_request *wr = (struct work_request *) param;

  int update_result = ds_update_nodes(wr->updates, wr->update_count);

  if (update_result == 0) {
    wr->http_status = 200;
    wr->response_body = strdup("{\"status\":\"true\",\"message\":\"Success\"}");
  } else {
    wr->http_status = 503;
    wr->response_body = strdup("{\"status\":\"false\",\"message\":\"Update failed\"}");
  }
  wr->response_len = strlen(wr->response_body);

  push_result(wr->conn_id, wr->http_status, wr->response_body, wr->response_len);
  wr->response_body = NULL;
  free_work_request(wr);
}

#if !defined(CSV_MODE)
#define DS_MODE "SQLite"
#define DS_MODE_TYPE 1
#else
#define DS_MODE "CSV"
#define DS_MODE_TYPE 0
#endif

struct user {
  const char *name, *pass;
  char access_token[65];  // 64 chars + '\0'
};

static struct user s_users[] = {
    {"admin", "admin", ""},
    {"user1", "user1", ""},
    {"user2", "user2", ""},
    {NULL, NULL, ""},
};

// Sentinel user returned when a request authenticates via the global Api-Token
// (from data_config.json) — through the "Api-Token" header, the access_token
// cookie / query param, or a Bearer token. Has no password and no session
// token: the Api-Token itself is the credential.
static struct user global_token_user = {"_api_token_", NULL, ""};

static void generate_access_tokens(void) {
  for (struct user *u = s_users; u->name != NULL; u++) {
    mg_random_str(u->access_token, sizeof(u->access_token));
    MG_INFO(("Generated token for user [%s]", u->name));
  }
}

static const char *s_json_header =
    "Content-Type: application/json\r\n"
    "Cache-Control: no-cache\r\n";

static void free_work_request(struct work_request *wr) {
  if (wr == NULL) return;
  if (wr->updates != NULL) {
    free(wr->updates);
    wr->updates = NULL;
  }
  free(wr);
}

struct async_result {
  unsigned long conn_id;
  int http_status;
  char *body;
  size_t len;
  struct async_result *next;
};

static struct async_result *s_results_head = NULL;
static struct async_result *s_results_tail = NULL;

#if defined(_WIN32) || defined(_WIN64)
static CRITICAL_SECTION s_results_mutex;
static int s_results_mutex_inited = 0;
static void results_mutex_init(void) {
  if (!s_results_mutex_inited) {
    InitializeCriticalSection(&s_results_mutex);
    s_results_mutex_inited = 1;
  }
}
static void results_lock(void) { EnterCriticalSection(&s_results_mutex); }
static void results_unlock(void) { LeaveCriticalSection(&s_results_mutex); }
#else
static pthread_mutex_t s_results_mutex = PTHREAD_MUTEX_INITIALIZER;
static void results_mutex_init(void) { (void) s_results_mutex; }
static void results_lock(void) { pthread_mutex_lock(&s_results_mutex); }
static void results_unlock(void) { pthread_mutex_unlock(&s_results_mutex); }
#endif

static void push_result(unsigned long conn_id, int http_status,
                        char *body, size_t len) {
  struct async_result *r = (struct async_result *) calloc(1, sizeof(*r));
  if (r == NULL) {
    if (body != NULL) free(body);
    return;
  }
  r->conn_id = conn_id;
  r->http_status = http_status;
  r->body = body;
  r->len = len;
  r->next = NULL;
  results_lock();
  if (s_results_tail != NULL) {
    s_results_tail->next = r;
    s_results_tail = r;
  } else {
    s_results_head = s_results_tail = r;
  }
  results_unlock();
}

static void dispatch_results(void *arg) {
  struct mg_mgr *mgr = (struct mg_mgr *) arg;
  results_lock();
  struct async_result *r = s_results_head;
  s_results_head = s_results_tail = NULL;
  results_unlock();

  while (r != NULL) {
    struct async_result *next = r->next;
    struct mg_connection *c;
    for (c = mgr->conns; c != NULL; c = c->next) {
      if (c->id == r->conn_id) {
        mg_http_reply(c, r->http_status, s_json_header, "%.*s",
                      (int) r->len, r->body);
        break;
      }
    }
    if (r->body != NULL) free(r->body);
    free(r);
    r = next;
  }
}

static char *g_cfg_buf = NULL;
static time_t g_cfg_mtime = 0;

static char *get_config_buf(void) {
  cfg_lock();

  if (g_cfg_buf != NULL) {
    struct stat st;
    if (stat("data_config.json", &st) == 0 && st.st_mtime == g_cfg_mtime) {
      cfg_unlock();
      return g_cfg_buf;
    }
    free(g_cfg_buf);
    g_cfg_buf = NULL;
  }

  FILE *fp = fopen("data_config.json", "rb");
  if (fp == NULL) {
    cfg_unlock();
    return NULL;
  }

  fseek(fp, 0, SEEK_END);
  long size = ftell(fp);
  fseek(fp, 0, SEEK_SET);

  g_cfg_buf = (char *) malloc((size_t) size + 1);
  if (g_cfg_buf == NULL) {
    fclose(fp);
    cfg_unlock();
    return NULL;
  }

  fread(g_cfg_buf, 1, (size_t) size, fp);
  fclose(fp);
  g_cfg_buf[size] = '\0';

  struct stat st;
  if (stat("data_config.json", &st) == 0) {
    g_cfg_mtime = st.st_mtime;
    g_cfg_parsed_valid = 0;
  }

  cfg_unlock();
  return g_cfg_buf;
}

static void unicode_to_utf8(unsigned int codepoint, char *out, int *out_len) {
  if (codepoint < 0x80) {
    out[0] = (char) codepoint;
    *out_len = 1;
  } else if (codepoint < 0x800) {
    out[0] = (char) (0xC0 | ((codepoint >> 6) & 0x1F));
    out[1] = (char) (0x80 | (codepoint & 0x3F));
    *out_len = 2;
  } else if (codepoint < 0x10000) {
    out[0] = (char) (0xE0 | ((codepoint >> 12) & 0x0F));
    out[1] = (char) (0x80 | ((codepoint >> 6) & 0x3F));
    out[2] = (char) (0x80 | (codepoint & 0x3F));
    *out_len = 3;
  } else {
    out[0] = (char) (0xF0 | ((codepoint >> 18) & 0x07));
    out[1] = (char) (0x80 | ((codepoint >> 12) & 0x3F));
    out[2] = (char) (0x80 | ((codepoint >> 6) & 0x3F));
    out[3] = (char) (0x80 | (codepoint & 0x3F));
    *out_len = 4;
  }
}

static int my_json_unescape(struct mg_str json, const char *path, char *to, size_t n) {
  struct mg_str s = mg_json_get_tok(json, path);
  if (s.len > 1 && s.buf[0] == '"') {
    s.len -= 2;
    s.buf++;
    size_t i = 0, j = 0;
    while (i < s.len && j < n - 1) {
      if (s.buf[i] == '\\' && i + 5 < s.len && s.buf[i + 1] == 'u') {
        unsigned int codepoint = 0;
        for (int k = 0; k < 4; k++) {
          char c = s.buf[i + 2 + k];
          if (c >= '0' && c <= '9') codepoint = codepoint * 16 + (c - '0');
          else if (c >= 'a' && c <= 'f') codepoint = codepoint * 16 + (c - 'a' + 10);
          else if (c >= 'A' && c <= 'F') codepoint = codepoint * 16 + (c - 'A' + 10);
        }
        int utf8_len;
        unicode_to_utf8(codepoint, to + j, &utf8_len);
        j += (size_t) utf8_len;
        i += 6;
      } else if (s.buf[i] == '\\' && i + 1 < s.len) {
        char c = s.buf[i + 1];
        if (c == 'n') to[j++] = '\n';
        else if (c == 'r') to[j++] = '\r';
        else if (c == 't') to[j++] = '\t';
        else if (c == 'b') to[j++] = '\b';
        else if (c == 'f') to[j++] = '\f';
        else if (c == '"') to[j++] = '"';
        else if (c == '\\') to[j++] = '\\';
        else if (c == '/') to[j++] = '/';
        else to[j++] = c;
        i += 2;
      } else {
        to[j++] = s.buf[i++];
      }
    }
    to[j] = '\0';
    return (int) j;
  }
  return 0;
}

static void handle_nodes_batchset(struct mg_connection *c, struct mg_str body) {
  struct ds_node updates_local[MAX_NODE_UPDATES];
  int update_count = 0;

  int too_many_updates = 0;
  for (int i = 0; i < MAX_NODE_UPDATES; i++) {
    char path[64];
    snprintf(path, sizeof(path), "$.updates[%d].id", i);
    struct mg_str id_tok = mg_json_get_tok(body, path);
    if (id_tok.len == 0) break;

    my_json_unescape(body, path, updates_local[update_count].id, sizeof(updates_local[update_count].id));
    if (updates_local[update_count].id[0] == '\0') break;

    snprintf(path, sizeof(path), "$.updates[%d].operation", i);
    struct mg_str op_tok = mg_json_get_tok(body, path);
    if (op_tok.len > 0) {
      updates_local[update_count].has_operation = 1;
      my_json_unescape(body, path, updates_local[update_count].operation, sizeof(updates_local[update_count].operation));
    } else {
      updates_local[update_count].has_operation = 0;
      updates_local[update_count].operation[0] = '\0';
    }

    snprintf(path, sizeof(path), "$.updates[%d].customOperation", i);
    struct mg_str cop_tok = mg_json_get_tok(body, path);
    if (cop_tok.len > 0) {
      updates_local[update_count].has_customOperation = 1;
      my_json_unescape(body, path, updates_local[update_count].customOperation, sizeof(updates_local[update_count].customOperation));
    } else {
      updates_local[update_count].has_customOperation = 0;
      updates_local[update_count].customOperation[0] = '\0';
    }

    updates_local[update_count].name[0] = '\0';
    updates_local[update_count].channelCode[0] = '\0';
    updates_local[update_count].isOnline[0] = '\0';
    updates_local[update_count].cameraType[0] = '\0';

    update_count++;
    if (update_count == MAX_NODE_UPDATES) {
      snprintf(path, sizeof(path), "$.updates[%d].id", i + 1);
      struct mg_str next_id_tok = mg_json_get_tok(body, path);
      if (next_id_tok.len > 0) {
        too_many_updates = 1;
      }
      break;
    }
  }

  if (too_many_updates) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"status\":\"false\",\"message\":\"Too many updates, max %d allowed\"}",
                  MAX_NODE_UPDATES);
    return;
  }

  if (update_count == 0) {
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"false\",\"message\":\"No updates\"}");
    return;
  }

  struct work_request *wr = (struct work_request *) calloc(1, sizeof(*wr));
  wr->mgr = c->mgr;
  wr->conn_id = c->id;
  wr->updates = (struct ds_node *) malloc(sizeof(struct ds_node) * (size_t) update_count);
  memcpy(wr->updates, updates_local, sizeof(struct ds_node) * (size_t) update_count);
  wr->update_count = update_count;

  start_thread(nodes_batchset_thread, wr);
}

static void handle_nodes_get(struct mg_connection *c, struct mg_http_message *hm) {
  int page = 1;
  int page_size = get_default_page_size();
  char page_buf[16] = "";
  char size_buf[16] = "";
  char online_filter[MAX_FILTER_LEN] = "";
  char camera_filter[MAX_FILTER_LEN] = "";
  char operation_filter[MAX_FILTER_LEN] = "";
  char keyword_buf[MAX_KEYWORD_LEN] = "";

  mg_http_get_var(&hm->query, "page", page_buf, sizeof(page_buf));
  mg_http_get_var(&hm->query, "pageSize", size_buf, sizeof(size_buf));
  // 使用返回值判断参数是否存在于 URL 中：>=0 表示存在，-4 表示不存在
  int ret_online = mg_http_get_var(&hm->query, "isOnline", online_filter, sizeof(online_filter));
  int ret_camera = mg_http_get_var(&hm->query, "cameraType", camera_filter, sizeof(camera_filter));
  int ret_op = mg_http_get_var(&hm->query, "operation", operation_filter, sizeof(operation_filter));
  mg_http_get_var(&hm->query, "keyword", keyword_buf, sizeof(keyword_buf));

  if (page_buf[0] != '\0') page = atoi(page_buf);
  if (size_buf[0] != '\0') page_size = atoi(size_buf);

  if (page < 1 || page_size < 1) {
    mg_http_reply(c, 400, s_json_header, "{\"error\":\"Invalid parameters: page and pageSize must be positive integers\"}");
    return;
  }

  int max_page_size = get_max_page_size();
  if (page_size > max_page_size) page_size = max_page_size;

  struct work_request *wr = (struct work_request *) calloc(1, sizeof(*wr));
  wr->mgr = c->mgr;
  wr->conn_id = c->id;
  wr->page = page;
  wr->page_size = page_size;

  // 参数存在于 URL 中（ret >= 0）：即使值为空也要复制（空值 = 空字符串 = 无匹配 = 0 条）
  if (ret_online >= 0) {
    wr->has_isOnline = 1;
    strncpy(wr->isOnline_buf, online_filter, sizeof(wr->isOnline_buf) - 1);
    wr->isOnline_buf[sizeof(wr->isOnline_buf) - 1] = '\0';
  }
  if (ret_camera >= 0) {
    wr->has_cameraType = 1;
    strncpy(wr->cameraType_buf, camera_filter, sizeof(wr->cameraType_buf) - 1);
    wr->cameraType_buf[sizeof(wr->cameraType_buf) - 1] = '\0';
  }
  if (ret_op >= 0) {
    wr->has_operation = 1;
    strncpy(wr->operation_buf, operation_filter, sizeof(wr->operation_buf) - 1);
    wr->operation_buf[sizeof(wr->operation_buf) - 1] = '\0';
  }
  if (keyword_buf[0] != '\0') {
    strncpy(wr->keyword_buf, keyword_buf, sizeof(wr->keyword_buf) - 1);
    wr->keyword_buf[sizeof(wr->keyword_buf) - 1] = '\0';
  }

  start_thread(nodes_get_thread, wr);
}

static void handle_nodes_queryCategory_get(struct mg_connection *c, struct mg_http_message *hm) {
  // Return ALL matching data — page and pageSize parameters are accepted but ignored.
  (void) hm;  // query string not used

  struct work_request *wr = (struct work_request *) calloc(1, sizeof(*wr));
  wr->mgr = c->mgr;
  wr->conn_id = c->id;
  wr->page = 1;
  wr->page_size = 0;  // 0 = return all (build_nodes_queryCategory_response ignores this)

  start_thread(nodes_queryCategory_get_thread, wr);
}

// Return the value of the cookie named `name` in a Cookie header, with EXACT
// name matching: the match must start at a cookie boundary (start of the header
// or right after a ';'), so "access_token" does NOT match inside
// "secure_access_token" (which mg_http_get_header_var() does, causing auth
// failures when both cookies are present). Returns {NULL, 0} if not found.
static struct mg_str cookie_exact(struct mg_str hdr, const char *name) {
  size_t nlen = strlen(name), i = 0;
  while (i < hdr.len) {
    while (i < hdr.len && (hdr.buf[i] == ' ' || hdr.buf[i] == ';' ||
                           hdr.buf[i] == '\t')) {
      i++;
    }
    if (i + nlen < hdr.len && hdr.buf[i + nlen] == '=' &&
        memcmp(hdr.buf + i, name, nlen) == 0) {
      size_t vstart = i + nlen + 1, j = vstart;
      while (j < hdr.len && hdr.buf[j] != ';') j++;
      while (j > vstart && (hdr.buf[j - 1] == ' ' || hdr.buf[j - 1] == '\t')) j--;
      return mg_str_n(hdr.buf + vstart, j - vstart);
    }
    while (i < hdr.len && hdr.buf[i] != ';') i++;
  }
  return mg_str_n(NULL, 0);
}

static struct user *authenticate(struct mg_http_message *hm) {
  char user[64], pass[128];
  struct user *u, *result = NULL;
  mg_http_creds(hm, user, sizeof(user), pass, sizeof(pass));

  // mg_http_creds() reads the "access_token" cookie via mg_http_get_header_var(),
  // which matches cookie names by SUBSTRING (it has no boundary check). So
  // "access_token" also matches inside "secure_access_token". If a browser holds
  // BOTH cookies — e.g. it once logged in over HTTPS (when the cookie name was
  // "secure_access_token") and browsers treat localhost as a secure context, so
  // that Secure cookie is still sent over http://localhost:8000 — mongoose picks
  // up the STALE secure_access_token value and every request fails with 403,
  // even right after a fresh login. When no Authorization header is present
  // (Basic/Bearer didn't supply creds), re-parse the cookie ourselves with exact
  // name matching so the right cookie wins.
  if (mg_http_get_header(hm, "Authorization") == NULL) {
    user[0] = '\0';
    pass[0] = '\0';
    struct mg_str *ck = mg_http_get_header(hm, "Cookie");
    if (ck != NULL) {
      struct mg_str t = cookie_exact(*ck, "access_token");
      if (t.len == 0) t = cookie_exact(*ck, "secure_access_token");  // legacy
      if (t.len > 0)
        mg_snprintf(pass, sizeof(pass), "%.*s", (int) t.len, t.buf);
    } else {
      // No cookie; fall back to ?access_token= query parameter.
      mg_http_get_var(&hm->query, "access_token", pass, sizeof(pass));
    }
  }

  // Ensure config is loaded (for global API token)
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf();
    if (buf) parse_config_fields(buf);
  }
  cfg_unlock();

  // Custom "apiToken" header: if it matches the apiToken from data_config.json,
  // authenticate as the global token user. Header-based alternative to the
  // access_token cookie/query param for API clients (scripts, Postman, etc.).
  // NOTE: header name MUST be exactly "apiToken" (no hyphen). mongoose's
  // mg_http_get_header is case-insensitive on the name, but "apiToken" and
  // "Api-Token" are genuinely different headers (the hyphen changes the token).
  // All test scripts use "apiToken"; using "Api-Token" causes 100% 403 failures.
  if (s_global_api_token[0] != '\0') {
    struct mg_str *api_hdr = mg_http_get_header(hm, "apiToken");
    if (api_hdr != NULL && api_hdr->len > 0) {
      size_t tlen = strlen(s_global_api_token);
      // Strip optional whitespace (OWS) from header value before comparison,
      // since clients/proxies may pad the value with spaces or tabs.
      const char *vbuf = api_hdr->buf;
      size_t vlen = api_hdr->len;
      while (vlen > 0 && (*vbuf == ' ' || *vbuf == '\t')) { vbuf++; vlen--; }
      while (vlen > 0 && (vbuf[vlen - 1] == ' ' || vbuf[vlen - 1] == '\t'))
        vlen--;
      if (vlen == tlen && memcmp(vbuf, s_global_api_token, tlen) == 0) {
        MG_VERBOSE(("Authenticated via apiToken header"));
        return &global_token_user;
      }
    }
  }

  if (user[0] != '\0' && pass[0] != '\0') {
    for (u = s_users; result == NULL && u->name != NULL; u++)
      if (strcmp(user, u->name) == 0 && strcmp(pass, u->pass) == 0) result = u;
  } else if (user[0] == '\0' && pass[0] != '\0') {
    // Check global API token first (fixed token from config for Postman etc.)
    if (s_global_api_token[0] != '\0' && strcmp(pass, s_global_api_token) == 0) {
      MG_VERBOSE(("Authenticated via global API token"));
      return &global_token_user;
    }
    for (u = s_users; result == NULL && u->name != NULL; u++)
      if (strcmp(pass, u->access_token) == 0) result = u;
  }
  return result;
}

static struct user *find_user_by_creds(const char *name, const char *pw) {
  if (name == NULL || pw == NULL || name[0] == '\0' || pw[0] == '\0') return NULL;
  for (struct user *u = s_users; u->name[0] != '\0'; u++)
    if (strcmp(name, u->name) == 0 && strcmp(pw, u->pass) == 0) return u;
  return NULL;
}

static void handle_login(struct mg_connection *c, struct mg_http_message *hm,
                         struct user *u) {
  // Support 3 credential sources for /api/login:
  //   1. Basic auth header or valid cookie (authenticate() already resolved via `u`)
  //   2. POST body JSON: {"user":"xxx","password":"yyy"}
  //   3. POST form-encoded: user=xxx&password=yyy
  if (u == NULL && hm != NULL && hm->body.len > 0) {
    char nm[sizeof(((struct user*)0)->name)] = {0};
    char pw[sizeof(((struct user*)0)->pass)] = {0};
    size_t nm_n = mg_json_unescape(hm->body, "$.user", nm, sizeof(nm));
    size_t pw_n = mg_json_unescape(hm->body, "$.password", pw, sizeof(pw));
    if (nm_n > 0 && pw_n > 0) {
      u = find_user_by_creds(nm, pw);
    } else {
      mg_http_get_var(&hm->body, "user", nm, sizeof(nm));
      mg_http_get_var(&hm->body, "password", pw, sizeof(pw));
      if (nm[0] != '\0' && pw[0] != '\0') {
        u = find_user_by_creds(nm, pw);
      }
    }
  }
  if (u == NULL) {
    mg_http_reply(c, 401, s_json_header,
                  "{\"status\":\"false\",\"message\":\"Invalid credentials\"}");
    return;
  }
  mg_random_str(u->access_token, sizeof(u->access_token));
  MG_INFO(("User [%s] logged in, new token generated", u->name));
  char cookie[512];
  mg_snprintf(cookie, sizeof(cookie),
              "Set-Cookie: access_token=%s; Path=/; "
              "HttpOnly; SameSite=Lax; Max-Age=%d\r\n"
              "Content-Type: application/json\r\n",
              u->access_token, 3600 * 24);
  mg_http_reply(c, 200, cookie,
                "{\"status\":\"true\",%m:%m,%m:%m}",
                MG_ESC("user"), MG_ESC(u->name),
                MG_ESC("token"), MG_ESC(u->access_token));
}

static void handle_logout(struct mg_connection *c, struct user *u) {
  if (u != NULL && u->name != NULL && strcmp(u->name, "_api_token_") != 0) {
    memset(u->access_token, 0, sizeof(u->access_token));
    MG_INFO(("User [%s] logged out, token cleared", u->name));
  }
  char cookie[512];
  mg_snprintf(cookie, sizeof(cookie),
              "Set-Cookie: access_token=; Path=/; "
              "Expires=Thu, 01 Jan 1970 00:00:00 UTC; "
              "HttpOnly; Max-Age=0\r\n"
              "Content-Type: application/json\r\n");
  mg_http_reply(c, 200, cookie, "{\"status\":\"true\"}");
}

static void fn(struct mg_connection *c, int ev, void *ev_data) {
  if (ev == MG_EV_ACCEPT) {
    if (c->is_tls) {
      struct mg_tls_opts opts = {0};
      // NOTE: mg_file_read(NULL, ...) crashes because it dereferences fs->st.
      // Must pass &mg_fs_posix explicitly (mg_http_serve_dir defaults to it
      // internally, but mg_file_read does not).
      opts.cert = mg_file_read(&mg_fs_posix, "certs/server_cert.pem");
      opts.key = mg_file_read(&mg_fs_posix, "certs/server_key.pem");
      fprintf(stderr, "[TLS] conn %lu cert=%p(%lu) key=%p(%lu)\n", c->id,
              (void *)opts.cert.buf, (unsigned long)opts.cert.len,
              (void *)opts.key.buf, (unsigned long)opts.key.len);
      if (opts.cert.buf == NULL || opts.key.buf == NULL) {
        MG_ERROR(("Failed to load TLS cert/key (cert=%p key=%p) - closing TLS connection %lu",
                  (void *) opts.cert.buf, (void *) opts.key.buf, c->id));
        // Free whichever one succeeded and close the connection cleanly
        // instead of passing NULLs to mg_tls_init which would leave the
        // connection in a half-initialised TLS state.
        if (opts.cert.buf != NULL) mg_free((void *) opts.cert.buf);
        if (opts.key.buf != NULL) mg_free((void *) opts.key.buf);
        c->is_draining = 1;
      } else {
        mg_tls_init(c, &opts);
        fprintf(stderr, "[TLS] mg_tls_init done conn %lu c->tls=%p is_tls=%d is_tls_hs=%d\n",
                c->id, c->tls, c->is_tls, c->is_tls_hs);
      }
    }
  } else if (ev == MG_EV_HTTP_MSG) {
    struct mg_http_message *hm = (struct mg_http_message *) ev_data;
    struct user *u = authenticate(hm);

    // Path traversal defence: reject any URI containing ".." before routing.
    // mongoose's mg_http_serve_dir normalises "/../" back to "/" and serves
    // index.html (HTTP 200), leaking the parent dir listing. Reject early so
    // "/../", "/../net.c", "/%2e%2e/" etc. all return 403.
    if (mg_match(hm->uri, mg_str("*..*"), NULL)) {
      mg_http_reply(c, 403, "", "Forbidden\n");
      // PUBLIC routes (no auth required): /api/login (credentials via body) and
      // /api/mode/get (returns only the backend mode name + availability, needed
      // by the frontend BEFORE login to render the mode badge correctly).
    } else if (mg_match(hm->uri, mg_str("/api/login"), NULL)) {
      handle_login(c, hm, u);
    } else if (mg_match(hm->uri, mg_str("/api/mode/get"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        mg_http_reply(c, 200, s_json_header, "{\"mode\":\"%s\",\"available\":%d}", DS_MODE, ds_is_available());
      }
      // ALL OTHER /api/* ENDPOINTS REQUIRE AUTHENTICATION: reject u==NULL here,
      // BEFORE any route handler runs (prevents "route order accidentally skips auth" bugs).
    } else if (mg_match(hm->uri, mg_str("/api/#"), NULL) && u == NULL) {
      mg_http_reply(c, 403, s_json_header, "{\"status\":\"false\",\"message\":\"Not Authorised\"}");
      // ---- Protected routes below (guaranteed u != NULL for /api/*) ----
    } else if (mg_match(hm->uri, mg_str("/api/nodes/queryCategory"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_nodes_queryCategory_get(c, hm);
      }
    } else if (mg_match(hm->uri, mg_str("/api/nodes/get"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_nodes_get(c, hm);
      }
    } else if (mg_match(hm->uri, mg_str("/api/nodes/batchset"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("POST")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_nodes_batchset(c, hm->body);
      }
    } else if (mg_match(hm->uri, mg_str("/api/logout"), NULL)) {
      handle_logout(c, u);
    } else {
      struct mg_http_serve_opts opts;
      memset(&opts, 0, sizeof(opts));
      opts.root_dir = "web_root";
      mg_http_serve_dir(c, ev_data, &opts);
    }
    if (c->send.len > 9) {
      MG_DEBUG(("%lu %.*s %.*s -> %.*s", c->id, (int) hm->method.len,
                hm->method.buf, (int) hm->uri.len, hm->uri.buf, (int) 3,
                &c->send.buf[9]));
    }
  }
}

void web_init(struct mg_mgr *mgr) {
  results_mutex_init();
  cfg_mutex_init();
  generate_access_tokens();
  MG_INFO(("Web server starting in %s mode", DS_MODE));
  mg_timer_add(mgr, 20, MG_TIMER_REPEAT, dispatch_results, mgr);
  mg_http_listen(mgr, HTTP_URL, fn, NULL);
  mg_http_listen(mgr, HTTPS_URL, fn, NULL);
}
