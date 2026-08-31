# =============================================================================
#  win_test.ps1  -  Windows all-in-one test runner (parallel to ./linux_test)
#  USAGE (pick one):
#    powershell -ExecutionPolicy Bypass -File .\win_test.ps1
#    powershell  (then)  cd c:\s\vd ; .\win_test.ps1
#
#  Stages:
#   1) Env check (mingw32-make / gcc / python / curl / python modules)
#   2) Rebuild  (honours current Makefile FD_SETSIZE macro, default 4096)
#   3) Restart server + smoke tests (HTTP/HTTPS)
#   4) FD ladder test  (test\test_fd_setsize.py, target 1224 conns cumulative)
#   5) Full extreme stress (test\_extreme_stress_detailed.py, 24 levels)
#   6) verify_password unit tests (test\test_verify_password.c, 49 cases)
#   7) Post-stress health probes + report
# =============================================================================
[CmdletBinding()]
param(
    [string]$Token = '',    # optional, else read apiToken from data_config.json
    [switch]$SkipBuild      # skip phase2 recompile; reuse already-running server
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
# 强制 python 子进程用 UTF-8 输出: 否则在 GBK 代码页的控制台里,
# 测试脚本打印 emoji (🟢/🔴) 时会抛 UnicodeEncodeError -> 误报 FAIL
$env:PYTHONIOENCODING = 'utf-8'

# ---------- CONFIG ----------
$ROOT     = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT
$PROG     = "$ROOT\vvvv.exe"
$DATA_CFG = "$ROOT\data_config.json"
$TEST_DIR = "$ROOT\test"
$SRV_LOG  = "$TEST_DIR\server_stderr.log"
$PID_F    = "$ROOT\.win_test.pid"
$SRV_PID  = "$ROOT\.win_server.pid"
$TS       = Get-Date -Format 'yyyyMMdd_HHmmss'
$REPORT   = "$TEST_DIR\win_test_report_$TS.log"
New-Item -ItemType Directory -Force -Path $TEST_DIR | Out-Null
'' | Set-Content -Path $REPORT -Encoding utf8

# ---------- JSON config read (no jq needed) ----------
function Read-Cfg {
    if (-not (Test-Path $DATA_CFG)) { return $null }
    try {
        return [System.IO.File]::ReadAllText($DATA_CFG,[System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch { Write-Warning "parse data_config.json failed: $_"; return $null }
}
$cfg = Read-Cfg
if ([string]::IsNullOrEmpty($Token) -and $cfg -and $cfg.apiToken) { $Token = $cfg.apiToken }
$HTTP_PORT  = if ($cfg -and $cfg.httpPort)  { $cfg.httpPort  } else { 7777 }
$HTTPS_PORT = if ($cfg -and $cfg.httpsPort) { $cfg.httpsPort } else { 7443 }

# ---------- HELPERS ----------
$script:PASS = 0
$script:FAIL = 0
$script:STEP = 0
$T0 = Get-Date

function lg($m) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $m"
    Write-Host $line
    Add-Content -Path $REPORT -Value $line -Encoding UTF8
}
function ok($m)  { $script:PASS++; lg "PASS  $m" }
function bad($m) { $script:FAIL++; lg "FAIL  $m" }
# step(): scriptblock 的最后一个表达式输出作为结果（$true/$false 或 0/1）。
# 注意: $LASTEXITCODE 只由原生 exe 设置且会残留上一次的值, 不可作为纯
# PowerShell scriptblock 的成败依据 (曾因此把 curl 的 rc=35 残留给
# 后续 Invoke-WebRequest 步骤, 造成误报 FAIL)。
function step($desc, [scriptblock]$code) {
    $script:STEP++
    lg "--- [$STEP] $desc ---"
    $out = $null
    try {
        $out = & $code
    } catch {
        bad "$desc (exception: $_)"
        return
    }
    # 归一化判定: $true/0/'0'/'OK' 视为成功; 其余视为失败
    # ($null 输出 = scriptblock 无返回值 = 无人失败, 视为 PASS)
    $pass = $false
    if ($out -is [bool])      { $pass = $out }
    elseif ($null -eq $out)   { $pass = $true }
    elseif ($out -is [int])   { $pass = ($out -eq 0) }
    else                      { $pass = ("$out" -in @('0','OK','True','ok','pass')) }
    if ($pass) { ok "$desc" } else { bad "$desc (result=$out)" }
}

# ---------- SINGLE-INSTANCE LOCK ----------
# 锁文件内容 = "PID|进程启动时间"。仅当 PID 存在且启动时间一致才视为活锁，
# 防止把无关进程（用户自己的 PowerShell 窗口）误判为 win_test 实例。
$lockValue = "$PID|$((Get-Process -Id $PID).StartTime.Ticks)"
if (Test-Path $PID_F) {
    try {
        $raw = (Get-Content $PID_F -Raw -ErrorAction SilentlyContinue) -split '\|'
        if ($raw.Count -eq 2) {
            $oldPid = [int]$raw[0]
            $oldStart = [long]$raw[1]
            $p = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($p -and $p.StartTime.Ticks -eq $oldStart) {
                Write-Error "[LOCK] win_test already running (PID=$oldPid, started $($p.StartTime)). Kill it first."
                exit 3
            }
            # 陈旧锁: PID 不存在 / 或属于别的进程（时间戳不匹配）-> 自动清理
            lg "stale lock removed (pid=$oldPid absent=$( -not $p ) or identity mismatch)"
        }
    } catch {}
}
$lockValue | Set-Content -Path $PID_F -Encoding ascii
$null = Register-EngineEvent PowerShell.Exiting -Action {
    Remove-Item $PID_F -Force -ErrorAction SilentlyContinue
}

# ================================================================
lg "===== win_test START  $(Get-Date)  ====="
lg "ROOT=$ROOT  PROG=$PROG"
lg "HTTP_PORT=$HTTP_PORT  HTTPS_PORT=$HTTPS_PORT  TOKEN_LEN=$($Token.Length)"

# ================================================================
# PHASE 1 - ENV CHECK
# ================================================================
lg "--- [phase1] env check ---"
function ck($name, $cmd) {
    $g = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($g) { ok "dep: $name => $($g.Source)"; return $true } else { bad "dep: $name MISSING ($cmd)"; return $false }
}
$HAS_MAKE = ck 'mingw32-make' 'mingw32-make.exe'
$HAS_GCC  = ck 'gcc'          'gcc.exe'
$HAS_PY   = ck 'python'       'python.exe'
$HAS_CURL = ck 'curl'         'curl.exe'

# python modules: auto install
$PYOK = $false
if ($HAS_PY) {
    $modOk = $false
    try {
        & python.exe -c "import requests,aiohttp" 2>$null
        if ($LASTEXITCODE -eq 0) { $modOk = $true }
    } catch {}
    if ($modOk) { ok "dep: python requests+aiohttp" }
    else {
        lg "WARN: python modules missing, trying pip install (requests aiohttp multidict yarl)"
        $installCmds = @(
            @('-m','pip','install','--upgrade','pip','setuptools','wheel'),
            @('-m','pip','install','--upgrade','requests','aiohttp','multidict','yarl'),
            @('-m','pip','install','--user','--upgrade','requests','aiohttp','multidict','yarl')
        )
        foreach ($a in $installCmds) {
            lg "  pip: python $($a -join ' ')"
            $out = & python.exe @a 2>&1 | Out-String
            Add-Content -Path $REPORT -Value $out -Encoding UTF8
            & python.exe -c "import requests,aiohttp" 2>$null
            if ($LASTEXITCODE -eq 0) { $modOk = $true; break }
        }
        if ($modOk) { ok "dep: python requests+aiohttp (auto-install ok)" }
        else         { bad "dep: python modules install failed. Run manually: pip install requests aiohttp multidict yarl" }
    }
    $PYOK = $modOk
}

# report Makefile FD_SETSIZE setting
try {
    $mkText = Get-Content "$ROOT\Makefile" -Raw
    $m = [regex]::Match($mkText, 'DFD_SETSIZE=(\d+)')
    if ($m.Success) { lg "Makefile DFD_SETSIZE = $($m.Groups[1].Value)" }
} catch {}

# ================================================================
# PHASE 2 - REBUILD (skippable via -SkipBuild)
# ================================================================
$BUILD_LOG = "$TEST_DIR\win_build.log"
'' | Set-Content $BUILD_LOG -Encoding utf8

if ($SkipBuild) {
    lg "--- [phase2] rebuild SKIPPED (-SkipBuild) ---"
    if (-not (Test-Path $PROG)) {
        bad "binary missing: $PROG and -SkipBuild given. Abort."
        exit 10
    }
} else {
    lg "--- [phase2] rebuild ---"
    # Windows: 运行中的 exe 无法被链接器覆盖, 编译前必须停服务器
    Get-Process vvvv -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600

    step "mingw32-make clean" {
        Push-Location $ROOT
        & mingw32-make.exe clean 2>&1 | Tee-Object -FilePath $BUILD_LOG -Append | Out-String | Write-Host
        Pop-Location
    }

    step "mingw32-make vvvv.exe" {
        Push-Location $ROOT
        & mingw32-make.exe vvvv.exe 2>&1 | Tee-Object -FilePath $BUILD_LOG -Append | Out-String | Write-Host
        Pop-Location
    }

    if (-not (Test-Path $PROG)) {
        bad "binary not produced: $PROG. Abort. See $BUILD_LOG"
        exit 10
    }
}
# report gcc flags in final link line
Get-Content $BUILD_LOG -Tail 20 | Select-String 'gcc.*-o.*vvvv\.exe' | Select-Object -First 1 | ForEach-Object {
    lg "  final gcc line: $($_.Line)"
    if ($_.Line -match 'DFD_SETSIZE=(\d+)') { lg "  built with DFD_SETSIZE=$($Matches[1])" }
    if ($_.Line -match 'MG_ENABLE_EPOLL=0')   { lg "  built with MG_ENABLE_EPOLL=0 (select path)" }
    if ($_.Line -match 'MG_ENABLE_POLL=0')    { lg "  built with MG_ENABLE_POLL=0  (WSAPoll disabled)" }
}

# ================================================================
# PHASE 3 - RESTART SERVER + SMOKE
# ================================================================
lg "--- [phase3] server setup + smoke ---"
# 服务器启动策略:
#   1) 若已有 vvvv 在监听且 /api/mode/get 200 -> 直接复用
#   2) 否则 Stop 旧进程 + Start-Process 启动
# 说明: 在受限宿主(如 IDE 代理终端)下, 脚本内启动的子进程可能被宿主
#   job 管理机制拖累/终止(实测: 阶梯 89 断点/压测假死/秒死), 而 cmd /c、
#   WMI、schtasks 均被拦截。此时请先在外部长驻终端启动服务器再运行本
#   脚本, 本阶段会自动复用:   cd C:\s\vd ; .\vvvv.exe
$reuse = $false
try {
    $pre = Invoke-WebRequest -Uri "http://127.0.0.1:$HTTP_PORT/api/mode/get" `
        -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    if ($pre.StatusCode -eq 200 -and $pre.Content -match 'SQLite') { $reuse = $true }
} catch {}

if ($reuse) {
    $proc = Get-Process vvvv -ErrorAction SilentlyContinue | Select-Object -First 1
    $SRV_PID_VAL = if ($proc) { [int]$proc.Id } else { -1 }
    lg "reusing existing server pid=$SRV_PID_VAL"
} else {
    Get-Process vvvv -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $SRV_PID -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    '' | Set-Content -Path $SRV_LOG -Encoding utf8

    $proc = Start-Process -FilePath $PROG `
        -WorkingDirectory $ROOT `
        -RedirectStandardError $SRV_LOG `
        -WindowStyle Hidden `
        -PassThru
    $SRV_PID_VAL = [int]$proc.Id
    $SRV_PID_VAL | Set-Content -Path $SRV_PID -Encoding ascii
    lg "server started pid=$SRV_PID_VAL"
}
Start-Sleep -Seconds 2

function smoke-http {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$HTTP_PORT/api/mode/get" `
            -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        return ($r.StatusCode -eq 200 -and $r.Content -match 'SQLite')
    } catch { return $false }
}
# curl.exe(Windows) 使用 schannel, 无法与 mongoose 内置 TLS(TLS1.3-only +
# X25519) 握手 (rc=35), 属已知客户端限制; Python(OpenSSL) 客户端可正常验证
# (压测中 HTTPS 各级别 100% 通过即由 Python ssl 完成)。
function smoke-https {
    $py = @"
import ssl, sys, urllib.request
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    r = urllib.request.urlopen('https://127.0.0.1:$HTTPS_PORT/api/mode/get', context=ctx, timeout=10)
    sys.exit(0 if b'SQLite' in r.read() else 1)
except Exception:
    sys.exit(1)
"@
    $py | python.exe - 2>$null
    return ($LASTEXITCODE -eq 0)
}
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $SRV_PID_VAL -ErrorAction SilentlyContinue)) { bad "server died before smoke"; break }
    if (smoke-http) { break }
    Start-Sleep -Milliseconds 800
}
step "smoke: HTTP /api/mode/get"                    { smoke-http }
step "smoke: HTTPS /api/mode/get (TLS1.3 via python)" { smoke-https }
step "smoke: HTTP GET / root page len>500" {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$HTTP_PORT/" -UseBasicParsing -TimeoutSec 10
        ($r.StatusCode -eq 200 -and $r.Content.Length -gt 500)
    } catch { $false }
}

