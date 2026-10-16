"""Local dashboard for PrivateContext (stdlib http.server only).

A paper journal, not a control panel: warm paper background, serif type,
one accent color, zero external assets (no webfonts, no CDN — local-first
means the page loads nothing from the network). The page polls the same
JSON APIs every 2 seconds while viewing today — live updates without
WebSockets. Views: the day's entry (Ollama recap as prose), where the day
went (app-usage bars), marginalia (#tags), the hours (timeline grouped by
part of day), FTS5 search, and a quiet purge control. Every section is
date-addressable via ?date=YYYY-MM-DD; the masthead flips pages like a
diary (Yesterday / Today / Tomorrow).
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import app_usage, connect, day_recap, init_db, search, tag_usage, timeline

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrivateContext &middot; A Local Journal</title>
<style>
:root{color-scheme:light}
body{font-family:Georgia,'Times New Roman',serif;background:#faf6ee;color:#2a2620;
     margin:0;line-height:1.65}
.page{max-width:720px;margin:0 auto;padding:56px 30px 90px}
.eyebrow{text-align:center;font-size:11px;letter-spacing:4px;text-transform:uppercase;color:#8a7f6a}
h1.dateline{text-align:center;font-weight:400;font-size:3.1em;margin:14px 0 2px;line-height:1.15}
.year{text-align:center;color:#8a7f6a;font-size:1.05em;letter-spacing:2px}
.pagenav{display:flex;justify-content:center;align-items:center;gap:4px;margin:22px 0 6px}
.pagenav button{background:none;border:none;color:#0f766e;font-family:inherit;
     font-size:1.02em;cursor:pointer;padding:6px 10px}
.pagenav button:disabled{color:#cfc4ac;cursor:default}
.pagenav button:not(:disabled):hover{text-decoration:underline}
.pagenav .sep{color:#d8cdb4}
.searchrow{display:flex;gap:8px;justify-content:center;margin:26px 0 6px}
.searchrow input{font-family:inherit;font-size:1em;padding:10px 16px;width:min(420px,68%);
     border:1px solid #e0d5bd;border-radius:999px;background:#fffdf7;color:#2a2620}
.searchrow input::placeholder{color:#a89a7d;font-style:italic}
.searchrow button{font-family:inherit;font-size:1em;padding:10px 22px;border-radius:999px;
     border:1px solid #0f766e;background:#0f766e;color:#faf6ee;cursor:pointer}
.searchrow button:hover{background:#0c655e}
h2.sec{font-weight:400;font-size:1.6em;margin:54px 0 4px;padding-bottom:8px;
     border-bottom:1px solid #e0d5bd}
h2.sec small{font-size:0.62em;color:#8a7f6a}
.entry{font-size:1.1em}
.entry p{margin:0 0 1.1em;line-height:1.8}
.entry p:first-of-type::first-letter{font-size:3.3em;float:left;line-height:0.82;
     padding:6px 10px 0 0;color:#0f766e}
.entry .note{font-size:0.85em;color:#8a7f6a;font-style:italic}
.placeholder{font-style:italic;color:#8a7f6a}
.gentle{text-align:center;font-size:1.15em;margin:64px 0;color:#6b5f4b}
.bar{display:flex;align-items:baseline;gap:12px;margin:9px 0}
.bar .al{flex:none;width:190px;text-align:right;font-size:1.04em;white-space:nowrap;
     overflow:hidden;text-overflow:ellipsis}
.bar .track{flex:1;background:#f0e8d5;border-radius:3px;height:12px}
.bar .fill{background:#0f766e;height:12px;border-radius:3px;opacity:0.88}
.bar .v{flex:none;width:70px;color:#8a7f6a;font-size:0.95em;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{border:1px solid #0f766e;color:#0d5c56;background:#e9f3f1;border-radius:999px;
     padding:3px 13px;font-size:0.98em}
.period{font-style:italic;font-size:1.18em;color:#6b5f4b;margin:30px 0 2px}
.hour-ev{display:flex;gap:18px;padding:8px 0;border-bottom:1px dotted #e0d5bd;font-size:1.02em}
.hour-ev .t{flex:none;width:78px;text-align:right;color:#8a7f6a;font-size:0.92em;
     padding-top:3px;font-variant-numeric:tabular-nums}
.hour-ev .b{min-width:0;flex:1}
.hour-ev .app{font-weight:700}
.hour-ev .dur{color:#8a7f6a;font-size:0.9em}
.hour-ev .clicks{color:#0f766e}
.hour-ev .url{color:#a89a7d;font-size:0.85em;display:block;white-space:nowrap;
     overflow:hidden;text-overflow:ellipsis}
footer{margin-top:70px;text-align:center;color:#8a7f6a;font-size:0.95em}
footer .purgebtn{margin-top:14px;background:none;border:none;font-family:inherit;
     font-size:0.88em;color:#b3a488;cursor:pointer;text-decoration:underline}
footer .purgebtn:hover{color:#8a6f4d}
code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:0.9em}
@media (max-width:560px){
  .page{padding:36px 18px 70px}
  h1.dateline{font-size:2.2em}
  .bar .al{width:118px}
  .hour-ev .t{width:64px}
}
</style></head>
<body>
<div class="page">
<header>
<div class="eyebrow">PrivateContext &middot; A Local Journal</div>
<h1 class="dateline"><span id="dowMonth">&hellip;</span></h1>
<div class="year" id="yearLine"></div>
<nav class="pagenav">
<button id="prevD" onclick="stepDate(-1)" title="previous day">&larr; Yesterday</button>
<span class="sep">&middot;</span>
<button id="todayB" onclick="setDate(todayISO())" title="back to today">Today</button>
<span class="sep">&middot;</span>
<button id="nextD" onclick="stepDate(1)" title="next day">Tomorrow &rarr;</button>
</nav>
</header>
<div class="searchrow">
<input id="q" placeholder="Search the journal&hellip;" autocomplete="off">
<button onclick="doSearch()">Search</button>
</div>
<div id="results"></div>
<div id="emptyDay" class="gentle" style="display:none">Nothing recorded this day.</div>
<section id="entrySec">
<h2 class="sec" id="entryH">Today&rsquo;s entry</h2>
<div id="entry" class="entry"></div>
</section>
<section id="usageSec">
<h2 class="sec" id="usageH">Where the day went</h2>
<div id="usage"></div>
</section>
<section id="tagsec">
<h2 class="sec">Marginalia</h2>
<div id="tagusage" class="chips"></div>
</section>
<section id="hoursSec">
<h2 class="sec">The hours</h2>
<div id="timeline"></div>
</section>
<footer>
<div id="colophon"></div>
<div><button class="purgebtn" onclick="doPurge()">erase everything</button></div>
</footer>
</div>
<script>
async function j(p, opt){
  const r = await fetch(p, opt);
  if(!r.ok){ const e = await r.json().catch(()=>({})); throw new Error(e.error || r.status); }
  return r.json();
}
function todayISO(){ return new Date().toLocaleDateString('en-CA'); }
let curDate = todayISO();
let timer = null;
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function trunc(s, n){ s = s||''; return s.length>n ? s.slice(0,n)+'\u2026' : s; }
function fmtClock(ts){
  return new Date(ts*1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
}
function fmtStamp(ts){
  return new Date(ts*1000).toLocaleString([], {month:'short', day:'numeric',
    hour:'numeric', minute:'2-digit'});
}
/* Part of day for a local hour (0-23). */
function periodName(h){
  if(h>=5 && h<12) return 'Morning';
  if(h>=12 && h<17) return 'Afternoon';
  if(h>=17 && h<22) return 'Evening';
  return 'Night';
}
const PERIOD_ORDER = ['Morning','Afternoon','Evening','Night'];
/* entries: chronological (oldest first). Returns [{name, items}] in
   journal order, skipping empty periods. */
function groupByPeriod(entries){
  const groups = {};
  for(const e of entries){
    const name = periodName(new Date(e.ts*1000).getHours());
    (groups[name] = groups[name] || []).push(e);
  }
  return PERIOD_ORDER.filter(n => groups[n]).map(n => ({name:n, items:groups[n]}));
}
/* Per-entry duration: time until the next event, capped at 10 minutes —
   mirrors the capping in app_usage. entries: chronological. */
function entryDurations(entries, viewingToday){
  const CAP = 600, now = Date.now()/1000;
  return entries.map((e, i) => {
    if(i < entries.length-1) return Math.min(entries[i+1].ts - e.ts, CAP);
    if(viewingToday) return Math.min(Math.max(now - e.ts, 0), CAP);
    return null;
  });
}
function fmtDurEntry(secs){
  secs = Math.round(secs);
  if(secs >= 60) return Math.round(secs/60)+' min';
  return secs+' s';
}
function fmtDurLong(s){
  s = Math.round(s);
  if(s >= 3600){ const h = Math.floor(s/3600), m = Math.round((s%3600)/60); return h+'h '+m+'m'; }
  if(s >= 60) return Math.round(s/60)+'m';
  return '<1m';
}
function shortURL(u){
  try{ const x = new URL(u); return trunc(x.hostname + x.pathname, 60); }
  catch(e){ return trunc(u, 60); }
}
function entryHTML(r){
  if(r && r.summary){
    const paras = r.summary.split('\\n').map(l=>l.replace(/^[-\\u2022*]\\s*/,'').trim()).filter(Boolean);
    let html = paras.map(p=>'<p>'+esc(p)+'</p>').join('');
    if(r.date && r.date !== curDate){
      html += '<div class="note">'+esc(r.label||'Recap')+' &middot; '+esc(r.date)+'</div>';
    }
    return html;
  }
  const ph = (curDate === todayISO())
    ? "Tonight's entry hasn't been written yet. Run <code>pctx summarize</code>."
    : 'No entry was written for this day.';
  return '<p class="placeholder">'+ph+'</p>';
}
function bars(pairs){
  const max = Math.max(1, ...pairs.map(x=>x[1]));
  return pairs.map(pair =>
    '<div class="bar"><span class="al">'+esc(pair[0])+'</span>'+
    '<div class="track"><div class="fill" style="width:'+(100*pair[1]/max).toFixed(1)+'%"></div></div>'+
    '<span class="v">'+fmtDurLong(pair[1])+'</span></div>').join('');
}
function chips(pairs){
  return pairs.slice(0,20).map(pair=>'<span class="chip">#'+esc(pair[0])+'</span>').join('');
}
function hourEntryHTML(e, dur){
  const idle = (e.duration_ms != null)
    ? ' <span class="dur">('+Math.round(e.duration_ms/1000)+'s idle)</span>' : '';
  const durS = (dur != null && e.duration_ms == null)
    ? ' <span class="dur">('+fmtDurEntry(dur)+')</span>' : '';
  let extra = '';
  if(e.clicks) extra = ' <span class="clicks">&middot; '+e.clicks+' clicks</span>';
  else if(e.type === 'click') extra = ' <span class="clicks">&middot; click</span>';
  const urlS = e.url ? '<span class="url">'+esc(shortURL(e.url))+'</span>' : '';
  return '<div class="hour-ev"><span class="t">'+fmtClock(e.ts)+'</span>'+
    '<span class="b"><span class="app">'+esc(e.app||'(unknown)')+'</span> &mdash; '+
    esc(trunc(e.title, 80))+extra+durS+idle+urlS+'</span></div>';
}
function hoursHTML(tl, viewingToday){
  const chrono = tl.slice().reverse();  // API is newest-first; the journal reads morning to night
  const durs = entryDurations(chrono, viewingToday);
  return groupByPeriod(chrono).map((g, gi) =>
    '<div class="period">'+g.name+'</div>'+
    g.items.map(e => hourEntryHTML(e, durs[chrono.indexOf(e)])).join('')
  ).join('');
}
function renderMasthead(){
  const d = new Date(curDate+'T12:00:00');
  document.getElementById('dowMonth').textContent =
    d.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric'});
  document.getElementById('yearLine').textContent = d.getFullYear();
  document.getElementById('entryH').textContent =
    (curDate === todayISO()) ? "Today\u2019s entry" : 'The entry';
  document.title = 'PrivateContext \u00b7 ' +
    d.toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'});
}
async function loadEntry(){
  try{
    // Viewing today: server falls back to yesterday's entry. A picked
    // past date shows exactly that date's summary, no fallback.
    const url = (curDate === todayISO()) ? '/api/recap' : '/api/recap?date='+curDate;
    const r = await j(url);
    document.getElementById('entry').innerHTML = entryHTML(r);
    return !!(r && r.summary);
  }catch(e){ document.getElementById('entry').innerHTML = entryHTML(null); return false; }
}
async function refresh(){
  try{
    const viewingToday = (curDate === todayISO());
    const tl = await j('/api/timeline?limit=200&date='+curDate);
    const u = await j('/api/usage?date='+curDate);
    const t = await j('/api/tagusage?date='+curDate);
    const hasEntry = await loadEntry();
    const hasEvents = tl.length > 0;
    document.getElementById('timeline').innerHTML =
      hasEvents ? hoursHTML(tl, viewingToday) : '';
    document.getElementById('usage').innerHTML = bars(u);
    document.getElementById('tagusage').innerHTML = chips(t);
    document.getElementById('usageSec').style.display = u.length ? '' : 'none';
    document.getElementById('tagsec').style.display = t.length ? '' : 'none';
    document.getElementById('hoursSec').style.display = hasEvents ? '' : 'none';
    document.getElementById('emptyDay').style.display =
      (!hasEvents && !hasEntry) ? '' : 'none';
    document.getElementById('colophon').textContent =
      tl.length+' event'+(tl.length==1?'':'s')+' \u00b7 kept on this machine \u00b7 nothing uploaded';
    renderMasthead();
  }catch(e){
    document.getElementById('colophon').textContent = 'error: '+e;
  }
}
function setDate(d){
  if(d > todayISO()) d = todayISO();  // no peeking into the future
  curDate = d;
  document.getElementById('nextD').disabled = (d >= todayISO());
  if(timer){ clearInterval(timer); timer = null; }
  if(d === todayISO()){ timer = setInterval(refresh, 2000); }
  refresh();
}
function stepDate(n){
  const d = new Date(curDate+'T12:00:00');
  d.setDate(d.getDate()+n);
  setDate(d.toLocaleDateString('en-CA'));
}
async function doSearch(){
  const q = document.getElementById('q').value;
  if(!q.trim()){ document.getElementById('results').innerHTML=''; return; }
  const r = await j('/api/search?q='+encodeURIComponent(q));
  document.getElementById('results').innerHTML =
    '<h2 class="sec">From the margins <small>'+r.length+' result'+(r.length==1?'':'s')+'</small></h2>'+
    r.map(e => '<div class="hour-ev"><span class="t">'+fmtStamp(e.ts)+'</span>'+
      '<span class="b"><span class="app">'+esc(e.app||'(unknown)')+'</span> &mdash; '+
      esc(trunc(e.title, 80))+'</span></div>').join('');
}
async function doPurge(){
  if(!confirm('Delete ALL captured data? This cannot be undone.')) return;
  const r = await j('/api/purge', {method:'POST'});
  alert('Deleted '+r.deleted+' events.');
  document.getElementById('results').innerHTML='';
  refresh();
}
document.getElementById('q').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });
setDate(todayISO());
</script>
</body></html>
"""


def _date_param(qs: dict) -> str | None:
    """Validated ?date=YYYY-MM-DD, or None when absent. Raises ValueError
    on bad format (caller turns it into HTTP 400)."""
    vals = qs.get("date", [])
    if not vals:
        return None
    return date.fromisoformat(vals[0]).isoformat()


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
        elif u.path in ("/api/timeline", "/api/usage",
                          "/api/tagusage", "/api/recap"):
            qs = parse_qs(u.query)
            try:
                d = _date_param(qs)
            except ValueError:
                self._json({"error": "bad date — use YYYY-MM-DD"}, 400)
                return
            if u.path == "/api/timeline":
                limit = int(qs.get("limit", ["200"])[0])
                self._json(self._with_db(
                    lambda db: timeline(db, min(limit, 1000), d)))
            elif u.path == "/api/usage":
                self._json(self._with_db(
                    lambda db: app_usage(db, date_str=d)))
            elif u.path == "/api/tagusage":
                self._json(self._with_db(
                    lambda db: tag_usage(db, date_str=d)))
            else:
                self._json(self._with_db(lambda db: day_recap(db, d)))
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
