// Copyright (c) 2020-2023 Cesanta Software Limited
// All rights reserved

#include "mongoose.h"
#include "net.h"
#include "data_source.h"

static int s_sig_num;
static void signal_handler(int sig_num) {
  signal(sig_num, signal_handler);
  s_sig_num = sig_num;
}

static const char *get_path_from_config(const char *json_path, const char *default_path) {
  FILE *fp = fopen("data_config.json", "rb");
  if (fp == NULL) {
    fprintf(stderr, "Warning: Cannot open data_config.json, using default path: %s\n", default_path);
    return default_path;
  }

  fseek(fp, 0, SEEK_END);
  long size = ftell(fp);
  fseek(fp, 0, SEEK_SET);

  char *buf = (char *) malloc((size_t) size + 1);
  if (buf == NULL) {
    fclose(fp);
    fprintf(stderr, "Warning: Cannot allocate memory for config, using default path: %s\n", default_path);
    return default_path;
  }

  fread(buf, 1, (size_t) size, fp);
  fclose(fp);
  buf[size] = '\0';

  char *path = mg_json_get_str(mg_str(buf), json_path);
  const char *result = path != NULL ? path : default_path;
  
  // Make a copy since mg_json_get_str may return temporary buffer
  static char path_buf[256];
  strncpy(path_buf, result, sizeof(path_buf) - 1);
  path_buf[sizeof(path_buf) - 1] = '\0';
  
  free(buf);
  return path_buf;
}

int main(void) {
  struct mg_mgr mgr;

  signal(SIGPIPE, SIG_IGN);
  signal(SIGINT, signal_handler);
  signal(SIGTERM, signal_handler);

  mg_log_set(MG_LL_DEBUG);  // Set debug log level

#if !defined(CSV_MODE)
  // Initialize SQLite database with path from config
  const char *sqlite_path = get_path_from_config("$.sqliteFilePath", "device_dashboard.db");
  if (ds_init(sqlite_path) != 0) {
    fprintf(stderr, "Failed to initialize database\n");
    return 1;
  }
  MG_INFO(("Using SQLite database mode, file: %s", sqlite_path));
#else
  // Initialize CSV mode with path from config
  const char *csv_path = get_path_from_config("$.csvFilePath", "leaf_nodes.csv");
  if (ds_init(csv_path) != 0) {
    fprintf(stderr, "Failed to initialize CSV\n");
    return 1;
  }
  MG_INFO(("Using CSV mode, file: %s", csv_path));
#endif

  mg_mgr_init(&mgr);
  mg_wakeup_init(&mgr);

  web_init(&mgr);
  while (s_sig_num == 0) {
    mg_mgr_poll(&mgr, 50);
  }

  mg_mgr_free(&mgr);
  ds_cleanup();
  MG_INFO(("Exiting on signal %d", s_sig_num));

  return 0;
}
