// Copyright (c) 2023 Cesanta Software Limited
// All rights reserved

#include "net.h"
#include "password_hash.h"
#include "data_source.h"
#include <math.h>

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
#define MAX_USERS               16
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
static char *get_config_buf_unlocked(void);
static char *get_config_buf(void);
static int get_max_page_size(void);
static int get_default_page_size(void);
static void load_users_from_config(const char *cfg_buf);
static void alarm_dispatch_results(struct mg_mgr *mgr);

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
  int httpPort;
  int httpsPort;
  int enableVideoDevices;          // 1=启用视频设备, 0=禁用（侧边栏/接口/数据加载全部跳过）
  int alarmEventAll;               // 1=告警含ALARM+RECOVER, 0=仅ALARM（配置项 event_type）
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

// Parse config fields using mg_json_get_tok / mg_json_get_num / mg_json_unescape
// for proper JSON parsing. The previous hand-rolled strstr/strchr parser was
// fragile and could not handle escaped characters or whitespace variations.
static void parse_config_fields(const char *cfg_buf) {
  struct cfg_parsed *cp = &g_cfg_parsed;
  memset(cp, 0, sizeof(*cp));

  cp->defaultPageSize = 50;
  cp->maxPageSize = 100;
  cp->httpPort = HTTP_PORT;
  cp->httpsPort = HTTPS_PORT;
  cp->enableVideoDevices = 1;  // 默认启用
  cp->alarmEventAll = 1;       // 默认含 ALARM+RECOVER

  struct mg_str json = mg_str((char *)cfg_buf);
  double v;

  // 视频设备总开关：true=启用，false/no/0=禁用
  {
    bool bv = true;
    if (mg_json_get_bool(json, "$.enableVideoDevices", &bv) && !bv) {
      cp->enableVideoDevices = 0;
    }
  }

  // 告警事件范围：true=ALARM+RECOVER，false=仅ALARM
  {
    bool bv = true;
    if (mg_json_get_bool(json, "$.event_type", &bv) && !bv) {
      cp->alarmEventAll = 0;
    }
  }

  // Parse numeric fields via mg_json_get_num
  if (mg_json_get_num(json, "$.defaultPageSize", &v) && v > 0)
    cp->defaultPageSize = (int)v;
  if (mg_json_get_num(json, "$.maxPageSize", &v) && v > 0)
    cp->maxPageSize = (int)v;
  if (mg_json_get_num(json, "$.httpPort", &v) && v >= 1 && v <= 65535)
    cp->httpPort = (int)v;
  if (mg_json_get_num(json, "$.httpsPort", &v) && v >= 1 && v <= 65535)
    cp->httpsPort = (int)v;

  // Parse apiToken string via mg_json_unescape (handles \" \\ \/ etc.)
  size_t tlen = mg_json_unescape(json, "$.apiToken",
                                 s_global_api_token, sizeof(s_global_api_token));
  if (tlen > 0)
    MG_INFO(("Global API token loaded: %s", s_global_api_token));

  // Parse fields array via mg_json_get_tok for the whole array, then iterate
  // with mg_json_unescape for each $.fields[i].key
  struct mg_str cfg_fields_tok = mg_json_get_tok(json, "$.fields");
  cp->fields_tok_len = cfg_fields_tok.len;

  if (cfg_fields_tok.len > 0) {
    cp->fields_json_copy = (char *)malloc((size_t)cfg_fields_tok.len + 1);
    if (cp->fields_json_copy) {
      memcpy(cp->fields_json_copy, cfg_fields_tok.buf, (size_t)cfg_fields_tok.len);
      cp->fields_json_copy[cfg_fields_tok.len] = '\0';

      for (int i = 0; i < MAX_FIELD_KEYS; i++) {
        char path[48];
        mg_snprintf(path, sizeof(path), "$.fields[%d].key", i);
        size_t klen = mg_json_unescape(json, path,
                                       cp->field_keys[cp->field_key_count],
                                       MAX_FIELD_KEY_LEN);
        if (klen == 0) break;  // No more items in array
        cp->field_key_count++;
      }
    }
  }

  // Load users from config into s_users
  load_users_from_config(cfg_buf);

  g_cfg_parsed_valid = 1;
}

static int get_max_page_size(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf_unlocked();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.maxPageSize;
  cfg_unlock();
  return val;
}

static int get_default_page_size(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf_unlocked();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.defaultPageSize;
  cfg_unlock();
  return val;
}

// 视频设备总开关：1=启用，0=禁用（配置项 enableVideoDevices）
static int video_devices_enabled(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf_unlocked();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.enableVideoDevices;
  cfg_unlock();
  return val;
}

// 告警事件范围：1=ALARM+RECOVER，0=仅ALARM（配置项 event_type）
static int alarm_event_all(void) {
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf_unlocked();
    if (buf) parse_config_fields(buf);
  }
  int val = g_cfg_parsed.alarmEventAll;
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
          if (pos + 6 > cap) return cap;  // 截断：必须返回 cap 让调用方感知
          out[pos++] = '\\';
          out[pos++] = 'u';
          out[pos++] = '0';
          out[pos++] = '0';
          out[pos++] = hex[c >> 4];
          out[pos++] = hex[c & 0xf];
        } else {
          if (pos + 1 > cap) return cap;  // 截断
          out[pos++] = (char) c;
        }
        continue;
    }
    if (pos + seqlen > cap) return cap;  // 截断
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

#if defined(PG_MODE)
#define DS_MODE "PostgreSQL"
#define DS_MODE_TYPE 2
#else
#define DS_MODE "SQLite"
#define DS_MODE_TYPE 1
#endif

struct user {
  char name[64];
  char pass[128];          // Plaintext password (legacy fallback when passHash absent)
  char passHash[65];       // 64 hex chars + '\0' — 10000 iterations of SHA256(salt+password)
  char salt[33];           // 32 hex chars + '\0'
  char access_token[65];   // 64 chars + '\0'
};

// Users are loaded from data_config.json at startup (see load_users_from_config).
// The last element is an all-zero sentinel (name[0] == '\0').
static struct user s_users[MAX_USERS + 1];

// Sentinel user returned when a request authenticates via the global Api-Token
// (from data_config.json) — through the "Api-Token" header, the access_token
// cookie / query param, or a Bearer token. Has no password and no session
// token: the Api-Token itself is the credential.
static struct user global_token_user = {"_api_token_", "", "", "", ""};

