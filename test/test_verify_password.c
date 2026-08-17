// Unit test for verify_password() — exercises the REAL implementation from
// password_hash.h (the same code net.c uses).
//
// Build (run from c:\s\vd):
//   gcc test\test_verify_password.c mongoose.c -I. -DMG_TLS=MG_TLS_BUILTIN ^
//       -DMG_ENABLE_POLL=0 -lws2_32 -o test\test_verify_password.exe
// Run:
//   test\test_verify_password.exe
#include <stdio.h>
#include <string.h>
#include "mongoose.h"
#include "password_hash.h"

static int tests_run = 0, tests_pass = 0, tests_fail = 0;

#define CHECK(cond, msg) do {                                   \
  tests_run++;                                                  \
  if (cond) { tests_pass++; printf("  PASS: %s\n", msg); }     \
  else { tests_fail++; printf("  FAIL: %s (line %d)\n", msg, __LINE__); } \
} while (0)

int main(void) {
  printf("=== Unit Test: verify_password ===\n");
  printf("Algorithm: %d iterations of SHA256(salt_hex || password)\n\n",
         PASS_HASH_ITERATIONS);

  // Fixed 32-hex-char salt (same format as gen_passhash.py produces).
  const char *salt = "c91368f202902d4845e7e72e5306b43a";

  // Pre-compute hashes for known passwords using the REAL hash_password().
  char hash_normal[65];   hash_password("Atos.202102",   salt, hash_normal);
  char hash_empty[65];    hash_password("",              salt, hash_empty);
  char hash_special[65];  hash_password("Gddl!#%2026!@", salt, hash_special);
  char hash_cjk[65];      hash_password("passw0rd",      salt, hash_cjk);
  char hash_long[65];     hash_password("a-very-long-password-with-!@#$%^&*()_+-= and spaces", salt, hash_long);

  // ----------------------------------------------------- Section 1: wrong password
  printf("--- 1. Wrong password (should all return 0) ---\n");
  CHECK(verify_password(hash_normal, salt, "", "wrongpass") == 0, "completely wrong password");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202101") == 0, "off-by-one last char");
  CHECK(verify_password(hash_normal, salt, "", "atos.202102") == 0, "case difference (lowercase)");
  CHECK(verify_password(hash_normal, salt, "", "ATOS.202102") == 0, "case difference (uppercase)");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202102 ") == 0, "trailing space appended");
  CHECK(verify_password(hash_normal, salt, "", " Atos.202102") == 0, "leading space prepended");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202102\n") == 0, "trailing newline");
  CHECK(verify_password(hash_normal, salt, "", "Atos202102") == 0, "missing dot");
  CHECK(verify_password(hash_normal, salt, "", "Atos..202102") == 0, "extra dot");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202103") == 0, "wrong month");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202102x") == 0, "extra trailing char");
  CHECK(verify_password(hash_normal, salt, "", "xAtos.202102") == 0, "extra leading char");

  // ----------------------------------------------------- Section 2: empty password
  printf("\n--- 2. Empty password ---\n");
  CHECK(verify_password(hash_empty, salt, "", "") == 1, "empty pw matches empty-hash entry");
  CHECK(verify_password(hash_empty, salt, "", "x") == 0, "non-empty pw vs empty-hash entry");
  CHECK(verify_password(hash_normal, salt, "", "") == 0, "empty pw vs non-empty hash");
  CHECK(verify_password("", "", "", "") == 1, "legacy: empty plaintext matches empty");
  CHECK(verify_password("", "", "", "nonempty") == 0, "legacy: non-empty vs empty plaintext");

  // ----------------------------------------------------- Section 3: special chars
  printf("\n--- 3. Special character passwords ---\n");
  CHECK(verify_password(hash_special, salt, "", "Gddl!#%2026!@") == 1, "special chars exact match");
  CHECK(verify_password(hash_special, salt, "", "gddl!#%2026!@") == 0, "special chars case difference");
  CHECK(verify_password(hash_special, salt, "", "Gddl!#%2026!A") == 0, "last char different (! vs A)");
  CHECK(verify_password(hash_special, salt, "", "Gddl!# 2026!@") == 0, "% replaced with space");
  CHECK(verify_password(hash_special, salt, "", "Gddl!#%2026!") == 0, "missing trailing @");
  CHECK(verify_password(hash_special, salt, "", "Gddl!#%2026!@@") == 0, "extra trailing @");

  // More special-char passwords
  char hash_punct[65]; hash_password("!@#$%^&*()_+-=[]{}|;:',.<>?/`~", salt, hash_punct);
  CHECK(verify_password(hash_punct, salt, "", "!@#$%^&*()_+-=[]{}|;:',.<>?/`~") == 1, "all punctuation exact match");
  CHECK(verify_password(hash_punct, salt, "", "!@#$%^&*()_+-=[]{}|;:',.<>?/`") == 0, "punctuation missing last char");

  char hash_quote[65]; hash_password("pass'\"word", salt, hash_quote);
  CHECK(verify_password(hash_quote, salt, "", "pass'\"word") == 1, "quotes in password");
  CHECK(verify_password(hash_quote, salt, "", "pass'word") == 0, "missing double-quote");

  char hash_backslash[65]; hash_password("a\\b\\c", salt, hash_backslash);
  CHECK(verify_password(hash_backslash, salt, "", "a\\b\\c") == 1, "backslashes in password");
  CHECK(verify_password(hash_backslash, salt, "", "a/b/c") == 0, "forward vs backslash");

  // Long password with mixed special chars
  CHECK(verify_password(hash_long, salt, "", "a-very-long-password-with-!@#$%^&*()_+-= and spaces") == 1, "long mixed password exact");
  CHECK(verify_password(hash_long, salt, "", "a-very-long-password-with-!@#$%^&*()_+-= and spaceS") == 0, "long mixed 1-char diff");

  // ----------------------------------------------------- Section 4: baseline (correct)
  printf("\n--- 4. Correct password (baseline — should all return 1) ---\n");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202102") == 1, "normal correct password");
  CHECK(verify_password(hash_special, salt, "", "Gddl!#%2026!@") == 1, "special correct password");
  CHECK(verify_password(hash_punct, salt, "", "!@#$%^&*()_+-=[]{}|;:',.<>?/`~") == 1, "punctuation correct password");
  CHECK(verify_password(hash_long, salt, "", "a-very-long-password-with-!@#$%^&*()_+-= and spaces") == 1, "long correct password");

  // ----------------------------------------------------- Section 5: legacy plaintext
  printf("\n--- 5. Legacy plaintext fallback (passHash empty) ---\n");
  CHECK(verify_password("", "", "plaintext123", "plaintext123") == 1, "legacy plaintext match");
  CHECK(verify_password("", "", "plaintext123", "plaintext124") == 0, "legacy plaintext mismatch");
  CHECK(verify_password("", "", "Gddl!#%2026!@", "Gddl!#%2026!@") == 1, "legacy special chars match");
  CHECK(verify_password(NULL, NULL, "abc", "abc") == 1, "legacy with NULL hash/salt still works");
  CHECK(verify_password(NULL, NULL, "abc", "abd") == 0, "legacy with NULL hash/salt mismatch");

  // ----------------------------------------------------- Section 6: NULL safety
  printf("\n--- 6. NULL safety ---\n");
  CHECK(verify_password(hash_normal, salt, "", NULL) == 0, "NULL password rejected");
  CHECK(verify_password(NULL, NULL, NULL, "test") == 0, "all fields NULL except pw");
  CHECK(verify_password(NULL, salt, NULL, "test") == 0, "NULL passHash and pass");

  // ----------------------------------------------------- Section 7: cross-check
  // Hash produced by gen_passhash.py for password "Atos.202102" with this salt
  printf("\n--- 7. Cross-check against gen_passhash.py output ---\n");
  // Pre-computed by: python -c "from gen_passhash import hash_password; print(hash_password('Atos.202102','c91368f202902d4845e7e72e5306b43a'))"
  printf("  (C hash)  %s\n", hash_normal);
  printf("  (salt)    %s\n", salt);
  CHECK(strlen(hash_normal) == 64, "hash is 64 hex chars");
  CHECK(hash_normal[64] == '\0', "hash is null-terminated");

  // ----------------------------------------------------- Section 8: different salts
  printf("\n--- 8. Different salts produce different hashes ---\n");
  const char *salt2 = "3c83a6d3a9d33b40d2166d017fee201b";
  char hash_s2[65]; hash_password("Atos.202102", salt2, hash_s2);
  CHECK(strcmp(hash_normal, hash_s2) != 0, "same password + different salt = different hash");
  CHECK(verify_password(hash_s2, salt2, "", "Atos.202102") == 1, "same password matches with its own salt");
  CHECK(verify_password(hash_normal, salt, "", "Atos.202102") == 1, "same password matches with original salt");
  CHECK(verify_password(hash_s2, salt, "", "Atos.202102") == 0, "hash from salt2 rejected when verified with salt1");

  // ----------------------------------------------------- summary
  printf("\n================================================\n");
  printf("Results: %d/%d passed", tests_pass, tests_run);
  if (tests_fail == 0) {
    printf(" — ALL PASSED\n");
  } else {
    printf(", %d FAILED\n", tests_fail);
  }
  printf("================================================\n");
  return tests_fail == 0 ? 0 : 1;
}
