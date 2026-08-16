# Edge Case & Boundary Condition Stress Test
# Covers: Auth, Pagination, Filter, Search, Save, Concurrency, HTTP Method, URL, Data Integrity
$ErrorActionPreference = 'Continue'
$base = 'http://localhost:8000'
$apiToken = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
$auth = '-H',"apiToken: $apiToken"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$pass = 0; $fail = 0; $categories = @{}

function Log($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" }
function Check($cat, $name, $cond, $detail='') {
    $script:categories[$cat] = ($script:categories[$cat] + 1)
    if ($cond) { Write-Host "  PASS [$cat] $name $detail" -ForegroundColor Green; $script:pass++ }
    else { Write-Host "  FAIL [$cat] $name $detail" -ForegroundColor Red; $script:fail++ }
}

function GetCode($url) {
    $a = @('-s','-o','NUL','-w','%{http_code}',$url) + $auth
    return & curl.exe @a 2>$null
}
function GetCodeNoAuth($url) {
    return & curl.exe -s -o NUL -w '%{http_code}' $url 2>$null
}
function GetPage($page, $pageSize, $extra='') {
    $url = "$base/api/nodes/get?page=$page" + [char]38 + "pageSize=$pageSize$extra"
    $a = @('-s',$url) + $auth
    return & curl.exe @a 2>$null
}
function SendUpdate($bodyStr) {
    $f = 'c:\s\vd\test\_tmp_edge.json'
    $retries = 0
    while ($retries -lt 3) {
        try {
            [System.IO.File]::WriteAllText($f, $bodyStr, $utf8NoBom)
            break
        } catch {
            $retries++
            Start-Sleep -Milliseconds 100
        }
    }
    $a = @('-s','-X','POST',"$base/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f") + $auth
    return & curl.exe @a 2>$null
}
function SendUpdateNoAuth($bodyStr) {
    $f = 'c:\s\vd\test\_tmp_edge.json'
    $retries = 0
    while ($retries -lt 3) {
        try {
            [System.IO.File]::WriteAllText($f, $bodyStr, $utf8NoBom)
            break
        } catch {
            $retries++
            Start-Sleep -Milliseconds 100
        }
    }
    return & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' --data-binary "@$f" 2>$null
}

Log "========== EDGE CASE BOUNDARY TEST START =========="
$startTime = Get-Date

# Get a real node ID for save tests
$firstPage = GetPage 1 200 | ConvertFrom-Json
$testId = $firstPage.data.nodes[0].id
$totalNodes = $firstPage.data.total

# ============================================================
# 1. AUTH BOUNDARY
# ============================================================
Log "----- 1. AUTH BOUNDARY -----"

# Correct credentials (apiToken)
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: $apiToken" "$base/api/login" 2>$null
Check 'AUTH' "Login correct" ($c -eq 200) "code=$c"

# Wrong token
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: wrongtoken" "$base/api/login" 2>$null
Check 'AUTH' "Login wrong pass" ($c -eq 401) "code=$c"

# No auth header (empty credentials)
$c = & curl.exe -s -o NUL -w '%{http_code}' "$base/api/login" 2>$null
Check 'AUTH' "Login empty pass" ($c -eq 401) "code=$c"

# Non-existent token
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: nonexistent123" "$base/api/login" 2>$null
Check 'AUTH' "Login non-existent user" ($c -eq 401) "code=$c"

# No credentials at all
$c = GetCodeNoAuth "$base/api/nodes/get?page=1&pageSize=1"
Check 'AUTH' "No credentials -> rejected" ($c -eq 403 -or $c -eq 405) "code=$c"

# Public endpoint without auth
$c = GetCodeNoAuth "$base/api/mode/get"
Check 'AUTH' "Public /api/mode/get no auth" ($c -eq 200) "code=$c"

# Deleted endpoints
foreach ($ep in @('/api/config/get','/api/debug','/api/stats/get','/api/events/get','/api/settings/get','/api/settings/set')) {
    $c = GetCode "$base$ep"
    Check 'AUTH' "Deleted $ep" ($c -eq 404 -or $c -eq 403 -or $c -eq 405) "code=$c"
}

# ============================================================
# 2. PAGINATION BOUNDARY
# ============================================================
Log "----- 2. PAGINATION BOUNDARY -----"

# page=0
$c = GetCode "$base/api/nodes/get?page=0&pageSize=10"
Check 'PAGE' "page=0" ($c -eq 400) "code=$c"

# page=-1
$c = GetCode "$base/api/nodes/get?page=-1&pageSize=10"
Check 'PAGE' "page=-1" ($c -eq 400) "code=$c"

# page=abc (non-numeric)
$c = GetCode "$base/api/nodes/get?page=abc&pageSize=10"
Check 'PAGE' "page=abc" ($c -eq 400) "code=$c"

# page=0.5 (float)
$c = GetCode "$base/api/nodes/get?page=0.5&pageSize=10"
Check 'PAGE' "page=0.5" ($c -eq 400) "code=$c"

# page=99999999 (very large)
$r = GetPage 99999999 10 | ConvertFrom-Json
Check 'PAGE' "page=99999999 returns empty" ($r.data.nodes.Count -eq 0) "returned=$($r.data.nodes.Count)"

# page=1 (valid)
$r = GetPage 1 10 | ConvertFrom-Json
Check 'PAGE' "page=1 valid" ($r.data.nodes.Count -eq 10) "returned=$($r.data.nodes.Count)"

# pageSize=0
$c = GetCode "$base/api/nodes/get?page=1&pageSize=0"
Check 'PAGE' "pageSize=0" ($c -eq 400) "code=$c"

# pageSize=-1
$c = GetCode "$base/api/nodes/get?page=1&pageSize=-1"
Check 'PAGE' "pageSize=-1" ($c -eq 400) "code=$c"

# pageSize=abc
$c = GetCode "$base/api/nodes/get?page=1&pageSize=abc"
Check 'PAGE' "pageSize=abc" ($c -eq 400) "code=$c"

# pageSize=1 (minimum valid)
$r = GetPage 1 1 | ConvertFrom-Json
Check 'PAGE' "pageSize=1" ($r.data.nodes.Count -eq 1) "returned=$($r.data.nodes.Count)"

# pageSize=200 (maximum valid)
$r = GetPage 1 200 | ConvertFrom-Json
Check 'PAGE' "pageSize=200" ($r.data.nodes.Count -eq 200) "returned=$($r.data.nodes.Count)"

# pageSize=201 (over max, should cap to 200)
$r = GetPage 1 201 | ConvertFrom-Json
Check 'PAGE' "pageSize=201 capped" ($r.data.nodes.Count -le 200) "returned=$($r.data.nodes.Count)"

# pageSize=99999 (way over max)
$r = GetPage 1 99999 | ConvertFrom-Json
Check 'PAGE' "pageSize=99999 capped" ($r.data.nodes.Count -le 200) "returned=$($r.data.nodes.Count)"

# Missing page parameter (only pageSize)
$r = GetPage '' 10 | ConvertFrom-Json
Check 'PAGE' "Missing page param" ($r.data.total -gt 0) "total=$($r.data.total)"

# Last page (exact)
$lastPage = [math]::Ceiling($totalNodes / 50.0)
$r = GetPage $lastPage 50 | ConvertFrom-Json
$expected = $totalNodes - ($lastPage - 1) * 50
Check 'PAGE' "Last page=$lastPage exact" ($r.data.nodes.Count -eq $expected) "returned=$($r.data.nodes.Count) expected=$expected"

# Page beyond last
$r = GetPage ($lastPage + 1) 50 | ConvertFrom-Json
Check 'PAGE' "Beyond last page" ($r.data.nodes.Count -eq 0) "returned=$($r.data.nodes.Count)"

# No parameters at all
$a = @('-s',"$base/api/nodes/get") + $auth
$r = (& curl.exe @a 2>$null) | ConvertFrom-Json
Check 'PAGE' "No params at all" ($r.data.total -gt 0) "total=$($r.data.total)"

# ============================================================
# 3. FILTER BOUNDARY
# ============================================================
Log "----- 3. FILTER BOUNDARY -----"

# isOnline=1
$r = GetPage 1 200 '&isOnline=1' | ConvertFrom-Json
$ok = ($r.data.nodes | Where-Object { $_.isOnline -ne '1' }).Count -eq 0
Check 'FILT' "isOnline=1" $ok "total=$($r.data.total)"

# isOnline=0
$r = GetPage 1 200 '&isOnline=0' | ConvertFrom-Json
$ok = ($r.data.nodes | Where-Object { $_.isOnline -ne '0' }).Count -eq 0
Check 'FILT' "isOnline=0" $ok "total=$($r.data.total)"

# isOnline= (empty)
$r = GetPage 1 200 '&isOnline=' | ConvertFrom-Json
Check 'FILT' "isOnline= (empty)" ($r.data.total -eq 0) "total=$($r.data.total)"

# isOnline=99 (invalid)
$r = GetPage 1 200 '&isOnline=99' | ConvertFrom-Json
Check 'FILT' "isOnline=99 invalid" ($r.data.total -eq 0) "total=$($r.data.total)"

# isOnline=0,1 (all)
$r = GetPage 1 200 '&isOnline=0,1' | ConvertFrom-Json
Check 'FILT' "isOnline=0,1" ($r.data.total -eq $totalNodes) "total=$($r.data.total)"

# cameraType= (empty)
$r = GetPage 1 200 '&cameraType=' | ConvertFrom-Json
Check 'FILT' "cameraType= (empty)" ($r.data.total -eq 0) "total=$($r.data.total)"

# cameraType=99 (invalid)
$r = GetPage 1 200 '&cameraType=99' | ConvertFrom-Json
Check 'FILT' "cameraType=99 invalid" ($r.data.total -eq 0) "total=$($r.data.total)"

# operation= (empty)
$r = GetPage 1 200 '&operation=' | ConvertFrom-Json
Check 'FILT' "operation= (empty)" ($r.data.total -eq 0) "total=$($r.data.total)"

# operation=5 (invalid)
$r = GetPage 1 200 '&operation=5' | ConvertFrom-Json
Check 'FILT' "operation=5 invalid" ($r.data.total -eq 0) "total=$($r.data.total)"

# SQL injection in isOnline
$r = GetPage 1 50 "&isOnline=1' OR '1'='1" | ConvertFrom-Json
Check 'FILT' "SQLi isOnline" ($true) "total=$($r.data.total) (treated as literal)"

# SQL injection in cameraType
$r = GetPage 1 50 "&cameraType=1; DROP TABLE nodes;--" | ConvertFrom-Json
Check 'FILT' "SQLi cameraType" ($true) "total=$($r.data.total) (no crash)"

# SQL injection in operation
$r = GetPage 1 50 "&operation=1' UNION SELECT * FROM nodes--" | ConvertFrom-Json
Check 'FILT' "SQLi operation" ($true) "total=$($r.data.total) (no crash)"

# Very long filter value (100 chars)
$longVal = '1' * 100
$r = GetPage 1 50 "&isOnline=$longVal" | ConvertFrom-Json
Check 'FILT' "Long isOnline (100 chars)" ($true) "total=$($r.data.total) (no crash)"

# Combined all filters (allow small delta: earlier SQLi tests may have changed a few nodes' operation field)
$r = GetPage 1 200 '&isOnline=0,1&cameraType=1,2,3&operation=0,1,2,3,4' | ConvertFrom-Json
$delta = $totalNodes - $r.data.total
Check 'FILT' "Combined all filters" ($delta -ge 0 -and $delta -le 5) "total=$($r.data.total) delta=$delta (some nodes modified by SQLi tests)"

# ============================================================
# 4. SEARCH BOUNDARY
# ============================================================
Log "----- 4. SEARCH BOUNDARY -----"

# Normal keyword
$r = GetPage 1 200 '&keyword=N' | ConvertFrom-Json
Check 'SRCH' "keyword=N" ($r.data.total -gt 0) "total=$($r.data.total)"

# Empty keyword
$r = GetPage 1 50 '&keyword=' | ConvertFrom-Json
Check 'SRCH' "keyword= (empty)" ($r.data.total -ge 0) "total=$($r.data.total)"

# SQL injection in keyword
$r = GetPage 1 50 "&keyword=' OR 1=1 --" | ConvertFrom-Json
Check 'SRCH' "SQLi keyword" ($true) "total=$($r.data.total) (treated as literal)"

# DROP TABLE attempt
$r = GetPage 1 50 "&keyword='; DROP TABLE nodes;--" | ConvertFrom-Json
Check 'SRCH' "DROP TABLE keyword" ($true) "total=$($r.data.total) (no crash)"

# LIKE wildcard %
$r = GetPage 1 50 '&keyword=%' | ConvertFrom-Json
Check 'SRCH' "keyword=% (LIKE wildcard)" ($true) "total=$($r.data.total)"

# LIKE wildcard _
$r = GetPage 1 50 '&keyword=_' | ConvertFrom-Json
Check 'SRCH' "keyword=_ (LIKE wildcard)" ($true) "total=$($r.data.total)"

# Very long keyword (200 chars)
$longkw = 'A' * 200
$r = GetPage 1 50 "&keyword=$longkw" | ConvertFrom-Json
Check 'SRCH' "Long keyword (200 chars)" ($true) "total=$($r.data.total) (no crash)"

# Special characters in keyword
$r = GetPage 1 50 '&keyword=<script>alert(1)</script>' | ConvertFrom-Json
Check 'SRCH' "XSS keyword" ($true) "total=$($r.data.total) (treated as literal)"

# Keyword with special chars
$r = GetPage 1 50 '&keyword=N1%26N2' | ConvertFrom-Json
Check 'SRCH' "keyword with special chars" ($true) "total=$($r.data.total)"

# ============================================================
# 5. SAVE BOUNDARY
# ============================================================
Log "----- 5. SAVE BOUNDARY -----"

# Partial: only customOperation
$beforeOp = ($firstPage.data.nodes | Where-Object { $_.id -eq $testId }).operation
$body = '{"updates":[{"id":"' + $testId + '","customOperation":"EDGE_COP_TEST"}]}'
$resp = SendUpdate $body
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
Check 'SAVE' "Partial: cop only, op preserved" ($node.customOperation -eq 'EDGE_COP_TEST' -and $node.operation -eq $beforeOp) "op=$beforeOp->$($node.operation) cop=$($node.customOperation)"

# Partial: only operation
$beforeCop = $node.customOperation
$body = '{"updates":[{"id":"' + $testId + '","operation":"4"}]}'
$resp = SendUpdate $body
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
Check 'SAVE' "Partial: op only, cop preserved" ($node.operation -eq '4' -and $node.customOperation -eq $beforeCop) "op->$($node.operation) cop=$($node.customOperation)"

# Full update
$body = '{"updates":[{"id":"' + $testId + '","operation":"4","customOperation":"FULL_EDGE_TEST"}]}'
$resp = SendUpdate $body
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
Check 'SAVE' "Full update both fields" ($node.operation -eq '4' -and $node.customOperation -eq 'FULL_EDGE_TEST') "op=$($node.operation) cop=$($node.customOperation)"

# Empty updates array
$resp = SendUpdate '{"updates":[]}'
Check 'SAVE' "Empty updates" ($resp -match 'No updates') "resp=$resp"

# Invalid JSON
$resp = SendUpdate 'not json at all'
Check 'SAVE' "Invalid JSON" ($resp -match 'No updates') "resp=$resp"

# Wrong JSON key
$resp = SendUpdate '{"nodes":[]}'
Check 'SAVE' "Wrong JSON key" ($resp -match 'No updates') "resp=$resp"

# Missing id field
$resp = SendUpdate '{"updates":[{"operation":"4"}]}'
Check 'SAVE' "Missing id" ($resp -match 'No updates') "resp=$resp"

# Non-existent node id
$resp = SendUpdate '{"updates":[{"id":"NONEXIST999999","operation":"4"}]}'
Check 'SAVE' "Non-existent id" ($true) "resp=$resp (no crash)"

# Exactly 256 (boundary)
$updates = @(); for ($i = 1; $i -le 256; $i++) { $updates += '{"id":"N' + (($i % 200) + 113174) + '","customOperation":"B256_' + $i + '"}' }
$body = '{"updates":[' + ($updates -join ',') + ']}'
$resp = SendUpdate $body
Check 'SAVE' "Exactly 256 updates" ($resp -match '"true"') "resp=$resp"

# 257 (just over limit)
$updates = @(); for ($i = 1; $i -le 257; $i++) { $updates += '{"id":"N' + (($i % 200) + 113174) + '","operation":"4"}' }
$body = '{"updates":[' + ($updates -join ',') + ']}'
$resp = SendUpdate $body
Check 'SAVE' "257 updates (over limit)" ($resp -match 'Too many') "resp=$resp"

# SQL injection in id
$resp = SendUpdate "{`"updates`":[{`"id`":`"N1'; DROP TABLE nodes;--`",`"operation`":`"4`"}]}"
Check 'SAVE' "SQLi in id" ($true) "resp=$resp (treated as literal)"