// hash_password / ct_memcmp / verify_password live in password_hash.h
// (header-only, so unit tests can exercise the real implementation).

static void generate_access_tokens(void) {
  for (struct user *u = s_users; u->name[0] != '\0'; u++) {
    mg_random_str(u->access_token, sizeof(u->access_token));
    MG_INFO(("Generated token for user [%s]", u->name));
  }
}

// Parse "users" array from data_config.json into s_users.
// JSON format (preferred — hashed):
//   {"name":"xxx","passHash":"<64 hex>","salt":"<32 hex>"}
// JSON format (legacy — plaintext, auto-migrated to hash on first login attempt):
//   {"name":"xxx","pass":"yyy"}
// Uses mg_json_get_tok / mg_json_unescape for proper JSON parsing,
// which correctly handles escaped characters (e.g. \" \\ \/) in names
// and passwords — the previous hand-rolled strstr/strchr parser broke
// on passwords containing double-quote characters.
// Called from parse_config_fields under cfg_lock.
static void load_users_from_config(const char *cfg_buf) {
  memset(s_users, 0, sizeof(s_users));
  struct mg_str json = mg_str((char *)cfg_buf);

  int count = 0;
  for (int i = 0; i < MAX_USERS; i++) {
    char path[40];
    // Try $.users[i].name
    mg_snprintf(path, sizeof(path), "$.users[%d].name", i);
    size_t nlen = mg_json_unescape(json, path,
                                   s_users[count].name, sizeof(s_users[0].name));
    if (nlen == 0) break;  // No more users in array

    // Preferred: $.users[i].passHash (64 hex chars) + $.users[i].salt (32 hex chars)
    mg_snprintf(path, sizeof(path), "$.users[%d].passHash", i);
    size_t hlen = mg_json_unescape(json, path,
                                   s_users[count].passHash, sizeof(s_users[0].passHash));
    mg_snprintf(path, sizeof(path), "$.users[%d].salt", i);
    size_t slen = mg_json_unescape(json, path,
                                   s_users[count].salt, sizeof(s_users[0].salt));

    if (hlen == 64 && slen == 32) {
      // Hashed password entry — preferred path
      MG_INFO(("User loaded (hashed) from config: [%s]", s_users[count].name));
      count++;
      continue;
    }

    // Fallback: $.users[i].pass (plaintext, legacy)
    s_users[count].passHash[0] = '\0';
    s_users[count].salt[0] = '\0';
    mg_snprintf(path, sizeof(path), "$.users[%d].pass", i);
    size_t plen = mg_json_unescape(json, path,
                                   s_users[count].pass, sizeof(s_users[0].pass));
    if (plen == 0) continue;  // No pass and no passHash — skip

    MG_INFO(("User loaded (plaintext, legacy) from config: [%s]", s_users[count].name));
    count++;
  }

  MG_INFO(("Loaded %d user(s) from data_config.json", count));
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
  char *headers;  // 可选：非 NULL 时覆盖默认 JSON 响应头（CSV 导出等）
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

static void push_result_headers(unsigned long conn_id, int http_status,
                                char *body, size_t len, char *headers) {
  struct async_result *r = (struct async_result *) calloc(1, sizeof(*r));
  if (r == NULL) {
    if (body != NULL) free(body);
    if (headers != NULL) free(headers);
    return;
  }
  r->conn_id = conn_id;
  r->http_status = http_status;
  r->body = body;
  r->len = len;
  r->headers = headers;
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

static void push_result(unsigned long conn_id, int http_status,
                        char *body, size_t len) {
  push_result_headers(conn_id, http_status, body, len, NULL);
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
        mg_http_reply(c, r->http_status,
                      r->headers != NULL ? r->headers : s_json_header,
                      "%.*s", (int) r->len, r->body);
        break;
      }
    }
    if (r->body != NULL) free(r->body);
    if (r->headers != NULL) free(r->headers);
    free(r);
    r = next;
  }

  // 消费告警轮询 worker 线程的查询结果（见告警区块）
  alarm_dispatch_results(mgr);
}

static char *g_cfg_buf = NULL;
static time_t g_cfg_mtime = 0;

static char *get_config_buf_unlocked(void) {
  if (g_cfg_buf != NULL) {
    struct stat st;
    if (stat("data_config.json", &st) == 0 && st.st_mtime == g_cfg_mtime) {
      return g_cfg_buf;
    }
    free(g_cfg_buf);
    g_cfg_buf = NULL;
  }

  FILE *fp = fopen("data_config.json", "rb");
  if (fp == NULL) {
    return NULL;
  }

  fseek(fp, 0, SEEK_END);
  long size = ftell(fp);
  fseek(fp, 0, SEEK_SET);

  g_cfg_buf = (char *) malloc((size_t) size + 1);
  if (g_cfg_buf == NULL) {
    fclose(fp);
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

  return g_cfg_buf;
}

static char *get_config_buf(void) {
  cfg_lock();
  char *result = get_config_buf_unlocked();
  cfg_unlock();
  return result;
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
    char *buf = get_config_buf_unlocked();
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
    for (u = s_users; result == NULL && u->name[0] != '\0'; u++)
      if (strcmp(user, u->name) == 0 && verify_password(u->passHash, u->salt, u->pass, pass)) result = u;
  } else if (user[0] == '\0' && pass[0] != '\0') {
    // Session tokens are compared in constant time to avoid timing
    // side-channels on long-lived 64-char random credentials.
    size_t pwlen = strlen(pass);
    // Check global API token first (fixed token from config for Postman etc.)
    size_t gtlen = strlen(s_global_api_token);
    if (gtlen > 0 && pwlen == gtlen &&
        ct_memcmp(pass, s_global_api_token, gtlen) == 0) {
      MG_VERBOSE(("Authenticated via global API token"));
      memset(user, 0, sizeof(user));
      memset(pass, 0, sizeof(pass));
      return &global_token_user;
    }
    for (u = s_users; result == NULL && u->name[0] != '\0'; u++) {
      size_t tlen = strlen(u->access_token);
      if (tlen > 0 && pwlen == tlen &&
          ct_memcmp(pass, u->access_token, tlen) == 0)
        result = u;
    }
  }
  // Scrub credentials from the stack frame before returning.
  memset(user, 0, sizeof(user));
  memset(pass, 0, sizeof(pass));
  return result;
}

