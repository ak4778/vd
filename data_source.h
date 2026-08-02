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
  char P1[128];
  char P3[128];
  char P4[128];
  // 标志位：字段是否在更新请求中提供（用于动态拼接 SET 子句，避免覆盖未提供的字段）
  int has_operation;
  int has_customOperation;
};

struct ds_query {
  const char *isOnline;
  const char *cameraType;
  const char *operation;
  const char *keyword;
  int page;
  int pageSize;
  // 标志位：参数是否出现在 URL 中（区分空值和未设置）
  int has_isOnline;
  int has_cameraType;
  int has_operation;
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
