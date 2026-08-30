// Copyright (c) 2020-2023 Cesanta Software Limited
// All rights reserved

// Enable custom log format before including mongoose.h
#define MG_ENABLE_CUSTOM_LOG 1

#include "mongoose.h"
#include "net.h"
#include "data_source.h"

// Custom log: human-readable timestamp instead of hex milliseconds
// Format: "2026-08-15 08:49:19 2I main.c:80:main  Using SQLite..."
// Optionally also writes to a log file (configured via logToFile/logFile in data_config.json)

static FILE *s_log_fp = NULL;  // Log file handle, NULL if logToFile is false

// Line buffer: accumulate a complete log line, then write it with a single
// fwrite per sink.  Writing char-by-char to unbuffered stderr costs one
// syscall per byte, which is catastrophic when stderr is a PowerShell
// Start-Process redirect pipe (~10ms per line -> +25-50ms per request at
// debug level, serializing the whole event loop down to ~20 r/s).
// Line buffer size: 2048 covers the longest line (full URI with a URL-encoded
// CJK keyword: ~9 bytes per char, net.c:1187 / mongoose.c:1335).  Override at
// build time with -DLOG_LINE_BUF_SIZE=4096 (Makefile sets it).  If a line
// still overflows, log_pfn flushes mid-line in chunks -- nothing is lost.
#ifndef LOG_LINE_BUF_SIZE
#define LOG_LINE_BUF_SIZE 2048
#endif
#if LOG_LINE_BUF_SIZE < 256
#error "LOG_LINE_BUF_SIZE too small: the log prefix alone can reach ~60 bytes"
#endif
static char s_lbuf[LOG_LINE_BUF_SIZE];
static size_t s_llen = 0;

static void lbuf_flush(void) {
  if (s_llen == 0) return;
  fwrite(s_lbuf, 1, s_llen, stderr);
  if (s_log_fp != NULL) {
    fwrite(s_lbuf, 1, s_llen, s_log_fp);
    fflush(s_log_fp);  // Flush after each complete log line
  }
  s_llen = 0;
}

void mg_log_prefix(int level, const char *file, int line, const char *fname) {
  const char *p = strrchr(file, '/');
  if (p == NULL) p = strrchr(file, '\\');
  if (p == NULL) p = file; else p++;

  time_t now = time(NULL);
  struct tm *t = localtime(&now);
  char ts[20];
  strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", t);

  char lc = (level >= 0 && level <= 4) ? "NEIDV"[level] : '?';
  int n = snprintf(s_lbuf, sizeof(s_lbuf), "%-19s %d%c %s:%d:%s  ", ts, level,
                   lc, p, line, fname);
  s_llen = (n > 0) ? ((size_t) n >= sizeof(s_lbuf) ? sizeof(s_lbuf) - 1
                                                   : (size_t) n)
                   : 0;
}

// Feed one char into the line buffer.  Required because mg_vxprintf drives
// formatting via a char-by-char callback, which is the only way mongoose's
// non-standard %M/%m specifiers get expanded.
static void log_pfn(char c, void *param) {
  (void) param;
  if (s_llen + 1 >= sizeof(s_lbuf)) lbuf_flush();  // overflow: flush early
  s_lbuf[s_llen++] = c;
}

void mg_log(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  // Use mongoose's formatter so %M/%m invoke the custom mg_pm_t callbacks
  // (e.g. mg_print_ip_port). C standard vfprintf does NOT understand %M.
  mg_vxprintf(log_pfn, NULL, fmt, &ap);
  va_end(ap);
  if (s_llen + 1 >= sizeof(s_lbuf)) lbuf_flush();
  s_lbuf[s_llen++] = '\n';
  lbuf_flush();
}

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

  // Runtime FD_SETSIZE probe: prove the compiled fd_set capacity.
  fprintf(stderr, "[probe] FD_SETSIZE=%d sizeof(fd_set)=%d\n",
          (int) FD_SETSIZE, (int) sizeof(fd_set));

  // Disable stdout/stderr buffering so logs flush immediately when
  // redirected to files (Windows defaults to full buffering for pipes/files).
  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stderr, NULL, _IONBF, 0);

  signal(SIGPIPE, SIG_IGN);
  signal(SIGINT, signal_handler);
  signal(SIGTERM, signal_handler);

  // Read log level from config (default: info)
  {
    const char *ll = get_path_from_config("$.logLevel", "info");
    int level = MG_LL_INFO;  // default
    if (strcmp(ll, "none") == 0) level = MG_LL_NONE;
    else if (strcmp(ll, "error") == 0) level = MG_LL_ERROR;
    else if (strcmp(ll, "info") == 0) level = MG_LL_INFO;
    else if (strcmp(ll, "debug") == 0) level = MG_LL_DEBUG;
    else if (strcmp(ll, "verbose") == 0) level = MG_LL_VERBOSE;
    mg_log_set(level);
    // Note: get_path_from_config returns static buffer, no free needed
  }

  // Open log file if logToFile is enabled in config
  {
    bool log_to_file = false;
    {
      FILE *fp = fopen("data_config.json", "rb");
      if (fp != NULL) {
        fseek(fp, 0, SEEK_END);
        long sz = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        char *buf = (char *) malloc((size_t) sz + 1);
        if (buf != NULL) {
          fread(buf, 1, (size_t) sz, fp);
          buf[sz] = '\0';
          mg_json_get_bool(mg_str(buf), "$.logToFile", &log_to_file);
          free(buf);
        }
        fclose(fp);
      }
    }
    if (log_to_file) {
      const char *log_file = get_path_from_config("$.logFile", "server.log");
      s_log_fp = fopen(log_file, "a");
      if (s_log_fp != NULL) {
        setvbuf(s_log_fp, NULL, _IOLBF, 0);  // Line-buffered for timely flush
      } else {
        fprintf(stderr, "Warning: Cannot open log file: %s\n", log_file);
      }
    }
  }

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
    mg_mgr_poll(&mgr, 10);
  }

  mg_mgr_free(&mgr);
  ds_cleanup();
  MG_INFO(("Exiting on signal %d", s_sig_num));
  if (s_log_fp != NULL) fclose(s_log_fp);

  return 0;
}
