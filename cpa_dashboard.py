#!/usr/bin/env python3
"""
CPA Auth Dashboard v2 - Enhanced quota visibility for Grok auth pool.
Web UI with: pool health gauge, per-account ok/fail bars, pending/quarantine views.
"""
import json, os, re, sys, time, urllib.request
from collections import Counter
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CPA_DIR = Path("/vol1/1000/openzl/cpa")
MGMT_URL = "http://127.0.0.1:8317/v0/management/auth-files"
HOST, PORT = "0.0.0.0", 8318

def load_mgmt_key():
    for line in (CPA_DIR / ".secrets.env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() == "CPA_MGMT_KEY":
                return v.strip().strip('"').strip("'")
    return ""

def fetch_auths(mgmt_key):
    req = urllib.request.Request(MGMT_URL, headers={
        "Authorization": f"Bearer {mgmt_key}", "User-Agent": "cpa-dashboard/1.0"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    files = [f for f in data.get("files", []) if f.get("id", "").startswith("xai-")]
    # enrich with file stats from disk
    auth_dir = CPA_DIR / "auths"
    for f in files:
        fname = f.get("id", "") + ".json"
        fp = auth_dir / fname
        if fp.exists():
            age = time.time() - fp.stat().st_mtime
            f["file_age_h"] = round(age / 3600, 1)
        else:
            f["file_age_h"] = -1
    return files

def count_pending_quarantine():
    pend = len(list((CPA_DIR / "auths_pending").glob("xai-*.json"))) if (CPA_DIR / "auths_pending").exists() else 0
    quar = len(list((CPA_DIR / "auths_quarantine").glob("xai-*.json"))) if (CPA_DIR / "auths_quarantine").exists() else 0
    return pend, quar

def build_stats(files):
    c = Counter()
    for f in files:
        if f.get("disabled"): c["disabled"] += 1
        elif f.get("unavailable"): c["unavailable"] += 1
        else:
            msg = (f.get("status_message") or "").lower()
            if "free-usage-exhausted" in msg or "spending" in msg: c["spending"] += 1
            elif "permission-denied" in msg: c["perm_denied"] += 1
            elif "rate" in msg: c["rate_limit"] += 1
            elif msg: c["other"] += 1
            else: c["healthy"] += 1
    # calculate totals
    c["total"] = len(files)
    c["active"] = c["total"] - c.get("disabled", 0) - c.get("unavailable", 0)
    return c

def render_html(files, stats, ts, pending, quarantine):
    total = stats["total"]
    healthy = stats.get("healthy", 0)
    pct = round(healthy / max(total, 1) * 100)
    disabled = stats.get("disabled", 0)
    spending = stats.get("spending", 0)
    perm_denied = stats.get("perm_denied", 0)
    
    rows = ""
    for f in sorted(files, key=lambda x: x.get("email", "")):
        e = f.get("email", "?")[:40]
        status = "active"
        if f.get("disabled"): status = "disabled"
        elif f.get("unavailable"): status = "unavailable"
        ok = f.get("success", 0)
        fail = f.get("failed", 0)
        total_req = ok + fail
        msg = (f.get("status_message") or "")[:80]
        age = f.get("file_age_h", 0)
        
        if f.get("disabled"): cls = "row-disabled"
        elif f.get("unavailable") or "exhausted" in msg.lower(): cls = "row-bad"
        elif "denied" in msg.lower(): cls = "row-bad"
        elif fail > 10 and fail >= ok * 2: cls = "row-warn"
        elif ok > 0: cls = "row-ok"
        else: cls = "row-new"
        
        bar_total = max(total_req, 1)
        ok_w = round(ok / bar_total * 100)
        fail_w = round(fail / bar_total * 100)
        
        rows += f'''<tr class="{cls}">
  <td class="email">{e}</td>
  <td><span class="tag t-{status}">{status}</span></td>
  <td class="bar-cell">
    <div class="mini-bar">
      <div class="bar-ok" style="width:{ok_w}%"></div>
      <div class="bar-fail" style="width:{fail_w}%"></div>
    </div>
  </td>
  <td class="num">{ok}</td>
  <td class="num">{fail}</td>
  <td class="num">{age}h</td>
  <td class="msg">{msg}</td>
</tr>\n'''

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPA Pool Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}}
h1{{font-size:22px;margin-bottom:4px;color:#f0f6fc;display:flex;align-items:center;gap:10px}}
h1 small{{font-size:13px;color:#8b949e;font-weight:400}}
.subtitle{{color:#8b949e;font-size:13px;margin-bottom:20px}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;min-width:110px;flex:1}}
.card .num{{font-size:26px;font-weight:700;display:block}}
.card .label{{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}}
.card.health .num{{color:#3fb950}}
.card.total .num{{color:#58a6ff}}
.card.exhausted .num{{color:#d29922}}
.card.bad .num{{color:#f85149}}
.card.pending .num{{color:#6e7681}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#21262d;padding:6px 10px;text-align:left;font-weight:600;border-bottom:2px solid #30363d;position:sticky;top:0;color:#8b949e;text-transform:uppercase;font-size:10px;letter-spacing:.5px}}
td{{padding:4px 10px;border-bottom:1px solid #21262d}}
.scroll{{max-height:70vh;overflow-y:auto;border:1px solid #30363d;border-radius:8px}}
.email{{font-family:'SF Mono',monospace;font-size:11px}}
.tag{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase}}
.t-active{{background:#003d20;color:#3fb950}}
.t-disabled{{background:#3d0000;color:#f85149}}
.t-unavailable{{background:#3d1f00;color:#d29922}}
.bar-cell{{width:120px}}
.mini-bar{{display:flex;height:6px;border-radius:3px;overflow:hidden;background:#21262d}}
.bar-ok{{background:#3fb950;transition:width .3s}}
.bar-fail{{background:#f85149;transition:width .3s}}
.num{{font-family:'SF Mono',monospace;font-size:11px;text-align:right}}
.msg{{color:#8b949e;font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.row-disabled{{background:#1c1010}} .row-disabled .email{{color:#f85149}}
.row-bad{{background:#1c1310}} .row-bad .email{{color:#f0883e}}
.row-warn{{background:#1c1a10}} .row-warn .email{{color:#d29922}}
.row-ok .email{{color:#7ee787}}
.row-new .email{{color:#484f58}}
::-webkit-scrollbar{{width:6px}}
::-webkit-scrollbar-track{{background:#161b22}}
::-webkit-scrollbar-thumb{{background:#30363d;border-radius:3px}}
.footer{{color:#8b949e;font-size:11px;margin-top:8px;display:flex;justify-content:space-between}}
.footer a{{color:#58a6ff;text-decoration:none}}
.gauge{{display:flex;align-items:center;gap:12px;padding:10px 0}}
.gauge-ring{{width:60px;height:60px;border-radius:50%;background:conic-gradient(#3fb950 {pct}%, #30363d {pct}%);display:flex;align-items:center;justify-content:center}}
.gauge-ring-inner{{width:48px;height:48px;border-radius:50%;background:#161b22;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:#f0f6fc}}
.gauge-info{{font-size:12px;color:#8b949e;line-height:1.6}}
.gauge-info strong{{color:#c9d1d9}}
</style>
<meta http-equiv="refresh" content="30">
</head><body>
<h1>🛡 CPA Grok Pool <small>qd.523615668.xyz</small></h1>
<p class="subtitle">📡 Last: {ts} &middot; 🔄 Auto-refresh 30s</p>

<div class="summary">
  <div class="card health"><span class="num">{healthy}</span><span class="label">Healthy</span></div>
  <div class="card total"><span class="num">{total}</span><span class="label">Total</span></div>
  <div class="card pending"><span class="num">{spending}</span><span class="label">Exhausted</span></div>
  <div class="card pending"><span class="num">{pending}</span><span class="label">Pending</span></div>
  <div class="card pending"><span class="num">{quarantine}</span><span class="label">Quarantine</span></div>
  <div class="card bad"><span class="num">{disabled}</span><span class="label">Disabled</span></div>
</div>

<div class="gauge">
  <div class="gauge-ring"><div class="gauge-ring-inner">{pct}%</div></div>
  <div class="gauge-info">
    <strong>{healthy}/{total}</strong> 可用 &middot;
    <strong>{stats.get('active',0)}</strong> active &middot;
    <strong>{disabled}</strong> disabled &middot;
    <strong>{spending}</strong> exhausted &middot;
    <strong>{perm_denied}</strong> auth denied
  </div>
</div>

<div class="scroll">
<table>
  <thead><tr>
    <th>Account</th><th>Status</th><th>Ok/Fail</th><th>✓</th><th>✗</th><th>Age</th><th>Message</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table></div>

<div class="footer">
  <span>⚡ {total} accounts &middot; {pct}% health rate</span>
  <span><a href="https://cpa.523615668.xyz">CPA API</a> · <a href="https://cpa.523615668.xyz/management.html">CPA Admin</a></span>
</div>
</body></html>"""

class Handler(BaseHTTPRequestHandler):
    mgmt_key = ""
    cache_html = "<html><body>loading...</body></html>"
    cache_time = 0
    
    def do_GET(self):
        now = time.time()
        if self.path in ("/", "/dashboard"):
            if now - self.cache_time > 15:
                try:
                    files = fetch_auths(self.mgmt_key)
                    stats = build_stats(files)
                    pending, quarantine = count_pending_quarantine()
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.cache_html = render_html(files, stats, ts, pending, quarantine)
                    self.cache_time = now
                except Exception as e:
                    self.cache_html = f"<html><body><h2>⚠ Error</h2><pre>{e}</pre></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.cache_html.encode())
        elif self.path == "/api/auths":
            try:
                files = fetch_auths(self.mgmt_key)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"files": files, "count": len(files)}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

def main():
    key = load_mgmt_key()
    if not key:
        print("ERROR: CPA_MGMT_KEY not found")
        sys.exit(1)
    Handler.mgmt_key = key
    server = HTTPServer((HOST, PORT), Handler)
    print(f"✅ CPA Dashboard running: http://{HOST}:{PORT}")
    print(f"   https://qd.523615668.xyz")
    print(f"   https://dash.523615668.xyz")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.server_close()

if __name__ == "__main__":
    main()