static struct user *find_user_by_creds(const char *name, const char *pw) {
  if (name == NULL || pw == NULL || name[0] == '\0' || pw[0] == '\0') return NULL;
  for (struct user *u = s_users; u->name[0] != '\0'; u++)
    if (strcmp(name, u->name) == 0 && verify_password(u->passHash, u->salt, u->pass, pw)) return u;
  return NULL;
}

// ---- 告警实时推送（wind_alarm 增量轮询 + WebSocket 广播，仅 PG 模式） ----
// 数据流：1s 定时器投递查询任务 -> worker 线程阻塞查库（ds_get_new_alarms，
// 内部经 pg 全局 mutex 与 nodes 查询串行化）-> 结果挂 s_alarm_results 链表
// （复用 results 临界区）-> 事件循环的 dispatch_results（20ms）消费：游标
// 推进、追加内存快照缓存、广播给所有标记 'A' 的 WS 连接；新连接先发
// hello + 快照（旧->新），后续增量推送。
// 与 /api/nodes/get 同一异步模式：DB 抖动/慢查询只阻塞 worker 线程，
// 事件循环不受影响；s_alarm_query_inflight 防止上轮未回时任务堆积。
#define ALARM_POLL_MS        1000  // 轮询周期
#define ALARM_POLL_LIMIT     100   // 单轮最多拉取行数（主键索引扫描，毫秒级）
#define ALARM_CACHE_MAX      200   // 快照缓存容量（新连接重放最近 200 条）
#define ALARM_JSON_ROW_MAX   1536  // 单行 JSON 预算（detail 已扩到 1KB）
#define ALARM_WS_SEND_MAX    (8 * 1024 * 1024)  // 慢客户端发送缓冲上限，超出即断开

static struct ds_alarm s_alarm_cache[ALARM_CACHE_MAX];
static int s_alarm_cache_count = 0;             // 有效行数（旧 -> 新）
static unsigned long long s_alarm_last_id = 0;  // 已拉取的最大事件 id（游标）
static int s_alarm_poll_enabled = 0;            // web_init 时探测一次

static int is_alarm_watcher(struct mg_connection *c) {
  return c->data[0] == 'A' && c->is_websocket;
}

static int have_alarm_watchers(struct mg_mgr *mgr) {
  int count = 0;
  for (struct mg_connection *c = mgr->conns; c != NULL; c = c->next) {
    if (is_alarm_watcher(c)) count++;
  }
  return count;
}

// 带截断保护的格式化追加：返回新 pos；空间不足时停在 cap（JSON 残缺仅影响
// 单条消息，预算内不会发生）。仅用于字面量/数字等可预测长度的片段。
static int alarm_append(char *out, int pos, int cap, const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  int room = cap - pos;
  int n = vsnprintf(out + pos, room > 0 ? (size_t) room : 0, fmt, ap);
  va_end(ap);
  if (n < 0) return pos;
  if (n >= room) return cap;  // 截断
  return pos + n;
}

// 数值字段：非有限值（NaN/Infinity，可能来自库里的异常数据）输出 JSON null，
// 避免生成 nan/inf 这类非法字面量导致整帧被前端 JSON.parse 丢弃。
static int alarm_append_num(char *out, int pos, int cap, const char *fmt,
                            double v) {
  if (!isfinite(v)) return alarm_append(out, pos, cap, "null");
  return alarm_append(out, pos, cap, fmt, v);
}

// 单行告警 JSON（字段顺序与前端展示一致）
static int alarm_row_json(char *out, int pos, int cap, const struct ds_alarm *r) {
  pos = alarm_append(out, pos, cap,
                     "{\"id\":%llu,\"event\":\"%s\","
                     "\"ts\":\"%s\",\"region_company_name\":\"",
                     r->id, r->event, r->ts);
  pos = json_escape_append(out, pos, cap, r->region_company_name);
  pos = alarm_append(out, pos, cap, "\",\"station_name\":\"");
  pos = json_escape_append(out, pos, cap, r->station_name);
  pos = alarm_append(out, pos, cap, "\",\"device_id\":\"");
  pos = json_escape_append(out, pos, cap, r->device_id);
  pos = alarm_append(out, pos, cap, "\",\"device_name\":\"");
  pos = json_escape_append(out, pos, cap, r->device_name);
  pos = alarm_append(out, pos, cap, "\",\"point_code\":\"");
  pos = json_escape_append(out, pos, cap, r->point_code);
  pos = alarm_append(out, pos, cap, "\",\"point_name\":\"");
  pos = json_escape_append(out, pos, cap, r->point_name);
  pos = alarm_append(out, pos, cap, "\",\"rule_code\":\"");
  pos = json_escape_append(out, pos, cap, r->rule_code);
  pos = alarm_append(out, pos, cap, "\",\"temperature\":");
  pos = alarm_append_num(out, pos, cap, "%.2f", r->temperature);
  pos = alarm_append(out, pos, cap, ",\"temp_rate\":");
  pos = alarm_append_num(out, pos, cap, "%.4f", r->temp_rate);
  pos = alarm_append(out, pos, cap, ",\"range_1h\":");
  pos = alarm_append_num(out, pos, cap, "%.2f", r->range_1h);
  pos = alarm_append(out, pos, cap, ",\"range_20s\":");
  pos = alarm_append_num(out, pos, cap, "%.2f", r->range_20s);
  pos = alarm_append(out, pos, cap, ",\"alarm_type\":\"");
  pos = json_escape_append(out, pos, cap, r->alarm_type);
  pos = alarm_append(out, pos, cap, "\",\"alarm_value\":");
  pos = alarm_append_num(out, pos, cap, "%.2f", r->alarm_value);
  pos = alarm_append(out, pos, cap, ",\"detail\":\"");
  pos = json_escape_append(out, pos, cap, r->detail);
  pos = alarm_append(out, pos, cap, "\"}");
  return pos;
}