# ================================================================
# PHASE 4 - FD LADDER TEST
# ================================================================
lg "--- [phase4] FD ladder test ---"
$LADDER_LOG = "$TEST_DIR\fd_ladder_win.log"
if ($PYOK) {
    step "FD ladder test (target 1224 cumulative conns)" {
        Push-Location $ROOT
        # Out-Host: 防止 Tee 的管道输出混入 scriptblock 返回值
        # (step() 会把整段 stdout 当结果 => 全部级别 OK 却被误判 FAIL)
        & python.exe test\test_fd_setsize.py --host 127.0.0.1 --port $HTTP_PORT --token $Token --hold 10 `
            2>&1 | Tee-Object -FilePath $LADDER_LOG | Out-Host
        $rc = $LASTEXITCODE
        Pop-Location
        if (Test-Path $LADDER_LOG) {
            Get-Content $LADDER_LOG | Select-String -Pattern 'OK  \]|FAIL|全级别|崩溃|🟢|🔴' | ForEach-Object { lg "  ladder: $($_.Line)" }
        }
        $rc
    }
} else { bad "SKIP: FD ladder test (python modules not ready)" }
Start-Sleep -Seconds 6

# ================================================================
# PHASE 5 - EXTREME STRESS (24 levels)
# ================================================================
lg "--- [phase5] extreme stress ---"
$STRESS_LOG = "$TEST_DIR\detailed_stress_win.log"
if ($PYOK) {
    step "extreme stress 24 levels (load/save/query/filter/search/page/frontend/https)" {
        Push-Location $ROOT
        # Out-Host: 同 ladder, 防止 stdout 混入 step() 返回值
        & python.exe test\_extreme_stress_detailed.py --token $Token `
            2>&1 | Tee-Object -FilePath $STRESS_LOG | Out-Host
        $rc = $LASTEXITCODE
        Pop-Location
        $labels = @('QUERY no-result 500x','FILTER 5-dim combo 2000x','SEARCH CJK long 500x','MIXED filter+search+page 2000x','FRONTEND 2000x','SUSTAINED 60s load','HTTPS login concurrent 100x')
        lg "Key-level pass-rate summary:"
        foreach ($L in $labels) {
            $line = Get-Content $STRESS_LOG -ErrorAction SilentlyContinue |
                Select-String -Pattern "\[$([regex]::Escape($L))\]" |
                Select-Object -Last 1
            if ($line -and $line.Line -match '\((\d+)%\)') {
                $p = [int]$Matches[1]
                lg "  $L => ${p}%  |  $($line.Line)"
                if ($p -ge 95) { ok "rate ok >=95%: $L ($p%)" } else { bad "rate below 95%: $L ($p%)" }
            } else { lg "  $L => (not found in stress log, script may have exited early)" }
        }
        $rc
    }
} else { bad "SKIP: extreme stress (python modules not ready)" }
Start-Sleep -Seconds 6