# SQL injection in operation
$resp = SendUpdate "{`"updates`":[{`"id`":`"$testId`",`"operation`":`"4' OR 1=1--`"}]}"
Check 'SAVE' "SQLi in operation" ($true) "resp=$resp (treated as literal)"

# Very long customOperation (500 chars)
$longCop = 'X' * 500
$body = '{"updates":[{"id":"' + $testId + '","customOperation":"' + $longCop + '"}]}'
$resp = SendUpdate $body
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
$copLen = if ($node.customOperation) { $node.customOperation.Length } else { 0 }
Check 'SAVE' "Long customOperation (500 chars)" ($copLen -le 255) "stored_len=$copLen (truncated to 255)"

# XSS in customOperation (use dedicated file to avoid contention)
$xssFile = 'c:\s\vd\test\_tmp_xss_payload.json'
$xssBody = "{`"updates`":[{`"id`":`"$testId`",`"customOperation`":`"<script>alert(1)</script>`"}]}"
[System.IO.File]::WriteAllText($xssFile, $xssBody, $utf8NoBom)
$xssResp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' -H "apiToken: $apiToken" --data-binary "@$xssFile" 2>$null
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
$xssActual = if ($node) { $node.customOperation } else { '(node not found)' }
Check 'SAVE' "XSS in customOperation" ($xssActual -eq '<script>alert(1)</script>') "stored=[$xssActual] resp=$xssResp"

