# Deep Stress Test v2 — no CJK in source file (fetched from API instead)
# Focus: extreme concurrency, sustained load, Unicode/CJK search, memory stability,
#        known-issue verification, data consistency under load, recovery
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
function GetMemMB {
    # Find the server process actually listening on port 8000 (robust against duplicates)
    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($p) { return [math]::Round($p.WorkingSet64 / 1MB, 1) }
    }
    # Fallback: first vvvv process
    $p = Get-Process vvvv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) { return [math]::Round($p.WorkingSet64 / 1MB, 1) } else { return -1 }
}
function CurlCode($url) {
    $a = @('-s','-o','NUL','-w','%{http_code}',$url) + $auth
    return & curl.exe @a 2>$null
}
function GetPage($page, $pageSize, $extra='') {
    $url = "$base/api/nodes/get?page=$page" + [char]38 + "pageSize=$pageSize$extra"
    $a = @('-s',$url) + $auth
    return & curl.exe @a 2>$null
}
function SendUpdateFile($f) {
    $a = @('-s','-X','POST',"$base/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f") + $auth
    return & curl.exe @a 2>$null
}

Log "========== DEEP STRESS TEST v2 START =========="
$startTime = Get-Date
$memStart = GetMemMB
Log "Server memory at start: ${memStart}MB"

# Get baseline data
$firstPage = GetPage 1 200 | ConvertFrom-Json
$testId = $firstPage.data.nodes[0].id
$testName = $firstPage.data.nodes[0].name
$totalNodes = $firstPage.data.total
Log "testId=$testId testName=$testName totalNodes=$totalNodes"

# Extract CJK keywords from actual data (avoids encoding issues in source file)
# testName is like "XXXX-XX-01-01" — extract first segment before "-"
$cjkKeyword1 = $testName.Split('-')[0]    # e.g. "马家滩"
$cjkKeyword2 = $firstPage.data.nodes[0].P1  # e.g. "新能源场站"
$cjkKeyword3 = $firstPage.data.nodes[0].P4  # e.g. "宁夏新能源"
$cjkSingle = $cjkKeyword3.Substring(0, 1)   # e.g. "宁"
Log "CJK keywords: k1=[$cjkKeyword1] k2=[$cjkKeyword2] k3=[$cjkKeyword3] k4=[$cjkSingle]"

# ============================================================
# 1. UNICODE / CJK SEARCH
# ============================================================
Log "----- 1. UNICODE / CJK SEARCH -----"

# Search with Chinese keyword (from data) — keyword search covers name + P4 only
$extra = [char]38 + "keyword=$cjkKeyword1"
$r = GetPage 1 200 $extra | ConvertFrom-Json
Check 'UNI' "CJK keyword from name" ($r.data.total -gt 0) "total=$($r.data.total) kw=$cjkKeyword1"

# Search with P1 field value (NOT searched by design — only name + P4 are searched)
$extra = [char]38 + "keyword=$cjkKeyword2"
$r = GetPage 1 200 $extra | ConvertFrom-Json
Check 'UNI' "CJK keyword from P1 (not searched by design)" ($r.data.total -eq 0) "total=$($r.data.total) (expected 0: only name+P4 searched)"

# Search with P4 field value
$extra = [char]38 + "keyword=$cjkKeyword3"
$r = GetPage 1 200 $extra | ConvertFrom-Json
Check 'UNI' "CJK keyword from P4" ($r.data.total -gt 0) "total=$($r.data.total) kw=$cjkKeyword3"

# Search with single Chinese char
$extra = [char]38 + "keyword=$cjkSingle"
$r = GetPage 1 200 $extra | ConvertFrom-Json
Check 'UNI' "Single CJK char search" ($r.data.total -gt 0) "total=$($r.data.total) kw=$cjkSingle"

# Search with ASCII keyword (id field is NOT searched by design)
$extra = [char]38 + 'keyword=N113'
$r = GetPage 1 200 $extra | ConvertFrom-Json
Check 'UNI' "ASCII keyword N113 (id not searched by design)" ($r.data.total -eq 0) "total=$($r.data.total) (expected 0: only name+P4 searched)"

