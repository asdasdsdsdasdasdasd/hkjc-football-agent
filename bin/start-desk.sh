#!/usr/bin/env bash
# Keep the football desk running: Qwen + odds/news/lineup monitor.
set -euo pipefail

ROOT=/mnt/d/openclaw/hkjc-football-agent
LLAMA=/mnt/d/ai/bin/start-llama-qwen.sh
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR" "$ROOT/odds-history"

# Stop a leftover standalone odds tracker (folded into monitor).
pkill -f "python3 -m pipeline.odds_tracker" 2>/dev/null || true

if ! curl -sf -m 3 http://127.0.0.1:8080/health >/dev/null; then
  echo "[desk] starting llama-server"
  nohup bash "$LLAMA" >>"$LOG_DIR/llama-server.log" 2>&1 &
  for i in $(seq 1 90); do
    if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null; then
      echo "[desk] llama ready"
      break
    fi
    sleep 2
  done
else
  echo "[desk] llama already up"
fi

cd "$ROOT"
export PYTHONPATH=.

# Monitor in background; this window process is the dashboard.
if [[ -f "$ROOT/odds-history/monitor.pid" ]] && kill -0 "$(cat "$ROOT/odds-history/monitor.pid")" 2>/dev/null; then
  echo "[desk] monitor already running"
else
  echo "[desk] monitor logging to $LOG_DIR/monitor.log"
  nohup python3 -m pipeline.monitor >>"$LOG_DIR/monitor.log" 2>&1 &
  sleep 1
fi

pkill -f "python3 -m pipeline.desk_ui" 2>/dev/null || true
sleep 0.3
echo "[desk] window http://127.0.0.1:8765"
exec python3 -m pipeline.desk_ui --host 127.0.0.1 --port 8765 --open