# ================================================================
# PHASE 6 - verify_password UNIT TESTS
# ================================================================
lg "--- [phase6] verify_password unit tests ---"
$UNIT_BIN = "$TEST_DIR\test_verify_password.exe"
$UNIT_BUILD_LOG = "$TEST_DIR\unit_build.log"
Remove-Item $UNIT_BIN -Force -ErrorAction SilentlyContinue
$unitOk = $false
try {
    Push-Location $ROOT
    # 链接 mongoose.c: test_verify_password.c 经 password_hash.h 引用
    # mg_sha256_* / mg_snprintf, 这些符号在 mongoose.c 中实现
    & gcc.exe -o $UNIT_BIN test\test_verify_password.c mongoose.c -I. -Wall -Wextra -Wno-unused-parameter -lws2_32 2>$UNIT_BUILD_LOG
    if ($LASTEXITCODE -eq 0 -and (Test-Path $UNIT_BIN)) { $unitOk = $true }
    Pop-Location
} catch {}
if ($unitOk) {
    $UNIT_LOG = "$TEST_DIR\unit_verify_password.log"
    step "test_verify_password (expect 49 cases / 0 failed)" {
        & $UNIT_BIN 2>&1 | Tee-Object -FilePath $UNIT_LOG | Out-String | Write-Host
        $rc = $LASTEXITCODE
        $tail = Get-Content $UNIT_LOG -Tail 3
        # 匹配 'ALL PASSED' 或旧格式 '0 failed' (脚本尾 3 行为 ==== 包裹的结果行)
        if (($tail | Select-String 'ALL PASSED|0 failed') -and $rc -eq 0) { 0 } else { 1 }
    }
    Get-Content $UNIT_LOG -Tail 3 | ForEach-Object { lg "  unit: $_" }
} else { bad "unit test build failed. See $UNIT_BUILD_LOG" }