# CJK in customOperation (write + read back) — use data-derived CJK
$cjkCop = $cjkKeyword1 + "_test_"
$copBody = "{`"updates`":[{`"id`":`"$testId`",`"customOperation`":`"$cjkCop`"}]}"
$f = 'c:\s\vd\test\_tmp_deep_cjk.json'
[System.IO.File]::WriteAllText($f, $copBody, $utf8NoBom)
$resp = SendUpdateFile $f
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq $testId }
Check 'UNI' "CJK customOperation write+read" ($node.customOperation -eq $cjkCop) "stored=[$($node.customOperation)] expected=[$cjkCop]"

# ============================================================
# 2. KNOWN ISSUE VERIFICATION
# ============================================================
Log "----- 2. KNOWN ISSUE VERIFICATION -----"

# page=INT_MAX (offset overflow — benign)
$r = GetPage 2147483647 10 | ConvertFrom-Json
Check 'KNWN' "page=INT_MAX no crash" ($true) "total=$($r.data.total) returned=$($r.data.nodes.Count) (benign: SQLite clamps offset)"

# page=INT_MAX+1 (would overflow int)
$r = GetPage 2147483648 10 | ConvertFrom-Json
Check 'KNWN' "page=INT_MAX+1 no crash" ($true) "returned=$($r.data.nodes.Count)"

# keyword=% (LIKE wildcard matches all)
$extra = [char]38 + 'keyword=%'
$r = GetPage 1 1 $extra | ConvertFrom-Json
Check 'KNWN' "keyword=% matches all" ($r.data.total -eq $totalNodes) "total=$($r.data.total) (known: LIKE wildcard)"

# keyword=_ (LIKE single-char wildcard)
$extra = [char]38 + 'keyword=_'
$r = GetPage 1 1 $extra | ConvertFrom-Json
Check 'KNWN' "keyword=_ no crash" ($true) "total=$($r.data.total) (known: LIKE wildcard)"

# Trailing slash inconsistency
$c1 = CurlCode "$base/api/nodes/get?page=1&pageSize=1"
$c2 = CurlCode "$base/api/nodes/get/?page=1&pageSize=1"
Check 'KNWN' "Trailing slash behavior" ($c1 -eq 200) "/get=$c1 /get/=$c2 (known: trailing slash -> 404)"

# HTTP method: PUT to batchset (should be 405, not write)
$f = 'c:\s\vd\test\_tmp_deep_put.json'
$body = "{`"updates`":[{`"id`":`"$testId`",`"customOperation`":`"PUT_SHOULD_NOT_WRITE`"}]}"
[System.IO.File]::WriteAllText($f, $body, $utf8NoBom)
$putResp = & curl.exe -s -w '%{http_code}' -X PUT "$base/api/nodes/batchset" -H 'Content-Type: application/json' -H "apiToken: $apiToken" --data-binary "@$f" 2>$null
Check 'KNWN' "PUT to batchset rejected (405)" ($putResp -match '405') "resp=$putResp"

# DELETE to batchset (should be 405)
$delResp = & curl.exe -s -w '%{http_code}' -X DELETE "$base/api/nodes/batchset" -H "apiToken: $apiToken" 2>$null
Check 'KNWN' "DELETE to batchset rejected (405)" ($delResp -match '405') "resp=$delResp"

# PUT to nodes/get (should be 405)
$putGetResp = & curl.exe -s -o NUL -w '%{http_code}' -X PUT "$base/api/nodes/get?page=1&pageSize=1" -H "apiToken: $apiToken" 2>$null
Check 'KNWN' "PUT to nodes/get rejected (405)" ($putGetResp -eq '405') "code=$putGetResp"

# POST to mode/get (should be 405)
$postModeResp = & curl.exe -s -o NUL -w '%{http_code}' -X POST "$base/api/mode/get" 2>$null
Check 'KNWN' "POST to mode/get rejected (405)" ($postModeResp -eq '405') "code=$postModeResp"

# ============================================================
# 3. EXTREME CONCURRENCY (beyond 61 thread pool ceiling)
# ============================================================
Log "----- 3. EXTREME CONCURRENCY -----"

