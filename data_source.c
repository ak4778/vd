#include "data_source.h"

#if !defined(CSV_MODE)
extern const struct ds_driver sqlite_driver;
#endif
extern const struct ds_driver csv_driver;

static const struct ds_driver *s_active_driver = NULL;

static const struct ds_driver *select_driver(const char *path) {
#if defined(CSV_MODE)
  (void)path;
  return &csv_driver;
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

const struct ds_driver *ds_get_driver(void) {
  return s_active_driver;
}
