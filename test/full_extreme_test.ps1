# Full Extreme Test Suite - Front/Back/Server
# Covers: data loading, saving, filtering, search, auth, login/logout, concurrency, errors
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$base  = 'http://127.0.0.1:7777'
$sbase = 'https://127.0.0.1:7443'
$apiToken = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
$logFile = 'c:\s\vd\test\full_extreme_test.log'
if (Test-Path $logFile) { Remove-Item $logFile -Force }
$cfgPar = 'c:\s\vd\test\_par_cfg.txt'
$startTime = Get-Date

# ===== Global stats =====
$script:pass = 0; $script:fail = 0; $script:reqs = 0
$script:latencies = @()
$script:failures = @()
$script:sectionResults = [ordered]@{}

function Log($msg) {
  $ts = Get-Date -Format 'HH:mm:ss'
  $line = "[$ts] $msg"
  Write-Host $line
  Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Check($cond, $desc, $extra='') {
  $script:reqs++
  if ($cond) {
    $script:pass++
  } else {
    $script:fail++
    $script:failures += "$desc $extra"
    Log "  FAIL: $desc $extra"
  }
}

function Req($url, $headers=@(), $method='GET', $body=$null, $timeout=20) {
  $ca = @('-s','-o','NUL','-w','%{http_code}|%{time_total}','--max-time',$timeout)
  foreach ($h in $headers) { $ca += @('-H',$h) }
  if ($method -ne 'GET') { $ca += @('-X',$method) }
  if ($body) {
    $tmp = 'c:\s\vd\test\_tmp_full.json'
    [System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))
    $ca += @('--data-binary',"@$tmp")
  }
  $resp = & curl.exe @ca $url 2>$null
  if ($body) { Remove-Item 'c:\s\vd\test\_tmp_full.json' -Force -ErrorAction SilentlyContinue }
  $parts = $resp -split '\|'
  $code = if ($parts[0]) { $parts[0] } else { '000' }
  $time = if ($parts[1]) { try { [double]$parts[1] } catch { 0.0 } } else { 0.0 }
  return @{ code = $code; time = $time }
}

function ReqJson($url, $headers=@(), $method='GET', $body=$null, $timeout=20) {
  $ca = @('-s','--max-time',$timeout)
  foreach ($h in $headers) { $ca += @('-H',$h) }
  if ($method -ne 'GET') { $ca += @('-X',$method) }
  if ($body) {
    $tmp = 'c:\s\vd\test\_tmp_full.json'
    [System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))
    $ca += @('--data-binary',"@$tmp")
  }
  $resp = & curl.exe @ca $url 2>$null
  if ($body) { Remove-Item 'c:\s\vd\test\_tmp_full.json' -Force -ErrorAction SilentlyContinue }
  try { return $resp | ConvertFrom-Json } catch { return $null }
}

function Get-MemMB() {
  $conn = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
  if ($conn.Count -gt 0 -and $conn[0].OwningProcess) {
    $p = Get-Process -Id $conn[0].OwningProcess -ErrorAction SilentlyContinue
    if ($p) { return [math]::Round($p.WorkingSet64/1MB,2) }
  }
  return -1
}

# Run N parallel GET reads via curl --parallel; returns count of 200s
function ParallelReads($level, $urlTpl, $headers=@(), $timeout=30) {
  $lines = @()
  foreach ($h in $headers) { $lines += 'header = "' + $h + '"' }
  for ($i = 0; $i -lt $level; $i++) {
    $lines += 'url = "' + ($urlTpl -f $i) + '"'
    $lines += 'output = "NUL"'
  }
  [System.IO.File]::WriteAllLines($cfgPar, $lines, (New-Object System.Text.UTF8Encoding $false))
  $t0 = Get-Date
  $resp = & curl.exe --parallel --parallel-max $level -s -w "%{http_code}`n" --max-time $timeout -K $cfgPar 2>$null
  $dur = ((Get-Date) - $t0).TotalSeconds
  Remove-Item $cfgPar -Force -ErrorAction SilentlyContinue
  $codes = @($resp -split "`n" | Where-Object { $_ -ne '' -and $_ -match '^\d{3}$' })
  $ok = ($codes | Where-Object { $_ -eq '200' }).Count
  return @{ ok = $ok; total = $level; dur = $dur }
}

