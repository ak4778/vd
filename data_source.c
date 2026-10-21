#include "data_source.h"

#if !defined(CSV_MODE) && !defined(PG_MODE)
extern const struct ds_driver sqlite_driver;
#endif
extern const struct ds_driver csv_driver;
#if defined(PG_MODE)
extern const struct ds_driver pg_driver;
#endif

static const struct ds_driver *s_active_driver = NULL;

static const struct ds_driver *select_driver(const char *path) {
#if defined(CSV_MODE)
  (void)path;
  return &csv_driver;
#elif defined(PG_MODE)
  (void)path;
  return &pg_driver;
#else
  (void)path;
  return &sqlite_driver;
#endif
}

int ds_init(const char *path) {
  s_active_driver = select_driver(path);
  if (s_active_driver == NULL) return -1;
  return s_active_driver->init(path);
}

void ds_cleanup(void) {
  if (s_active_driver != NULL) {
    s_active_driver->cleanup();
  }
  s_active_driver = NULL;
}

int ds_get_nodes(struct ds_query *query, struct ds_result *result) {
  if (s_active_driver == NULL) return -1;
  return s_active_driver->get_nodes(query, result);
}

int ds_update_nodes(struct ds_node *nodes, int count) {
  if (s_active_driver == NULL) return -1;
  return s_active_driver->update_nodes(nodes, count);
}

int ds_is_available(void) {
  if (s_active_driver == NULL) return 0;
  return s_active_driver->is_available();
}

// ---- 告警事件流水查询：不进驱动 vtable（仅 PG 有此能力，避免改三处驱动） ----
#if defined(PG_MODE)
int pg_alarm_supported(void);
int pg_get_new_alarms(unsigned long long after_id, struct ds_alarm *rows,
                      int max_rows);
int pg_get_alarms_page(int page, int page_size,
                       unsigned long long *total_out, struct ds_alarm *rows,
                       int max_rows);
int pg_get_recent_alarms(struct ds_alarm *rows, int max_rows);
#endif

int ds_alarm_supported(void) {
#if defined(PG_MODE)
  return pg_alarm_supported();
#else
  return 0;
#endif
}

int ds_get_new_alarms(unsigned long long after_id, struct ds_alarm *rows,
                      int max_rows) {
#if defined(PG_MODE)
  return pg_get_new_alarms(after_id, rows, max_rows);
#else
  (void) after_id;
  (void) rows;
  (void) max_rows;
  return -1;
#endif
}

int ds_get_alarms_page(int page, int page_size,
                       unsigned long long *total_out, struct ds_alarm *rows,
                       int max_rows) {
#if defined(PG_MODE)
  return pg_get_alarms_page(page, page_size, total_out, rows, max_rows);
#else
  (void) page;
  (void) page_size;
  (void) rows;
  (void) max_rows;
  if (total_out != NULL) *total_out = 0;
  return -1;
#endif
}

int ds_get_recent_alarms(struct ds_alarm *rows, int max_rows) {
#if defined(PG_MODE)
  return pg_get_recent_alarms(rows, max_rows);
#else
  (void) rows;
  (void) max_rows;
  return -1;
#endif
}

const struct ds_driver *ds_get_driver(void) {
  return s_active_driver;
}
