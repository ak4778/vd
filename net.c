// Copyright (c) 2023 Cesanta Software Limited
// All rights reserved

#include "net.h"
#include "data_source.h"

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#define mg_msleep(ms) Sleep(ms)
#else
#include <time.h>
#define mg_msleep(ms) do { \
    struct timespec ts; \
    ts.tv_sec = (ms) / 1000; \
    ts.tv_nsec = ((ms) % 1000) * 1000000L; \
    nanosleep(&ts, NULL); \
} while(0)
#endif

#if !defined(CSV_MODE)
#define DS_MODE "SQLite"
#define DS_MODE_TYPE 1
#else
#define DS_MODE "CSV"
#define DS_MODE_TYPE 0
#endif

struct user {
  const char *name, *pass, *access_token;
};

struct settings {
  bool log_enabled;
  int log_level;
  long brightness;
  char *device_name;
};

static struct settings s_settings = {true, 1, 57, NULL};

static const char *s_json_header =
    "Content-Type: application/json\r\n"
    "Cache-Control: no-cache\r\n";

// 配置文件缓存 - 避免每次请求都读盘
static char *g_cfg_buf = NULL;

// 获取配置文件内容（带缓存）。返回的是缓存指针，调用者不要 free。
static char *get_config_buf(void) {
  if (g_cfg_buf != NULL) return g_cfg_buf;

  FILE *fp = fopen("data_config.json", "rb");
  if (fp == NULL) return NULL;

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

  return g_cfg_buf;
}

static int get_max_page_size(void) {
  char *cfg_buf = get_config_buf();
  if (cfg_buf == NULL) return 100;

  const char *key = "\"maxPageSize\"";
  char *pos = strstr(cfg_buf, key);
  if (pos == NULL) return 100;

  pos += strlen(key);
  while (*pos == ' ' || *pos == ':') pos++;

  if (!isdigit((unsigned char)*pos)) return 100;

  return atoi(pos);
}

static int get_default_page_size(void) {
  char *cfg_buf = get_config_buf();
  if (cfg_buf == NULL) return 50;

  const char *key = "\"defaultPageSize\"";
  char *pos = strstr(cfg_buf, key);
  if (pos == NULL) return 50;

  pos += strlen(key);
  while (*pos == ' ' || *pos == ':') pos++;

  if (!isdigit((unsigned char)*pos)) return 50;

  return atoi(pos);
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
        j += utf8_len;
        i += 6;
      } else if (s.buf[i] == '\\' && i + 1 < s.len) {
        char c = s.buf[i + 1];
        if (c == 'n') to[j++] = '\n';
        else if (c == 'r') to[j++] = '\r';
        else if (c == 't') to[j++] = '\t';
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

int ui_event_next(int no, struct ui_event *e) {
  if (no < 0 || no >= MAX_EVENTS_NO) return 0;

  srand((unsigned) no);
  e->type = (uint8_t) rand() % 4;
  e->prio = (uint8_t) rand() % 3;
  e->timestamp =
      (unsigned long) ((int64_t) mg_now() - 86400 * 1000 +
                       no * 300 * 1000 +
                       1000 * (rand() % 300)) /
      1000UL;

  mg_snprintf(e->text, MAX_EVENT_TEXT_SIZE, "event#%d", no);
  return no + 1;
}

// 批量更新节点 - 使用数据库抽象层
static void handle_nodes_batchset(struct mg_connection *c, struct mg_str body) {
  struct ds_node updates[256];
  int update_count = 0;

  // 遍历 JSON 数组，收集所有 updates
  for (int i = 0; i < 256; i++) {
    char path[64];
    snprintf(path, sizeof(path), "$.updates[%d].id", i);
    struct mg_str id_tok = mg_json_get_tok(body, path);
    if (id_tok.len == 0) break;

    my_json_unescape(body, path, updates[update_count].id, sizeof(updates[update_count].id));
    if (updates[update_count].id[0] == '\0') break;

    snprintf(path, sizeof(path), "$.updates[%d].operation", i);
    my_json_unescape(body, path, updates[update_count].operation, sizeof(updates[update_count].operation));

    snprintf(path, sizeof(path), "$.updates[%d].customOperation", i);
    my_json_unescape(body, path, updates[update_count].customOperation, sizeof(updates[update_count].customOperation));

    // 其他字段保持为空，数据库层会处理
    updates[update_count].name[0] = '\0';
    updates[update_count].channelCode[0] = '\0';
    updates[update_count].isOnline[0] = '\0';
    updates[update_count].cameraType[0] = '\0';

    update_count++;
  }

  if (update_count == 0) {
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"false\",\"message\":\"No updates\"}");
    return;
  }

  int retries = 0;
  int max_retries = 2;
  int update_result = -1;
  int retry_delay = 30;

  while (retries < max_retries && update_result != 0) {
    update_result = ds_update_nodes(updates, update_count);
    if (update_result != 0) {
      retries++;
      mg_msleep(retry_delay);
      retry_delay *= 2;
    }
  }

  if (update_result == 0) {
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"true\",\"message\":\"Success\",\"count\":%d}", update_count);
  } else {
    mg_http_reply(c, 503, s_json_header, "{\"status\":\"false\",\"message\":\"Update failed after %d retries\"}", max_retries);
  }
}