# Run N parallel requests mixing reads and writes
function ParallelMixed($level, $headers=@(), $timeout=30) {
  $lines = @()
  foreach ($h in $headers) { $lines += 'header = "' + $h + '"' }
  $tmpBody = 'c:\s\vd\test\_mix_body.json'
  for ($i = 0; $i -lt $level; $i++) {
    if ($i % 3 -eq 0) {
      $bid = ($i % 256) + 1
      $body = '{"updates":[{"id":"' + $bid + '","operation":"' + (($i % 4)+1) + '","customOperation":"mix' + $i + '"}]}'
      [System.IO.File]::WriteAllText($tmpBody + $i, $body, (New-Object System.Text.UTF8Encoding $false))
      $lines += 'url = "' + "$base/api/nodes/batchset" + '"'
      $lines += 'request = "POST"'
      $lines += 'data = "' + $tmpBody + $i + '"'
      $lines += 'output = "NUL"'
    } else {
      $pg = ($i % 100) + 1
      $lines += 'url = "' + "$base/api/nodes/get?page=$pg&pageSize=10" + '"'
      $lines += 'output = "NUL"'
    }
  }
  [System.IO.File]::WriteAllLines($cfgPar, $lines, (New-Object System.Text.UTF8Encoding $false))
  $t0 = Get-Date
  $resp = & curl.exe --parallel --parallel-max $level -s -w "%{http_code}`n" --max-time $timeout -K $cfgPar 2>$null
  $dur = ((Get-Date) - $t0).TotalSeconds
  Remove-Item $cfgPar -Force -ErrorAction SilentlyContinue
  for ($i = 0; $i -lt $level; $i++) { Remove-Item ($tmpBody + $i) -Force -ErrorAction SilentlyContinue }
  $codes = @($resp -split "`n" | Where-Object { $_ -ne '' -and $_ -match '^\d{3}$' })
  $ok = ($codes | Where-Object { $_ -eq '200' }).Count
  return @{ ok = $ok; total = $level; dur = $dur }
}

function Section($name, $action) {
  Log ""
  Log "========== $name =========="
  $s = Get-Date
  $beforePass = $script:pass; $beforeFail = $script:fail; $beforeReqs = $script:reqs
  & $action
  $dur = ((Get-Date) - $s).TotalSeconds
  $sp = $script:pass - $beforePass; $sf = $script:fail - $beforeFail; $sr = $script:reqs - $beforeReqs
  $script:sectionResults[$name] = @{ pass=$sp; fail=$sf; reqs=$sr; dur=[math]::Round($dur,2) }
  Log ("  -> $name done: $sp pass / $sf fail / $sr reqs in {0}s" -f [math]::Round($dur,2))
}

$basicScnqjs = 'Authorization: Basic c2NucWpzOkF0b3MuMjAyMTAy'
$basicAdmin = $basicScnqjs
$apiHdr = "apiToken: $apiToken"

Log "############################################################"
Log "# FULL EXTREME TEST - Front/Back/Server"
Log "# Server: $base (HTTP) + $sbase (HTTPS)"
Log "# Started: $(Get-Date)"
Log "# Data: 29623 nodes (SQLite), apiToken=$($apiToken.Substring(0,8))..."
Log "############################################################"

$memStart = Get-MemMB
Log "Initial server memory: $memStart MB"

