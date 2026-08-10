# Functional Test Suite (non-stress)
# Covers: data loading, saving, filtering, search, auth, login/logout, errors, frontend
# No concurrency / stress — verifies correct behavior under normal single-request use.
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$base     = 'http://127.0.0.1:8000'
$apiToken = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
$apiHdr   = "apiToken: $apiToken"
$logFile  = 'c:\s\vd\test\functional_test.log'
if (Test-Path $logFile) { Remove-Item $logFile -Force }
$startTime = Get-Date

$script:pass = 0; $script:fail = 0; $script:failures = @()

function Log($msg) {
  $ts = Get-Date -Format 'HH:mm:ss'
  $line = "[$ts] $msg"
  Write-Host $line
  Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Check($cond, $desc, $extra='') {
  if ($cond) {
    $script:pass++
    Log "  PASS: $desc $extra"
  } else {
    $script:fail++
    $script:failures += "$desc $extra"
    Log "  FAIL: $desc $extra"
  }
}

# HTTP status code only
function Req($url, $headers=@(), $method='GET', $body=$null) {
  $ca = @('-s','-o','NUL','-w','%{http_code}','--max-time','15')
  foreach ($h in $headers) { $ca += @('-H',$h) }
  if ($method -ne 'GET') { $ca += @('-X',$method) }
  if ($body) {
    $tmp = 'c:\s\vd\test\_tmp_func.json'
    [System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))
    $ca += @('--data-binary',"@$tmp")
  }
  $code = & curl.exe @ca $url 2>$null
  if ($body) { Remove-Item 'c:\s\vd\test\_tmp_func.json' -Force -ErrorAction SilentlyContinue }
  if (-not $code) { $code = '000' }
  return $code
}

# Parsed JSON response
function ReqJson($url, $headers=@(), $method='GET', $body=$null) {
  $ca = @('-s','--max-time','15')
  foreach ($h in $headers) { $ca += @('-H',$h) }
  if ($method -ne 'GET') { $ca += @('-X',$method) }
  if ($body) {
    $tmp = 'c:\s\vd\test\_tmp_func.json'
    [System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))
    $ca += @('--data-binary',"@$tmp")
  }
  $resp = & curl.exe @ca $url 2>$null
  if ($body) { Remove-Item 'c:\s\vd\test\_tmp_func.json' -Force -ErrorAction SilentlyContinue }
  try { return $resp | ConvertFrom-Json } catch { return $null }
}

function Section($name, $sb) {
  Log ""
  Log "========== $name =========="
  $t0 = Get-Date
  & $sb
  $dur = [math]::Round(((Get-Date) - $t0).TotalSeconds, 2)
  Log "  -> $name done in ${dur}s"
}

# ==================================================================
Log "############################################################"
Log "# FUNCTIONAL TEST (non-stress) - Front/Back/Server"
Log "# Server: $base"
Log "# Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Log "############################################################"

# ==================================================================
# 1. DATA LOADING
# Response shape: {"config":{...},"data":{"total":N,"nodes":[...]}}
# ==================================================================
Section '1. DATA LOADING' {
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=50" @($apiHdr)
  $script:totalNodes = $j.data.total
  Check ($j -and $script:totalNodes -gt 0) "total = $script:totalNodes" "total=$($j.data.total)"
  Check ($j -and $j.data.nodes.Count -eq 50) "page 1 returns 50 rows" "count=$($j.data.nodes.Count)"

  # last page: dynamically calculate based on actual total
  $lastPage = [math]::Ceiling($script:totalNodes / 50.0)
  $expected = $script:totalNodes - ($lastPage - 1) * 50
  $j = ReqJson "$base/api/nodes/get?page=$lastPage&pageSize=50" @($apiHdr)
  Check ($j -and $j.data.nodes.Count -eq $expected) "last page ($lastPage) returns $expected rows" "count=$($j.data.nodes.Count)"

  # mode/get returns DB mode (NOT config); config is embedded in nodes/get
  $j = ReqJson "$base/api/mode/get" @($apiHdr)
  Check ($j -and $j.mode -eq 'SQLite') "mode/get returns mode=SQLite" ""
  Check ($j -and $j.available -eq 1) "mode/get available=1" ""

  # config is embedded in nodes/get, not mode/get
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=1" @($apiHdr)
  Check ($j -and $j.config.defaultPageSize -eq 50) "config.defaultPageSize=50" ""
  Check ($j -and $j.config.fields.Count -gt 0) "config.fields present" ""
  # apiToken must NOT be leaked in any response
  $raw = & curl.exe -s --max-time 15 -H $apiHdr "$base/api/mode/get" 2>$null
  Check ($raw -notmatch 'm4h38NPR') "apiToken value NOT leaked" ""
}