static void handle_nodes_get(struct mg_connection *c, struct mg_http_message *hm) {
  int page = 1;
  int page_size = get_default_page_size();
  char page_buf[16] = "";
  char size_buf[16] = "";
  char online_filter[16] = "";
  char camera_filter[16] = "";
  char operation_filter[64] = "";
  char keyword_buf[256] = "";

  mg_http_get_var(&hm->query, "page", page_buf, sizeof(page_buf));
  mg_http_get_var(&hm->query, "pageSize", size_buf, sizeof(size_buf));
  mg_http_get_var(&hm->query, "isOnline", online_filter, sizeof(online_filter));
  mg_http_get_var(&hm->query, "cameraType", camera_filter, sizeof(camera_filter));
  mg_http_get_var(&hm->query, "operation", operation_filter, sizeof(operation_filter));
  mg_http_get_var(&hm->query, "keyword", keyword_buf, sizeof(keyword_buf));

  if (page_buf[0] != '\0') page = atoi(page_buf);
  if (size_buf[0] != '\0') page_size = atoi(size_buf);

  if (page < 1 || page_size < 1) {
    mg_http_reply(c, 400, s_json_header, "{\"error\":\"Invalid parameters: page and pageSize must be positive integers\"}");
    return;
  }

  int max_page_size = get_max_page_size();
  if (page_size > max_page_size) page_size = max_page_size;

  // 使用缓存的配置文件（不要 free cfg_buf）
  char *cfg_buf = get_config_buf();
  if (cfg_buf == NULL) {
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Cannot read data_config.json\"}");
    return;
  }



  // 解析 fields 配置，只返回配置中定义的字段
  char field_keys[32][64] = {""};
  int field_key_count = 0;
  struct mg_str cfg_fields_tok = mg_json_get_tok(mg_str(cfg_buf), "$.fields");
  if (cfg_fields_tok.len > 0) {
    char *fields_str = (char *) malloc((size_t) cfg_fields_tok.len + 1);
    if (fields_str != NULL) {
      memcpy(fields_str, cfg_fields_tok.buf, (size_t) cfg_fields_tok.len);
      fields_str[cfg_fields_tok.len] = '\0';
      char *ptr = fields_str;
      while (*ptr != '\0' && field_key_count < 32) {
        char *key_ptr = strstr(ptr, "\"key\"");
        if (key_ptr == NULL) break;
        key_ptr += 5; // skip "\"key\""
        // skip optional whitespace and colon
        while (*key_ptr == ' ' || *key_ptr == '\t' || *key_ptr == ':') key_ptr++;
        while (*key_ptr == ' ' || *key_ptr == '\t') key_ptr++;
        if (*key_ptr != '"') break;
        key_ptr++; // skip opening quote
        char *end_ptr = strchr(key_ptr, '"');
        if (end_ptr == NULL) break;
        int len = (int) (end_ptr - key_ptr);
        if (len > 0 && len < 64) {
          strncpy(field_keys[field_key_count], key_ptr, len);
          field_keys[field_key_count][len] = '\0';
          field_key_count++;
        }
        ptr = end_ptr + 1;
      }
      free(fields_str);
    }
  }

  // 使用数据源抽象层查询数据
  struct ds_query query;
  memset(&query, 0, sizeof(query));
  query.page = page;
  query.pageSize = page_size;
  
  int has_online = (online_filter[0] != '\0' || strstr(hm->query.buf, "isOnline=") != NULL);
  int has_camera = (camera_filter[0] != '\0' || strstr(hm->query.buf, "cameraType=") != NULL);
  int has_operation = (operation_filter[0] != '\0' || strstr(hm->query.buf, "operation=") != NULL);
  
  if (has_online) {
    query.isOnline = online_filter;
  }
  if (has_camera) {
    query.cameraType = camera_filter;
  }
  if (has_operation) {
    query.operation = operation_filter;
  }

  if (keyword_buf[0] != '\0') {
    query.keyword = keyword_buf;
  }

  struct ds_result result = {0};
  int retries = 0;
  int max_retries = 2;
  int query_result = -1;
  int retry_delay = 30;

  while (retries < max_retries && query_result != 0) {
    query_result = ds_get_nodes(&query, &result);
    if (query_result != 0) {
      retries++;
      mg_msleep(retry_delay);
      retry_delay *= 2;
    }
  }

  if (query_result != 0) {
    mg_http_reply(c, 503, s_json_header, "{\"error\":\"Database query failed after %d retries\"}", max_retries);
    return;
  }

  // 分配响应缓冲区
  size_t cfg_len = strlen(cfg_buf);
  long total_size = (long) cfg_len + (long) page_size * 1024 + 2048;
  char *response = (char *) malloc((size_t) total_size);
  if (response == NULL) {
    if (result.nodes != NULL) free(result.nodes);
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Memory allocation failed\"}");
    return;
  }

  int pos = 0;
  pos += snprintf(response + pos, total_size - pos, "{\"config\":{\"defaultPageSize\":%d,\"maxPageSize\":%d,\"fields\":", 
                  get_default_page_size(), get_max_page_size());
  struct mg_str fields_tok = mg_json_get_tok(mg_str(cfg_buf), "$.fields");
  if (fields_tok.len > 0) {
    memcpy(response + pos, fields_tok.buf, (size_t) fields_tok.len);
    pos += (int) fields_tok.len;
  } else {
    pos += snprintf(response + pos, total_size - pos, "[]");
  }
  pos += snprintf(response + pos, total_size - pos, "},");
  pos += snprintf(response + pos, total_size - pos, "\"data\":{\"total\":%d,\"nodes\":[", result.total);

  int first = 1;
  for (int i = 0; i < result.count; i++) {
    struct ds_node *node = &result.nodes[i];

    if (!first) pos += snprintf(response + pos, total_size - pos, ",");
    first = 0;

    pos += snprintf(response + pos, total_size - pos, "{");
    for (int j = 0; j < field_key_count; j++) {
      if (j > 0) pos += snprintf(response + pos, total_size - pos, ",");
      pos += snprintf(response + pos, total_size - pos, "\"%s\":\"", field_keys[j]);

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

      for (int k = 0; field_val[k] != '\0'; k++) {
        if (field_val[k] == '"' || field_val[k] == '\\') {
          pos += snprintf(response + pos, total_size - pos, "\\%c", field_val[k]);
        } else if (field_val[k] == '\n') {
          pos += snprintf(response + pos, total_size - pos, "\\n");
        } else if (field_val[k] == '\r') {
          pos += snprintf(response + pos, total_size - pos, "\\r");
        } else if (field_val[k] == '\t') {
          pos += snprintf(response + pos, total_size - pos, "\\t");
        } else if (field_val[k] == '\b') {
          pos += snprintf(response + pos, total_size - pos, "\\b");
        } else if (field_val[k] == '\f') {
          pos += snprintf(response + pos, total_size - pos, "\\f");
        } else if ((unsigned char)field_val[k] < 0x20) {
          pos += snprintf(response + pos, total_size - pos, "\\u%04x", (unsigned char)field_val[k]);
        } else {
          pos += snprintf(response + pos, total_size - pos, "%c", field_val[k]);
        }
      }
      pos += snprintf(response + pos, total_size - pos, "\"");
    }
    pos += snprintf(response + pos, total_size - pos, "}");
  }

  pos += snprintf(response + pos, total_size - pos, "]}}");

  mg_http_reply(c, 200, s_json_header, "%s", response);

  if (result.nodes != NULL) free(result.nodes);
  free(response);
}