# ==================================================================
# SECTION 1: DATA LOADING
# ==================================================================
Section '1. DATA LOADING' {
  foreach ($ps in @(1, 10, 50, 100, 200)) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=$ps" @($basicAdmin)
    $script:latencies += $r.time
    Check ($r.code -eq '200') "load pageSize=$ps" "code=$($r.code) t=$($r.time)s"
  }
  $r = Req "$base/api/nodes/get?page=1&pageSize=1000" @($basicAdmin)
  Check ($r.code -eq '200') "pageSize=1000 clamped" "code=$($r.code)"
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=1000" @($basicAdmin)
  Check ($j.data.nodes.Count -le 200) "pageSize clamped to 200" "got $($j.data.nodes.Count)"

  foreach ($pg in @(1, 100, 297, 593, 594, 595, 1000, 9999)) {
    $r = Req "$base/api/nodes/get?page=$pg&pageSize=50" @($basicAdmin)
    $script:latencies += $r.time
    Check ($r.code -eq '200') "page=$pg" "code=$($r.code) t=$($r.time)s"
  }
  $j = ReqJson "$base/api/nodes/get?page=593&pageSize=50" @($basicAdmin)
  Check ($j.data.nodes.Count -eq 23) "last page count=23" "got $($j.data.nodes.Count)"
  Check ($j.data.total -eq 29623) "total stays 29623" "got $($j.data.total)"

  $r = Req "$base/api/nodes/queryCategory" @($basicAdmin)
  Check ($r.code -eq '200') "queryCategory" "code=$($r.code) t=$($r.time)s"

  $j = ReqJson "$base/api/mode/get" @($basicAdmin)
  Check ($j.mode -eq 'SQLite' -and $j.available -eq 1) "mode=get SQLite" "got mode=$($j.mode)"
}