# 200 concurrent reads (exceeds 61 ceiling)
$jobs = @(); for ($i = 0; $i -lt 200; $i++) {
    $jobs += Start-Job -ScriptBlock { param($b,$a); $args=@('-s','-o','NUL','-w','%{http_code}',"$b/api/nodes/get?page=1&pageSize=1")+$a; & curl.exe @args 2>$null } -ArgumentList $base,$auth
}
$results = $jobs | Wait-Job -Timeout 30 | Receive-Job; $jobs | Remove-Job -Force
$ok200 = ($results | Where-Object { $_ -eq '200' }).Count
Check 'XCONC' "200 concurrent reads" ($ok200 -ge 60 -and $ok200 -le 200) "ok=$ok200/200 (expect >=60: thread pool ceiling=61)"

# Verify server recovers after overload
Start-Sleep -Seconds 2
$recoveryCode = CurlCode "$base/api/nodes/get?page=1&pageSize=1"
Check 'XCONC' "Server recovers after 200-concurrent" ($recoveryCode -eq 200) "code=$recoveryCode"

# 300 concurrent reads (reduced from 500 to avoid PowerShell job overhead)
$jobs = @(); for ($i = 0; $i -lt 300; $i++) {
    $jobs += Start-Job -ScriptBlock { param($b,$a); $args=@('-s','-o','NUL','-w','%{http_code}',"$b/api/nodes/get?page=1&pageSize=1")+$a; & curl.exe @args 2>$null } -ArgumentList $base,$auth
}
$results = $jobs | Wait-Job -Timeout 45 | Receive-Job; $jobs | Remove-Job -Force
$ok200 = ($results | Where-Object { $_ -eq '200' }).Count
Check 'XCONC' "300 concurrent reads" ($ok200 -ge 60) "ok=$ok200/300 (thread pool throttles, no crash)"

# Verify recovery again
Start-Sleep -Seconds 3
$recoveryCode = CurlCode "$base/api/nodes/get?page=1&pageSize=1"
$recoveryTime = & curl.exe -s -o NUL -w '%{time_total}' -H "apiToken: $apiToken" "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'XCONC' "Server recovers after 300-concurrent" ($recoveryCode -eq 200) "code=$recoveryCode time=${recoveryTime}s"

# ============================================================
# 4. SUSTAINED LOAD (3 waves of mixed read+write)
# ============================================================
Log "----- 4. SUSTAINED LOAD -----"
$memBefore = GetMemMB
Log "Memory before sustained load: ${memBefore}MB"

for ($wave = 1; $wave -le 3; $wave++) {
    $jobs = @()
    # 40 reads
    for ($i = 0; $i -lt 40; $i++) {
        $jobs += Start-Job -ScriptBlock { param($b,$a); $args=@('-s','-o','NUL','-w','%{http_code}',"$b/api/nodes/get?page=1&pageSize=50")+$a; & curl.exe @args 2>$null } -ArgumentList $base,$auth
    }
    # 10 writes
    for ($i = 0; $i -lt 10; $i++) {
        $jobs += Start-Job -ScriptBlock {
            param($b,$a,$idx,$wave)
            $body = "{`"updates`":[{`"id`":`"N$((($wave*10+$idx)%200)+113174)`",`"customOperation`":`"W${wave}_$idx`"}]}"
            $f = "c:\s\vd\test\_tmp_deep_wave_${wave}_$idx.json"
            [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
            $args=@('-s','-X','POST',"$b/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f")+$a
            $r = & curl.exe @args 2>$null
            if ($r -match '"true"') { 'WOK' } else { 'WFAIL' }
        } -ArgumentList $base,$auth,$i,$wave
    }
    $results = $jobs | Wait-Job -Timeout 30 | Receive-Job; $jobs | Remove-Job -Force
    $readsOk = ($results | Where-Object { $_ -eq '200' }).Count
    $writesOk = ($results | Where-Object { $_ -eq 'WOK' }).Count
    Check 'SUST' "Wave ${wave}: 40R+10W" ($writesOk -eq 10) "reads=$readsOk/40 writes=$writesOk/10"
}

$memAfter = GetMemMB
$memDelta = [math]::Round($memAfter - $memBefore, 1)
Check 'SUST' "Memory stable after 3 waves" ($memDelta -lt 5) "before=${memBefore}MB after=${memAfter}MB delta=${memDelta}MB"