# ==================================================================
# 2. DATA SAVING (single + verify persistence)
# ==================================================================
Section '2. DATA SAVING' {
  $verifyId = 'N113174'
  $val = 'func_test_' + (Get-Date -Format 'HHmmss')
  $b = '{"updates":[{"id":"' + $verifyId + '","operation":"2","customOperation":"' + $val + '"}]}'
  $c = Req "$base/api/nodes/batchset" @($apiHdr) 'POST' $b
  Check ($c -eq '200') "batchset single node" "code=$c"

  # async write: wait for commit
  Start-Sleep -Milliseconds 600
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=10&keyword=$verifyId" @($apiHdr)
  $matched = $false
  if ($j -and $j.data.nodes) {
    foreach ($n in $j.data.nodes) { if ($n.customOperation -eq $val) { $matched = $true; break } }
  }
  Check ($matched) "saved value persisted ($val)" ""

  # batch of 5
  $updates = @()
  for ($i = 0; $i -lt 5; $i++) {
    $updates += '{"id":"N11317' + $i + '","operation":"1"}'
  }
  $b = '{"updates":[' + ($updates -join ',') + ']}'
  $c = Req "$base/api/nodes/batchset" @($apiHdr) 'POST' $b
  Check ($c -eq '200') "batchset 5 nodes" "code=$c"

  # restore the verify node
  $b = '{"updates":[{"id":"' + $verifyId + '","operation":"1","customOperation":""}]}'
  $null = Req "$base/api/nodes/batchset" @($apiHdr) 'POST' $b
}

# ==================================================================
# 3. FILTERING
# ==================================================================
Section '3. FILTERING' {
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&isOnline=1" @($apiHdr)
  Check ($j -and $j.data.total -gt 0) "isOnline=1 filter returns data" "total=$($j.data.total)"

  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&cameraType=1" @($apiHdr)
  Check ($j -and $j.data.total -gt 0) "cameraType=1 filter returns data" "total=$($j.data.total)"

  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&operation=0" @($apiHdr)
  Check ($j -and $j.data.total -gt 0) "operation=0 (unmarked) filter" "total=$($j.data.total)"

  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&isOnline=0,1" @($apiHdr)
  Check ($j -and $j.data.total -eq $script:totalNodes) "isOnline=0,1 returns all" "total=$($j.data.total)"

  # empty value -> 1=0 condition -> 0 results
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&isOnline=" @($apiHdr)
  Check ($j -and $j.data.total -eq 0) "isOnline= (empty) returns 0" "total=$($j.data.total)"
}

# ==================================================================
# 4. SEARCH (keyword, incl. CJK)
# Build CJK keyword from char codes to avoid console encoding issues.
# ==================================================================
Section '4. SEARCH' {
  # ASCII keyword
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&keyword=N113174" @($apiHdr)
  Check ($j -and $j.data.total -gt 0) "keyword by id (N113174)" "total=$($j.data.total)"

  # CJK keyword: 马家滩 = U+9A6C U+5BB6 U+6EE9 (144 nodes)
  $cjkRaw = [string][char]0x9A6C + [string][char]0x5BB6 + [string][char]0x6EE9
  $cjk = [System.Uri]::EscapeDataString($cjkRaw)
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&keyword=$cjk" @($apiHdr)
  Check ($j -and $j.data.total -eq 144) "CJK keyword (马家滩) returns 144" "total=$($j.data.total)"

  # empty keyword = no filter (same total as no keyword)
  $j1 = ReqJson "$base/api/nodes/get?page=1&pageSize=1" @($apiHdr)
  $j2 = ReqJson "$base/api/nodes/get?page=1&pageSize=1&keyword=" @($apiHdr)
  Check ($j1.data.total -eq $j2.data.total) "empty keyword = no filter" "t1=$($j1.data.total) t2=$($j2.data.total)"

  # non-existent keyword returns 0
  $j = ReqJson "$base/api/nodes/get?page=1&pageSize=5&keyword=ZZZZNOTEXIST99999" @($apiHdr)
  Check ($j -and $j.data.total -eq 0) "non-existent keyword returns 0" "total=$($j.data.total)"
}