# ==================================================================
# SECTION 2: DATA SAVING
# ==================================================================
Section '2. DATA SAVING' {
  $b = '{"updates":[{"id":"1","operation":"1","customOperation":"single_test"}]}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "single update" "code=$($r.code)"

  $u = @(); for ($i=1; $i -le 10; $i++) { $u += "{`"id`":`"$i`",`"operation`":`"$($i % 4 + 1)`"}" }
  $b = '{"updates":[' + ($u -join ',') + ']}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "batch 10" "code=$($r.code)"

  $u = @(); for ($i=1; $i -le 100; $i++) { $u += "{`"id`":`"$i`",`"operation`":`"$($i % 4 + 1)`"}" }
  $b = '{"updates":[' + ($u -join ',') + ']}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "batch 100" "code=$($r.code) t=$($r.time)s"

  $u = @(); for ($i=1; $i -le 256; $i++) { $u += "{`"id`":`"$i`",`"operation`":`"$($i % 4 + 1)`",`"customOperation`":`"b256_$i`"}" }
  $b = '{"updates":[' + ($u -join ',') + ']}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "batch 256 (max)" "code=$($r.code) t=$($r.time)s"

  $u = @(); for ($i=1; $i -le 257; $i++) { $u += "{`"id`":`"$i`",`"operation`":`"1`"}" }
  $b = '{"updates":[' + ($u -join ',') + ']}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '400') "batch 257 rejected" "code=$($r.code)"

  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' '{"updates":[]}'
  Check ($r.code -eq '200') "empty updates" "code=$($r.code)"
  $j = ReqJson "$base/api/nodes/batchset" @($basicAdmin) 'POST' '{"updates":[]}'
  Check ($j.status -eq 'false' -and $j.message -eq 'No updates') "empty updates message" "got $($j.message)"

  $b = '{"updates":[{"id":"5","operation":"2"}]}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "update operation only" "code=$($r.code)"

  $b = '{"updates":[{"id":"6","customOperation":"only_custom"}]}'
  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Check ($r.code -eq '200') "update customOperation only" "code=$($r.code)"

  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'GET'
  Check ($r.code -eq '405') "GET on batchset -> 405" "code=$($r.code)"

  # Write-then-read verification: write to a known id, wait for async commit, read back
  # keyword searches id/name/channelCode/P1/P4 (NOT customOperation), so search by id
  $verifyId = 'N113174'
  $verifyVal = 'verify_tag_' + (Get-Date -Format 'HHmmss')
  $b = '{"updates":[{"id":"' + $verifyId + '","operation":"3","customOperation":"' + $verifyVal + '"}]}'
  $null = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' $b
  Start-Sleep -Milliseconds 600   # async write: thread + 20ms dispatch timer
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&keyword=$verifyId" @($basicAdmin)
  $found = $false
  if ($j -and $j.data.nodes) {
    foreach ($n in $j.data.nodes) { if ($n.id -eq $verifyId -and $n.customOperation -eq $verifyVal) { $found = $true } }
  }
  Check $found "write verified id=$verifyId" "found=$found customOp=$(if($j.data.nodes){$j.data.nodes[0].customOperation})"
}

# ==================================================================
# SECTION 3: FILTERING
# ==================================================================
Section '3. FILTERING' {
  foreach ($v in @('1','0','0,1','')) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=10&isOnline=$v" @($basicAdmin)
    Check ($r.code -eq '200') "isOnline=$v" "code=$($r.code)"
  }
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&isOnline=" @($basicAdmin)
  Check ($j.data.total -eq 0) "isOnline= empty -> 0 results" "got total=$($j.data.total)"

  foreach ($v in @('1','0','0,1')) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=10&cameraType=$v" @($basicAdmin)
    Check ($r.code -eq '200') "cameraType=$v" "code=$($r.code)"
  }

  foreach ($v in @('1','2','3','0')) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=10&operation=$v" @($basicAdmin)
    Check ($r.code -eq '200') "operation=$v" "code=$($r.code)"
  }

  $r = Req "$base/api/nodes/get?page=1&pageSize=10&isOnline=1&cameraType=1&operation=1" @($basicAdmin)
  Check ($r.code -eq '200') "combined filters" "code=$($r.code)"

  $j1 = ReqJson "$base/api/nodes/get?page=1&pageSize=1" @($basicAdmin)
  $j2 = ReqJson "$base/api/nodes/get?page=1&pageSize=1&isOnline=0,1" @($basicAdmin)
  Check ($j1.data.total -eq $j2.data.total) "absent filter = all" "t1=$($j1.data.total) t2=$($j2.data.total)"
}

# ==================================================================
# SECTION 4: SEARCH (keyword)
# ==================================================================
Section '4. SEARCH' {
  # keyword='a' matches 4189 nodes (verified in data exploration)
  $r = Req "$base/api/nodes/get?page=1&pageSize=10&keyword=a" @($basicAdmin)
  Check ($r.code -eq '200') "keyword=a" "code=$($r.code) t=$($r.time)s"
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&keyword=a" @($basicAdmin)
  Check ($j.data.total -gt 0) "keyword=a returns results" "total=$($j.data.total)"

  # CJK keyword: 通道 matches ~298 nodes
  $cjkRaw = [string][char]0x901A + [string][char]0x9053  # 通道
  $cjk = [System.Uri]::EscapeDataString($cjkRaw)
  $r = Req "$base/api/nodes/get?page=1&pageSize=10&keyword=$cjk" @($basicAdmin)
  Check ($r.code -eq '200') "keyword=CJK(通道)" "code=$($r.code)"
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&keyword=$cjk" @($basicAdmin)
  Check ($j.data.total -gt 0) "CJK keyword returns results" "total=$($j.data.total)"

  $j1 = ReqJson "$base/api/nodes/get?page=1&pageSize=1" @($basicAdmin)
  $j2 = ReqJson "$base/api/nodes/get?page=1&pageSize=1&keyword=" @($basicAdmin)
  Check ($j1.data.total -eq $j2.data.total) "empty keyword = no filter" "t1=$($j1.data.total) t2=$($j2.data.total)"

  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&keyword=ZZZZNOTEXIST99999" @($basicAdmin)
  Check ($j.data.total -eq 0) "non-existent keyword -> 0" "got total=$($j.data.total)"

  $longkw = 'a' * 200
  $r = Req "$base/api/nodes/get?page=1&pageSize=10&keyword=$longkw" @($basicAdmin)
  Check ($r.code -eq '200') "long keyword (200 chars)" "code=$($r.code)"

  $sp = [System.Uri]::EscapeDataString('test"\\')
  $r = Req "$base/api/nodes/get?page=1&pageSize=10&keyword=$sp" @($basicAdmin)
  Check ($r.code -eq '200') "keyword with special chars" "code=$($r.code)"
}

# ==================================================================
# SECTION 5: AUTHENTICATION
# ==================================================================
Section '5. AUTHENTICATION' {
  foreach ($h in @($basicScnqjs)) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=1" @($h)
    Check ($r.code -eq '200') "Basic auth user" "code=$($r.code)"
  }
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @('Authorization: Basic c2NucWpzOndyb25ncGFzcw==')
  Check ($r.code -eq '401' -or $r.code -eq '403') "wrong password rejected" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @('Authorization: Basic bm9vbmU6bm9vbmU=')
  Check ($r.code -eq '401' -or $r.code -eq '403') "non-existent user rejected" "code=$($r.code)"

  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @($apiHdr)
  Check ($r.code -eq '200') "apiToken header" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @("apiToken:   $apiToken   ")
  Check ($r.code -eq '200') "apiToken with OWS whitespace" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @('apiToken: WRONGVALUE')
  Check ($r.code -eq '403') "wrong apiToken rejected" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @('apiToken: ')
  Check ($r.code -eq '403') "empty apiToken rejected" "code=$($r.code)"

  $b = '{"updates":[{"id":"8888","operation":"1","customOperation":"apitoken_write"}]}'
  $r = Req "$base/api/nodes/batchset" @($apiHdr) 'POST' $b
  Check ($r.code -eq '200') "apiToken write" "code=$($r.code)"

  $loginResp = curl.exe -s -D - -o NUL -X POST -u 'scnqjs:Atos.202102' --max-time 10 "$base/api/login" 2>$null
  $tok = $null
  foreach ($l in $loginResp -split "`n") { if ($l -match 'access_token=([^;\r]+)') { $tok = $matches[1]; break } }
  Check ($tok -ne $null) "login -> get token" ""
  if ($tok) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=1&access_token=$tok"
    Check ($r.code -eq '200') "access_token query param" "code=$($r.code)"
    $r = Req "$base/api/nodes/get?page=1&pageSize=1" @("Cookie: access_token=$tok")
    Check ($r.code -eq '200') "access_token cookie" "code=$($r.code)"
    $r = Req "$base/api/nodes/get?page=1&pageSize=1" @("Authorization: Bearer $tok")
    Check ($r.code -eq '200') "Bearer token" "code=$($r.code)"
  }

  $r = Req "$base/api/nodes/get?page=1&pageSize=1"
  Check ($r.code -eq '403') "no auth -> 403" "code=$($r.code)"

  # /api/mode/get is intentionally PUBLIC (no auth) — the frontend calls it
  # BEFORE login to render the SQLite/CSV mode badge. It is routed before the
  # /api/# && u==NULL auth guard (net.c:1069, guard at net.c:1077).
  $r = Req "$base/api/mode/get"
  Check ($r.code -eq '200') "mode/get is public (no auth)" "code=$($r.code)"
}

