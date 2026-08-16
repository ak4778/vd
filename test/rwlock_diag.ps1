# Diagnostic: rwlock read-path under concurrency, all in one process scope.
$ErrorActionPreference = 'Continue'
$token = "m4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x"
$errLog = "c:\s\vd\test\srv_err.log"
$outLog = "c:\s\vd\test\srv_out.log"

Get-Process -Name vvvv -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Remove-Item $errLog,$outLog -ErrorAction SilentlyContinue

$svc = Start-Process -FilePath c:\s\vd\vvvv.exe -WorkingDirectory c:\s\vd `
  -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
  -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2

function SvcAlive { param($pid_) $p = Get-Process -Id $pid_ -ErrorAction SilentlyContinue; [bool]$p }
function CountMarkers { (Get-Content $errLog -ErrorAction SilentlyContinue | Select-String -Pattern "GN:post-lock").Count }

if (-not (SvcAlive $svc.Id)) { "FATAL: server died at startup"; exit 1 }
"Server up: PID $($svc.Id)"

# Phase 1: 5 sequential authenticated requests
"--- Phase 1: 5 sequential authenticated /api/nodes/get ---"
$seqCodes = @()
for ($i=1; $i -le 5; $i++) {
  $c = curl.exe -s -o NUL -w "%{http_code}" -H "apiToken: $token" "http://127.0.0.1:8000/api/nodes/get?page=1&pageSize=10" 2>$null
  $seqCodes += $c
}
"codes: $($seqCodes -join ' ')"
"alive after sequential: $(SvcAlive $svc.Id)"

# Phase 2: 30 concurrent authenticated requests
"--- Phase 2: 30 concurrent authenticated /api/nodes/get ---"
$jobs = 1..30 | ForEach-Object {
  Start-Job -ScriptBlock { param($t) curl.exe -s -o NUL -w "%{http_code}" -H "apiToken: $t" "http://127.0.0.1:8000/api/nodes/get?page=1&pageSize=10" 2>$null } -ArgumentList $token
}
$jobs | Wait-Job -Timeout 20 | Out-Null
$codes = $jobs | Receive-Job
$jobs | Remove-Job -Force
$ok = ($codes | Where-Object { $_ -eq '200' }).Count
"concurrent codes: $($codes -join ' ')  -> 200 count: $ok / 30"
"alive after concurrent: $(SvcAlive $svc.Id)"

# Phase 3: 100 concurrent
"--- Phase 3: 100 concurrent ---"
$jobs = 1..100 | ForEach-Object {
  Start-Job -ScriptBlock { param($t) curl.exe -s -o NUL -w "%{http_code}" -H "apiToken: $t" "http://127.0.0.1:8000/api/nodes/get?page=1&pageSize=10" 2>$null } -ArgumentList $token
}
$jobs | Wait-Job -Timeout 30 | Out-Null
$codes = $jobs | Receive-Job
$jobs | Remove-Job -Force
$ok = ($codes | Where-Object { $_ -eq '200' }).Count
"100-concurrent 200 count: $ok / 100"
"alive after 100-concurrent: $(SvcAlive $svc.Id)"

"`n--- GN:post-lock marker count (= get_nodes read-lock acquisitions): $(CountMarkers) ---"
"--- last 6 stderr lines ---"
Get-Content $errLog -Tail 6 -ErrorAction SilentlyContinue

# leave server running for further tests
"`nFinal: PID $($svc.Id) alive=$(SvcAlive $svc.Id)"
