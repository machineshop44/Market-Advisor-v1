"""
Local / remote web monitor for Market Advisor.
Serves a status dashboard + JSON API (default :8791).

- Localhost HTTP is fine for on-PC viewing.
- LAN / port-forward use HTTPS + required Basic Auth so user/pass are TLS-encrypted
  in transit. Failed logins are rate-limited / locked out.
- Optional authenticated POST /api/auto for companion arm/disarm.
- Optional authenticated POST /api/halt for Panic Halt All.
No buy/sell endpoints.
"""
import json
import threading
import base64
import ssl
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from version import APP_NAME, display_name, __version__ as APP_VERSION
except ImportError:
    APP_NAME = "Market Advisor"
    APP_VERSION = "0.0.0"
    def display_name():
        return f"{APP_NAME} {APP_VERSION}"

try:
    import monitor_tls
except ImportError:
    monitor_tls = None

_lock = threading.RLock()
_status = {
    "updated_at": None,
    "app": display_name(),
    "version": APP_VERSION,
    "mode": "LIVE",
    "market": "Unknown",
    "auto_trader": {"Robinhood": False, "Coinbase": False, "E*TRADE": False},
    "brokers": {},
    "banner": "Offline",
    "balances": {
        "Robinhood": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
        "Coinbase": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
        "E*TRADE": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
        "combined": {"equity": 0.0, "cash": 0.0, "day_pnl": 0.0},
    },
    "queue": [],
    "recent_trades": [],
    "recent_log": [],
    "holdings_count": {"Robinhood": 0, "Coinbase": 0, "E*TRADE": 0},
    "controls_enabled": False,
    "tls": False,
    "cert_fingerprint": "",
}

_auth_user = ""
_auth_pass = ""
_auth_required = False
_cursor_agent_enabled = False
_cursor_agent_token = ""
_controls_enabled = False
_control_handler = None
_halt_handler = None
_advisor_handler = None
_eod_handler = None
_etrade_oauth_handler = None
_server = None
_thread = None
_tls_enabled = False
_cert_fingerprint = ""

# Brute-force guard: client_ip -> {fails, locked_until}
_auth_fail_lock = threading.Lock()
_auth_failures = {}
_AUTH_MAX_FAILS = 5
_AUTH_WINDOW_SEC = 300
_AUTH_LOCK_SEC = 900

VALID_BROKERS = ("Robinhood", "Coinbase", "E*TRADE")