# ==================================================================
# SECTION 6: LOGIN / LOGOUT
# ==================================================================
Section '6. LOGIN / LOGOUT' {
  # Single login call capturing BOTH headers (Set-Cookie) and body (JSON) —
  # a second login would regenerate the token and invalidate the first cookie.
  $hdrFile = 'c:\s\vd\test\_login_hdr.txt'
  $bodyFile = 'c:\s\vd\test\_login_body.txt'
  curl.exe -s -D $hdrFile -o $bodyFile -X POST -u 'scnqjs:Atos.202102' --max-time 10 "$base/api/login" 2>$null
  $hdrContent = Get-Content $hdrFile -Raw -ErrorAction SilentlyContinue
  $bodyContent = Get-Content $bodyFile -Raw -ErrorAction SilentlyContinue
  Remove-Item $hdrFile, $bodyFile -Force -ErrorAction SilentlyContinue

  $tok = $null
  foreach ($l in $hdrContent -split "`n") { if ($l -match 'access_token=([^;\r]+)') { $tok = $matches[1]; break } }
  Check ($tok -ne $null) "login admin (Set-Cookie)" ""

  $j = $bodyContent | ConvertFrom-Json
  Check ($j.user -eq 'scnqjs' -and $j.token.Length -eq 64) "login response body" "user=$($j.user) toklen=$($j.token.Length)"
  # The token in the JSON body must match the token in the Set-Cookie header
  Check ($j.token -eq $tok) "body token == cookie token" "match=$($j.token -eq $tok)"

  if ($tok) {
    $r = Req "$base/api/nodes/get?page=1&pageSize=1" @("Cookie: access_token=$tok")
    Check ($r.code -eq '200') "cookie works after login" "code=$($r.code)"

    $r = Req "$base/api/logout" @("Cookie: access_token=$tok") 'POST'
    Check ($r.code -eq '200') "logout" "code=$($r.code)"

    $r = Req "$base/api/nodes/get?page=1&pageSize=1" @("Cookie: access_token=$tok")
    Check ($r.code -eq '403') "cookie invalidated after logout" "code=$($r.code)"
  }

  $r = Req "$base/api/login" @('Authorization: Basic c2NucWpzOndyb25ncGFzcw==') 'POST'
  Check ($r.code -eq '401') "login wrong creds -> 401" "code=$($r.code)"

  foreach ($cred in @('scnqjs:Atos.202102')) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($cred))
    $r = Req "$base/api/login" @("Authorization: Basic $b64") 'POST'
    Check ($r.code -eq '200') "login $cred" "code=$($r.code)"
  }

  $r = Req "$base/api/logout" @($apiHdr) 'POST'
  Check ($r.code -eq '200') "logout via apiToken" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=1" @($apiHdr)
  Check ($r.code -eq '200') "apiToken still works after logout" "code=$($r.code)"

  $tokens = @{}
  foreach ($cred in @('scnqjs:Atos.202102')) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($cred))
    $body = curl.exe -s -X POST --max-time 10 -H "Authorization: Basic $b64" "$base/api/login" 2>$null
    $j = $body | ConvertFrom-Json
    $tokens[$cred] = $j.token
  }
  $distinct = ($tokens.Values | Sort-Object -Unique).Count
  Check ($distinct -eq 1) "1 user gets distinct token" "distinct=$distinct"
}

