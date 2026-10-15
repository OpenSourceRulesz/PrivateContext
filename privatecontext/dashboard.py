"""Local dashboard for PrivateContext (stdlib http.server only).

Single page on 127.0.0.1:18790. The page polls /api/timeline and /api/usage
every 2 seconds — live updates without WebSockets. Views: today's timeline
(reverse-chron), app-usage bars, FTS5 search, and a big Purge button.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import app_usage, connect, day_recap, init_db, search, tag_usage, timeline

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrivateContext</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,-apple-system,sans-serif;background:#141414;color:#e8e8e8;
     max-width:920px;margin:0 auto;padding:24px;line-height:1.45}
h1 small{color:#888;font-weight:normal}
#status{color:#888;font-size:0.9em}
input#q{width:60%;padding:8px;font-size:1em;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
button{padding:8px 14px;font-size:1em;border-radius:6px;border:1px solid #444;background:#2a2a2a;color:#eee;cursor:pointer}
button:hover{background:#333}
.ev{padding:6px 0;border-bottom:1px solid #222;font-size:0.95em}
.ev .t{color:#888;font-variant-numeric:tabular-nums}
.ev .ty{display:inline-block;min-width:52px;color:#9cf;font-size:0.85em}
.ev .c{color:#8a8;font-size:0.85em}
.bar{display:flex;align-items:center;gap:10px;margin:6px 0}
.bar .al{width:180px;text-align:right;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{flex:1;background:#222;border-radius:4px;height:14px}
.bar .fill{background:#4a9;height:14px;border-radius:4px}
.bar .v{width:60px;color:#888;font-size:0.9em}
a{color:#9cf}
#purge{background:#5a1d1d;border-color:#a33;margin-top:24px;font-size:1.1em;padding:12px 22px}
#purge:hover{background:#7a2626}
.card{background:#1d2a1d;border:1px solid #3a5a3a;border-radius:8px;padding:14px 18px;margin:14px 0}
.card h3{margin:0 0 8px 0;color:#8f8}
.card small{color:#888;font-weight:normal}
.card p{margin:0;white-space:pre-wrap}
</style></head>
<body>
<h1>&#9673; PrivateContext <small>v0 &middot; local only</small></h1>
<p id="status">loading&hellip;</p>
<div id="recap"></div>
<div>
<input id="q" placeholder="Search everything: servicenow, eduroam, zoom&hellip;" autocomplete="off">
<button onclick="doSearch()">Search</button>
</div>
<div id="results"></div>
<h2>Today &mdash; app usage</h2>
<div id="usage"></div>
<div id="tagsec"><h2>Today &mdash; by tag</h2><div id="tagusage"></div></div>
<h2>Timeline (newest first)</h2>
<div id="timeline"></div>
<hr>
<button id="purge" onclick="doPurge()">&#128465; Purge all data</button>
<script>
async function j(p, opt){ const r = await fetch(p, opt); return r.json(); }
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtT(ts){ return new Date(ts*1000).toLocaleTimeString(); }
function evHTML(e){
  return '<div class="ev"><span class="t">'+fmtT(e.ts)+'</span> '+
    '<span class="ty">'+esc(e.type)+'</span> '+
    '<b>'+esc(e.app)+'</b> &mdash; '+esc(e.title)+
    (e.url ? ' <a href="'+esc(e.url)+'">link</a>' : '')+
    (e.tags ? ' <span class="c">['+esc(e.tags)+']</span>' : '')+
    (e.clicks ? ' <span class="c">&times;'+e.clicks+' &middot; '+fmtT(e.ts_end)+' &rarr; '+fmtT(e.ts)+'</span>'
              : (e.x!=null ? ' <span class="c">click@'+e.x+','+e.y+'</span>' : ''))+
    (e.duration_ms!=null ? ' <span class="c">'+Math.round(e.duration_ms/1000)+'s idle</span>' : '')+
    '</div>';
}
function bars(pairs){
  const max = Math.max(1, ...pairs.map(x=>x[1]));
  return pairs.map(pair =>
    '<div class="bar"><span class="al">'+esc(pair[0])+'</span>'+
    '<div class="track"><div class="fill" style="width:'+(100*pair[1]/max).toFixed(1)+'%"></div></div>'+
    '<span class="v">'+(pair[1]/60).toFixed(0)+'m</span></div>').join('');
}
async function loadRecap(){
  try{
    const r = await j('/api/recap');
    if(r && r.summary){
      document.getElementById('recap').innerHTML =
        '<div class="card"><h3>'+esc(r.label)+' <small>'+esc(r.date)+'</small></h3>'+
        '<p>'+esc(r.summary)+'</p></div>';
    }
  }catch(e){ /* no summary yet — no card, no nagging */ }
}
async function refresh(){
  try{
    const tl = await j('/api/timeline?limit=200');
    const u = await j('/api/usage');
    const t = await j('/api/tagusage');
    document.getElementById('timeline').innerHTML = tl.map(evHTML).join('');
    document.getElementById('usage').innerHTML = bars(u);
    const tagsec = document.getElementById('tagsec');
    if(t.length){ tagsec.style.display=''; document.getElementById('tagusage').innerHTML = bars(t.slice(0,10)); }
    else { tagsec.style.display='none'; }
    document.getElementById('status').textContent =
      tl.length+' events today · updated '+new Date().toLocaleTimeString()+' (auto-refreshes every 2s)';
  }catch(e){ document.getElementById('status').textContent = 'error: '+e; }
}
async function doSearch(){
  const q = document.getElementById('q').value;
  const r = await j('/api/search?q='+encodeURIComponent(q));
  document.getElementById('results').innerHTML =
    '<h3>'+r.length+' result'+(r.length==1?'':'s')+'</h3>'+r.map(evHTML).join('');
}
async function doPurge(){
  if(!confirm('Delete ALL captured data? This cannot be undone.')) return;
  const r = await j('/api/purge', {method:'POST'});
  alert('Deleted '+r.deleted+' events.');
  document.getElementById('results').innerHTML='';
  refresh();
}
document.getElementById('q').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });
setInterval(refresh, 2000);
loadRecap();
refresh();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PrivateContext/0.1"

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _with_db(self, fn):
        with self.server.lock:
            return fn(self.server.db)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/timeline":
            qs = parse_qs(u.query)
            limit = int(qs.get("limit", ["200"])[0])
            self._json(self._with_db(lambda db: timeline(db, min(limit, 1000))))
        elif u.path == "/api/usage":
            self._json(self._with_db(app_usage))
        elif u.path == "/api/tagusage":
            self._json(self._with_db(tag_usage))
        elif u.path == "/api/recap":
            self._json(self._with_db(day_recap))
        elif u.path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0]
            self._json(self._with_db(lambda db: search(db, q)))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/api/purge":
            def _purge(db):
                n = db.execute("DELETE FROM events").rowcount
                db.execute("DELETE FROM days")
                db.commit()
                db.execute("VACUUM")
                return {"deleted": n}
            self._json(self._with_db(_purge))
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args) -> None:  # keep the console quiet
        pass


def serve(db_path: str | None = None, port: int = 18790) -> None:
    db = connect(db_path)
    init_db(db)
    # On some Windows machines the default port is blocked (WinError 10013)
    # or already in use — walk forward instead of dying with a traceback.
    server = None
    for try_port in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", try_port), Handler)
            port = try_port
            break
        except OSError:
            continue
    if server is None:
        print(f"[privatecontext] could not bind ports {port}–{port + 9} on "
              f"127.0.0.1. Try: pctx dashboard --port 8888",
              file=sys.stderr)
        raise SystemExit(1)
    server.db = db  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    print(f"[privatecontext] dashboard at http://127.0.0.1:{port} — Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[privatecontext] dashboard stopped.")
