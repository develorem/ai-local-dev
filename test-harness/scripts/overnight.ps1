# Overnight benchmark runner.
#
# What this does:
#   1. Stops the Ollama tray app + daemon
#   2. Starts a headless `ollama serve` with OLLAMA_FLASH_ATTENTION=1 + OLLAMA_KV_CACHE_TYPE=q8_0
#      in this PowerShell session so the daemon inherits them
#   3. Runs bench.py runall with the overnight plan
#   4. Logs everything (stdout+stderr) to results\overnight_<timestamp>.log
#   5. Leaves Ollama running so you can verify in the morning
#
# Outputs you'll inspect in the morning:
#   - test-harness\results\overnight_*.log               (full stdout/stderr trace)
#   - test-harness\results\csv\runs.csv                  (throughput rows)
#   - test-harness\results\csv\quality.csv               (quality rows)
#   - test-harness\results\raw\*.json                    (per-run rich data)

$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "results"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "overnight_$Stamp.log"
$OllamaServeLog = Join-Path $LogDir "ollama_serve_$Stamp.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

Log "=== overnight start ==="
Log "repo root: $Root"
Log "log: $LogFile"

# 1. Stop any running Ollama processes (tray + daemon)
Log "Stopping existing Ollama processes..."
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "*ollama*" } |
    ForEach-Object {
        Log ("  killing " + $_.Name + " pid=" + $_.ProcessId)
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { Log "    (already gone)" }
    }
Start-Sleep -Seconds 2

# 2. Set env vars in THIS shell so the child ollama serve inherits them
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE   = "q8_0"
Log "OLLAMA_FLASH_ATTENTION = $env:OLLAMA_FLASH_ATTENTION"
Log "OLLAMA_KV_CACHE_TYPE   = $env:OLLAMA_KV_CACHE_TYPE"

# 3. Launch ollama serve headless (no tray)
Log "Launching ollama serve headless..."
$serveProc = Start-Process -FilePath "ollama" `
    -ArgumentList "serve" `
    -RedirectStandardOutput $OllamaServeLog `
    -RedirectStandardError ($OllamaServeLog + ".err") `
    -WindowStyle Hidden `
    -PassThru
Log ("ollama serve pid=" + $serveProc.Id + " log=" + $OllamaServeLog)

# Wait for API
$apiReady = $false
for ($i = 0; $i -lt 30 -and -not $apiReady; $i++) {
    try {
        $r = Invoke-WebRequest -Uri http://localhost:11434/api/tags -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $apiReady = $true }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $apiReady) {
    Log "ERROR: ollama API never came up. Aborting."
    exit 1
}
Log "Ollama API reachable."

# 4. Run the master plan, tee'd to the log file
$Plan = Join-Path $Root "benchmarks\plan_overnight.json"
Log "Plan: $Plan"
Log "=== bench.py runall begins ==="

$benchScript = Join-Path $Root "bench.py"
# Use py launcher; pipe stderr into stdout into Tee-Object so log captures both.
& py $benchScript runall --plan $Plan 2>&1 | Tee-Object -FilePath $LogFile -Append

Log "=== bench.py runall complete ==="
Log "=== overnight end ==="
Log "ollama serve is still running (pid=$($serveProc.Id)). Stop it when you're done inspecting results."