# ==================================================================
# SECTION 7: ERROR HANDLING & EDGE CASES
# ==================================================================
Section '7. ERROR HANDLING' {
  $r = Req "$base/api/nodes/get?page=0&pageSize=10" @($basicAdmin)
  Check ($r.code -eq '400') "page=0 -> 400" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=1&pageSize=0" @($basicAdmin)
  Check ($r.code -eq '400') "pageSize=0 -> 400" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=-1&pageSize=10" @($basicAdmin)
  Check ($r.code -eq '400') "page=-1 -> 400" "code=$($r.code)"
  $r = Req "$base/api/nodes/get?page=abc&pageSize=10" @($basicAdmin)
  Check ($r.code -eq '400') "page=abc -> 400" "code=$($r.code)"

  $r = Req "$base/api/nonexistent" @($basicAdmin)
  Check ($r.code -eq '404' -or $r.code -eq '403') "non-existent endpoint" "code=$($r.code)"

  $r = Req "$base/api/nodes/get" @($basicAdmin) 'POST'
  Check ($r.code -in @('404','405','403')) "POST on /api/nodes/get rejected" "code=$($r.code)"

  # Path traversal: must NOT leak the file (403 or 404 both acceptable)
  $r = Req "$base/../net.c"
  Check ($r.code -in @('403','404')) "path traversal /../net.c blocked" "code=$($r.code)"
  $r = Req "$base/api/../net.c"
  Check ($r.code -in @('403','404')) "path traversal /api/../net.c blocked" "code=$($r.code)"
  $body = curl.exe -s --max-time 5 "$base/../net.c" 2>$null
  Check ($body -notmatch 'handle_login') "path traversal no file leak" "leaked=$($body -match 'handle_login')"

  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' 'not json'
  Check ($r.code -eq '200') "malformed JSON batchset" "code=$($r.code)"
  $j = ReqJson "$base/api/nodes/batchset" @($basicAdmin) 'POST' 'not json'
  Check ($j.message -eq 'No updates') "malformed JSON -> No updates" "got $($j.message)"

  $r = Req "$base/api/nodes/batchset" @($basicAdmin) 'POST' '{"foo":"bar"}'
  Check ($r.code -eq '200') "missing updates key" "code=$($r.code)"
}