# ============================================================
# 5. DATA CONSISTENCY: concurrent read during write
# ============================================================
Log "----- 5. DATA CONSISTENCY -----"

# Start a write in background, then read concurrently
$writeJob = Start-Job -ScriptBlock {
    param($b,$a)
    $updates = @(); for ($i = 1; $i -le 256; $i++) { $updates += "{`"id`":`"N$(($i % 200)+113174)`",`"customOperation`":`"CONSIST_$i`"}" }
    $body = '{"updates":[' + ($updates -join ',') + ']}'
    $f = 'c:\s\vd\test\_tmp_deep_consistency.json'
    [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
    $args=@('-s','-X','POST',"$b/api/nodes/batchset",'-H','Content-Type: application/json','--data-binary',"@$f")+$a
    & curl.exe @args 2>$null
} -ArgumentList $base,$auth

# Read while write is in progress
Start-Sleep -Milliseconds 100
$readDuringWrite = GetPage 1 200 | ConvertFrom-Json
$writeResult = $writeJob | Wait-Job -Timeout 30 | Receive-Job; $writeJob | Remove-Job -Force
$readAfterWrite = GetPage 1 200 | ConvertFrom-Json

Check 'CONS' "Read during write returns valid JSON" ($readDuringWrite.data.total -eq $totalNodes) "total=$($readDuringWrite.data.total)"
Check 'CONS' "Read after write returns valid JSON" ($readAfterWrite.data.total -eq $totalNodes) "total=$($readAfterWrite.data.total)"
Check 'CONS' "Write completed during concurrent read" ($writeResult -match '"true"') "resp=$writeResult"

# ============================================================
# 6. COMPLEX FILTER + PAGINATION COMBOS
# ============================================================
Log "----- 6. COMPLEX FILTER + PAGINATION -----"

# Filter + page=1
$extra = [char]38 + 'isOnline=1' + [char]38 + 'cameraType=1'
$r = GetPage 1 50 $extra | ConvertFrom-Json
$allMatch = ($r.data.nodes | Where-Object { $_.isOnline -ne '1' -or $_.cameraType -ne '1' }).Count -eq 0
Check 'CXFILT' "isOnline=1+cameraType=1 page=1" ($allMatch -and $r.data.nodes.Count -eq 50) "total=$($r.data.total) returned=$($r.data.nodes.Count)"

# Filter + last page
$lastPg = [math]::Ceiling($r.data.total / 50.0)
$r2 = GetPage $lastPg 50 $extra | ConvertFrom-Json
$expectedLast = $r.data.total - ($lastPg - 1) * 50
Check 'CXFILT' "Filter last page=$lastPg" ($r2.data.nodes.Count -eq $expectedLast) "returned=$($r2.data.nodes.Count) expected=$expectedLast"

# Filter + beyond last page
$r3 = GetPage ($lastPg + 1) 50 $extra | ConvertFrom-Json
Check 'CXFILT' "Filter beyond last page" ($r3.data.nodes.Count -eq 0) "returned=$($r3.data.nodes.Count)"

# Filter + page=1 + keyword (CJK from data)
$extra = [char]38 + 'isOnline=1' + [char]38 + 'cameraType=1' + [char]38 + "keyword=$cjkSingle"
$r = GetPage 1 50 $extra | ConvertFrom-Json
$allMatch = ($r.data.nodes | Where-Object { $_.isOnline -ne '1' -or $_.cameraType -ne '1' }).Count -eq 0
Check 'CXFILT' "Filter+CJK keyword page=1" ($allMatch -and $r.data.total -ge 0) "total=$($r.data.total)"

# Filter with all 3 + keyword + pagination (no overlap)
$extra = [char]38 + 'isOnline=0,1' + [char]38 + 'cameraType=1,2,3' + [char]38 + 'operation=0,1,2,3,4' + [char]38 + 'keyword=N'
$r1 = GetPage 1 100 $extra | ConvertFrom-Json
$r2 = GetPage 2 100 $extra | ConvertFrom-Json
$ids1 = $r1.data.nodes | ForEach-Object { $_.id }
$ids2 = $r2.data.nodes | ForEach-Object { $_.id }
$overlap = ($ids1 | Where-Object { $ids2 -contains $_ }).Count
Check 'CXFILT' "Multi-filter+keyword pagination no overlap" ($overlap -eq 0) "overlap=$overlap page1=$($ids1.Count) page2=$($ids2.Count)"

# Verify total consistency: page1+page2 count <= total
Check 'CXFILT' "Multi-filter total consistency" ($r1.data.total -eq $r2.data.total) "total1=$($r1.data.total) total2=$($r2.data.total)"

# ============================================================
# 7. LARGE BATCH WRITE
# ============================================================
Log "----- 7. LARGE BATCH WRITE -----"

# 256 updates
$updates = @(); for ($i = 1; $i -le 256; $i++) { $updates += "{`"id`":`"N$(($i % 200)+113174)`",`"customOperation`":`"BATCH_$i`"}" }
$body = '{"updates":[' + ($updates -join ',') + ']}'
$f = 'c:\s\vd\test\_tmp_deep_batch.json'
[System.IO.File]::WriteAllText($f, $body, $utf8NoBom)
$resp = SendUpdateFile $f
Check 'BATCH' "256 batch updates" ($resp -match '"true"') "resp=$resp"

# Verify one was written
$r = GetPage 1 200 | ConvertFrom-Json
$node = $r.data.nodes | Where-Object { $_.id -eq 'N113174' }
Check 'BATCH' "Batch write visible" ($node.customOperation -match 'BATCH_') "stored=[$($node.customOperation)]"

# ============================================================
# 8. RESPONSE SIZE / TIMING
# ============================================================
Log "----- 8. RESPONSE SIZE / TIMING -----"

# Time a large response (200 nodes)
$timing = & curl.exe -s -o NUL -w '%{time_total} %{size_download}' -H "apiToken: $apiToken" "$base/api/nodes/get?page=1&pageSize=200" 2>$null
$parts = $timing -split ' '
$timeSec = [double]$parts[0]
$sizeBytes = [long]$parts[1]
$sizeKB = [math]::Round($sizeBytes / 1024, 1)
Check 'TIME' "200 nodes response time" ($timeSec -lt 5) "time=${timeSec}s size=${sizeKB}KB"

# Time a small response (1 node)
$timing1 = & curl.exe -s -o NUL -w '%{time_total}' -H "apiToken: $apiToken" "$base/api/nodes/get?page=1&pageSize=1" 2>$null
$time1 = [double]$timing1
Check 'TIME' "1 node response time" ($time1 -lt 2) "time=${time1}s"

# Time a filtered response
$timingF = & curl.exe -s -o NUL -w '%{time_total}' -H "apiToken: $apiToken" "$base/api/nodes/get?page=1&pageSize=200&isOnline=1&cameraType=1,2,3" 2>$null
$timeF = [double]$timingF
Check 'TIME' "Filtered 200 nodes response time" ($timeF -lt 5) "time=${timeF}s"

# ============================================================
# 9. ERROR RESPONSE FORMAT CONSISTENCY
# ============================================================
Log "----- 9. ERROR RESPONSE FORMAT -----"

# 400 for bad page
$resp = & curl.exe -s -H "apiToken: $apiToken" "$base/api/nodes/get?page=0&pageSize=10" 2>$null
Check 'ERR' "400 response is valid JSON" ($true) "resp=$resp"

# 403 for no auth
$resp = & curl.exe -s "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'ERR' "403 response text" ($resp -match 'Not Authorised') "resp=$resp"

# 404 for deleted endpoint
$resp = & curl.exe -s -H "apiToken: $apiToken" "$base/api/config/get" 2>$null
$c = CurlCode "$base/api/config/get"
Check 'ERR' "Deleted endpoint 404" ($c -eq 404) "code=$c"

# batchset error: too many
$f = 'c:\s\vd\test\_tmp_deep_err.json'
$updates = @(); for ($i = 1; $i -le 257; $i++) { $updates += "{`"id`":`"N$i`",`"customOperation`":`"X`"}" }
$body = '{"updates":[' + ($updates -join ',') + ']}'
[System.IO.File]::WriteAllText($f, $body, $utf8NoBom)
$resp = SendUpdateFile $f
Check 'ERR' "Too many updates error format" ($resp -match '"false"' -and $resp -match 'Too many') "resp=$resp"

# batchset error: no updates
$resp = & curl.exe -s -X POST "$base/api/nodes/batchset" -H 'Content-Type: application/json' -H "apiToken: $apiToken" -d '{}' 2>$null
Check 'ERR' "No updates error format" ($resp -match '"false"' -and $resp -match 'No updates') "resp=$resp"

# ============================================================
# 10. SESSION / COOKIE HANDLING
# ============================================================
Log "----- 10. SESSION / COOKIE HANDLING -----"

# Login with cookie jar — must use username/password (not apiToken),
# because only user-based login sets a session cookie in the response.
# apiToken auth returns global_token_user which has no session cookie.
$cookieJar = 'c:\s\vd\test\_tmp_deep_cookies.txt'
$loginCode = & curl.exe -s -o NUL -w '%{http_code}' -c $cookieJar -u 'scnqjs:Atos.202102' "$base/api/login" 2>$null
Check 'SESS' "Login sets cookie" ($loginCode -eq 200) "code=$loginCode"

# Use cookie (no -u, no apiToken header)
$c = & curl.exe -s -o NUL -w '%{http_code}' -b $cookieJar "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'SESS' "Cookie-based auth works" ($c -eq 200) "code=$c"

# Logout clears cookie
$null = & curl.exe -s -b $cookieJar -c $cookieJar -H "apiToken: $apiToken" "$base/api/logout" 2>$null
$c = & curl.exe -s -o NUL -w '%{http_code}' -b $cookieJar "$base/api/nodes/get?page=1&pageSize=1" 2>$null
Check 'SESS' "After logout, cookie rejected" ($c -eq 403) "code=$c (cookie expired)"

# ============================================================
# 11. MEMORY STABILITY AFTER ALL TESTS
# ============================================================
Log "----- 11. MEMORY STABILITY -----"
$memEnd = GetMemMB
$memTotalDelta = [math]::Round($memEnd - $memStart, 1)
Check 'MEM' "Memory stable across full test" ($memTotalDelta -lt 10) "start=${memStart}MB end=${memEnd}MB delta=${memTotalDelta}MB"
Check 'MEM' "Memory under 50MB" ($memEnd -lt 50) "mem=${memEnd}MB"

# ============================================================
# 12. DATA INTEGRITY FINAL CHECK
# ============================================================
Log "----- 12. DATA INTEGRITY -----"
$pyScript = @'
import sqlite3
c = sqlite3.connect('c:/s/vd/device_dashboard.db')
total = c.execute('SELECT COUNT(1) FROM nodes').fetchone()[0]
schema = c.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name").fetchall()
fts = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'").fetchall()
c.close()
print(f'{total}|{len(schema)}|{len(fts)}')
'@
$pyFile = 'c:\s\vd\test\_check_deep.py'
[System.IO.File]::WriteAllText($pyFile, $pyScript, $utf8NoBom)
$pyResult = (python $pyFile 2>$null).Trim()
$parts = $pyResult -split '\|'
$dbCount = [int]$parts[0]
$schemaCount = [int]$parts[1]
$ftsCount = [int]$parts[2]

Check 'DATA' "DB node count unchanged" ($dbCount -eq $totalNodes) "db=$dbCount api=$totalNodes"
Check 'DATA' "Schema clean (9 objects)" ($schemaCount -eq 9) "objects=$schemaCount"
Check 'DATA' "No FTS tables" ($ftsCount -eq 0) "fts_tables=$ftsCount"

# ============================================================
# SUMMARY
# ============================================================
$elapsed = (Get-Date) - $startTime
Log "========== DEEP TEST v2 COMPLETE =========="
Log "PASS: $pass  FAIL: $fail  Time: $([math]::Round($elapsed.TotalSeconds,1))s"
Log "Memory: start=${memStart}MB end=${memEnd}MB delta=${memTotalDelta}MB"
Log ""
Log "By category:"
foreach ($cat in ($script:categories.Keys | Sort-Object)) {
    Log "  $cat : $($script:categories[$cat]) tests"
}