static struct user *authenticate(struct mg_http_message *hm) {
  static struct user users[] = {
      {"admin", "admin", "admin_token"},
      {"user1", "user1", "user1_token"},
      {"user2", "user2", "user2_token"},
      {NULL, NULL, NULL},
  };
  char user[64], pass[64];
  struct user *u, *result = NULL;
  mg_http_creds(hm, user, sizeof(user), pass, sizeof(pass));
  MG_VERBOSE(("user [%s] pass [%s]", user, pass));

  if (user[0] != '\0' && pass[0] != '\0') {
    for (u = users; result == NULL && u->name != NULL; u++)
      if (strcmp(user, u->name) == 0 && strcmp(pass, u->pass) == 0) result = u;
  } else if (user[0] == '\0') {
    for (u = users; result == NULL && u->name != NULL; u++)
      if (strcmp(pass, u->access_token) == 0) result = u;
  }
  return result;
}

static void handle_login(struct mg_connection *c, struct user *u) {
  if (u == NULL) {
    mg_http_reply(c, 401, "", "Unauthorized\n");
    return;
  }
  char cookie[256];
  const char *cookie_name = c->is_tls ? "secure_access_token" : "access_token";
  mg_snprintf(cookie, sizeof(cookie),
              "Set-Cookie: %s=%s; Path=/; "
              "%sHttpOnly; SameSite=Lax; Max-Age=%d\r\n",
              cookie_name, u->access_token,
              c->is_tls ? "Secure; " : "", 3600 * 24);
  mg_http_reply(c, 200, cookie, "{%m:%m}", MG_ESC("user"), MG_ESC(u->name));
}