# ================================================================
# PHASE 7 - POST-STRESS HEALTH + FINAL SUMMARY
# ================================================================
lg "--- [phase7] health + summary ---"
step "health: HTTP smoke post-stress"  { smoke-http }
step "health: HTTPS smoke post-stress" { smoke-https }
if (Get-Process -Id $SRV_PID_VAL -ErrorAction SilentlyContinue) { ok "server alive pid=$SRV_PID_VAL" } else { bad "server DIED pid=$SRV_PID_VAL" }

if (Test-Path $SRV_LOG) {
    lg "--- server stderr tail ---"
    Get-Content $SRV_LOG -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { lg "  srv: $_" }
}

$ELAPSED = [int]((Get-Date) - $T0).TotalSeconds
lg ""
lg "=========================================================="
lg " FINAL SUMMARY   elapsed=${ELAPSED}s   PASS=$PASS  FAIL=$FAIL"
lg "=========================================================="
lg "report     = $REPORT"
lg "build log  = $BUILD_LOG"
lg "ladder log = $LADDER_LOG"
lg "stress log = $STRESS_LOG"
lg "unit log   = $TEST_DIR\unit_verify_password.log"
lg "srv stderr = $SRV_LOG"
if ($FAIL -eq 0) {
    lg "ALL PASS"
    schtasks.exe /Delete /TN vvvv_test_srv /F 2>$null | Out-Null
    exit 0
} else {
    lg "HAS FAILURES ($FAIL) -> review FAIL lines above"
    schtasks.exe /Delete /TN vvvv_test_srv /F 2>$null | Out-Null
    exit 1
}