// 动态预算的告警帧构建：{"prefix","items":[row,...]}，以 n*768B 起步，
// 截断（pos==cap）则预算翻倍重试（上限 16MB），杜绝整帧静默丢弃。
// 成功返回 malloc 缓冲（调用方 free）并写 out_len；失败返回 NULL。
static char *alarm_build_items_frame(const char *prefix,
                                     const struct ds_alarm *rows, int n,
                                     size_t *out_len) {
  size_t cap = (size_t) n * ALARM_JSON_ROW_MAX + 256;
  if (cap < 1024) cap = 1024;
  for (int attempt = 0; attempt < 6; attempt++) {
    if (cap > (size_t) 16 * 1024 * 1024) break;
    char *buf = (char *) malloc(cap);
    if (buf == NULL) return NULL;
    int pos = alarm_append(buf, 0, (int) cap, "%s", prefix);
    for (int i = 0; i < n; i++) {
      if (i > 0) pos = alarm_append(buf, pos, (int) cap, ",");
      pos = alarm_row_json(buf, pos, (int) cap, &rows[i]);
    }
    pos = alarm_append(buf, pos, (int) cap, "]}");
    if (pos < (int) cap) {  // 未截断：帧完整
      if (attempt > 0) {
        MG_INFO(("alarm frame needed enlarged budget: %zu bytes (n=%d)", cap, n));
      }
      *out_len = (size_t) pos;
      return buf;
    }
    free(buf);
    cap *= 2;
  }
  return NULL;
}

static void alarm_send_hello(struct mg_connection *c) {
  char buf[96];
  int n = snprintf(buf, sizeof(buf), "{\"type\":\"hello\",\"supported\":%s}",
                   s_alarm_poll_enabled ? "true" : "false");
  if (n > 0) mg_ws_send(c, buf, (size_t) n, WEBSOCKET_OP_TEXT);
}

// 快照：把缓存整体发给新连接（旧 -> 新，单帧）。缓存 <= 200 行，帧预算自适应。
static void alarm_send_snapshot(struct mg_connection *c) {
  size_t len = 0;
  char *buf = alarm_build_items_frame("{\"type\":\"snapshot\",\"items\":[",
                                      s_alarm_cache, s_alarm_cache_count, &len);
  if (buf == NULL) {
    MG_ERROR(("alarm snapshot frame build failed (cache=%d rows)",
              s_alarm_cache_count));
    return;
  }
  mg_ws_send(c, buf, len, WEBSOCKET_OP_TEXT);
  free(buf);
}

// 1s 定时器：投递查询任务（查询本身在 worker 线程执行，见 alarm_query_thread）
static struct alarm_work {
  struct mg_mgr *mgr;
  unsigned long long last_id;              // 查询起点游标（投递时快照）
  struct ds_alarm rows[ALARM_POLL_LIMIT];
  int n;                                   // worker 填：行数，<0 查询错误
  struct alarm_work *next;
} *s_alarm_results_head = NULL, *s_alarm_results_tail = NULL;
static int s_alarm_query_inflight = 0;     // 仅事件循环线程读写

// worker 线程：阻塞查库，结果挂链表（复用 results 临界区保护）
static void alarm_query_thread(void *param) {
  struct alarm_work *w = (struct alarm_work *) param;
  w->n = ds_get_new_alarms(w->last_id, w->rows, ALARM_POLL_LIMIT);
  w->next = NULL;
  results_lock();
  if (s_alarm_results_tail != NULL) {
    s_alarm_results_tail->next = w;
    s_alarm_results_tail = w;
  } else {
    s_alarm_results_head = s_alarm_results_tail = w;
  }
  results_unlock();
}

// 事件循环线程：游标推进 + 快照缓存追加 + 广播
static void alarm_handle_rows(struct mg_mgr *mgr, const struct ds_alarm *rows,
                              int n) {
  // 追加进快照缓存（满则丢最老），游标推进到最后一条
  for (int i = 0; i < n; i++) {
    if (s_alarm_cache_count == ALARM_CACHE_MAX) {
      memmove(&s_alarm_cache[0], &s_alarm_cache[1],
              sizeof(struct ds_alarm) * (size_t) (ALARM_CACHE_MAX - 1));
      s_alarm_cache_count--;
    }
    s_alarm_cache[s_alarm_cache_count++] = rows[i];
    s_alarm_last_id = rows[i].id;
  }

  // 广播前先确认有 watcher，避免无谓的 JSON 构建
  struct mg_connection *c;
  bool have_watcher = false;
  for (c = mgr->conns; c != NULL; c = c->next) {
    if (is_alarm_watcher(c)) { have_watcher = true; break; }
  }
  if (!have_watcher) return;

  size_t len = 0;
  char *buf = alarm_build_items_frame("{\"type\":\"alarm\",\"items\":[",
                                      rows, n, &len);
  if (buf == NULL) {
    MG_ERROR(("alarm broadcast frame build failed (n=%d rows dropped)", n));
    return;
  }
  for (c = mgr->conns; c != NULL; c = c->next) {
    if (is_alarm_watcher(c)) {
      // 背压保护：慢客户端不消费时发送缓冲会持续增长，超阈值直接断开，
      // 让客户端按重连逻辑重新拉快照，避免服务端内存无上限膨胀。
      if (c->send.len > ALARM_WS_SEND_MAX) {
        MG_ERROR(("alarm ws client %lu too slow (%lu bytes queued), closing",
                  c->id, (unsigned long) c->send.len));
        c->is_closing = 1;
        continue;
      }
      mg_ws_send(c, buf, len, WEBSOCKET_OP_TEXT);
    }
  }
  free(buf);
}

static void alarm_poll_timer(void *arg) {
  struct mg_mgr *mgr = (struct mg_mgr *) arg;
  if (!s_alarm_poll_enabled) return;
  if (s_alarm_query_inflight) return;  // 上轮未回（DB 慢），跳过本轮
  struct alarm_work *w = (struct alarm_work *) calloc(1, sizeof(*w));
  if (w == NULL) return;
  w->mgr = mgr;
  w->last_id = s_alarm_last_id;
  s_alarm_query_inflight = 1;
  start_thread(alarm_query_thread, w);
}

// 事件循环线程：由 dispatch_results 每 20ms 调用，消费 worker 查回的结果
static void alarm_dispatch_results(struct mg_mgr *mgr) {
  results_lock();
  struct alarm_work *w = s_alarm_results_head;
  s_alarm_results_head = s_alarm_results_tail = NULL;
  results_unlock();

  while (w != NULL) {
    struct alarm_work *wnext = w->next;
    s_alarm_query_inflight = 0;  // 事件循环独占写，允许下一轮投递
    MG_DEBUG(("alarm result n=%d watchers=%d", w->n,
              have_alarm_watchers(mgr)));
    if (w->n > 0) {
      alarm_handle_rows(mgr, w->rows, w->n);
    } else if (w->n < 0) {
      MG_ERROR(("alarm poll query failed, cursor kept at %llu", w->last_id));
    }
    free(w);
    w = wnext;
  }
}

