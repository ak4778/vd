// Copyright (c) 2026 Cesanta Software Limited
// Password hashing & verification — header-only, shared by net.c and unit tests.
// Algorithm MUST match gen_passhash.py exactly.
#pragma once

#include "mongoose.h"
#include <string.h>

// 10000 iterations of SHA256(salt_hex || password).
//   h = SHA256(salt_hex + password)   # ASCII bytes
//   for _ in range(ITERATIONS - 1): h = SHA256(h)  # raw 32-byte digest
//   return h.hex()
// Output: 64-char lowercase hex + '\0' (out must be >= 65 bytes).
#define PASS_HASH_ITERATIONS 10000

static inline void hash_password(const char *password, const char *salt_hex,
                                  char out_hex[65]) {
  uint8_t digest[32];
  char buf[256];
  mg_snprintf(buf, sizeof(buf), "%s%s", salt_hex, password);
  mg_sha256_ctx ctx;
  mg_sha256_init(&ctx);
  mg_sha256_update(&ctx, (uint8_t *) buf, strlen(buf));
  mg_sha256_final(digest, &ctx);
  for (int i = 0; i < PASS_HASH_ITERATIONS - 1; i++) {
    uint8_t tmp[32];
    memcpy(tmp, digest, 32);
    mg_sha256_init(&ctx);
    mg_sha256_update(&ctx, tmp, 32);
    mg_sha256_final(digest, &ctx);
  }
  static const char hex[] = "0123456789abcdef";
  for (int i = 0; i < 32; i++) {
    out_hex[i * 2]     = hex[digest[i] >> 4];
    out_hex[i * 2 + 1] = hex[digest[i] & 0x0F];
  }
  out_hex[64] = '\0';
}

// Constant-time comparison to avoid timing side-channels.
static inline int ct_memcmp(const void *a, const void *b, size_t n) {
  const unsigned char *pa = (const unsigned char *) a;
  const unsigned char *pb = (const unsigned char *) b;
  unsigned char diff = 0;
  for (size_t i = 0; i < n; i++) diff |= (unsigned char)(pa[i] ^ pb[i]);
  return diff == 0 ? 0 : 1;
}

// Verify a plaintext password against stored credentials.
//   passHash: stored hex hash (64 chars) — if empty, fall back to plaintext.
//   salt:     stored salt (32 hex chars) — required if passHash non-empty.
//   pass:     stored plaintext password (legacy, used only if passHash empty).
//   pw:       plaintext password supplied by the user.
// Returns 1 on match, 0 on mismatch or NULL pw.
static inline int verify_password(const char *passHash, const char *salt,
                                   const char *pass, const char *pw) {
  if (pw == NULL) return 0;
  if (passHash != NULL && passHash[0] != '\0' &&
      salt != NULL && salt[0] != '\0') {
    char computed[65];
    hash_password(pw, salt, computed);
    return ct_memcmp(computed, passHash, 64) == 0;
  }
  if (pass == NULL) return 0;
  return strcmp(pw, pass) == 0;
}
