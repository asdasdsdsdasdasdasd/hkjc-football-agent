# Deploy on Windows

This repo is the HKJC football desk: Dixon-Coles model, live odds poll, FotMob lineups/injuries, Google News, local Qwen verdicts, and a dashboard at `http://127.0.0.1:8765`.

There are **two** Windows setups. Use A unless you have already built `llama-server.exe` with CUDA.

---

## A. Recommended — WSL2 (same D: drive, existing Linux GPU build)

The Qwen binary under `D:\ai\llama.cpp\build\bin\llama-server` is a **Linux** ELF. It will not run in PowerShell. WSL2 can run it and still share `D:\`.

### 1. Install WSL (once)

In **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu
```

Reboot if Windows asks. Open Ubuntu and finish the first-run username.

### 2. Python + Playwright inside WSL

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
cd /mnt/d/openclaw/hkjc-football-agent   # or wherever you cloned this repo
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 -m playwright install-deps chromium
```

If the clone is not on `D:\openclaw`, `cd` to that folder instead (`/mnt/c/...` or `/mnt/d/...`).

### 3. Confirm Qwen

```bash
curl -sf http://127.0.0.1:8080/health || bash /mnt/d/ai/bin/start-llama-qwen.sh
```

Model files stay at `/mnt/d/ai/models/Qwen3.8-27B-GGUF/`. If your AI root is not `D:\ai`, edit `start-llama-qwen.sh` or set paths there.

### 4. Start the desk

From Ubuntu/WSL:

```bash
cd /mnt/d/openclaw/hkjc-football-agent
bash bin/start-desk.sh
```

On **Windows** Edge/Chrome open: [http://127.0.0.1:8765](http://127.0.0.1:8765)

WSL2 forwards localhost, so the browser on Windows talks to the Linux process.

One-liner from Windows PowerShell:

```powershell
wsl -d Ubuntu -- bash /mnt/d/openclaw/hkjc-football-agent/bin/start-desk.sh
```

---

## B. Native Windows (no WSL)

Use this only if you want PowerShell/Python on Windows and a **Windows** CUDA `llama-server.exe`.

### 1. Clone

```powershell
git clone https://github.com/asdasdsdsdasdasdasd/hkjc-football-agent.git D:\openclaw\hkjc-football-agent
cd D:\openclaw\hkjc-football-agent
```

### 2. Python 3.11+

Install from [python.org](https://www.python.org/downloads/windows/) and tick **Add python.exe to PATH**.

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m playwright install chromium
```

### 3. Windows CUDA llama-server (required for Qwen verdicts)

The Linux `llama-server` in `D:\ai\llama.cpp\build\bin` **cannot** be used here.

1. Install Visual Studio (C++ workload) and a matching [CUDA Toolkit](https://developer.nvidia.com/cuda-downloads).
2. Build [llama.cpp](https://github.com/ggml-org/llama.cpp) with CUDA (`GGML_CUDA=ON`).
3. Copy `llama-server.exe` to `D:\ai\bin\llama-server.exe` (or `D:\ai\llama.cpp\build\bin\Release\llama-server.exe`).
4. Keep the GGUF files at:

   - `D:\ai\models\Qwen3.8-27B-GGUF\Qwen3.8-27B-Q4_K_M.gguf`
   - `D:\ai\models\Qwen3.8-27B-GGUF\mmproj-Qwen3.8-27B-f16.gguf`

5. Chat template (if you use the same Qwen jinja as Linux):

   - `D:\ai\llama.cpp\templates\qwen-fixed-chat.jinja`

If your AI folder is not `D:\ai`:

```powershell
$env:OPENCLAW_AI_ROOT = "E:\ai"
```

`bin\start-llama-qwen.ps1` looks for `llama-server.exe` under `%OPENCLAW_AI_ROOT%`. Dual-GPU `tensor-split 1,1` matches the Linux script; change it if you have one GPU.

Without `llama-server.exe`, the dashboard still opens; Qwen verdicts stay empty until the server is up on `127.0.0.1:8080`.

### 4. Start

```powershell
cd D:\openclaw\hkjc-football-agent
powershell -ExecutionPolicy Bypass -File .\bin\start-desk.ps1
```

This starts Qwen (if the `.exe` exists), the odds/intel monitor, and the dashboard, then opens the browser.

Dashboard: [http://127.0.0.1:8765](http://127.0.0.1:8765)

---

## What should be running

| Process | Port | Role |
|---------|------|------|
| `llama-server` | 8080 | Local Qwen 3.8 27B |
| `python -m pipeline.monitor` | — | Odds every 2 min, news/lineups every 30 min, LLM card every 30 min |
| `python -m pipeline.desk_ui` | 8765 | Live card + steam alerts |

A **bet** appears only when Qwen returns `"verdict": "bet"` (model EV, odds move, and intel all agree). Lean/pass rows are tracking, not stakes.

## Data that is not in git

`output/`, `logs/`, `intel/`, `odds-history/` are local runtime files. After clone, generate a snapshot and a live book before the monitor has anything to score:

```powershell
cd D:\openclaw\hkjc-football-agent
$env:PYTHONPATH = (Get-Location).Path
py -3 -m pipeline.snapshot_api --date 2026-08-31
py -3 predict_v32.py --date 2026-08-31 --snapshot (Get-ChildItem output\odds_snapshots\*.json | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
```

Then start `bin\start-desk.ps1` again.

## GPU notes

- Linux/WSL: existing `start-llama-qwen.sh` (`GGML_CUDA_DISABLE_GRAPHS=1`, layer split).
- Native Windows: same flags in `bin\start-llama-qwen.ps1`. First load of Q4_K_M is several minutes.