// ---- 告警历史分页查询（/api/alarms/get，仅 PG 模式） ----
// 与 /api/nodes/get 同一异步模型：worker 线程查库 + 构建 JSON，事件循环
// 线程由 dispatch_results 回写 HTTP。实时页仍走 WS，本接口只服务历史页。
#define ALARM_PAGE_MAX 200  // 单页行数上限（200 = data_config.maxPageSize）
#define ALARM_PAGE_INDEX_MAX 10000  // 页码上限：防止超大 OFFSET 拖垮数据库

struct alarm_page_work {
  unsigned long long conn_id;
  int page;
  int page_size;
  char point_code[96];  // 可选精确过滤；空串 = 不过滤
  char rule_code[48];   // 可选精确过滤；空串 = 不过滤
  char event[16];       // 可选精确过滤；空串 = 不过滤（ALARM / RECOVER）
  int n;                    // worker 填：行数，<0 查询失败
  unsigned long long total; // worker 填：匹配总数
  struct ds_alarm rows[ALARM_PAGE_MAX];
};

// worker 线程：查库 + 组装 {"total":N,"page":p,"pageSize":s,"items":[...]}
static void alarm_page_thread(void *param) {
  struct alarm_page_work *w = (struct alarm_page_work *) param;
  unsigned long long total = 0;
  int n = ds_get_alarms_page(w->page, w->page_size, &total, w->rows,
                             ALARM_PAGE_MAX,
                             w->point_code[0] ? w->point_code : NULL,
                             w->rule_code[0] ? w->rule_code : NULL,
                             w->event[0] ? w->event : NULL);
  w->n = n;
  w->total = total;

  int status = 200;
  char *body = NULL;
  size_t len = 0;
  if (n < 0) {
    status = 503;
    body = strdup("{\"error\":\"Alarm history query failed or unsupported\"}");
  } else {
    char prefix[128];
    snprintf(prefix, sizeof(prefix),
             "{\"total\":%llu,\"page\":%d,\"pageSize\":%d,\"items\":[",
             total, w->page, w->page_size);
    body = alarm_build_items_frame(prefix, w->rows, n, &len);
    if (body == NULL) {
      status = 500;
      body = strdup("{\"error\":\"Memory allocation failed\"}");
    }
  }

  if (body != NULL) {
    if (len == 0) len = strlen(body);  // 错误分支用固定 JSON 长度
    push_result(w->conn_id, status, body, len);
  }
  free(w);
}

// 严格解析正整数：必须全为数字且落在 [1, max]。返回 1 成功，0 非法。
// 与 atoi 的区别：拒绝 "1abc"、" 2"、"+2" 等形态，避免宽松解析掩盖错误。
static int parse_positive_int(const char *s, long max, int *out) {
  if (s == NULL || *s == '\0') return 0;
  long v = 0;
  const char *p = s;
  while (*p >= '0' && *p <= '9') {
    v = v * 10 + (*p - '0');
    if (v > max) return 0;
    p++;
  }
  if (p == s || *p != '\0' || v < 1) return 0;
  *out = (int) v;
  return 1;
}

static void handle_alarms_get(struct mg_connection *c,
                              struct mg_http_message *hm) {
  int page = 1;
  int page_size = get_default_page_size();
  char page_buf[16] = "";
  char size_buf[16] = "";

  mg_http_get_var(&hm->query, "page", page_buf, sizeof(page_buf));
  mg_http_get_var(&hm->query, "pageSize", size_buf, sizeof(size_buf));
  if (page_buf[0] != '\0' &&
      !parse_positive_int(page_buf, ALARM_PAGE_INDEX_MAX, &page)) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"error\":\"Invalid parameter: page must be an integer in [1,10000]\"}");
    return;
  }
  if (size_buf[0] != '\0' &&
      !parse_positive_int(size_buf, 1000000, &page_size)) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"error\":\"Invalid parameter: pageSize must be a positive integer\"}");
    return;
  }

  if (page < 1 || page_size < 1) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"error\":\"Invalid parameters: page and pageSize must be positive integers\"}");
    return;
  }

  int max_page_size = get_max_page_size();
  if (max_page_size > ALARM_PAGE_MAX) max_page_size = ALARM_PAGE_MAX;
  if (page_size > max_page_size) page_size = max_page_size;

  struct alarm_page_work *w =
      (struct alarm_page_work *) calloc(1, sizeof(*w));
  if (w == NULL) {
    mg_http_reply(c, 500, s_json_header,
                  "{\"error\":\"Memory allocation failed\"}");
    return;
  }
  w->conn_id = c->id;
  w->page = page;
  w->page_size = page_size;
  // 可选过滤：测点 / 规则 / 事件 精确匹配。mg_http_get_var 已做 URL 解码；
  // 长度被缓冲截断时值不再完整，最多匹配不到行，无注入面（值走参数化）。
  mg_http_get_var(&hm->query, "point_code", w->point_code,
                  sizeof(w->point_code));
  mg_http_get_var(&hm->query, "rule_code", w->rule_code,
                  sizeof(w->rule_code));
  mg_http_get_var(&hm->query, "event", w->event,
                  sizeof(w->event));
  start_thread(alarm_page_thread, w);
}

// ---- 告警 CSV 导出（/api/alarms/export?date=... 或 start=...&end=...，仅 PG 模式） ----
// 与分页查询同一异步模型：worker 线程按时间区间查库 -> 组装 CSV（UTF-8 BOM）
// -> 事件循环线程带附件响应头回写。行数上限防止超大导出耗尽内存。
#define ALARM_EXPORT_MAX 20000  // 单次导出行数上限（超出部分截断）

struct alarm_export_work {
  unsigned long long conn_id;
  char start[24];  // 归一化 'YYYY-MM-DD HH:MM:SS'
  char end[24];    // 归一化 'YYYY-MM-DD HH:MM:SS'
  char fname[96];  // 下载文件名（仅含日期/时间，无特殊字符）
};

