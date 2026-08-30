# HKJC football desk on Windows: Qwen + monitor + dashboard.
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File D:\openclaw\hkjc-football-agent\bin\start-desk.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Ai = if ($env:OPENCLAW_AI_ROOT) { $env:OPENCLAW_AI_ROOT } else { "D:\ai" }
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir, (Join-Path $Root "odds-history") | Out-Null

function Find-Python {
  $candidates = @(
    @{ Exe = "py"; Extra = @("-3") },
    @{ Exe = "python"; Extra = @() },
    @{ Exe = "python3"; Extra = @() }
  )
  foreach ($c in $candidates) {
    try {
      $args = $c.Extra + @("-c", "import sys; print(sys.executable)")
      $out = & $c.Exe @args 2>$null
      if ($LASTEXITCODE -eq 0 -and $out) { return $c }
    } catch { }
  }
  throw "Python 3 not found. Install from python.org and tick 'Add python.exe to PATH'."
}

function Test-Llama {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2 -UseBasicParsing
    return $r.StatusCode -eq 200
  } catch { return $false }
}

$Py = Find-Python
$env:PYTHONPATH = $Root
Set-Location $Root

if (-not (Test-Llama)) {
  $llama = Join-Path $PSScriptRoot "start-llama-qwen.ps1"
  if (-not (Test-Path $llama)) { $llama = Join-Path $Ai "bin\start-llama-qwen.ps1" }
  if (Test-Path $llama) {
    Write-Host "[desk] starting llama-server"
    Start-Process powershell -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $llama) -WindowStyle Minimized
    for ($i = 0; $i -lt 90; $i++) {
      Start-Sleep -Seconds 2
      if (Test-Llama) { Write-Host "[desk] llama ready"; break }
    }
  } else {
    Write-Host "[desk] llama not up and $llama missing — dashboard will run without Qwen verdicts"
  }
} else {
  Write-Host "[desk] llama already up"
}

$pidFile = Join-Path $Root "odds-history\monitor.pid"
$monRunning = $false
if (Test-Path $pidFile) {
  $old = [int](Get-Content $pidFile | Select-Object -First 1)
  try { Get-Process -Id $old -ErrorAction Stop | Out-Null; $monRunning = $true } catch { }
}
if (-not $monRunning) {
  Write-Host "[desk] starting monitor -> $LogDir\monitor.log"
  $pyArgs = @($Py.Extra) + @("-m", "pipeline.monitor")
  Start-Process $Py.Exe -ArgumentList $pyArgs -WorkingDirectory $Root -RedirectStandardOutput (Join-Path $LogDir "monitor.log") -RedirectStandardError (Join-Path $LogDir "monitor.err.log") -WindowStyle Hidden
  Start-Sleep -Seconds 1
} else {
  Write-Host "[desk] monitor already running"
}

Write-Host "[desk] window http://127.0.0.1:8765"
$uiArgs = @($Py.Extra) + @("-m", "pipeline.desk_ui", "--host", "127.0.0.1", "--port", "8765", "--open")
& $Py.Exe @uiArgs
