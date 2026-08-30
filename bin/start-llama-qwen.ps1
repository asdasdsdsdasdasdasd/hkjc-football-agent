# Native Windows launcher for Qwen llama-server (same flags as start-llama-qwen.sh).
# Models stay on D:\ai\models. The Linux llama-server ELF will NOT run here —
# you need a Windows CUDA build of llama.cpp (llama-server.exe).
$ErrorActionPreference = "Stop"
$Ai = if ($env:OPENCLAW_AI_ROOT) { $env:OPENCLAW_AI_ROOT } else { "D:\ai" }
$env:GGML_CUDA_DISABLE_GRAPHS = "1"

$candidates = @(
  (Join-Path $Ai "bin\llama-server.exe"),
  (Join-Path $Ai "llama.cpp\build\bin\llama-server.exe"),
  (Join-Path $Ai "llama.cpp\build\bin\Release\llama-server.exe")
)
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) {
  Write-Error @"
No llama-server.exe found under $Ai.

This PC's current llama.cpp build is Linux-only (.so). On Windows you either:
  1) Stay on WSL:  wsl -d Ubuntu -- bash /mnt/d/openclaw/hkjc-football-agent/bin/start-desk.sh
  2) Build llama.cpp with CUDA in Visual Studio, then put llama-server.exe in D:\ai\bin\
"@
}

$model = Join-Path $Ai "models\Qwen3.8-27B-GGUF\Qwen3.8-27B-Q4_K_M.gguf"
$mmproj = Join-Path $Ai "models\Qwen3.8-27B-GGUF\mmproj-Qwen3.8-27B-f16.gguf"
$jinja = Join-Path $Ai "llama.cpp\templates\qwen-fixed-chat.jinja"

& $exe `
  --model $model `
  --mmproj $mmproj `
  --no-mmproj-offload `
  --host 127.0.0.1 --port 8080 `
  --ctx-size 200000 `
  --parallel 1 `
  --n-gpu-layers 999 `
  --tensor-split 1,1 `
  --split-mode layer `
  --flash-attn on `
  --cache-type-k q8_0 `
  --cache-type-v q8_0 `
  --jinja `
  --chat-template-file $jinja `
  --chat-template-kwargs '{"reasoning_effort":"low","enable_thinking":true}' `
  --reasoning auto `
  --reasoning-effort low `
  --reasoning-format deepseek `
  --reasoning-budget 2048 `
  --reasoning-budget-message 'Enough thinking. Call a tool or answer now.' `
  --spec-type draft-mtp `
  --spec-draft-n-max 3 `
  --alias Qwen3.8-27B-Q4_K_M `
  @args