def update_status(payload: dict):
    """Merge a status snapshot from the GUI (thread-safe)."""
    with _lock:
        for k, v in payload.items():
            _status[k] = v
        _status["controls_enabled"] = bool(_controls_enabled)
        _status["tls"] = bool(_tls_enabled)
        _status["cert_fingerprint"] = _cert_fingerprint
        _status["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_status() -> dict:
    with _lock:
        snap = json.loads(json.dumps(_status))
        snap["controls_enabled"] = bool(_controls_enabled)
        snap["tls"] = bool(_tls_enabled)
        snap["cert_fingerprint"] = _cert_fingerprint
        return snap


def get_cert_fingerprint() -> str:
    return _cert_fingerprint


def set_control_handler(handler):
    """Register GUI callback: handler(broker, armed) -> {"ok": bool, "error"?: str}."""
    global _control_handler
    _control_handler = handler


def set_halt_handler(handler):
    """Register GUI callback for Panic Halt All: handler() -> {"ok": bool, ...}."""
    global _halt_handler
    _halt_handler = handler


def set_advisor_handler(handler):
    """
    Register GUI callback for companion advisor approve/reject.
    handler(proposal_id, action) -> dict
      action: approve | reject | reject_all
    """
    global _advisor_handler
    _advisor_handler = handler


def set_eod_handler(handler):
    """Register GUI callback for companion EOD protective pass: handler() -> dict."""
    global _eod_handler
    _eod_handler = handler


def set_etrade_oauth_handler(handler):
    """
    Register GUI callback for companion E*TRADE OAuth.
    handler(action, verifier=None) -> dict
      action: "start" | "complete"
    """
    global _etrade_oauth_handler
    _etrade_oauth_handler = handler


def configure_controls(enabled: bool):
    global _controls_enabled
    _controls_enabled = bool(enabled)
    with _lock:
        _status["controls_enabled"] = _controls_enabled


def configure_cursor_agent(enabled: bool, token: str = ""):
    """Read-only Bearer token for /api/agent/* (Cursor MCP bridge)."""
    global _cursor_agent_enabled, _cursor_agent_token
    tok = str(token or "").strip()
    _cursor_agent_token = tok
    _cursor_agent_enabled = bool(enabled) and bool(tok)


def _client_ip(handler) -> str:
    try:
        return handler.client_address[0] if handler.client_address else "unknown"
    except Exception:
        return "unknown"


def _auth_is_locked(ip: str) -> bool:
    now = time.time()
    with _auth_fail_lock:
        info = _auth_failures.get(ip) or {}
        locked_until = float(info.get("locked_until") or 0)
        if locked_until > now:
            return True
        # Expire old fail window
        fails = info.get("fails") or []
        fails = [t for t in fails if now - t < _AUTH_WINDOW_SEC]
        if fails:
            _auth_failures[ip] = {"fails": fails, "locked_until": 0}
        elif ip in _auth_failures:
            _auth_failures.pop(ip, None)
        return False


def _auth_lockout_remaining(ip: str) -> int:
    """Seconds remaining on auth lockout for ip (0 if not locked)."""
    now = time.time()
    with _auth_fail_lock:
        info = _auth_failures.get(ip) or {}
        locked_until = float(info.get("locked_until") or 0)
        if locked_until > now:
            return max(1, int(locked_until - now + 0.999))
        return 0


def _auth_register_failure(ip: str):
    now = time.time()
    with _auth_fail_lock:
        info = _auth_failures.get(ip) or {"fails": [], "locked_until": 0}
        fails = [t for t in (info.get("fails") or []) if now - t < _AUTH_WINDOW_SEC]
        fails.append(now)
        locked_until = 0
        if len(fails) >= _AUTH_MAX_FAILS:
            locked_until = now + _AUTH_LOCK_SEC
            fails = []
        _auth_failures[ip] = {"fails": fails, "locked_until": locked_until}


def _auth_register_success(ip: str):
    with _auth_fail_lock:
        _auth_failures.pop(ip, None)


def clear_auth_lockouts():
    """Clear all client IP auth failure / lockout state (GUI / local only)."""
    with _auth_fail_lock:
        _auth_failures.clear()


def is_running() -> bool:
    return _server is not None


def describe_runtime() -> dict:
    """Live monitor process state (not draft Settings widgets)."""
    return {
        "running": _server is not None,
        "tls": bool(_tls_enabled),
        "controls_enabled": bool(_controls_enabled),
        "has_auth": bool(_auth_user),
        "fingerprint": _cert_fingerprint or "",
    }


def _credentials_ok(handler) -> bool:
    if not _auth_user:
        return not _auth_required
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
        return user == _auth_user and pwd == _auth_pass
    except Exception:
        return False


def _bearer_token_ok(handler) -> bool:
    if not _cursor_agent_enabled or not _cursor_agent_token:
        return False
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header[7:].strip() == _cursor_agent_token


def _check_agent_auth(handler) -> bool:
    """Read-only agent routes: Bearer read token OR monitor Basic auth."""
    ip = _client_ip(handler)
    if _auth_is_locked(ip):
        return False
    if _bearer_token_ok(handler):
        _auth_register_success(ip)
        return True
    if _auth_user and _credentials_ok(handler):
        _auth_register_success(ip)
        return True
    if _cursor_agent_enabled and _cursor_agent_token:
        if _auth_user or _auth_required:
            _auth_register_failure(ip)
        return False
    if not _auth_user and not _auth_required:
        return True
    if _auth_user or _auth_required:
        _auth_register_failure(ip)
    return False


def _check_auth(handler) -> bool:
    """Return True if request is allowed. Records lockouts on failure when auth is set."""
    ip = _client_ip(handler)
    if _auth_is_locked(ip):
        return False
    if not _auth_user:
        return not _auth_required
    if _credentials_ok(handler):
        _auth_register_success(ip)
        return True
    _auth_register_failure(ip)
    return False


def _require_auth_for_controls(handler):
    if not _auth_user:
        return False
    return _check_auth(handler)


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>Market Advisor Monitor</title>
<style>
  :root {
    /* Match desktop dark theme (gui.py UI_ACCENT / theme_colors) */
    --bg: #0f1115; --panel: #1a1d24; --top: #151820; --text: #e8eaed; --muted: #9aa0a6;
    --accent: #1f8a70; --accent-hover: #26a69a; --green: #00e676; --red: #ff5252;
    --amber: #ffb300; --line: #2a2f3a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
    background: radial-gradient(900px 480px at 8% -8%, #16352c 0%, var(--bg) 52%);
    color: var(--text); min-height: 100vh;
    padding: 16px;
    padding-left: max(16px, env(safe-area-inset-left));
    padding-right: max(16px, env(safe-area-inset-right));
    padding-bottom: max(20px, env(safe-area-inset-bottom));
  }
  .brand {
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
    margin: 0 0 4px;
  }
  h1 { font-size: 1.25rem; margin: 0; font-weight: 650; letter-spacing: 0.02em; color: var(--text); }
  .brand .tag { color: var(--accent); font-size: 0.78rem; font-weight: 700; letter-spacing: 0.04em; }
  .sub { color: var(--muted); font-size: 0.82rem; margin-bottom: 14px; line-height: 1.35; }
  .grid { display: grid; gap: 10px; grid-template-columns: 1fr 1fr; }
  .grid.metrics { grid-template-columns: 1fr; }
  .grid.brokers { grid-template-columns: 1fr; }
  .grid.autos { grid-template-columns: 1fr; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
  }
  .label { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; }
  .value { font-size: 1.35rem; font-weight: 650; margin-top: 6px; word-break: break-word; }
  .value.sm { font-size: 1rem; }
  .pos { color: var(--green); } .neg { color: var(--red); } .neu { color: var(--text); }
  .banner {
    margin: 12px 0; padding: 12px 14px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--top); font-weight: 600; font-size: 0.92rem; line-height: 1.35;
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
  a { color: var(--accent-hover); }
  .cluster-row { margin-top: 10px; }
  .cluster-row:first-child { margin-top: 6px; }
  .cluster-top {
    display: flex; justify-content: space-between; gap: 8px; align-items: baseline;
    font-size: 0.88rem; font-weight: 650;
  }
  .cluster-meta { color: var(--muted); font-size: 0.78rem; font-weight: 600; }
  .cluster-bar {
    height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 5px;
  }
  .cluster-fill { height: 100%; border-radius: 4px; }
  .cluster-fill.ok { background: var(--accent); }
  .cluster-fill.full { background: var(--red); }
  .cluster-held {
    color: var(--muted); font-size: 0.75rem; margin-top: 4px; text-transform: none;
    letter-spacing: 0; font-weight: 500;
  }
  .badge-full {
    display: inline-block; margin-left: 6px; padding: 1px 6px; border-radius: 4px;
    font-size: 0.65rem; font-weight: 700; background: #3a1515; color: var(--red);
  }

  @media (min-width: 640px) {
    body { padding: 20px; }
    h1 { font-size: 1.4rem; }
    .sub { font-size: 0.9rem; margin-bottom: 18px; }
    .grid { gap: 14px; grid-template-columns: repeat(4, 1fr); }
    .grid.metrics { grid-template-columns: repeat(3, 1fr); }
    .grid.brokers { grid-template-columns: 1fr 1fr 1fr; }
    .grid.autos { grid-template-columns: repeat(3, 1fr); }
    .card { padding: 14px 16px; }
    .value { font-size: 1.45rem; }
    .value.sm { font-size: 1.05rem; }
    table { min-width: 0; font-size: 0.88rem; }
    .log { font-size: 0.78rem; max-height: 220px; }
  }
</style>
</head>
<body>
  <div class="brand"><h1>Market Advisor</h1><span class="tag">MONITOR</span></div>
  <div class="sub"><span id="appver">—</span> · <span id="modehint">read-only</span> · auto-refreshes · <span id="updated">—</span></div>

  <div class="grid">
    <div class="card"><div class="label">Mode</div><div class="value sm" id="mode">—</div></div>
    <div class="card"><div class="label">Market</div><div class="value sm" id="market">—</div></div>
  </div>
  <div class="grid autos" id="auto_cards" style="margin-top:10px"></div>

  <div class="banner" id="banner">Waiting for app…</div>
  <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button id="halt_btn" style="background:#B71C1C;color:#fff;border:0;padding:8px 14px;border-radius:6px;font-weight:700;cursor:pointer">HALT ALL</button>
    <span id="risk_bits" class="label" style="opacity:.85">—</span>
  </div>

  <div class="card" style="margin-top:14px">
    <div class="label">Cluster Heat</div>
    <div id="clusters"><div class="value sm" style="opacity:.7;font-size:0.9rem">—</div></div>
  </div>

  <div class="card" style="margin-top:14px">
    <div class="label">Risk · Stops · Shadow · Frac</div>
    <div id="risk_panel" class="value sm" style="font-size:0.9rem;line-height:1.45">—</div>
  </div>

  <div class="card" style="margin-top:14px">
    <div class="label">Walk-forward (journal · bar)</div>
    <div id="wf_panel" class="value sm" style="font-size:0.85rem;line-height:1.4;opacity:.9">—</div>
  </div>

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

  <div class="grid brokers" id="broker_cards" style="margin-top:14px"></div>

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
function brokerNames(d){
  const bal = d.balances || {};
  const names = Object.keys(bal).filter(k => k !== 'combined');
  if(names.length) return names;
  return Object.keys(d.auto_trader || {});
}

async function refresh(){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const d = await r.json();
    document.getElementById('appver').textContent = d.app || ('v' + (d.version || ''));
    document.getElementById('updated').textContent = 'Updated ' + (d.updated_at || '—');
    document.getElementById('mode').textContent = d.mode || '—';
    document.getElementById('market').textContent = d.market || '—';
    const bits = [];
    if(d.tls) bits.push('HTTPS');
    bits.push(d.controls_enabled ? 'companion controls on' : 'read-only');
    document.getElementById('modehint').textContent = bits.join(' · ');
    document.getElementById('banner').textContent = d.banner || '—';
    const c = (d.balances||{}).combined || {};
    document.getElementById('eq').textContent = money(c.equity);
    document.getElementById('cash').textContent = money(c.cash);
    const p = document.getElementById('pnl');
    p.textContent = money(c.day_pnl);
    p.className = 'value ' + pnlClass(c.day_pnl);

    const names = brokerNames(d);
    const autoHost = document.getElementById('auto_cards');
    autoHost.innerHTML = '';
    names.forEach(n=>{
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = '<div class="label">'+n+' Auto</div><div class="value sm">'+
        pill(!!(d.auto_trader||{})[n])+'</div>';
      autoHost.appendChild(card);
    });
    const bHost = document.getElementById('broker_cards');
    bHost.innerHTML = '';
    names.forEach(n=>{
      const b = (d.balances||{})[n] || {};
      const info = (d.brokers||{})[n] || {};
      const bits = [];
      if(info.reauth_needed) bits.push('REAUTH');
      if(info.dd_pause) bits.push('DD pause');
      if(info.armed) bits.push('armed');
      const lk = ((d.locked_capital||{}).by_broker||{})[n] || {};
      if((lk.value||0) > 0.01) bits.push('locked '+money(lk.value));
      if(n==='E*TRADE'){
        if(info.sandbox_no_bp) bits.push('Sandbox/no BP');
        else if(info.environment) bits.push(String(info.environment));
        bits.push('stops N/A');
      }
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = '<div class="label">'+n+(bits.length?' · '+bits.join(' · '):'')+'</div><div class="value sm">'+
        money(b.equity)+' · cash '+money(b.cash)+
        ' · <span class="'+pnlClass(b.day_pnl)+'">'+money(b.day_pnl)+'</span></div>';
      bHost.appendChild(card);
    });

    const risk = document.getElementById('risk_bits');
    if(risk){
      const ph = d.protective_health || {};
      const heat = ((d.portfolio_heat||{}).combined) || {};
      const bits = [];
      bits.push('Stops missing: '+(ph.missing_count||0));
      if(ph.fractional_na_count) bits.push('frac N/A: '+ph.fractional_na_count);
      if(heat.open_risk_pct != null) bits.push('open risk '+Number(heat.open_risk_pct).toFixed(1)+'%');
      if(heat.session_risk_used_pct != null) bits.push('session '+Number(heat.session_risk_used_pct).toFixed(0)+'%');
      if(heat.dd_paused) bits.push('DD pause');
      const locked = d.locked_capital || {};
      if((locked.total||0) > 0.01) bits.push('locked '+money(locked.total));
      if(heat.peak_dd_worst_pct != null && heat.peak_dd_worst_pct < -0.001) {
        bits.push('peak DD '+(Number(heat.peak_dd_worst_pct)*100).toFixed(1)+'%');
      }
      if(d.halted) bits.push('HALTED');
      risk.textContent = bits.join(' · ');
    }
    const riskPanel = document.getElementById('risk_panel');
    if(riskPanel){
      const ph = d.protective_health || {};
      const heat = ((d.portfolio_heat||{}).combined) || {};
      const locked = d.locked_capital || {};
      const sg = d.shadow_guard || {};
      const fp = d.frac_policy || {};
      const et = d.etrade || {};
      const lines = [];
      lines.push('Open risk ≈ $'+Number(heat.open_risk_dollars||0).toFixed(2)+
        ' ('+Number(heat.open_risk_pct||0).toFixed(1)+'% eq) · session '+
        Number(heat.session_risk_used_pct||0).toFixed(0)+'%');
      if((locked.total||0) > 0.01){
        lines.push('Locked capital '+money(locked.total)+' excluded from sizing/deployable BP');
      }
      if(heat.peak_dd_worst_pct != null && heat.peak_dd_worst_pct < -0.001){
        lines.push('Peak drawdown from session high: '+(Number(heat.peak_dd_worst_pct)*100).toFixed(1)+'%');
      }
      lines.push('Stops missing '+(ph.missing_count||0)+
        (ph.fractional_na_count ? (' · fractional N/A '+ph.fractional_na_count) : '')+
        ' · Repair skips E*TRADE (stops N/A)');
      lines.push('Shadow: '+(sg.status||sg.tip||'—')+
        (sg.tighten ? (' · size×'+Number(sg.size_mult||1).toFixed(2)) : ''));
      lines.push('Frac policy: prefer whole='+(fp.prefer_whole_shares?'yes':'no')+
        ' · TTP-only frac='+(fp.allow_ttp_only?'yes':'no'));
      if(et.note) lines.push('E*TRADE: '+et.note+(et.sandbox_no_bp?' (sandbox BP stub)':''));
      lines.push('Mode '+(d.mode||'—')+(d.halted?' · HALTED':''));
      riskPanel.textContent = lines.join('\\n');
      riskPanel.style.whiteSpace = 'pre-wrap';
    }
    const wfPanel = document.getElementById('wf_panel');
    if(wfPanel){
      const wf = d.walk_forward || {};
      const j = wf.journal || {};
      const b = wf.bar || {};
      const jn = j.note || (j.oos_steps!=null ? ('Journal folds: '+j.oos_steps+' OOS · net '+Number(j.oos_net_sum||0).toFixed(2)) : 'Journal folds: —');
      const bn = b.note || (b.n_trades!=null ? ('Bar WF: '+b.n_trades+' trades · OOS '+Number(b.oos_net_sum||0).toFixed(2)) : 'Bar walk-forward: —');
      wfPanel.textContent = jn + '\\n' + bn;
      wfPanel.style.whiteSpace = 'pre-wrap';
    }
    const cHost = document.getElementById('clusters');
    if(cHost){
      const ch = (d.cluster_heat || []).filter(x => x && (x.count > 0 || x.full));
      if(!ch.length){
        cHost.innerHTML = '<div class="value sm" style="opacity:.7;font-size:0.9rem">No holdings in tracked clusters…</div>';
      } else {
        cHost.innerHTML = ch.map(x => {
          const mx = Math.max(1, Number(x.max)||2);
          const n = Math.min(mx, Number(x.count)||0);
          const pct = Math.round(100 * n / mx);
          const full = !!x.full || n >= mx;
          const held = (x.held || []).join(', ') || '—';
          return '<div class="cluster-row">' +
            '<div class="cluster-top"><span>'+x.name+
            (full ? '<span class="badge-full">FULL</span>' : '')+
            '</span><span class="cluster-meta">'+n+'/'+mx+
            (full ? '' : ' · room')+'</span></div>' +
            '<div class="cluster-bar"><div class="cluster-fill '+(full?'full':'ok')+
            '" style="width:'+pct+'%"></div></div>' +
            '<div class="cluster-held">'+held+'</div></div>';
        }).join('');
      }
    }
    const haltBtn = document.getElementById('halt_btn');
    if(haltBtn && !haltBtn._bound){
      haltBtn._bound = true;
      haltBtn.onclick = async ()=>{
        if(!confirm('Panic Halt All brokers?')) return;
        try{
          const r = await fetch('/api/halt', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
          const j = await r.json();
          alert(j.ok ? 'Halted' : ('Halt failed: '+(j.error||r.status)));
          refresh();
        }catch(e){ alert('Halt failed: '+e); }
      };
    }

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
        if self.path.startswith("/api") and "404" in str(args):
            return
        return

    def _unauthorized(self, locked=False):
        remaining = _auth_lockout_remaining(_client_ip(self)) if locked else 0
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MarketAdvisor Monitor"')
        self.send_header("Content-Type", "application/json")
        if locked and remaining > 0:
            self.send_header("Retry-After", str(remaining))
        self.end_headers()
        if locked:
            msg = {
                "ok": False,
                "error": (
                    f"Too many failed logins — try again in {remaining}s"
                    if remaining > 0
                    else "Too many failed logins — try again later"
                ),
            }
            if remaining > 0:
                msg["lockout_seconds"] = remaining
        else:
            msg = {"ok": False, "error": "Unauthorized"}
        self.wfile.write(json.dumps(msg).encode("utf-8"))

    def _json_response(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 65536:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        # Public: cert fingerprint for companion TOFU / pin check (not a secret)
        if path.startswith("/api/tls"):
            self._json_response(200, {
                "tls": bool(_tls_enabled),
                "fingerprint": _cert_fingerprint,
                "algo": "sha256",
            })
            return

        if path.startswith("/api/agent/digest"):
            if not _check_agent_auth(self):
                self._unauthorized(locked=_auth_is_locked(_client_ip(self)))
                return
            try:
                import cursor_monitor as cm
                payload = cm.build_agent_digest(get_status())
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/agent/log"):
            if not _check_agent_auth(self):
                self._unauthorized(locked=_auth_is_locked(_client_ip(self)))
                return
            try:
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(self.path).query)
                limit = int((qs.get("limit") or ["25"])[0])
            except Exception:
                limit = 25
            limit = max(1, min(limit, 80))
            lines = list(get_status().get("recent_log") or [])[-limit:]
            body = json.dumps({"ok": True, "lines": lines}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/agent/snags"):
            if not _check_agent_auth(self):
                self._unauthorized(locked=_auth_is_locked(_client_ip(self)))
                return
            try:
                import desk_watchdog as dw
                payload = dw.scan_snags(get_status())
            except Exception as e:
                payload = {"ok": False, "error": str(e), "snags": []}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        ip = _client_ip(self)
        if _auth_is_locked(ip):
            self._unauthorized(locked=True)
            return
        if not _check_auth(self):
            self._unauthorized(locked=_auth_is_locked(ip))
            return
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/status"):
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

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in (
            "/api/auto",
            "/api/halt",
            "/api/eod/run",
            "/api/advisor/approve",
            "/api/etrade/oauth/start",
            "/api/etrade/oauth/complete",
        ):
            self.send_response(404)
            self.end_headers()
            return

        if not _controls_enabled:
            self._json_response(403, {
                "ok": False,
                "error": "Remote controls are disabled. Enable Companion Controls in Settings.",
            })
            return

        ip = _client_ip(self)
        if _auth_is_locked(ip):
            self._unauthorized(locked=True)
            return
        if not _require_auth_for_controls(self):
            self._unauthorized(locked=_auth_is_locked(ip))
            return

        if path == "/api/halt":
            handler = _halt_handler
            if handler is None:
                self._json_response(503, {"ok": False, "error": "App halt handler not ready"})
                return
            try:
                result = handler()
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
                return
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            code = 200 if result.get("ok") else 400
            self._json_response(code, result)
            return

        if path == "/api/eod/run":
            handler = _eod_handler
            if handler is None:
                self._json_response(503, {"ok": False, "error": "EOD handler not ready"})
                return
            try:
                result = handler()
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
                return
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            code = 200 if result.get("ok") else 400
            self._json_response(code, result)
            return

        if path == "/api/advisor/approve":
            handler = _advisor_handler
            if handler is None:
                self._json_response(503, {"ok": False, "error": "Advisor handler not ready"})
                return
            data = self._read_json_body()
            if data is None:
                data = {}
            if not isinstance(data, dict):
                self._json_response(400, {"ok": False, "error": "Expected JSON body"})
                return
            proposal_id = str(data.get("id") or data.get("proposal_id") or "").strip()
            action = str(data.get("action") or "approve").lower().strip()
            if action not in ("approve", "reject", "reject_all"):
                self._json_response(400, {"ok": False, "error": "action must be approve, reject, or reject_all"})
                return
            if action == "reject" and not proposal_id:
                self._json_response(400, {"ok": False, "error": "id required for reject"})
                return
            try:
                result = handler(proposal_id, action)
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
                return
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            code = 200 if result.get("ok") else 400
            self._json_response(code, result)
            return

        if path in ("/api/etrade/oauth/start", "/api/etrade/oauth/complete"):
            handler = _etrade_oauth_handler
            if handler is None:
                self._json_response(503, {"ok": False, "error": "E*TRADE OAuth handler not ready"})
                return
            data = self._read_json_body()
            if data is None:
                data = {}
            if not isinstance(data, dict):
                self._json_response(400, {"ok": False, "error": "Expected JSON body"})
                return
            action = "start" if path.endswith("/start") else "complete"
            verifier = str(data.get("verifier") or "").strip()
            try:
                result = handler(action, verifier)
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
                return
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            code = 200 if result.get("ok") else 400
            self._json_response(code, result)
            return

        data = self._read_json_body()
        if not isinstance(data, dict):
            self._json_response(400, {"ok": False, "error": "Expected JSON body"})
            return

        handler = _control_handler
        if handler is None:
            self._json_response(503, {"ok": False, "error": "App control handler not ready"})
            return

        # Batch: {"brokers": {"Robinhood": true, "Coinbase": false}}
        # Single: {"broker": "Robinhood", "armed"|"enabled": true}
        brokers_map = data.get("brokers")
        if isinstance(brokers_map, dict) and brokers_map:
            results = {}
            all_ok = True
            for name, want in brokers_map.items():
                broker = str(name).strip()
                if broker not in VALID_BROKERS:
                    results[broker] = {
                        "ok": False,
                        "error": f"broker must be one of: {', '.join(VALID_BROKERS)}",
                    }
                    all_ok = False
                    continue
                armed = bool(want)
                try:
                    result = handler(broker, armed)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
                if not isinstance(result, dict):
                    result = {"ok": bool(result)}
                results[broker] = result
                if not result.get("ok"):
                    all_ok = False
            self._json_response(
                200 if all_ok else 400,
                {"ok": all_ok, "results": results},
            )
            return

        broker = str(data.get("broker", "")).strip()
        if broker not in VALID_BROKERS:
            self._json_response(400, {
                "ok": False,
                "error": f"broker must be one of: {', '.join(VALID_BROKERS)}",
            })
            return

        if "armed" in data:
            armed = bool(data.get("armed"))
        elif "enabled" in data:
            armed = bool(data.get("enabled"))
        else:
            self._json_response(
                400,
                {"ok": False, "error": "Missing boolean field 'armed' (or 'enabled')"},
            )
            return

        try:
            result = handler(broker, armed)
        except Exception as e:
            self._json_response(500, {"ok": False, "error": str(e)})
            return

        if not isinstance(result, dict):
            result = {"ok": bool(result)}
        code = 200 if result.get("ok") else 400
        self._json_response(code, result)


def start_monitor(
    host="127.0.0.1",
    port=8791,
    username="",
    password="",
    controls_enabled=False,
    use_tls=False,
    cursor_agent_enabled=False,
    cursor_agent_token="",
):
    """Start the monitor HTTP(S) server in a daemon thread. Returns (ok, message)."""
    global _server, _thread, _auth_user, _auth_pass, _controls_enabled
    configure_cursor_agent(cursor_agent_enabled, cursor_agent_token)
    global _auth_required, _tls_enabled, _cert_fingerprint
    if _server is not None:
        scheme = "https" if _tls_enabled else "http"
        return True, f"Monitor already running on {scheme}://{host}:{port}"

    host = (host or "127.0.0.1").strip()
    remote_bind = host not in ("127.0.0.1", "localhost", "::1")
    _auth_user = (username or "").strip()
    _auth_pass = password or ""
    _auth_required = bool(remote_bind) or bool(use_tls)
    if _auth_required and not _auth_user:
        return False, "Remote / HTTPS monitor requires User + Pass (refused to start open)"

    # Force TLS for anything reachable beyond loopback
    want_tls = bool(use_tls) or remote_bind
    _tls_enabled = False
    _cert_fingerprint = ""
    if want_tls:
        if monitor_tls is None:
            return False, "HTTPS requested but monitor_tls module missing"
        try:
            cert_file, key_file, fp = monitor_tls.ensure_tls_material()
            _cert_fingerprint = fp
        except Exception as e:
            return False, f"TLS cert setup failed: {e}"

    _controls_enabled = bool(controls_enabled) and bool(_auth_user)
    with _lock:
        _status["controls_enabled"] = _controls_enabled
        _status["tls"] = False
        _status["cert_fingerprint"] = _cert_fingerprint

    try:
        _server = ThreadingHTTPServer((host, int(port)), _Handler)
        if want_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            _server.socket = ctx.wrap_socket(_server.socket, server_side=True)
            _tls_enabled = True
    except OSError as e:
        _server = None
        return False, f"Monitor bind failed: {e}"
    except Exception as e:
        try:
            if _server is not None:
                _server.server_close()
        except Exception:
            pass
        _server = None
        return False, f"Monitor TLS wrap failed: {e}"

    with _lock:
        _status["tls"] = _tls_enabled
        _status["cert_fingerprint"] = _cert_fingerprint

    def _run():
        try:
            _server.serve_forever(poll_interval=0.5)
        except Exception:
            pass

    _thread = threading.Thread(target=_run, name="MA-Monitor", daemon=True)
    _thread.start()
    scheme = "https" if _tls_enabled else "http"
    auth_note = " (Basic Auth on)" if _auth_user else ""
    ctrl_note = " · controls on" if _controls_enabled else ""
    tls_note = f" · pin {_cert_fingerprint[:17]}…" if _cert_fingerprint else ""
    return True, f"Monitor at {scheme}://{host}:{port}/{auth_note}{ctrl_note}{tls_note}"


def stop_monitor():
    global _server, _thread, _tls_enabled
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
        _tls_enabled = False