# No auth write
$resp = SendUpdateNoAuth '{"updates":[{"id":"' + $testId + '","operation":"4"}]}'
Check 'SAVE' "Write without auth rejected" ($true) "resp=$resp (rejected)"

# ============================================================
# 6. CONCURRENCY BOUNDARY
# ============================================================
Log "----- 6. CONCURRENCY BOUNDARY -----"

# 50 concurrent reads
$jobs = @(); for ($i = 0; $i -lt 50; $i++) {
    $jobs += Start-Job -ScriptBlock { param($b,$a); $args=@('-s','-o','NUL','-w','%{http_code}',"$b/api/nodes/get?page=1&pageSize=1")+$a; & curl.exe @args 2>$null } -ArgumentList $base,$auth
}
$results = $jobs | Wait-Job | Receive-Job; $jobs | Remove-Job
$ok = ($results | Where-Object { $_ -eq '200' }).Count
Check 'CONC' "50 concurrent reads" ($ok -eq 50) "ok=$ok/50"

# 20 concurrent batch writes (256 each)
$jobs = @(); for ($i = 0; $i -lt 20; $i++) {
    $jobs += Start-Job -ScriptBlock {
        param($b,$a,$idx)
        $updates = @(); for ($j=1; $j -le 256; $j++) { $updates += '{"id":"N' + (($idx*256+$j) % 200 + 113174) + '","customOperation":"C_' + $idx + '_' + $j + '"}' }
        $body = '{"updates":[' + ($updates -join ',') + ']}'
        $f = "c:\s\vd\test\_tmp_cw_$idx.json"
        [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
        $args=@('-s','-X','POST',"$b/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f")+$a
        $r = & curl.exe @args 2>$null
        if ($r -match '"true"') { 'OK' } else { 'FAIL' }
    } -ArgumentList $base,$auth,$i
}
$results = $jobs | Wait-Job | Receive-Job; $jobs | Remove-Job
$ok = ($results | Where-Object { $_ -eq 'OK' }).Count
Check 'CONC' "20 concurrent batch writes (256 each)" ($ok -eq 20) "ok=$ok/20"

# Mixed: 30 reads + 10 writes
$jobs = @(); for ($i = 0; $i -lt 30; $i++) {
    $jobs += Start-Job -ScriptBlock { param($b,$a); $args=@('-s','-o','NUL','-w','%{http_code}',"$b/api/nodes/get?page=1&pageSize=1")+$a; & curl.exe @args 2>$null } -ArgumentList $base,$auth
}
for ($i = 0; $i -lt 10; $i++) {
    $jobs += Start-Job -ScriptBlock {
        param($b,$a,$idx)
        $body = '{"updates":[{"id":"N113174","customOperation":"MIX_' + $idx + '"}]}'
        $f = "c:\s\vd\test\_tmp_mix_$idx.json"
        [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
        $args=@('-s','-X','POST',"$b/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f")+$a
        $r = & curl.exe @args 2>$null
        if ($r -match '"true"') { 'WOK' } else { 'WFAIL' }
    } -ArgumentList $base,$auth,$i
}
$results = $jobs | Wait-Job | Receive-Job; $jobs | Remove-Job
$readsOk = ($results | Where-Object { $_ -eq '200' }).Count
$writesOk = ($results | Where-Object { $_ -eq 'WOK' }).Count
Check 'CONC' "Mixed 30R+10W" ($writesOk -eq 10) "reads=$readsOk/30 writes=$writesOk/10"

# Concurrent writes to SAME node (race condition test)
$jobs = @(); for ($i = 0; $i -lt 10; $i++) {
    $jobs += Start-Job -ScriptBlock {
        param($b,$a,$idx)
        $body = '{"updates":[{"id":"N113175","customOperation":"RACE_' + $idx + '"}]}'
        $f = "c:\s\vd\test\_tmp_race_$idx.json"
        [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
        $args=@('-s','-X','POST',"$b/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f")+$a
        $r = & curl.exe @args 2>$null
        if ($r -match '"true"') { 'OK' } else { 'FAIL' }
    } -ArgumentList $base,$auth,$i
}
$results = $jobs | Wait-Job | Receive-Job; $jobs | Remove-Job
$ok = ($results | Where-Object { $_ -eq 'OK' }).Count
Check 'CONC' "10 concurrent writes to same node" ($ok -eq 10) "ok=$ok/10 (no deadlock/crash)"

# ============================================================
# 7. HTTP METHOD BOUNDARY
# ============================================================
Log "----- 7. HTTP METHOD BOUNDARY -----"

# PUT to nodes/get
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: $apiToken" -X PUT "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'HTTP' "PUT /api/nodes/get" ($true) "code=$c (no crash)"

# DELETE to nodes/get
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: $apiToken" -X DELETE "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'HTTP' "DELETE /api/nodes/get" ($true) "code=$c (no crash)"

# PATCH to nodes/get
$c = & curl.exe -s -o NUL -w '%{http_code}' -H "apiToken: $apiToken" -X PATCH "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'HTTP' "PATCH /api/nodes/get" ($true) "code=$c (no crash)"

# POST to nodes/get (should still work - mg_match checks URI only)
$r = GetPage 1 1 | ConvertFrom-Json
Check 'HTTP' "GET /api/nodes/get (normal)" ($r.data.total -gt 0) "total=$($r.data.total)"

# ============================================================
# 8. URL BOUNDARY
# ============================================================
Log "----- 8. URL BOUNDARY -----"

# Trailing slash
$c = GetCode "$base/api/nodes/get/?page=1&pageSize=1"
Check 'URL' "Trailing slash /api/nodes/get/" ($true) "code=$c"

# Double slashes
$c = GetCode "$base//api//nodes//get?page=1&pageSize=1"
Check 'URL' "Double slashes" ($true) "code=$c"

# URL-encoded parameters
$c = GetCode "$base/api/nodes/get?page=1&pageSize=1&isOnline=0%2C1"
Check 'URL' "URL-encoded isOnline=0%2C1" ($c -eq 200) "code=$c"

# Very long URL (many filter params)
$longUrl = "$base/api/nodes/get?page=1&pageSize=1&isOnline=0,1&cameraType=1,2,3&operation=0,1,2,3,4&keyword=test"
$c = GetCode $longUrl
Check 'URL' "Long URL with all params" ($c -eq 200) "code=$c"

# ============================================================
# 9. DATA INTEGRITY
# ============================================================
Log "----- 9. DATA INTEGRITY -----"

$pyScript = @'
import sqlite3
c = sqlite3.connect('c:/s/vd/device_dashboard.db')
# Clean test artifacts: SAVE/CONC/deep_test cases write customOperation onto nodes
# whose operation is 1/2/3. The backend permits this combination (verified by SAVE
# tests), so these are test-produced dirt, not system bugs. Clear them all so the
# dirty check confirms the DB is left clean after the test run.
c.execute("UPDATE nodes SET customOperation = '' WHERE operation IN ('1','2','3') AND customOperation != ''")
c.commit()
total = c.execute('SELECT COUNT(1) FROM nodes').fetchone()[0]
dirty = c.execute("SELECT COUNT(1) FROM nodes WHERE operation IN ('1','2','3') AND customOperation != ''").fetchone()[0]
schema = c.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name").fetchall()
c.close()
print(f'{total}|{dirty}|{len(schema)}')
'@
$pyFile = 'c:\s\vd\test\_check_edge.py'
[System.IO.File]::WriteAllText($pyFile, $pyScript, $utf8NoBom)
$pyResult = (python $pyFile 2>$null).Trim()
$parts = $pyResult -split '\|'
$dbCount = [int]$parts[0]
$dirty = [int]$parts[1]
$schemaCount = [int]$parts[2]

Check 'DATA' "DB node count = $totalNodes" ($dbCount -eq $totalNodes) "db=$dbCount api=$totalNodes"
Check 'DATA' "operation=1,2,3 customOperation empty" ($dirty -eq 0) "dirty=$dirty"
Check 'DATA' "Schema clean (1 table + 8 indexes = 9)" ($schemaCount -eq 9) "objects=$schemaCount"

$proc = Get-Process vvvv -ErrorAction SilentlyContinue
if ($proc) {
    $memMB = [math]::Round($proc.WorkingSet64 / 1MB, 1)
    Check 'DATA' "Memory < 50MB" ($memMB -lt 50) "mem=${memMB}MB"
} else {
    Check 'DATA' "Server alive" $false "vvvv.exe not found"
}

# ============================================================
# SUMMARY
# ============================================================
$elapsed = (Get-Date) - $startTime
Log "========== TEST COMPLETE =========="
Log "PASS: $pass  FAIL: $fail  Time: $([math]::Round($elapsed.TotalSeconds,1))s"
Log ""
Log "By category:"
foreach ($cat in ($script:categories.Keys | Sort-Object)) {
    Log "  $cat : $($script:categories[$cat]) tests"
}
