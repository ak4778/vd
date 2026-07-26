#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

struct ds_node {
  char id[64];
  char name[128];
  char channelCode[128];
  char isOnline[16];
  char cameraType[16];
  char operation[64];
  char customOperation[256];
};

struct ds_query {
  const char *isOnline;
  const char *cameraType;
  const char *operation;
  int page;
  int pageSize;
};

struct ds_result {
  struct ds_node *nodes;
  int count;
  int total;
};

int ds_init(const char *path);
void ds_cleanup(void);

int ds_get_nodes(struct ds_query *query, struct ds_result *result);
int ds_update_nodes(struct ds_node *nodes, int count);

// Check if data source is available (file exists for CSV, database open for SQLite)
int ds_is_available(void);

#ifdef __cplusplus
}
#endif