// 动态增长字符串缓冲：失败后置 failed，后续写入全部跳过
struct csv_out {
  char *buf;
  size_t len;
  size_t cap;
  int failed;
};

static void csv_reserve(struct csv_out *o, size_t extra) {
  if (o->failed) return;
  size_t need = o->len + extra + 1;
  if (need <= o->cap) return;
  size_t cap = o->cap > 0 ? o->cap : 8192;
  while (cap < need) {
    if (cap > (size_t) 128 * 1024 * 1024) {  // 上限 128MB
      o->failed = 1;
      return;
    }
    cap *= 2;
  }
  char *p = (char *) realloc(o->buf, cap);
  if (p == NULL) {
    o->failed = 1;
    return;
  }
  o->buf = p;
  o->cap = cap;
}

static void csv_put(struct csv_out *o, const char *s, size_t n) {
  csv_reserve(o, n);
  if (o->failed) return;
  memcpy(o->buf + o->len, s, n);
  o->len += n;
  o->buf[o->len] = '\0';
}

static void csv_put_str(struct csv_out *o, const char *s) {
  csv_put(o, s, strlen(s));
}

// RFC4180 字段转义：含逗号/引号/换行时整体加引号，内部引号双写
static void csv_field(struct csv_out *o, const char *s) {
  int quote = 0;
  for (const char *p = s; *p != '\0'; p++) {
    if (*p == ',' || *p == '"' || *p == '\n' || *p == '\r') {
      quote = 1;
      break;
    }
  }
  if (!quote) {
    csv_put_str(o, s);
    return;
  }
  csv_put(o, "\"", 1);
  for (const char *p = s; *p != '\0'; p++) {
    if (*p == '"') csv_put(o, "\"\"", 2);
    else csv_put(o, p, 1);
  }
  csv_put(o, "\"", 1);
}

// 数值列：NULL/NaN/Infinity 输出空单元格（Excel 显示为空）
static void csv_num(struct csv_out *o, double v, const char *fmt) {
  if (!isfinite(v)) return;
  char tmp[64];
  snprintf(tmp, sizeof(tmp), fmt, v);
  csv_put_str(o, tmp);
}

static void csv_sep(struct csv_out *o) {
  csv_put(o, ",", 1);
}

static void csv_alarm_row(struct csv_out *o, const struct ds_alarm *r) {
  char idbuf[24];
  snprintf(idbuf, sizeof(idbuf), "%llu", r->id);
  csv_put_str(o, idbuf);
  csv_sep(o);
  csv_field(o, r->event);
  csv_sep(o);
  csv_field(o, r->ts);
  csv_sep(o);
  csv_field(o, r->region_company_name);
  csv_sep(o);
  csv_field(o, r->station_name);
  csv_sep(o);
  csv_field(o, r->device_id);
  csv_sep(o);
  csv_field(o, r->device_name);
  csv_sep(o);
  csv_field(o, r->point_code);
  csv_sep(o);
  csv_field(o, r->point_name);
  csv_sep(o);
  csv_field(o, r->rule_code);
  csv_sep(o);
  csv_field(o, r->alarm_type);
  csv_sep(o);
  csv_num(o, r->temperature, "%.2f");
  csv_sep(o);
  csv_num(o, r->temp_rate, "%.4f");
  csv_sep(o);
  csv_num(o, r->range_1h, "%.2f");
  csv_sep(o);
  csv_num(o, r->range_20s, "%.2f");
  csv_sep(o);
  csv_num(o, r->alarm_value, "%.2f");
  csv_sep(o);
  csv_field(o, r->detail);
  csv_put(o, "\r\n", 2);
}

static void alarm_export_thread(void *param) {
  struct alarm_export_work *w = (struct alarm_export_work *) param;
  int status = 200;
  char *body = NULL;
  size_t len = 0;
  char *headers = NULL;

  struct ds_alarm *rows =
      (struct ds_alarm *) calloc(ALARM_EXPORT_MAX, sizeof(*rows));
  int n = rows == NULL ? -1 : ds_get_alarms_range(w->start, w->end, rows,
                                                  ALARM_EXPORT_MAX);

  if (n < 0) {
    status = 503;
    body = strdup("{\"error\":\"Alarm export unsupported or query failed\"}");
  } else {
    struct csv_out o;
    memset(&o, 0, sizeof(o));
    csv_put(&o, "\xEF\xBB\xBF", 3);  // UTF-8 BOM：Excel 直接识别中文
    csv_put_str(&o, "事件ID,事件类型,触发时刻,区域公司,场站,设备ID,设备名称,"
                    "测点编码,测点名称,规则,告警类型,温度,温升速率,"
                    "1h极差,20s极差,告警值,详情\r\n");
    for (int i = 0; i < n; i++) csv_alarm_row(&o, &rows[i]);
    if (o.failed) {
      free(o.buf);
      status = 500;
      body = strdup("{\"error\":\"Memory allocation failed\"}");
    } else {
      body = o.buf;
      len = o.len;
    }
  }
  if (rows != NULL) free(rows);

  // 附件响应头：浏览器直接下载为 alarms_<date>.csv
  if (status == 200) {
    headers = (char *) malloc(256);
    if (headers == NULL) {
      if (body != NULL) free(body);
      status = 500;
      body = strdup("{\"error\":\"Memory allocation failed\"}");
      len = 0;
    } else {
      snprintf(headers, 256,
               "Content-Type: text/csv; charset=utf-8\r\n"
               "Content-Disposition: attachment; filename=\"%s\"\r\n"
               "Cache-Control: no-cache\r\n",
               w->fname);
    }
  }

  if (body != NULL) {
    if (len == 0) len = strlen(body);
    push_result_headers(w->conn_id, status, body, len, headers);
  } else if (headers != NULL) {
    free(headers);
  }
  free(w);
}

