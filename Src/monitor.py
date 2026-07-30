"""
Local read-only web monitor for Market Advisor.
Serves a status dashboard + JSON API on localhost (default :8791).
No trade controls — observe only.
"""
import json
import threading
import base64
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from version import APP_NAME, display_name, __version__ as APP_VERSION
except ImportError:
    APP_NAME = "Market Advisor"
    APP_VERSION = "0.0.0"
    def display_name():
        return f"{APP_NAME} {APP_VERSION}"

_lock = threading.RLock()
_status = {
    "updated_at": None,
    "app": display_name(),
    "version": APP_VERSION,
    "mode": "LIVE",
    "market": "Unknown",
    "auto_trader": {"Robinhood": False, "Coinbase": False},
    "banner": "Offline",
    "balances": {
        "Robinhood": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
        "Coinbase": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
        "combined": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
    },
    "queue": [],
    "recent_trades": [],
    "recent_log": [],
    "holdings_count": {"Robinhood": 0, "Coinbase": 0},
}

_auth_user = ""
_auth_pass = ""
_server = None
_thread = None


def update_status(payload: dict):
    """Merge a status snapshot from the GUI (thread-safe)."""
    with _lock:
        for k, v in payload.items():
            _status[k] = v
        _status["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_status() -> dict:
    with _lock:
        return json.loads(json.dumps(_status))


def _check_auth(handler):
    if not _auth_user:
        return True
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
        return user == _auth_user and pwd == _auth_pass
    except Exception:
        return False


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>Market Advisor Monitor</title>
<style>
  :root {
    --bg: #0f1115; --panel: #1a1d24; --text: #e8eaed; --muted: #9aa0a6;
    --blue: #5b9fd4; --green: #3dd68c; --red: #f07178; --amber: #e6b450; --line: #2a2f3a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
    background: radial-gradient(900px 500px at 10% -10%, #1a2332 0%, var(--bg) 55%);
    color: var(--text); min-height: 100vh;
    padding: 16px;
    padding-left: max(16px, env(safe-area-inset-left));
    padding-right: max(16px, env(safe-area-inset-right));
    padding-bottom: max(20px, env(safe-area-inset-bottom));
  }
  h1 { font-size: 1.2rem; margin: 0 0 4px; font-weight: 650; letter-spacing: 0.02em; }
  .sub { color: var(--muted); font-size: 0.82rem; margin-bottom: 14px; line-height: 1.35; }
  .grid { display: grid; gap: 10px; grid-template-columns: 1fr 1fr; }
  .grid.metrics { grid-template-columns: 1fr; }
  .grid.brokers { grid-template-columns: 1fr; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
  }
  .label { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; }
  .value { font-size: 1.35rem; font-weight: 650; margin-top: 6px; word-break: break-word; }
  .value.sm { font-size: 1rem; }
  .pos { color: var(--green); } .neg { color: var(--red); } .neu { color: var(--text); }
  .banner {
    margin: 12px 0; padding: 12px 14px; border-radius: 10px; border: 1px solid var(--line);
    background: #141820; font-weight: 600; font-size: 0.92rem; line-height: 1.35;
  }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; margin-top: 8px; }
  table { width: 100%; min-width: 520px; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 0.7rem; text-transform: uppercase; }
  td.status { max-width: 220px; word-break: break-word; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 700;
  }
  .on { background: #14301f; color: var(--green); }
  .off { background: #22262e; color: var(--muted); }
  .log { font-family: ui-monospace, Consolas, monospace; font-size: 0.72rem; color: var(--muted);
         max-height: 200px; overflow: auto; white-space: pre-wrap; }
  a { color: var(--blue); }

  /* Tablet+ */
  @media (min-width: 640px) {
    body { padding: 20px; }
    h1 { font-size: 1.35rem; }
    .sub { font-size: 0.9rem; margin-bottom: 18px; }
    .grid { gap: 14px; grid-template-columns: repeat(4, 1fr); }
    .grid.metrics { grid-template-columns: repeat(3, 1fr); }
    .grid.brokers { grid-template-columns: 1fr 1fr; }
    .card { padding: 14px 16px; }
    .value { font-size: 1.45rem; }
    .value.sm { font-size: 1.05rem; }
    table { min-width: 0; font-size: 0.88rem; }
    .log { font-size: 0.78rem; max-height: 220px; }
  }
</style>
</head>
<body>
  <h1>Market Advisor Monitor</h1>
  <div class="sub"><span id="appver">—</span> · read-only · auto-refreshes · <span id="updated">—</span></div>

  <div class="grid">
    <div class="card"><div class="label">Mode</div><div class="value sm" id="mode">—</div></div>
    <div class="card"><div class="label">Market</div><div class="value sm" id="market">—</div></div>
    <div class="card"><div class="label">Robinhood Auto</div><div class="value sm" id="rh_auto">—</div></div>
    <div class="card"><div class="label">Coinbase Auto</div><div class="value sm" id="cb_auto">—</div></div>
  </div>

  <div class="banner" id="banner">Waiting for app…</div>

  <div class="grid metrics" style="margin-top:14px">
    <div class="card">
      <div class="label">Combined Equity</div>
      <div class="value" id="eq">—</div>
    </div>
    <div class="card">
      <div class="label">Combined Cash</div>
      <div class="value" id="cash">—</div>
    </div>
    <div class="card">
      <div class="label">Combined Day P&amp;L</div>
      <div class="value" id="pnl">—</div>
    </div>
  </div>

  <div class="grid brokers" style="margin-top:14px">
    <div class="card">
      <div class="label">Robinhood</div>
      <div class="value sm" id="rh_line">—</div>
    </div>
    <div class="card">
      <div class="label">Coinbase</div>
      <div class="value sm" id="cb_line">—</div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <div class="label">Recent Trades</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Broker</th><th>Side</th><th>Ticker</th><th>Status</th></tr></thead>
        <tbody id="trades"></tbody>
      </table>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <div class="label">Activity Log</div>
    <div class="log" id="log"></div>
  </div>

<script>
function money(n){
  const x = Number(n||0);
  return (x<0?'-':'') + '$' + Math.abs(x).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function pnlClass(n){ const x=Number(n||0); return x>0.001?'pos':(x<-0.001?'neg':'neu'); }
function pill(on){ return on ? '<span class="pill on">ON</span>' : '<span class="pill off">OFF</span>'; }

async function refresh(){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const d = await r.json();
    document.getElementById('appver').textContent = d.app || ('v' + (d.version || ''));
    document.getElementById('updated').textContent = 'Updated ' + (d.updated_at || '—');
    document.getElementById('mode').textContent = d.mode || '—';
    document.getElementById('market').textContent = d.market || '—';
    document.getElementById('rh_auto').innerHTML = pill(!!(d.auto_trader||{}).Robinhood);
    document.getElementById('cb_auto').innerHTML = pill(!!(d.auto_trader||{}).Coinbase);
    document.getElementById('banner').textContent = d.banner || '—';
    const c = (d.balances||{}).combined || {};
    document.getElementById('eq').textContent = money(c.equity);
    document.getElementById('cash').textContent = money(c.cash);
    const p = document.getElementById('pnl');
    p.textContent = money(c.day_pnl);
    p.className = 'value ' + pnlClass(c.day_pnl);
    const rh = (d.balances||{}).Robinhood || {};
    const cb = (d.balances||{}).Coinbase || {};
    document.getElementById('rh_line').innerHTML =
      money(rh.equity) + ' · cash ' + money(rh.cash) +
      ' · <span class="'+pnlClass(rh.day_pnl)+'">' + money(rh.day_pnl) + '</span>';
    document.getElementById('cb_line').innerHTML =
      money(cb.equity) + ' · cash ' + money(cb.cash) +
      ' · <span class="'+pnlClass(cb.day_pnl)+'">' + money(cb.day_pnl) + '</span>';
    const tb = document.getElementById('trades');
    tb.innerHTML = '';
    (d.recent_trades||[]).slice().reverse().forEach(t=>{
      const tr = document.createElement('tr');
      const st = String(t.status||'');
      tr.innerHTML = '<td>'+(t.timestamp||'').slice(11,19)+'</td><td>'+(t.broker||'')+
        '</td><td>'+(t.side||'')+'</td><td>'+(t.ticker||'')+
        '</td><td class="status">'+st.slice(0,80)+(st.length>80?'…':'')+'</td>';
      tb.appendChild(tr);
    });
    document.getElementById('log').textContent = (d.recent_log||[]).slice(-40).join('\\n');
  }catch(e){
    document.getElementById('banner').textContent = 'Monitor offline or app not publishing yet';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Keep console quiet unless errors
        if self.path.startswith("/api") and "404" in str(args):
            return
        return

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MarketAdvisor Monitor"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def do_GET(self):
        if not _check_auth(self):
            self._unauthorized()
            return
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/status"):
            body = json.dumps(get_status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def start_monitor(host="127.0.0.1", port=8791, username="", password=""):
    """Start the monitor HTTP server in a daemon thread. Returns (ok, message)."""
    global _server, _thread, _auth_user, _auth_pass
    if _server is not None:
        return True, f"Monitor already running on http://{host}:{port}"

    _auth_user = (username or "").strip()
    _auth_pass = password or ""

    try:
        _server = ThreadingHTTPServer((host, int(port)), _Handler)
    except OSError as e:
        _server = None
        return False, f"Monitor bind failed: {e}"

    def _run():
        try:
            _server.serve_forever(poll_interval=0.5)
        except Exception:
            pass

    _thread = threading.Thread(target=_run, name="MA-Monitor", daemon=True)
    _thread.start()
    auth_note = " (Basic Auth on)" if _auth_user else ""
    return True, f"Monitor at http://{host}:{port}/{auth_note}"


def stop_monitor():
    global _server, _thread
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
        try:
            _server.server_close()
        except Exception:
            pass
        _server = None
        _thread = None
