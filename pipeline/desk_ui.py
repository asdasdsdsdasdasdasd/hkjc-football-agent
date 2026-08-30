"""Local desk window: live card + intel + steam alerts.

  python3 -m pipeline.desk_ui --port 8765
  then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

def llama_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1.0) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
INTEL = ROOT / "intel"
PID = ROOT / "odds-history" / "monitor.pid"
ALERTS = OUT / "alerts.jsonl"
HKT = timezone(timedelta(hours=8))
PORT = 8765


def _mtime(p: Path | None) -> str | None:
    if not p or not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime, HKT).isoformat(timespec="seconds")


def _latest(dir_: Path, pattern: str) -> Path | None:
    files = sorted(dir_.glob(pattern)) if dir_.exists() else []
    return files[-1] if files else None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def monitor_status() -> dict[str, Any]:
    if not PID.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(PID.read_text().strip())
    except ValueError:
        return {"running": False, "pid": None}
    return {"running": _pid_alive(pid), "pid": pid}


def load_alerts(n: int = 40) -> list[dict[str, Any]]:
    if not ALERTS.exists():
        return []
    lines = ALERTS.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def load_card() -> tuple[list[dict[str, Any]], Path | None]:
    p = _latest(OUT, "card_*.json")
    if not p:
        return [], None
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], p
    return rows if isinstance(rows, list) else [], p


def snapshot() -> dict[str, Any]:
    card, card_path = load_card()
    intel_path = _latest(INTEL, "intel-*.json")
    bets = leans = passes = 0
    slim: list[dict[str, Any]] = []
    for r in card:
        v = str((r.get("llm") or {}).get("verdict") or "-")
        if v == "bet":
            bets += 1
        elif v == "lean":
            leans += 1
        elif v == "pass":
            passes += 1
        intel = r.get("intel") or {}
        un_h = len((intel.get("unavailable") or {}).get("home") or [])
        un_a = len((intel.get("unavailable") or {}).get("away") or [])
        slim.append({
            "match_id": r.get("match_id"),
            "teams": r.get("teams"),
            "competition": r.get("competition"),
            "pick": r.get("pick"),
            "odds": r.get("odds"),
            "ev": r.get("ev"),
            "move_pp": r.get("odds_move_pp"),
            "verdict": v,
            "confidence": (r.get("llm") or {}).get("confidence"),
            "reason": (r.get("llm") or {}).get("reason"),
            "flags": (r.get("llm") or {}).get("risk_flags") or [],
            "unavail": un_h + un_a,
            "headlines": intel.get("headlines") or [],
        })
    order = {"bet": 0, "lean": 1, "pass": 2, "-": 3}
    slim.sort(key=lambda x: (order.get(str(x["verdict"]), 9), -(float(x["ev"] or 0))))
    mon = monitor_status()
    return {
        "ts": datetime.now(HKT).isoformat(timespec="seconds"),
        "llama": llama_up(),
        "monitor": mon,
        "card_file": str(card_path) if card_path else None,
        "card_mtime": _mtime(card_path),
        "intel_file": str(intel_path) if intel_path else None,
        "intel_mtime": _mtime(intel_path),
        "counts": {"bet": bets, "lean": leans, "pass": passes, "n": len(slim)},
        "rows": slim,
        "alerts": load_alerts(50),
    }


PAGE = r"""<!doctype html>
<html lang="zh-HK">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>HKJC desk</title>
<style>
  :root { --bg:#1b1b1b; --fg:#e8e8e8; --muted:#9a9a9a; --line:#333; --bet:#3d9a6a; --lean:#c4a035; --pass:#7a7a7a; --err:#c45c5c; }
  * { box-sizing: border-box; }
  body { margin:0; font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:16px 20px 8px; border-bottom:1px solid var(--line); }
  h1 { font-size:20px; font-weight:600; margin:0 0 8px; }
  .meta { color:var(--muted); font-size:12px; }
  .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; padding:16px 20px; }
  .stat { padding:12px 0; }
  .stat b { display:block; font-size:22px; font-weight:600; }
  .stat span { color:var(--muted); font-size:12px; }
  .stat.bet b { color:var(--bet); } .stat.lean b { color:var(--lean); }
  .dots { display:flex; gap:14px; padding:0 20px 12px; font-size:12px; color:var(--muted); }
  .dots i { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .on { background:var(--bet); } .off { background:var(--err); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:500; font-size:12px; position:sticky; top:0; background:var(--bg); }
  .wrap { padding:0 12px 24px; }
  .v-bet { color:var(--bet); font-weight:600; } .v-lean { color:var(--lean); font-weight:600; } .v-pass { color:var(--pass); }
  .reason { color:var(--muted); font-size:12px; max-width:420px; }
  .alerts { padding:8px 20px 24px; }
  .alerts h2 { font-size:14px; font-weight:600; margin:16px 0 8px; }
  .alerts li { font-size:12px; color:var(--muted); margin:4px 0; list-style:none; }
  .alerts ul { margin:0; padding:0; }
  button { background:transparent; color:var(--fg); border:1px solid var(--line); padding:4px 10px; cursor:pointer; }
  button.on { border-color:var(--fg); }
  .filters { padding:0 20px 8px; display:flex; gap:8px; }
</style>
</head>
<body>
<header>
  <h1>HKJC football desk</h1>
  <div class="meta" id="meta">loading…</div>
</header>
<div class="dots" id="dots"></div>
<div class="stats" id="stats"></div>
<div class="filters">
  <button data-f="all" class="on">all</button>
  <button data-f="bet">bet</button>
  <button data-f="lean">lean</button>
  <button data-f="pass">pass</button>
</div>
<div class="wrap"><table>
  <thead><tr><th>verdict</th><th>match</th><th>pick</th><th>odds</th><th>EV</th><th>move</th><th>why</th></tr></thead>
  <tbody id="body"></tbody>
</table></div>
<div class="alerts">
  <h2>Steam alerts (latest)</h2>
  <ul id="alerts"></ul>
</div>
<script>
let filter = "all";
let cache = null;
document.querySelectorAll(".filters button").forEach(b => {
  b.onclick = () => { document.querySelectorAll(".filters button").forEach(x => x.classList.remove("on")); b.classList.add("on"); filter = b.dataset.f; render(); };
});
function pct(x) { return x == null ? "—" : (Number(x)*100).toFixed(1) + "%"; }
function pp(x) { return x == null ? "—" : (Number(x)>0?"+":"") + Number(x).toFixed(1) + "pp"; }
function render() {
  if (!cache) return;
  const d = cache;
  const mon = d.monitor.running ? "monitor up pid " + d.monitor.pid : "monitor down";
  const llama = d.llama ? "Qwen up" : "Qwen down";
  document.getElementById("meta").textContent =
    d.ts + " · " + llama + " · " + mon + " · card " + (d.card_mtime || "none") + " · intel " + (d.intel_mtime || "none") + " · auto-refresh 8s";
  document.getElementById("dots").innerHTML =
    `<span><i class="${d.llama?'on':'off'}"></i>Qwen</span><span><i class="${d.monitor.running?'on':'off'}"></i>monitor</span>`;
  const c = d.counts;
  document.getElementById("stats").innerHTML =
    `<div class="stat bet"><b>${c.bet}</b><span>bet</span></div>
     <div class="stat lean"><b>${c.lean}</b><span>lean</span></div>
     <div class="stat"><b>${c.pass}</b><span>pass</span></div>
     <div class="stat"><b>${c.n}</b><span>on card</span></div>`;
  const rows = d.rows.filter(r => filter === "all" || r.verdict === filter);
  document.getElementById("body").innerHTML = rows.map(r => `
    <tr>
      <td class="v-${r.verdict}">${r.verdict}</td>
      <td>${r.match_id}<br><span class="reason">${r.teams || ""}</span></td>
      <td>${r.pick || ""}</td>
      <td>${r.odds ?? "—"}</td>
      <td>${pct(r.ev)}</td>
      <td>${pp(r.move_pp)}</td>
      <td class="reason">${r.reason || ""}</td>
    </tr>`).join("");
  document.getElementById("alerts").innerHTML = (d.alerts || []).slice(0, 25).map(a =>
    `<li>${a.ts || ""} · ${a.match_id || ""} ${a.detail || a.kind}</li>`).join("") || "<li>none yet</li>";
}
async function tick() {
  try { cache = await (await fetch("/api/state")).json(); render(); }
  catch (e) { document.getElementById("meta").textContent = "UI error: " + e; }
}
tick();
setInterval(tick, 8000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[desk-ui] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/state"):
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[desk-ui] {url}", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