// 解析 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'（第 11 位也接受 'T'/'+'），
// 输出归一化 'YYYY-MM-DD HH:MM:SS'。仅日期时：is_end=0 取 00:00:00，
// is_end=1 取 23:59:59（与 Python 导出脚本 --date 语义一致）。
static int parse_ts_param(const char *s, int is_end, char *out, size_t cap) {
  if (s == NULL || out == NULL || cap < 20) return 0;
  size_t len = strlen(s);
  if (len != 10 && len != 19) return 0;
  for (int i = 0; i < 10; i++) {
    if (i == 4 || i == 7) {
      if (s[i] != '-') return 0;
    } else if (s[i] < '0' || s[i] > '9') {
      return 0;
    }
  }
  int y = (s[0] - '0') * 1000 + (s[1] - '0') * 100 + (s[2] - '0') * 10 +
          (s[3] - '0');
  int m = (s[5] - '0') * 10 + (s[6] - '0');
  int d = (s[8] - '0') * 10 + (s[9] - '0');
  if (y < 2000 || y > 2100 || m < 1 || m > 12 || d < 1 || d > 31) return 0;

  int hh = 0, mm = 0, ss = 0;
  if (len == 19) {
    char sep = s[10];
    if (sep != ' ' && sep != 'T' && sep != '+') return 0;
    if (s[13] != ':' || s[16] != ':') return 0;
    for (int i = 11; i < 19; i++) {
      if (i == 13 || i == 16) continue;
      if (s[i] < '0' || s[i] > '9') return 0;
    }
    hh = (s[11] - '0') * 10 + (s[12] - '0');
    mm = (s[14] - '0') * 10 + (s[15] - '0');
    ss = (s[17] - '0') * 10 + (s[18] - '0');
    if (hh > 23 || mm > 59 || ss > 59) return 0;
  } else if (is_end) {
    hh = 23;
    mm = 59;
    ss = 59;
  }

  snprintf(out, cap, "%04d-%02d-%02d %02d:%02d:%02d", y, m, d, hh, mm, ss);
  return 1;
}

// 文件名脱敏：空格/冒号替换为 '-'，避免响应头里出现特殊字符
static void make_export_filename(char *out, size_t cap, const char *start,
                                 const char *end) {
  char a[32], b[32];
  size_t j = 0;
  for (size_t i = 0; start[i] != '\0' && j + 1 < sizeof(a); i++) {
    a[j++] = (start[i] == ' ' || start[i] == ':') ? '-' : start[i];
  }
  a[j] = '\0';
  j = 0;
  for (size_t i = 0; end[i] != '\0' && j + 1 < sizeof(b); i++) {
    b[j++] = (end[i] == ' ' || end[i] == ':') ? '-' : end[i];
  }
  b[j] = '\0';
  snprintf(out, cap, "alarms_%s_%s.csv", a, b);
}

static void handle_alarms_export(struct mg_connection *c,
                                 struct mg_http_message *hm) {
  char date[24] = "", start[24] = "", end[24] = "";
  char norm_start[24] = "", norm_end[24] = "";
  int ok = 0;

  mg_http_get_var(&hm->query, "date", date, sizeof(date));
  if (date[0] != '\0') {
    // 单日模式：当天 00:00:00 ~ 23:59:59
    ok = parse_ts_param(date, 0, norm_start, sizeof(norm_start)) &&
         parse_ts_param(date, 1, norm_end, sizeof(norm_end));
  } else {
    // 区间模式：start/end 均必填
    mg_http_get_var(&hm->query, "start", start, sizeof(start));
    mg_http_get_var(&hm->query, "end", end, sizeof(end));
    if (start[0] != '\0' && end[0] != '\0') {
      ok = parse_ts_param(start, 0, norm_start, sizeof(norm_start)) &&
           parse_ts_param(end, 1, norm_end, sizeof(norm_end));
    }
  }

  if (!ok) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"error\":\"Invalid parameter: date=YYYY-MM-DD or "
                  "start/end=YYYY-MM-DD[ HH:MM:SS]\"}");
    return;
  }
  if (strcmp(norm_start, norm_end) > 0) {
    mg_http_reply(c, 400, s_json_header,
                  "{\"error\":\"Invalid range: start must not be later than "
                  "end\"}");
    return;
  }

  struct alarm_export_work *w =
      (struct alarm_export_work *) calloc(1, sizeof(*w));
  if (w == NULL) {
    mg_http_reply(c, 500, s_json_header,
                  "{\"error\":\"Memory allocation failed\"}");
    return;
  }
  w->conn_id = c->id;
  snprintf(w->start, sizeof(w->start), "%s", norm_start);
  snprintf(w->end, sizeof(w->end), "%s", norm_end);
  if (date[0] != '\0') {
    // 单日模式文件名简洁：alarms_YYYY-MM-DD.csv
    snprintf(w->fname, sizeof(w->fname), "alarms_%s.csv", date);
  } else {
    make_export_filename(w->fname, sizeof(w->fname), norm_start, norm_end);
  }
  start_thread(alarm_export_thread, w);
}

static void handle_login(struct mg_connection *c, struct mg_http_message *hm,
                         struct user *u) {
  // Lifted to function scope so we can scrub them on every return path.
  char nm[sizeof(((struct user*)0)->name)] = {0};
  char pw[sizeof(((struct user*)0)->pass)] = {0};
  // Support 3 credential sources for /api/login:
  //   1. Basic auth header or valid cookie (authenticate() already resolved via `u`)
  //   2. POST body JSON: {"user":"xxx","password":"yyy"}
  //   3. POST form-encoded: user=xxx&password=yyy
  if (u == NULL && hm != NULL && hm->body.len > 0) {
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
    memset(nm, 0, sizeof(nm));
    memset(pw, 0, sizeof(pw));
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
                "{\"status\":\"true\",%m:%m,%m:%m,\"videoEnabled\":%s,\"alarmEventAll\":%s}",
                MG_ESC("user"), MG_ESC(u->name),
                MG_ESC("token"), MG_ESC(u->access_token),
                video_devices_enabled() ? "true" : "false",
                alarm_event_all() ? "true" : "false");
  // Scrub submitted plaintext password from the stack frame.
  memset(nm, 0, sizeof(nm));
  memset(pw, 0, sizeof(pw));
}

