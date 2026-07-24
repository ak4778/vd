// Copyright (c) 2023 Cesanta Software Limited
// All rights reserved

#include "net.h"

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

static char *url_decode(const char *str) {
  if (str == NULL) return NULL;
  int len = strlen(str);
  char *result = (char *) malloc(len + 1);
  if (result == NULL) return NULL;
  int i = 0, j = 0;
  while (i < len) {
    if (str[i] == '%' && i + 2 < len) {
      char hex[3] = {str[i+1], str[i+2], '\0'};
      result[j++] = (char) strtol(hex, NULL, 16);
      i += 3;
    } else if (str[i] == '+') {
      result[j++] = ' ';
      i++;
    } else {
      result[j++] = str[i++];
    }
  }
  result[j] = '\0';
  return result;
}

static int is_value_in_list(const char *value, const char *list) {
  if (value == NULL || list == NULL || *list == '\0') return 0;
  char *copy = strdup(list);
  if (copy == NULL) return 0;
  char *token = strtok(copy, ",");
  while (token != NULL) {
    char *decoded_token = url_decode(token);
    if (decoded_token != NULL) {
      if (strcmp(decoded_token, value) == 0) {
        free(decoded_token);
        free(copy);
        return 1;
      }
      free(decoded_token);
    } else {
      if (strcmp(token, value) == 0) {
        free(copy);
        return 1;
      }
    }
    token = strtok(NULL, ",");
  }
  free(copy);
  return 0;
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

static int get_csv_field(char *line, int index, char *buf, int buf_len) {
  char *p = line;
  for (int i = 0; i < index; i++) {
    if (*p == '"') {
      p++;
      while (*p != '\0' && *p != '"') p++;
      if (*p == '"') p++;
    } else {
      while (*p != '\0' && *p != ',') p++;
    }
    if (*p == ',') p++;
    if (*p == '\0') return -1;
  }
  int len = 0;
  if (*p == '"') {
    p++;
    while (*p != '\0' && *p != '"' && len < buf_len - 1) {
      buf[len++] = *p++;
    }
  } else {
    while (*p != '\0' && *p != ',' && len < buf_len - 1) {
      buf[len++] = *p++;
    }
  }
  buf[len] = '\0';
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

static void handle_nodes_set(struct mg_connection *c, struct mg_str body) {
  char id[64] = "";
  char operation[64] = "";
  char custom_operation[256] = "";
  char csv_path[256] = "leaf_nodes.csv";
  char edited_path[256] = "leaf_nodes_edited.csv";

  my_json_unescape(body, "$.id", id, sizeof(id));
  my_json_unescape(body, "$.operation", operation, sizeof(operation));
  my_json_unescape(body, "$.customOperation", custom_operation, sizeof(custom_operation));

  if (id[0] == '\0') {
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"false\",\"message\":\"ID is required\"}");
    return;
  }

  FILE *fp_cfg = fopen("data_config.json", "rb");
  if (fp_cfg != NULL) {
    fseek(fp_cfg, 0, SEEK_END);
    long cfg_size = ftell(fp_cfg);
    fseek(fp_cfg, 0, SEEK_SET);
    char *cfg_buf = (char *) malloc((size_t) cfg_size + 1);
    if (cfg_buf != NULL) {
      fread(cfg_buf, 1, (size_t) cfg_size, fp_cfg);
      cfg_buf[cfg_size] = '\0';
      char *csv_path_ptr = mg_json_get_str(mg_str(cfg_buf), "$.csvFilePath");
      if (csv_path_ptr != NULL) {
        strncpy(csv_path, csv_path_ptr, sizeof(csv_path) - 1);
        csv_path[sizeof(csv_path) - 1] = '\0';
      }
      char *edited_path_ptr = mg_json_get_str(mg_str(cfg_buf), "$.editedFilePath");
      if (edited_path_ptr != NULL) {
        strncpy(edited_path, edited_path_ptr, sizeof(edited_path) - 1);
        edited_path[sizeof(edited_path) - 1] = '\0';
      }
      free(cfg_buf);
    }
    fclose(fp_cfg);
  }

  FILE *fp_in = fopen(csv_path, "r");
  if (fp_in == NULL) {
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"false\",\"message\":\"Cannot read CSV\"}");
    return;
  }

  FILE *fp_out = fopen(edited_path, "w");
  if (fp_out == NULL) {
    fclose(fp_in);
    mg_http_reply(c, 200, s_json_header, "{\"status\":\"false\",\"message\":\"Cannot write CSV\"}");
    return;
  }

  char line[4096];
  int operation_idx = -1;
  int custom_operation_idx = -1;
  int header_count = 0;

  if (fgets(line, sizeof(line), fp_in) != NULL) {
    fprintf(fp_out, "%s", line);

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
    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0' || *p == '\n' || *p == '\r') {
      fprintf(fp_out, "%s", line);
      continue;
    }

    char row_id[64] = "";
    get_csv_field(p, 0, row_id, sizeof(row_id));

    if (strcmp(row_id, id) == 0) {
      char new_line[4096] = "";
      int first = 1;
      for (int i = 0; i < header_count; i++) {
        if (!first) strcat(new_line, ",");
        first = 0;
        if (i == operation_idx) {
          strcat(new_line, operation);
        } else if (i == custom_operation_idx) {
          strcat(new_line, custom_operation);
        } else {
          char field_val[512] = "";
          get_csv_field(p, i, field_val, sizeof(field_val));
          strcat(new_line, field_val);
        }
      }
      char *end = line;
      while (*end != '\0' && *end != '\n' && *end != '\r') end++;
      if (*end == '\n' || *end == '\r') {
        strcat(new_line, end);
      }
      fprintf(fp_out, "%s", new_line);
    } else {
      fprintf(fp_out, "%s", line);
    }
  }

  fclose(fp_in);
  fclose(fp_out);

  remove(csv_path);
  rename(edited_path, csv_path);

  mg_http_reply(c, 200, s_json_header, "{\"status\":\"true\",\"message\":\"Success\"}");
}