# ==================================================================
# SECTION 8: STATIC FILES / FRONTEND
# ==================================================================
Section '8. STATIC FILES / FRONTEND' {
  $r = Req "$base/" @($basicAdmin)
  Check ($r.code -eq '200') "root /" "code=$($r.code)"
  $r = Req "$base/index.html" @($basicAdmin)
  Check ($r.code -eq '200') "index.html" "code=$($r.code)"
  $r = Req "$base/main.js" @($basicAdmin)
  Check ($r.code -eq '200') "main.js" "code=$($r.code)"
  $r = Req "$base/bundle.js" @($basicAdmin)
  Check ($r.code -eq '200') "bundle.js" "code=$($r.code)"
  $r = Req "$base/tailwind.css" @($basicAdmin)
  Check ($r.code -eq '200') "tailwind.css" "code=$($r.code)"
  $r = Req "$base/nonexistent.js" @($basicAdmin)
  Check ($r.code -eq '404') "non-existent static -> 404" "code=$($r.code)"
  $r = Req "$base/../net.c"
  Check ($r.code -in @('403','404')) "static dir traversal blocked" "code=$($r.code)"
}

# ==================================================================
# SECTION 9: CONCURRENCY STRESS (reads) - Python ThreadPool
# NOTE: curl --parallel on Windows hits a ~64 connection ceiling (Schannel
# limit). Python uses raw OS sockets and reflects the SERVER's true capacity.
# ==================================================================
Section '9. CONCURRENCY (reads)' {
  $pyOut = & python "c:\s\vd\test\py_concurrency_test.py" read 50 100 200 500 2>&1
  foreach ($line in $pyOut) {
    Log "  $line"
    if ($line -match '(\d+)/(\d+) ok.*in ([\d.]+)s') {
      $ok = [int]$matches[1]; $total = [int]$matches[2]; $dur = [double]$matches[3]
      $script:reqs += $total; $script:pass += $ok; $script:fail += ($total - $ok)
      if ($ok -lt $total) { $script:failures += "concurrency reads level=$total only $ok/$total" }
    }
  }
}

# ==================================================================
# SECTION 10: CONCURRENCY STRESS (mixed reads + writes) - Python
# ==================================================================
Section '10. CONCURRENCY (mixed read+write)' {
  $pyOut = & python "c:\s\vd\test\py_concurrency_test.py" mixed 50 100 200 2>&1
  foreach ($line in $pyOut) {
    Log "  $line"
    if ($line -match '(\d+)/(\d+) ok.*in ([\d.]+)s') {
      $ok = [int]$matches[1]; $total = [int]$matches[2]; $dur = [double]$matches[3]
      $script:reqs += $total; $script:pass += $ok; $script:fail += ($total - $ok)
      if ($ok -lt $total) { $script:failures += "mixed R/W level=$total only $ok/$total" }
    }
  }
}

# ==================================================================
# SECTION 11: SUSTAINED LOAD (durability)
# ==================================================================
Section '11. SUSTAINED LOAD' {
  $target = 300
  $ok = 0; $t0 = Get-Date
  for ($i = 1; $i -le $target; $i++) {
    $pg = ($i % 500) + 1
    $r = Req "$base/api/nodes/get?page=$pg&pageSize=50" @($apiHdr) 'GET' $null 10
    if ($r.code -eq '200') { $ok++; $script:latencies += $r.time }
  }
  $dur = ((Get-Date) - $t0).TotalSeconds
  $script:reqs += $target
  $script:pass += $ok
  $script:fail += ($target - $ok)
  $rps = [math]::Round($target / $dur, 1)
  Log ("  sustained {0} reqs in {1}s = {2} r/s, {3}/{4} ok" -f $target, [math]::Round($dur,2), $rps, $ok, $target)
}