static void handle_logout(struct mg_connection *c, struct user *u) {
  if (u != NULL && u->name[0] != '\0' && strcmp(u->name, "_api_token_") != 0) {
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
  } else if (ev == MG_EV_WS_OPEN) {
    // WS 握手完成（mg_ws_upgrade 内同步触发）：按连接标记分发
    MG_DEBUG(("%lu WS_OPEN data0=%c ws=%d", c->id, c->data[0], c->is_websocket));
    if (c->data[0] == 'A') {
      alarm_send_hello(c);
      if (s_alarm_poll_enabled) alarm_send_snapshot(c);
      MG_DEBUG(("%lu alarm hello+snapshot queued, send.len=%zu", c->id, c->send.len));
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
      if (mg_strcmp(hm->method, mg_str("POST")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_login(c, hm, u);
      }
    } else if (mg_match(hm->uri, mg_str("/api/logout"), NULL)) {
      // /api/logout is a PUBLIC endpoint (like /api/login): it must clear the
      // cookie even when no auth is presented, and POST-only enforcement must
      // run BEFORE the /api/# && u==NULL 403 guard below — otherwise GET
      // /api/logout with no creds would return 403 instead of 405, hiding the
      // method-not-allowed signal that protects against CSRF via <img> tags.
      if (mg_strcmp(hm->method, mg_str("POST")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_logout(c, u);
      }
    } else if (mg_match(hm->uri, mg_str("/api/mode/get"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        mg_http_reply(c, 200, s_json_header, "{\"mode\":\"%s\",\"available\":%d}", DS_MODE, ds_is_available());
      }
      // /api/time is PUBLIC: the frontend header clock syncs to the server's
      // wall clock (and therefore the server's TZ, e.g. Asia/Shanghai) instead
      // of trusting the browser's local timezone, which may differ.
    } else if (mg_match(hm->uri, mg_str("/api/time"), NULL)) {
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        time_t now = time(NULL);
        struct tm tmv;
#if defined(_WIN32)
        localtime_s(&tmv, &now);
#else
        localtime_r(&now, &tmv);
#endif
        char tb[32];
        strftime(tb, sizeof(tb), "%Y-%m-%d %H:%M:%S", &tmv);
        mg_http_reply(c, 200, s_json_header, "{\"now\":\"%s\",\"epoch\":%ld}", tb, (long) now);
      }
      // ALL OTHER /api/* ENDPOINTS REQUIRE AUTHENTICATION: reject u==NULL here,
      // BEFORE any route handler runs (prevents "route order accidentally skips auth" bugs).
    } else if (mg_match(hm->uri, mg_str("/api/#"), NULL) && u == NULL) {
      mg_http_reply(c, 403, s_json_header, "{\"status\":\"false\",\"message\":\"Not Authorised\"}");
      // ---- Protected routes below (guaranteed u != NULL for /api/*) ----
    } else if (mg_match(hm->uri, mg_str("/api/alarms/ws"), NULL)) {
      // 告警实时推送订阅：认证通过后升级为 WebSocket。升级成功同步触发
      // MG_EV_WS_OPEN（发送 hello + 快照），后续由 alarm_poll 1s 广播增量。
      // c->data[0]='A' 标记订阅连接；连接断开自动从 mgr->conns 消失，无需清理。
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        c->data[0] = 'A';
        mg_ws_upgrade(c, hm, NULL);
      }
    } else if (mg_match(hm->uri, mg_str("/api/alarms/export"), NULL)) {
      // 按日期导出告警 CSV（浏览器直接下载附件）
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_alarms_export(c, hm);
      }
    } else if (mg_match(hm->uri, mg_str("/api/alarms/get"), NULL)) {
      // 告警历史分页（实时页仍由 /api/alarms/ws 推送驱动）
      if (mg_strcmp(hm->method, mg_str("GET")) != 0) {
        mg_http_reply(c, 405, s_json_header, "{\"error\":\"Method Not Allowed\"}");
      } else {
        handle_alarms_get(c, hm);
      }
    } else if (mg_match(hm->uri, mg_str("/api/nodes/#"), NULL) && !video_devices_enabled()) {
      // 视频设备功能已禁用：所有 /api/nodes/* 接口统一拒绝，不触发任何数据库/数据加载
      mg_http_reply(c, 403, s_json_header,
                    "{\"status\":\"false\",\"message\":\"Video devices disabled\"}");
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
    } else {
      struct mg_http_serve_opts opts;
      memset(&opts, 0, sizeof(opts));
      opts.root_dir = "web_root";
      opts.extra_headers = "Cache-Control: no-cache\r\n";
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

  // Load config early: populates s_users (from data_config.json) and port settings
  cfg_lock();
  if (!g_cfg_parsed_valid) {
    char *buf = get_config_buf_unlocked();
    if (buf) parse_config_fields(buf);
  }
  int http_port = g_cfg_parsed.httpPort;
  int https_port = g_cfg_parsed.httpsPort;
  cfg_unlock();

  // Generate access tokens after users are loaded from config
  generate_access_tokens();

  MG_INFO(("Web server starting in %s mode", DS_MODE));
  mg_timer_add(mgr, 20, MG_TIMER_REPEAT, dispatch_results, mgr);

  // 告警推送：一次性探测（PG 连接 + wind_alarm 表存在性，结果缓存于驱动内）
  s_alarm_poll_enabled = (ds_alarm_supported() == 1);
  if (s_alarm_poll_enabled) {
    // 启动预热：以最近 200 行初始化快照缓存与增量游标，避免重启后把历史
    // 告警当作新增事件重放给客户端（实测曾一次重放 168 条）。
    int n = ds_get_recent_alarms(s_alarm_cache, ALARM_CACHE_MAX);
    if (n > 0) {
      s_alarm_cache_count = n;
      s_alarm_last_id = s_alarm_cache[n - 1].id;
      MG_INFO(("Alarm cache warmed: %d rows, cursor=%llu", n, s_alarm_last_id));
    } else if (n < 0) {
      MG_ERROR(("Alarm cache warm-up query failed; cursor starts at 0"));
    }
    MG_INFO(("Alarm push enabled (wind_alarm incremental polling every %dms)", ALARM_POLL_MS));
  } else {
    MG_INFO(("Alarm push disabled (PG mode with wind_alarm table required)"));
  }
  mg_timer_add(mgr, ALARM_POLL_MS, MG_TIMER_REPEAT, alarm_poll_timer, mgr);

  char http_url[64];
  char https_url[64];
  snprintf(http_url, sizeof(http_url), "http://0.0.0.0:%d", http_port);
  snprintf(https_url, sizeof(https_url), "https://0.0.0.0:%d", https_port);

  MG_INFO(("HTTP listening on port %d, HTTPS listening on port %d", http_port, https_port));
  mg_http_listen(mgr, http_url, fn, NULL);
  mg_http_listen(mgr, https_url, fn, NULL);
}
