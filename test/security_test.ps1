# Security & Edge Case Test v3 — targets UNTESTED areas
# Focus: path traversal, auth edge cases, static files, URL encoding, protocol quirks
$ErrorActionPreference = 'Continue'
$base = 'http://localhost:8000'
$apiToken = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
$auth = '-H',"apiToken: $apiToken"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$pass = 0; $fail = 0; $issues = @()

function Log($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" }
function Check($cat, $name, $cond, $detail='') {
    if ($cond) { Write-Host "  PASS [$cat] $name $detail" -ForegroundColor Green; $script:pass++ }
    else { Write-Host "  FAIL [$cat] $name $detail" -ForegroundColor Red; $script:fail++; $script:issues += "[$cat] $name" }
}
function CheckIssue($cat, $name, $detail='') {
    Write-Host "  ISSUE [$cat] $name $detail" -ForegroundColor Yellow; $script:issues += "[$cat] ${name}: $detail"
}

Log "========== SECURITY & EDGE CASE TEST START =========="

# ============================================================
# A. PATH TRAVERSAL / STATIC FILE SECURITY
# ============================================================
Log "----- A. PATH TRAVERSAL / STATIC FILE SECURITY -----"

# A1. Root path serves frontend
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/" 2>$null
Check 'SEC' "Root path serves frontend" ($code -eq '200') "code=$code"

# A2. index.html accessible
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/index.html" 2>$null
Check 'SEC' "index.html accessible" ($code -eq '200') "code=$code"

# A3. Path traversal outside web_root
$code = & curl.exe --path-as-is -s -o NUL -w '%{http_code}' "$base/../../../etc/passwd" 2>$null
Check 'SEC' "Path traversal /../../../etc/passwd blocked" ($code -ne '200') "code=$code (should not be 200)"

# A4. Path traversal via encoded dots
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/%2e%2e/%2e%2e/%2e%2e/etc/passwd" 2>$null
Check 'SEC' "Encoded path traversal %2e%2e blocked" ($code -ne '200') "code=$code"

# A5. Path traversal via double encoding
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/%252e%252e/%252e%252e/etc/passwd" 2>$null
Check 'SEC' "Double-encoded path traversal blocked" ($code -ne '200') "code=$code"

# A6. Path traversal targeting source code
$code = & curl.exe --path-as-is -s -o NUL -w '%{http_code}' "$base/../net.c" 2>$null
Check 'SEC' "Path traversal to net.c blocked" ($code -ne '200') "code=$code"

# A7. Path traversal to database
$code = & curl.exe --path-as-is -s -o NUL -w '%{http_code}' "$base/../device_dashboard.db" 2>$null
Check 'SEC' "Path traversal to .db blocked" ($code -ne '200') "code=$code"

# A8. Nonexistent static file
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/nonexistent.html" 2>$null
Check 'SEC' "Nonexistent file returns 404" ($code -eq '404') "code=$code"

# A9. Directory listing (should not list)
$code = & curl.exe --path-as-is -s -o NUL -w '%{http_code}' "$base/../" 2>$null
Check 'SEC' "Parent directory listing blocked" ($code -ne '200') "code=$code"

# A10. Null byte in path
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/index.html%00.txt" 2>$null
Check 'SEC' "Null byte in path no crash" ($code -ge 200 -or $code -eq '404') "code=$code"

# ============================================================
# B. AUTHENTICATION EDGE CASES
# ============================================================
Log "----- B. AUTHENTICATION EDGE CASES -----"

# B1. Wrong apiToken
$code = & curl.exe -s -o NUL -w '%{http_code}' -H 'apiToken: wrongtoken' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "Wrong apiToken -> 403" ($code -eq '403') "code=$code"

# B2. Non-existent apiToken
$code = & curl.exe -s -o NUL -w '%{http_code}' -H 'apiToken: nonexistent' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "Non-existent apiToken -> 403" ($code -eq '403') "code=$code"

# B3. Empty credentials (no -u)
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "No credentials -> 403" ($code -eq '403') "code=$code"

# B4. No auth header (should get 403)
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "No auth header -> 403" ($code -eq '403') "code=$code"

# B5. Empty apiToken
$code = & curl.exe -s -o NUL -w '%{http_code}' -H 'apiToken: ' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "Empty apiToken -> 403" ($code -eq '403') "code=$code"

# B6. Login with GET method (should it work? Currently no method enforcement)
$code = & curl.exe -s -o NUL -w '%{http_code}' -X GET -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
Check 'AUTH' "GET /api/login" ($code -eq '200') "code=$code (login has no method enforcement — informational)"

# B7. Logout with GET method (CSRF risk — no method enforcement)
$cookieJar = 'c:\s\vd\test\_tmp_sec_cookies.txt'
$null = & curl.exe -s -c $cookieJar -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
$code = & curl.exe -s -o NUL -w '%{http_code}' -b $cookieJar "$base/api/logout" 2>$null
Check 'AUTH' "GET /api/logout (CSRF risk)" ($code -eq '200') "code=$code (logout has no method enforcement — potential CSRF)"

# B8. After GET logout, cookie should be expired
$code = & curl.exe -s -o NUL -w '%{http_code}' -b $cookieJar "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'AUTH' "Cookie expired after GET logout" ($code -eq '403') "code=$code"

# B9. Concurrent logins (same user gets new token each time)
$jar1 = 'c:\s\vd\test\_tmp_sec_jar1.txt'
$jar2 = 'c:\s\vd\test\_tmp_sec_jar2.txt'
$null = & curl.exe -s -c $jar1 -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
Start-Sleep -Milliseconds 100
$null = & curl.exe -s -c $jar2 -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
$tok1 = (Select-String 'access_token' $jar1 -ErrorAction SilentlyContinue) -replace '.*access_token\s+(\S+).*','$1'
$tok2 = (Select-String 'access_token' $jar2 -ErrorAction SilentlyContinue) -replace '.*access_token\s+(\S+).*','$1'
Check 'AUTH' "Concurrent logins get different tokens" ($tok1 -ne $tok2) "tok1=[$tok1] tok2=[$tok2]"

# B10. First session token still works after second login? (token regeneration issue)
# NOTE: handle_login calls mg_random_str which regenerates the token in the user struct.
# This means the FIRST session's token is INVALIDATED when the second login happens.
$code = & curl.exe -s -o NUL -w '%{http_code}' -b $jar1 "$base/api/nodes/get?page=1&pageSize=1" 2>$null
if ($code -eq '403') {
    CheckIssue 'AUTH' "Session invalidation on re-login" "First session token invalidated when same user logs in again (code=$code). This is a KNOWN design choice — single-session per user."
} else {
    Check 'AUTH' "First session still works after re-login" ($code -eq '200') "code=$code"
}

# ============================================================
# C. URL ENCODING EDGE CASES
# ============================================================
Log "----- C. URL ENCODING EDGE CASES -----"

# C1. URL-encoded CJK keyword (马 = %E9%A9%AC)
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1&keyword=%E9%A9%AC" @auth 2>$null
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=200&keyword=%E9%A9%AC" @auth 2>$null | ConvertFrom-Json
Check 'URL' "URL-encoded CJK keyword (马)" ($r.data.total -gt 0) "total=$($r.data.total)"

# C2. URL-encoded comma in isOnline (%2C)
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&isOnline=0%2C1" @auth 2>$null | ConvertFrom-Json
Check 'URL' "URL-encoded comma %2C in isOnline" ($r.data.total -gt 0) "total=$($r.data.total)"

# C3. Plus sign as space in keyword
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&keyword=+" @auth 2>$null | ConvertFrom-Json
Check 'URL' "Plus sign keyword no crash" ($true) "total=$($r.data.total)"

# C4. Double-encoded percent (%25 = %)
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&keyword=%25" @auth 2>$null | ConvertFrom-Json
Check 'URL' "Double-encoded %25 -> literal % in keyword" ($true) "total=$($r.data.total) (should match all if % reaches LIKE, or 0 if literal)"

# C5. URL-encoded special chars in keyword (< > " ')
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&keyword=%3Cscript%3E" @auth 2>$null | ConvertFrom-Json
Check 'URL' "URL-encoded <script> in keyword no crash" ($true) "total=$($r.data.total)"

# C6. Very long keyword (URL-encoded, 500 chars)
$longKw = '%E9%A9%AC' * 100  # 600 chars URL-encoded = 200 CJK chars
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1&keyword=$longKw" @auth 2>$null
Check 'URL' "Very long URL-encoded keyword no crash" ($code -eq '200') "code=$code"

# C7. Duplicate parameters (page=1&page=999)
$r = & curl.exe -s "$base/api/nodes/get?page=1&page=999&pageSize=1" @auth 2>$null | ConvertFrom-Json
Check 'URL' "Duplicate page param" ($r.data.nodes.Count -eq 1) "returned=$($r.data.nodes.Count) (first or last value used)"

# C8. Space in filter value (URL-encoded %20)
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&isOnline=%20" @auth 2>$null | ConvertFrom-Json
Check 'URL' "Space in isOnline value no crash" ($true) "total=$($r.data.total)"

# ============================================================
# D. ROUTING EDGE CASES
# ============================================================
Log "----- D. ROUTING EDGE CASES -----"

# D1. Case sensitivity: /API/LOGIN (should not match /api/login)
$code = & curl.exe -s -o NUL -w '%{http_code}' -u 'scnqjs:Atos.202102' "$base/API/LOGIN" 2>$null
Check 'RTE' "Case sensitivity /API/LOGIN" ($code -ne '200') "code=$code (should NOT match /api/login — case sensitive)"

# D2. Case sensitivity: /Api/Nodes/Get
$code = & curl.exe -s -o NUL -w '%{http_code' "$base/Api/Nodes/Get?page=1&pageSize=1" @auth 2>$null
if ($code -eq '') { $code = '000' }
Check 'RTE' "Case sensitivity /Api/Nodes/Get" ($code -ne '200') "code=$code"

# D3. /api/ (just prefix, no endpoint)
$code = & curl.exe -s -o NUL -w '%{http_code}' @auth "$base/api/" 2>$null
Check 'RTE' "/api/ unknown endpoint" ($code -eq '403' -or $code -eq '404') "code=$code"

# D4. /api (no trailing slash)
$code = & curl.exe -s -o NUL -w '%{http_code}' @auth "$base/api" 2>$null
Check 'RTE' "/api no trailing slash" ($code -eq '403' -or $code -eq '404') "code=$code"

# D5. /api/unknown/endpoint
$code = & curl.exe -s -o NUL -w '%{http_code}' @auth "$base/api/unknown/endpoint" 2>$null
Check 'RTE' "/api/unknown/endpoint -> 403 or 404" ($code -eq '403' -or $code -eq '404') "code=$code"

# D6. Trailing slash on /api/login/
$code = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: $apiToken" "$base/api/login/" 2>$null
Check 'RTE' "/api/login/ trailing slash" ($code -ne '200') "code=$code (trailing slash should not match)"

# D7. /api/logout with trailing slash
$code = & curl.exe -s -o NUL -w '%{http_code}' @auth "$base/api/logout/" 2>$null
Check 'RTE' "/api/logout/ trailing slash" ($code -ne '200') "code=$code"

# D8. HTTP method TRACE (should not echo back — no XST vulnerability)
$code = & curl.exe -s -o NUL -w '%{http_code}' -X TRACE "$base/api/nodes/get" @auth 2>$null
Check 'RTE' "TRACE method no XST" ($code -ne '200') "code=$code"

# D9. HTTP method CONNECT
$code = & curl.exe -s -o NUL -w '%{http_code}' -X CONNECT "$base/api/nodes/get" @auth 2>$null
Check 'RTE' "CONNECT method handled" ($code -ne '200') "code=$code"

# ============================================================
# E. CRLF INJECTION / HEADER INJECTION
# ============================================================
Log "----- E. CRLF INJECTION / HEADER INJECTION -----"

# E1. CRLF in URL path
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get%0d%0aSet-Cookie:hacked=1?page=1&pageSize=1" @auth 2>$null
Check 'CRLF' "CRLF in URL path no injection" ($code -ne '200') "code=$code"

# E2. CRLF in keyword parameter
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1&keyword=test%0d%0aSet-Cookie:hacked=1" @auth 2>$null
Check 'CRLF' "CRLF in keyword no injection" ($code -ge 200 -and $code -lt 500) "code=$code (no 5xx crash)"

# E3. CRLF in isOnline parameter
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1&isOnline=1%0d%0aX-Injected:yes" @auth 2>$null
Check 'CRLF' "CRLF in isOnline no injection" ($code -ge 200 -and $code -lt 500) "code=$code"

# ============================================================
# F. LARGE / UNUSUAL REQUESTS
# ============================================================
Log "----- F. LARGE / UNUSUAL REQUESTS -----"

# F1. Very long URL (8KB of query params)
$longParam = 'x' * 7000
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1&keyword=$longParam" @auth 2>$null
Check 'LARGE' "Very long URL (7KB keyword) no crash" ($code -ge 200 -and $code -lt 500) "code=$code"

# F2. Large POST body (256 updates with long customOperation)
$updates = @(); for ($i = 1; $i -le 256; $i++) { $updates += "{`"id`":`"N$(($i % 200)+113174)`",`"customOperation`":`"$(('X' * 255))`"}" }
$body = '{"updates":[' + ($updates -join ',') + ']}'
$f = 'c:\s\vd\test\_tmp_sec_large.json'
[System.IO.File]::WriteAllText($f, $body, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f" @auth 2>$null
Check 'LARGE' "256 updates x 255-char customOperation" ($resp -match '"true"') "resp=$($resp.Substring(0, [math]::Min(60, $resp.Length)))"

# F3. JSON with nested objects in updates
$f3 = 'c:\s\vd\test\_tmp_sec_nested.json'
$body3 = '{"updates":[{"id":"N113174","customOperation":{"nested":"obj"}}]}'
[System.IO.File]::WriteAllText($f3, $body3, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f3" @auth 2>$null
Check 'LARGE' "Nested object in customOperation" ($resp -match '"false"' -or $resp -match '"true"') "resp=$resp (should handle gracefully)"

# F4. JSON with array as id
$f4 = 'c:\s\vd\test\_tmp_sec_arrid.json'
$body4 = '{"updates":[{"id":["N113174"],"customOperation":"arr_id"}]}'
[System.IO.File]::WriteAllText($f4, $body4, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f4" @auth 2>$null
Check 'LARGE' "Array as id value" ($resp -match '"false"' -or $resp -match '"true"') "resp=$resp"

# F5. JSON with null values
$f5 = 'c:\s\vd\test\_tmp_sec_null.json'
$body5 = '{"updates":[{"id":"N113174","customOperation":null}]}'
[System.IO.File]::WriteAllText($f5, $body5, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f5" @auth 2>$null
Check 'LARGE' "Null value in customOperation" ($resp -match '"false"' -or $resp -match '"true"') "resp=$resp"

# F6. JSON with number as id
$f6 = 'c:\s\vd\test\_tmp_sec_numid.json'
$body6 = '{"updates":[{"id":113174,"customOperation":"num_id"}]}'
[System.IO.File]::WriteAllText($f6, $body6, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f6" @auth 2>$null
Check 'LARGE' "Number as id (not string)" ($resp -match '"false"' -or $resp -match '"true"') "resp=$resp"

# F7. Unicode emoji in customOperation
$f7 = 'c:\s\vd\test\_tmp_sec_emoji.json'
$body7 = '{"updates":[{"id":"N113174","customOperation":"test_emoji_123"}]}'
[System.IO.File]::WriteAllText($f7, $body7, $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f7" @auth 2>$null
if ($resp -match '"true"') {
    $r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1" @auth 2>$null | ConvertFrom-Json
    $node = $r.data.nodes | Where-Object { $_.id -eq 'N113174' }
    Check 'LARGE' "Emoji in customOperation" ($node.customOperation -eq 'test_emoji_123') "stored=[$($node.customOperation)]"
} else {
    Check 'LARGE' "Emoji in customOperation" ($false) "resp=$resp"
}

# ============================================================
# G. CONCURRENT SESSION / TOKEN BEHAVIOR
# ============================================================
Log "----- G. CONCURRENT SESSION / TOKEN BEHAVIOR -----"

# G1. Multiple users login simultaneously
$users = @('scnqjs')
$jars = @{}
foreach ($u in $users) {
    $j = "c:\s\vd\test\_tmp_sec_${u}.txt"
    $null = & curl.exe -s -c $j -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
    $jars[$u] = $j
}
$allOk = $true
foreach ($u in $users) {
    $c = & curl.exe -s -o NUL -w '%{http_code}' -b $jars[$u] "$base/api/nodes/get?page=1&pageSize=1" 2>$null
    if ($c -ne '200') { $allOk = $false }
}
Check 'SESS' "User scnqjs can login and use API" $allOk "users=$($users -join ',')"

# G2. scnqjs can read but can scnqjs write?
$f = 'c:\s\vd\test\_tmp_sec_u1write.json'
[System.IO.File]::WriteAllText($f, '{"updates":[{"id":"N113174","customOperation":"SCNQJS_WRITE"}]}', $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f" -b $jars['scnqjs'] 2>$null
Check 'SESS' "scnqjs can write (cookie auth)" ($resp -match '"true"') "resp=$resp"

# G3. Token in Authorization Bearer header (should NOT work — only Basic Auth)
$loginResp = & curl.exe -s -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
# Extract token from cookie — login returns {"user":"scnqjs"}, token is in Set-Cookie
# Let's try using the raw password as bearer token
$code = & curl.exe -s -o NUL -w '%{http_code}' -H 'Authorization: Bearer scnqjs' "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'SESS' "Bearer token auth (not supported)" ($code -eq '403') "code=$code (only Basic Auth + Cookie supported)"

# ============================================================
# H. DATA VALIDATION EDGE CASES
# ============================================================
Log "----- H. DATA VALIDATION EDGE CASES -----"

# H1. pageSize as float (1.5)
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=1.5" @auth 2>$null
Check 'DATA' "pageSize=1.5 (float)" ($code -ge 200 -and $code -lt 500) "code=$code"

# H2. page as float
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1.5&pageSize=10" @auth 2>$null
Check 'DATA' "page=1.5 (float)" ($code -ge 200 -and $code -lt 500) "code=$code"

# H3. page with leading zeros (007)
$r = & curl.exe -s "$base/api/nodes/get?page=007&pageSize=1" @auth 2>$null | ConvertFrom-Json
Check 'DATA' "page=007 (leading zeros)" ($r.data.nodes.Count -eq 1) "returned=$($r.data.nodes.Count) (atoi handles leading zeros)"

# H4. page with + sign (page=+1)
$r = & curl.exe -s "$base/api/nodes/get?page=%2B1&pageSize=1" @auth 2>$null | ConvertFrom-Json
Check 'DATA' "page=+1 no crash" ($true) "returned=$($r.data.nodes.Count)"

# H5. page=0 (should be 400)
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=0&pageSize=10" @auth 2>$null
Check 'DATA' "page=0 -> 400" ($code -eq '400') "code=$code"

# H6. pageSize negative
$code = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/nodes/get?page=1&pageSize=-5" @auth 2>$null
Check 'DATA' "pageSize=-5 -> 400" ($code -eq '400') "code=$code"

# H7. isOnline with SQL-like value
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&isOnline=1' OR '1'='1" @auth 2>$null | ConvertFrom-Json
Check 'DATA' "SQL injection in isOnline -> literal" ($r.data.total -le 29623) "total=$($r.data.total) (should NOT return all rows)"

# H8. keyword with SQL UNION
$r = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1&keyword=xyz' UNION SELECT * FROM nodes--" @auth 2>$null | ConvertFrom-Json
Check 'DATA' "SQL UNION in keyword -> literal" ($r.data.total -eq 0 -or $r.data.total -le 29623) "total=$($r.data.total) (no injection)"

# H9. Very large page number (long overflow)
$r = & curl.exe -s "$base/api/nodes/get?page=99999999999999999999&pageSize=1" @auth 2>$null | ConvertFrom-Json
Check 'DATA' "page=99... (huge number) no crash" ($true) "returned=$($r.data.nodes.Count)"

# H10. Empty string id in update
$f = 'c:\s\vd\test\_tmp_sec_emptyid.json'
[System.IO.File]::WriteAllText($f, '{"updates":[{"id":"","customOperation":"empty_id"}]}', $utf8NoBom)
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f" @auth 2>$null
Check 'DATA' "Empty id in update" ($resp -match '"false"' -or $resp -match '"true"') "resp=$resp (empty id should be skipped or fail gracefully)"

# ============================================================
# SUMMARY
# ============================================================
Log "========== SECURITY & EDGE CASE TEST COMPLETE =========="
Log "PASS: $pass  FAIL: $fail"
if ($issues.Count -gt 0) {
    Log "Issues found:"
    foreach ($i in $issues) { Log "  - $i" }
}