# ==================================================================
# 5. AUTHENTICATION
# NOTE: /api/mode/get requires auth (caught by /api/# && u==NULL guard
# in net.c). Only /api/login is public.
# ==================================================================
Section '5. AUTHENTICATION' {
  $c = Req "$base/api/nodes/get?page=1&pageSize=1" @($apiHdr)
  Check ($c -eq '200') "apiToken correct" "code=$c"

  $c = Req "$base/api/nodes/get?page=1&pageSize=1" @("apiToken: wrong_token")
  Check ($c -eq '403') "apiToken wrong rejected" "code=$c"

  # apiToken with OWS (whitespace) - server strips it
  $c = Req "$base/api/nodes/get?page=1&pageSize=1" @("apiToken:   $apiToken   ")
  Check ($c -eq '200') "apiToken with OWS whitespace" "code=$c"

  $c = Req "$base/api/nodes/get?page=1&pageSize=1"
  Check ($c -eq '403') "no auth rejected" "code=$c"

  # /api/login is the only public endpoint
  $c = Req "$base/api/login" @("Authorization: Basic wrong") 'POST'
  Check ($c -eq '401') "login wrong password -> 401" "code=$c"
}

# ==================================================================
# 6. LOGIN / LOGOUT (session lifecycle)
# ==================================================================
Section '6. LOGIN / LOGOUT' {
  $resp = & curl.exe -s -D - -o NUL --max-time 15 -X POST -u admin:admin "$base/api/login" 2>$null
  $tok = $null
  foreach ($l in $resp -split "`n") {
    if ($l -match 'access_token=([^;\r]+)') { $tok = $matches[1]; break }
  }
  Check ($tok -ne $null) "login Basic Auth returns token" ""

  if ($tok) {
    $c = Req "$base/api/nodes/get?page=1&pageSize=1&access_token=$tok"
    Check ($c -eq '200') "token via query param" "code=$c"

    $c = Req "$base/api/nodes/get?page=1&pageSize=1" @("Cookie: access_token=$tok")
    Check ($c -eq '200') "token via cookie" "code=$c"

    $c = Req "$base/api/logout" @("Cookie: access_token=$tok") 'POST'
    Check ($c -eq '200') "logout" "code=$c"

    $c = Req "$base/api/nodes/get?page=1&pageSize=1&access_token=$tok"
    Check (($c -eq '403' -or $c -eq '401')) "token rejected after logout (query)" "code=$c"

    $c = Req "$base/api/nodes/get?page=1&pageSize=1" @("Cookie: access_token=$tok")
    Check (($c -eq '403' -or $c -eq '401')) "token rejected after logout (cookie)" "code=$c"
  }
}

# ==================================================================
# 7. ERROR HANDLING
# ==================================================================
Section '7. ERROR HANDLING' {
  $c = Req "$base/api/nonexistent" @($apiHdr)
  Check ($c -eq '404') "unknown endpoint 404" "code=$c"

  # bad batchset format (wrong key) -> handled gracefully
  $c = Req "$base/api/nodes/batchset" @($apiHdr) 'POST' '{"nodes":[]}'
  Check ($c -match '200|400') "bad batchset format handled" "code=$c"

  # invalid page param
  $c = Req "$base/api/nodes/get?page=abc&pageSize=10" @($apiHdr)
  Check ($c -match '200|400') "invalid page param handled" "code=$c"

  # missing static file
  $c = Req "$base/nonexistent.js"
  Check ($c -eq '404') "missing static file 404" "code=$c"
}

# ==================================================================
# 8. STATIC FILES / FRONTEND
# ==================================================================
Section '8. STATIC FILES / FRONTEND' {
  $c = Req "$base/"
  Check ($c -eq '200') "root index.html" "code=$c"

  $c = Req "$base/index.html"
  Check ($c -eq '200') "index.html" "code=$c"

  $c = Req "$base/bundle.js"
  Check ($c -eq '200') "bundle.js" "code=$c"

  $c = Req "$base/main.js"
  Check ($c -eq '200') "main.js" "code=$c"

  $c = Req "$base/tailwind.css"
  Check ($c -eq '200') "tailwind.css" "code=$c"
}

# ==================================================================
# FINAL REPORT
# ==================================================================
$totalDur = [math]::Round(((Get-Date) - $startTime).TotalSeconds, 2)
Log ""
Log "############################################################"
Log "# FINAL REPORT"
Log "############################################################"
Log ("Total: pass={0} fail={1}  duration={2}s" -f $script:pass, $script:fail, $totalDur)
if ($script:failures.Count -gt 0) {
  Log ""
  Log "FAILURES ($($script:failures.Count)):"
  foreach ($f in $script:failures) { Log "  - $f" }
} else {
  Log ""
  Log "ALL FUNCTIONAL TESTS PASSED - NO FAILURES"
}