# ==================================================================
# SECTION 12: HTTPS / TLS (TLS 1.3 only per mongoose builtin)
# ==================================================================
Section '12. HTTPS / TLS' {
  # Mongoose built-in TLS only supports TLS 1.3 (X25519 + AES-GCM/ChaCha20).
  # Windows curl.exe uses schannel (TLS 1.2 default) and cannot complete the
  # handshake, so HTTPS tests via curl always return code=000. Use Python's
  # ssl module (OpenSSL) to exercise the listener with a real TLS 1.3 client.
  # See test/https_tls13_test.py for the per-check logic.
  $pyOut = & python "c:\s\vd\test\https_tls13_test.py" --token $apiToken --host 127.0.0.1 --port 7443 2>&1
  foreach ($line in $pyOut) {
    Log "  $line"
    if ($line -match 'HTTPS_RESULT\|([^|]+)\|(\d+)\|(PASS|FAIL)\|(.*)') {
      $hname = $matches[1]; $hcode = $matches[2]; $hresult = $matches[3]; $hdetail = $matches[4]
      $script:reqs++
      if ($hresult -eq 'PASS') {
        $script:pass++
      } else {
        $script:fail++
        $script:failures += "$hname $hdetail"
        Log "  FAIL: $hname $hdetail"
      }
    }
  }
}

# ==================================================================
# FINAL REPORT
# ==================================================================
$memEnd = Get-MemMB
$totalDur = ((Get-Date) - $startTime).TotalSeconds
Log ""
Log "############################################################"
Log "# FINAL REPORT"
Log "############################################################"
Log ("Total: reqs={0} pass={1} fail={2} ({3}%)  duration={4}s  mem {5}->{6}MB (delta {7}MB)" -f `
  $script:reqs, $script:pass, $script:fail, `
  $(if ($script:reqs -gt 0) { [math]::Round($script:pass*100.0/$script:reqs,2) } else { 0 }), `
  [math]::Round($totalDur,2), $memStart, $memEnd, [math]::Round($memEnd-$memStart,2))

if ($script:latencies.Count -gt 0) {
  $avg = [math]::Round(($script:latencies | Measure-Object -Average).Average, 4)
  $max = [math]::Round(($script:latencies | Measure-Object -Maximum).Maximum, 4)
  $min = [math]::Round(($script:latencies | Measure-Object -Minimum).Minimum, 4)
  $sorted = $script:latencies | Sort-Object
  $p95 = [math]::Round($sorted[[int]($sorted.Count * 0.95)], 4)
  $p99 = [math]::Round($sorted[[int]($sorted.Count * 0.99)], 4)
  Log ("Latency (s): min={0} avg={1} max={2} p95={3} p99={4} (n={5})" -f $min,$avg,$max,$p95,$p99,$script:latencies.Count)
}

Log ""
Log "Per-section breakdown:"
foreach ($k in $script:sectionResults.Keys) {
  $v = $script:sectionResults[$k]
  $pct = if ($v.reqs -gt 0) { [math]::Round($v.pass*100.0/$v.reqs,1) } else { 0 }
  Log ("  {0,-32} pass={1,-4} fail={2,-4} reqs={3,-4} ({4}%) {5}s" -f $k, $v.pass, $v.fail, $v.reqs, $pct, $v.dur)
}

if ($script:failures.Count -gt 0) {
  Log ""
  Log "FAILURES ($($script:failures.Count)):"
  foreach ($f in $script:failures) { Log "  - $f" }
} else {
  Log ""
  Log "ALL TESTS PASSED - NO FAILURES"
}
Log "############################################################"