static void handle_nodes_get(struct mg_connection *c, struct mg_http_message *hm) {
  int page = 1;
  int page_size = 20;
  char page_buf[16] = "";
  char size_buf[16] = "";
  char online_filter[16] = "";
  char camera_filter[16] = "";
  char operation_filter[64] = "";

  mg_http_get_var(&hm->query, "page", page_buf, sizeof(page_buf));
  mg_http_get_var(&hm->query, "pageSize", size_buf, sizeof(size_buf));
  mg_http_get_var(&hm->query, "isOnline", online_filter, sizeof(online_filter));
  mg_http_get_var(&hm->query, "cameraType", camera_filter, sizeof(camera_filter));
  mg_http_get_var(&hm->query, "operation", operation_filter, sizeof(operation_filter));

  if (page_buf[0] != '\0') page = atoi(page_buf);
  if (size_buf[0] != '\0') page_size = atoi(size_buf);
  if (page < 1) page = 1;
  if (page_size < 1) page_size = 20;
  if (page_size > 100) page_size = 100;

  FILE *fp_cfg = fopen("data_config.json", "rb");
  if (fp_cfg == NULL) {
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Cannot read data_config.json\"}");
    return;
  }

  fseek(fp_cfg, 0, SEEK_END);
  long cfg_size = ftell(fp_cfg);
  fseek(fp_cfg, 0, SEEK_SET);
  char *cfg_buf = (char *) malloc((size_t) cfg_size + 1);
  if (cfg_buf == NULL) {
    fclose(fp_cfg);
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Memory allocation failed\"}");
    return;
  }
  fread(cfg_buf, 1, (size_t) cfg_size, fp_cfg);
  fclose(fp_cfg);
  cfg_buf[cfg_size] = '\0';

  char *csv_path_ptr = mg_json_get_str(mg_str(cfg_buf), "$.csvFilePath");
  char csv_path[256] = "leaf_nodes.csv";
  if (csv_path_ptr != NULL) {
    strncpy(csv_path, csv_path_ptr, sizeof(csv_path) - 1);
    csv_path[sizeof(csv_path) - 1] = '\0';
  }

  FILE *fp_data = fopen(csv_path, "r");
  if (fp_data == NULL) {
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Cannot read CSV\"}");
    free(cfg_buf);
    return;
  }

  if (online_filter[0] == '\0' || camera_filter[0] == '\0' || operation_filter[0] == '\0') {
    fclose(fp_data);
    struct mg_str fields_tok = mg_json_get_tok(mg_str(cfg_buf), "$.fields");
    if (fields_tok.len > 0) {
      mg_http_reply(c, 200, s_json_header, "{\"config\":{\"fields\":%.*s},\"data\":{\"total\":0,\"nodes\":[]}}", (int) fields_tok.len, fields_tok.buf);
    } else {
      mg_http_reply(c, 200, s_json_header, "{\"config\":{\"fields\":[]},\"data\":{\"total\":0,\"nodes\":[]}}");
    }
    free(cfg_buf);
    return;
  }

  char headers[32][64] = {""};
  int header_count = 0;
  int row_count = 0;
  int online_idx = -1;
  int camera_idx = -1;
  int operation_idx = -1;
  char line[4096];

  if (fgets(line, sizeof(line), fp_data) == NULL) {
    fclose(fp_data);
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Empty CSV\"}");
    free(cfg_buf);
    return;
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
      if (strcmp(headers[header_count], "isOnline") == 0) online_idx = header_count;
      if (strcmp(headers[header_count], "cameraType") == 0) camera_idx = header_count;
      if (strcmp(headers[header_count], "operation") == 0) operation_idx = header_count;
      header_count++;
    }
    while ((*end == ',' || *end == '\n' || *end == '\r') && *end != '\0') end++;
    h = end;
  }

  row_count = 0;
  while (fgets(line, sizeof(line), fp_data) != NULL) {
    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0' || *p == '\n' || *p == '\r') continue;

    if (online_filter[0] != '\0' && online_idx >= 0) {
      char val[32];
      if (get_csv_field(p, online_idx, val, sizeof(val)) != 0 || !is_value_in_list(val, online_filter)) continue;
    }

    if (camera_filter[0] != '\0' && camera_idx >= 0) {
      char val[32];
      if (get_csv_field(p, camera_idx, val, sizeof(val)) != 0 || !is_value_in_list(val, camera_filter)) continue;
    }

    if (operation_filter[0] != '\0') {
      if (operation_idx >= 0) {
        char val[64];
        if (get_csv_field(p, operation_idx, val, sizeof(val)) != 0) continue;
        if (is_value_in_list("0", operation_filter) && val[0] == '\0') {
        } else if (!is_value_in_list(val, operation_filter)) {
          continue;
        }
      }
    }

    row_count++;
  }

  fseek(fp_data, 0, SEEK_SET);

  long total_size = cfg_size + (long) page_size * 1024 + 2048;
  char *response = (char *) malloc((size_t) total_size);
  if (response == NULL) {
    fclose(fp_data);
    mg_http_reply(c, 200, s_json_header, "{\"error\":\"Memory allocation failed\"}");
    free(cfg_buf);
    return;
  }

  int pos = 0;
  pos += snprintf(response + pos, total_size - pos, "{\"config\":{\"fields\":");
  struct mg_str fields_tok = mg_json_get_tok(mg_str(cfg_buf), "$.fields");
  if (fields_tok.len > 0) {
    memcpy(response + pos, fields_tok.buf, (size_t) fields_tok.len);
    pos += (int) fields_tok.len;
  } else {
    pos += snprintf(response + pos, total_size - pos, "[]");
  }
  pos += snprintf(response + pos, total_size - pos, "},");
  free(cfg_buf);
  pos += snprintf(response + pos, total_size - pos, "\"data\":{\"total\":%d,\"nodes\":[", row_count);

  int first = 1;
  int count = 0;
  int skip_count = (page - 1) * page_size;
  int current_row = 0;

  fgets(line, sizeof(line), fp_data);

  while (fgets(line, sizeof(line), fp_data) != NULL && count < page_size) {
    char *line_start = line;
    while (*line_start == ' ' || *line_start == '\t') line_start++;
    if (*line_start == '\0' || *line_start == '\n' || *line_start == '\r') continue;

    if (online_filter[0] != '\0' && online_idx >= 0) {
      char val[32];
      if (get_csv_field(line_start, online_idx, val, sizeof(val)) != 0 || !is_value_in_list(val, online_filter)) continue;
    }

    if (camera_filter[0] != '\0' && camera_idx >= 0) {
      char val[32];
      if (get_csv_field(line_start, camera_idx, val, sizeof(val)) != 0 || !is_value_in_list(val, camera_filter)) continue;
    }

    if (operation_filter[0] != '\0') {
      if (operation_idx >= 0) {
        char val[64];
        if (get_csv_field(line_start, operation_idx, val, sizeof(val)) != 0) continue;
        if (is_value_in_list("0", operation_filter) && val[0] == '\0') {
        } else if (!is_value_in_list(val, operation_filter)) {
          continue;
        }
      }
    }

    current_row++;
    if (current_row <= skip_count) continue;

    char *p = line;
    while (*p != '\0' && *p != '\n' && *p != '\r') p++;
    *p = '\0';

    if (!first) pos += snprintf(response + pos, total_size - pos, ",");
    first = 0;
    count++;

    pos += snprintf(response + pos, total_size - pos, "{");
    for (int i = 0; i < header_count; i++) {
      if (i > 0) pos += snprintf(response + pos, total_size - pos, ",");
      pos += snprintf(response + pos, total_size - pos, "\"%s\":\"", headers[i]);

      char field_val[512] = "";
      get_csv_field(line_start, i, field_val, sizeof(field_val));

      const char *output_val = field_val;

      for (int j = 0; output_val[j] != '\0'; j++) {
        if (output_val[j] == '"' || output_val[j] == '\\') {
          pos += snprintf(response + pos, total_size - pos, "\\");
        }
        pos += snprintf(response + pos, total_size - pos, "%c", output_val[j]);
      }
      pos += snprintf(response + pos, total_size - pos, "\"");
    }
    pos += snprintf(response + pos, total_size - pos, "}");
  }

  fclose(fp_data);

  pos += snprintf(response + pos, total_size - pos, "]}}");

  mg_http_reply(c, 200, s_json_header, "%s", response);
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
    } else if (mg_match(hm->uri, mg_str("/api/nodes/get"), NULL)) {
      handle_nodes_get(c, hm);
    } else if (mg_match(hm->uri, mg_str("/api/nodes/set"), NULL)) {
      handle_nodes_set(c, hm->body);
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
  mg_http_listen(mgr, HTTP_URL, fn, NULL);
  mg_http_listen(mgr, HTTPS_URL, fn, NULL);
}