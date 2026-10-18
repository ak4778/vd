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

// 告警事件流水行（对应 PG 表 wind_alarm，由 monitor_fj_state.py 写入维护）
struct ds_alarm {
  unsigned long long id;   // 事件流水号（bigserial 主键，增量游标）
  char event[16];          // ALARM / RECOVER
  char event_time[24];     // 记录写入时刻 YYYY-MM-DD HH:MM:SS
  char data_ts[24];        // 触发数据点时刻（可为空串）
  char device_id[64];
  char device_name[128];
  char point_code[64];
  char point_name[128];
  char rule_code[32];      // C01_HIGH / C02_FROZEN / ...
  char alarm_type[32];
  double temperature;
  double alarm_value;
  char detail[256];
};

struct ds_driver {
  const char *name;
  int (*init)(const char *path);
  void (*cleanup)(void);
  int (*is_available)(void);
  int (*get_nodes)(struct ds_query *query, struct ds_result *result);
  int (*update_nodes)(struct ds_node *nodes, int count);
};

int ds_init(const char *path);
void ds_cleanup(void);

int ds_get_nodes(struct ds_query *query, struct ds_result *result);
int ds_update_nodes(struct ds_node *nodes, int count);

// Check if data source is available (file exists for CSV, database open for SQLite)
int ds_is_available(void);

// ---- 告警事件流水查询（仅 PG 模式实现；SQLite/CSV 模式不支持） ----
// 1: 支持告警查询（PG 连接就绪且 wind_alarm 表存在）；0: 不支持（结果缓存）
int ds_alarm_supported(void);
// 拉取 id > after_id 的新告警事件（按 id 升序，最多 max_rows 行，主键索引扫描）。
// 返回实际行数（0 = 无新数据；-1 = 不支持或查询失败）
int ds_get_new_alarms(unsigned long long after_id, struct ds_alarm *rows,
                      int max_rows);

#ifdef __cplusplus
}
#endif
