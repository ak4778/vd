# Logout / session invalidation verification using curl.exe
# Tests: login (Basic Auth) -> use token (ok) -> logout -> reuse token (must fail)
$ErrorActionPreference = 'Continue'
$base = 'http://127.0.0.1:8000'
$curl = 'curl.exe'

function Curl-Api([string]$url, [string]$method = 'GET', [string]$body = $null, [string[]]$headers = @()) {
  $args = @('-s', '-o', 'NUL', '-w', '%{http_code}', '-X', $method, '--max-time', '10', $url)
  # We need body too: capture to temp file
  $tmp = [IO.Path]::GetTempFileName()
  $a = @('-s', '-X', $method, '--max-time', '10', '-D', "$tmp.headers", '-o', "$tmp.body", '-w', '%{http_code}', $url)
  foreach ($h in $headers) { $a += @('-H', $h) }
  if ($body) { $a += @('--data-binary', $body) }
  $code = & $curl @a
  $respBody = ''
  if (Test-Path "$tmp.body") { $respBody = [IO.File]::ReadAllText("$tmp.body") }
  Remove-Item "$tmp.body", "$tmp.headers" -ErrorAction SilentlyContinue
  return [pscustomobject]@{ Status = $code; Body = $respBody }
}

$basic = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('admin:admin'))

Write-Host '=== Step 1: Login with HTTP Basic Auth ==='
$login = Curl-Api "$base/api/login" 'POST' $null @("Authorization: $basic")
Write-Host ("Login HTTP {0}" -f $login.Status)
$tok = $null
try { $tok = ($login.Body | ConvertFrom-Json).token } catch {}
if (-not $tok) { Write-Host "Could not parse token. Body: $($login.Body)"; exit 1 }
Write-Host ("Token (first 24): {0}..." -f $tok.Substring(0, [Math]::Min(24, $tok.Length)))

Write-Host ''
Write-Host '=== Step 2: Use token via query param BEFORE logout (expect 200) ==='
$r1 = Curl-Api "$base/api/nodes/get?page=1&pageSize=2&access_token=$tok"
Write-Host ("  HTTP {0}" -f $r1.Status)

Write-Host ''
Write-Host '=== Step 3: Use token via Cookie BEFORE logout (expect 200) ==='
$r1c = Curl-Api "$base/api/nodes/get?page=1&pageSize=2" 'GET' $null @("Cookie: access_token=$tok")
Write-Host ("  HTTP {0}" -f $r1c.Status)

Write-Host ''
Write-Host '=== Step 4: Logout (carry token so server clears it) ==='
$r2 = Curl-Api "$base/api/logout" 'POST' $null @("Cookie: access_token=$tok")
Write-Host ("  Logout HTTP {0}" -f $r2.Status)

Write-Host ''
Write-Host '=== Step 5: Reuse token via query param AFTER logout (expect 401/403) ==='
$r3 = Curl-Api "$base/api/nodes/get?page=1&pageSize=2&access_token=$tok"
Write-Host ("  HTTP {0}" -f $r3.Status)

Write-Host ''
Write-Host '=== Step 6: Reuse token via Cookie AFTER logout (expect 401/403) ==='
$r4 = Curl-Api "$base/api/nodes/get?page=1&pageSize=2" 'GET' $null @("Cookie: access_token=$tok")
Write-Host ("  HTTP {0}" -f $r4.Status)

Write-Host ''
Write-Host '=== Step 7: Wrong password login (expect 401) ==='
$bad = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('admin:wrongpass'))
$r5 = Curl-Api "$base/api/login" 'POST' $null @("Authorization: $bad")
Write-Host ("  HTTP {0}" -f $r5.Status)

Write-Host ''
Write-Host '=== Step 8: Access protected endpoint with NO auth (expect 401/403) ==='
$r6 = Curl-Api "$base/api/nodes/get?page=1&pageSize=2"
Write-Host ("  HTTP {0}" -f $r6.Status)

Write-Host ''
Write-Host '=== Step 9: apiToken header auth (expect 200) ==='
# Read apiToken from data_config.json
$cfgTok = $null
try { $cfgTok = (Get-Content 'c:\s\vd\data_config.json' -Raw | ConvertFrom-Json).apiToken } catch {}
if ($cfgTok) {
  $r7 = Curl-Api "$base/api/nodes/get?page=1&pageSize=2" 'GET' $null @("apiToken: $cfgTok")
  Write-Host ("  apiToken header auth: HTTP {0}" -f $r7.Status)
} else {
  Write-Host '  (skipped: could not read apiToken from data_config.json)'
  $r7 = [pscustomobject]@{ Status = 'skip' }
}

Write-Host ''
Write-Host '=== Summary ==='
$ok = ($r1.Status -eq '200') -and ($r1c.Status -eq '200') -and ($r3.Status -in '401','403') -and ($r4.Status -in '401','403') -and ($r5.Status -eq '401') -and ($r6.Status -in '401','403') -and ($r7.Status -eq '200' -or $r7.Status -eq 'skip')
$verdict = if ($ok) { 'PASS' } else { 'FAIL' }
Write-Host ("Session invalidation / auth test: {0}" -f $verdict)
Write-Host ("  token works before logout (query):  HTTP {0}  (expect 200)" -f $r1.Status)
Write-Host ("  token works before logout (cookie): HTTP {0}  (expect 200)" -f $r1c.Status)
Write-Host ("  token rejected after logout (query): HTTP {0}  (expect 401/403)" -f $r3.Status)
Write-Host ("  token rejected after logout (cookie):HTTP {0}  (expect 401/403)" -f $r4.Status)
Write-Host ("  wrong password rejected:             HTTP {0}  (expect 401)" -f $r5.Status)
Write-Host ("  no auth rejected:                    HTTP {0}  (expect 401/403)" -f $r6.Status)
Write-Host ("  apiToken header auth:                HTTP {0}  (expect 200)" -f $r7.Status)