static void handle_logout(struct mg_connection *c) {
  char cookie[256];
  const char *cookie_name = c->is_tls ? "secure_access_token" : "access_token";
  mg_snprintf(cookie, sizeof(cookie),
              "Set-Cookie: %s=; Path=/; "
              "Expires=Thu, 01 Jan 1970 00:00:00 UTC; "
              "%sHttpOnly; Max-Age=0; \r\n", cookie_name,
              c->is_tls ? "Secure; " : "");
  mg_http_reply(c, 200, cookie, "true\n");
}

static void handle_debug(struct mg_connection *c, struct mg_http_message *hm) {
  int level = (int) mg_json_get_long(hm->body, "$.level", MG_LL_DEBUG);
  mg_log_set(level);
  mg_http_reply(c, 200, "", "Debug level set to %d\n", level);
}

static size_t print_int_arr(void (*out)(char, void *), void *ptr, va_list *ap) {
  size_t i, len = 0, num = va_arg(*ap, size_t);
  int *arr = va_arg(*ap, int *);
  for (i = 0; i < num; i++) {
    len += mg_xprintf(out, ptr, "%s%d", i == 0 ? "" : ",", arr[i]);
  }
  return len;
}

static void handle_stats_get(struct mg_connection *c) {
  int points[] = {21, 22, 22, 19, 18, 20, 23, 23, 22, 22, 22, 23, 22};
  mg_http_reply(c, 200, s_json_header, "{%m:%d,%m:%d,%m:[%M]}\n",
                MG_ESC("temperature"), 21,
                MG_ESC("humidity"), 67,
                MG_ESC("points"), print_int_arr,
                sizeof(points) / sizeof(points[0]), points);
}

static size_t print_events(void (*out)(char, void *), void *ptr, va_list *ap) {
  size_t len = 0;
  struct ui_event ev;
  int pageno = va_arg(*ap, int);
  int no = (pageno - 1) * EVENTS_PER_PAGE;
  int end = no + EVENTS_PER_PAGE;

  while ((no = ui_event_next(no, &ev)) != 0 && no <= end) {
    len += mg_xprintf(out, ptr, "%s{%m:%lu,%m:%d,%m:%d,%m:%m}\n",
                      len == 0 ? "" : ",",
                      MG_ESC("time"), ev.timestamp,
                      MG_ESC("type"), ev.type,
                      MG_ESC("prio"), ev.prio,
                      MG_ESC("text"), MG_ESC(ev.text));
  }

  return len;
}

static void handle_events_get(struct mg_connection *c,
                              struct mg_http_message *hm) {
  int pageno = (int) mg_json_get_long(hm->body, "$.page", 1);
  mg_http_reply(c, 200, s_json_header, "{%m:[%M], %m:%d}\n", MG_ESC("arr"),
                print_events, pageno, MG_ESC("totalCount"), MAX_EVENTS_NO);
}

