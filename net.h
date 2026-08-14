// Copyright (c) 2023 Cesanta Software Limited
// All rights reserved
#pragma once

#include "mongoose.h"

#ifdef __cplusplus
extern "C" {
#endif

#if !defined(HTTP_PORT)
#define HTTP_PORT 8000
#endif

#if !defined(HTTPS_PORT)
#define HTTPS_PORT 8443
#endif

// Legacy macros - deprecated, use HTTP_PORT/HTTPS_PORT instead
#if !defined(HTTP_URL)
#define HTTP_URL "http://0.0.0.0:8000"
#endif

#if !defined(HTTPS_URL)
#define HTTPS_URL "https://0.0.0.0:8443"
#endif

void web_init(struct mg_mgr *mgr);

#ifdef __cplusplus
}
#endif