static void handle_settings_set(struct mg_connection *c, struct mg_str body) {
  struct settings settings;
  char *s = mg_json_get_str(body, "$.device_name");
  bool ok = true;
  memset(&settings, 0, sizeof(settings));
  mg_json_get_bool(body, "$.log_enabled", &settings.log_enabled);
  settings.log_level = (int) mg_json_get_long(body, "$.log_level", 0);
  settings.brightness = mg_json_get_long(body, "$.brightness", 0);
  if (s && strlen(s) < MAX_DEVICE_NAME) {
    mg_free(settings.device_name);
    settings.device_name = s;
  } else {
    mg_free(s);
  }
  s_settings = settings;
  mg_http_reply(c, 200, s_json_header,
                "{%m:%s,%m:%m}",
                MG_ESC("status"), ok ? "true" : "false",
                MG_ESC("message"), MG_ESC(ok ? "Success" : "Failed"));
}

static void handle_settings_get(struct mg_connection *c) {
  mg_http_reply(c, 200, s_json_header, "{%m:%s,%m:%hhu,%m:%hhu,%m:%m}\n",
                MG_ESC("log_enabled"),
                s_settings.log_enabled ? "true" : "false",
                MG_ESC("log_level"), s_settings.log_level,
                MG_ESC("brightness"), s_settings.brightness,
                MG_ESC("device_name"), MG_ESC(s_settings.device_name));
}

static void fn(struct mg_connection *c, int ev, void *ev_data) {
  if (ev == MG_EV_ACCEPT) {
    if (c->is_tls) {
      struct mg_tls_opts opts = {0};
      opts.cert = mg_file_read(NULL, "certs/server_cert.pem");
      opts.key = mg_file_read(NULL, "certs/server_key.pem");
      mg_tls_init(c, &opts);
    }
  } else if (ev == MG_EV_HTTP_MSG) {
    struct mg_http_message *hm = (struct mg_http_message *) ev_data;
    struct user *u = authenticate(hm);

    if (mg_match(hm->uri, mg_str("/api/login"), NULL)) {
      handle_login(c, u);
    } else if (mg_match(hm->uri, mg_str("/api/mode/get"), NULL)) {
      mg_http_reply(c, 200, s_json_header, "{\"mode\":\"%s\",\"available\":%d}", DS_MODE, ds_is_available());
    } else if (mg_match(hm->uri, mg_str("/api/config/get"), NULL)) {
      char *cfg_buf = get_config_buf();
      if (cfg_buf != NULL) {
        mg_http_reply(c, 200, s_json_header, "%s", cfg_buf);
      } else {
        mg_http_reply(c, 500, s_json_header, "{\"error\":\"Cannot read config\"}");
      }
    } else if (mg_match(hm->uri, mg_str("/api/nodes/get"), NULL)) {
      handle_nodes_get(c, hm);
    } else if (mg_match(hm->uri, mg_str("/api/nodes/batchset"), NULL)) {
      handle_nodes_batchset(c, hm->body);
    } else if (mg_match(hm->uri, mg_str("/api/#"), NULL) && u == NULL) {
      mg_http_reply(c, 403, "", "Not Authorised\n");
    } else if (mg_match(hm->uri, mg_str("/api/logout"), NULL)) {
      handle_logout(c);
    } else if (mg_match(hm->uri, mg_str("/api/debug"), NULL)) {
      handle_debug(c, hm);
    } else if (mg_match(hm->uri, mg_str("/api/stats/get"), NULL)) {
      handle_stats_get(c);
    } else if (mg_match(hm->uri, mg_str("/api/events/get"), NULL)) {
      handle_events_get(c, hm);
    } else if (mg_match(hm->uri, mg_str("/api/settings/get"), NULL)) {
      handle_settings_get(c);
    } else if (mg_match(hm->uri, mg_str("/api/settings/set"), NULL)) {
      handle_settings_set(c, hm->body);
    } else {
      struct mg_http_serve_opts opts;
      memset(&opts, 0, sizeof(opts));
      opts.root_dir = "web_root";
      mg_http_serve_dir(c, ev_data, &opts);
    }
    MG_DEBUG(("%lu %.*s %.*s -> %.*s", c->id, (int) hm->method.len,
              hm->method.buf, (int) hm->uri.len, hm->uri.buf, (int) 3,
              &c->send.buf[9]));
  }
}

void web_init(struct mg_mgr *mgr) {
  s_settings.device_name = strdup("My Device");
  MG_INFO(("Web server starting in %s mode", DS_MODE));
  mg_http_listen(mgr, HTTP_URL, fn, NULL);
  mg_http_listen(mgr, HTTPS_URL, fn, NULL);
}
