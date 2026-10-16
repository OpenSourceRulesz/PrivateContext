"""Local dashboard for PrivateContext (stdlib http.server only).

A paper journal, not a control panel: warm paper background, serif type,
one accent color, zero external assets (no webfonts, no CDN — local-first
means the page loads nothing from the network). The page polls the same
JSON APIs every 2 seconds while viewing today — live updates without
WebSockets. Views: the day's summary (Ollama recap as prose, with quiet
regenerate/remove controls), where the day went (app-usage bars),
marginalia (#tags), the hours (timeline grouped by part of day), a Notes
tab for handwritten per-day notes (which feed into summaries), manual
stickers to mark days that mattered, FTS5 search, and a quiet purge
control. Every section is date-addressable via ?date=YYYY-MM-DD; the
masthead flips pages like a diary (Yesterday / Today / Tomorrow). A
Journal/Diary theme toggle (persisted server-side) swaps the monotone
paper skin for a colorful one — same HTML, CSS variables only.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import (app_usage, connect, day_recap, default_db_path, get_accent,
                 get_note, get_sticker, get_theme, init_db, remove_day_entry,
                 search, set_accent, set_note, set_sticker, set_theme,
                 tag_usage, timeline)
from .summarize import (DEFAULT_MODEL, ollama_hint, ollama_reachable,
                        summarize_day)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrivateContext &middot; A Local Journal</title>
<style>
:root{color-scheme:light;
  /* Journal (default): monotone paper. Diary overrides below. */
  --paper:#faf6ee; --ink:#2a2620; --muted:#8a7f6a; --muted2:#6b5f4b;
  --accent:#0f766e; --accent-hi:#0c655e; --accent-ink:#0d5c56;
  --rule:#e0d5bd; --card:#fffdf7; --track:#f0e8d5; --chipbg:#e9f3f1;
  --faint:#cfc4ac; --sepline:#d8cdb4; --placeholder:#a89a7d;
  --purge:#b3a488; --purge-hi:#8a6f4d; --radius:3px;
}
body[data-theme="diary"]{
  /* Diary: colorful, playful — same HTML, candy accents, rounder shapes.
     Each accent below also tints the page background itself (paper, cards,
     rules, tracks). Journal mode never reads data-accent, so its monotone
     paper is untouched — verify no bare [data-accent] selector exists. */
  --paper:#fbe9f1; --ink:#3b2a1e; --muted:#a3805c; --muted2:#7d6547;
  --accent:#d6457b; --accent-hi:#b83a6c; --accent-ink:#a52c5c;
  --rule:#f0cfdf; --card:#fff5fa; --track:#f6d9e8; --chipbg:#fbe3ee;
  --faint:#dcc39a; --sepline:#e3cda6; --placeholder:#c09a72;
  --purge:#c4a075; --purge-hi:#9a6f45; --radius:14px;
}
/* Diary punch-up: accent-colored eyebrow and section rules so it reads
   visibly more playful than the monotone journal. */
body[data-theme="diary"] .eyebrow{color:var(--accent)}
body[data-theme="diary"] h2.sec{border-bottom:2px solid var(--accent)}
/* Per-accent full-page tints. --ink stays dark in all five for
   readability; chips and cards tint harmoniously with the paper. */
body[data-theme="diary"][data-accent="raspberry"]{
  --paper:#fbe9f1;--card:#fff5fa;--rule:#f0cfdf;--track:#f6d9e8;--chipbg:#fbe3ee;}
body[data-theme="diary"][data-accent="ocean"]{
  --accent:#2563eb;--accent-hi:#1d4ed8;--accent-ink:#1e40af;
  --paper:#e9f1fd;--card:#f5f9fe;--rule:#c9dafb;--track:#d9e7fd;--chipbg:#e4edfd;}
body[data-theme="diary"][data-accent="forest"]{
  --accent:#16a34a;--accent-hi:#12853c;--accent-ink:#166534;
  --paper:#e9f5ec;--card:#f4faf6;--rule:#c9e8d4;--track:#daf0e3;--chipbg:#e3f4e9;}
body[data-theme="diary"][data-accent="plum"]{
  --accent:#9333ea;--accent-hi:#7d2bd6;--accent-ink:#6b21a8;
  --paper:#f1eafb;--card:#f8f3fe;--rule:#dccdf5;--track:#e9ddf9;--chipbg:#f1e5fd;}
body[data-theme="diary"][data-accent="tangerine"]{
  --accent:#ea580c;--accent-hi:#cf4e0a;--accent-ink:#c2410c;
  --paper:#fdf0e2;--card:#fef8f1;--rule:#f4dcba;--track:#f9e6cc;--chipbg:#fdeee1;}
body{font-family:Georgia,'Times New Roman',serif;background:var(--paper);color:var(--ink);
     margin:0;line-height:1.65}
.page{max-width:720px;margin:0 auto;padding:56px 30px 90px;position:relative}
.eyebrow{text-align:center;font-size:11px;letter-spacing:4px;text-transform:uppercase;color:var(--muted)}
h1.dateline{text-align:center;font-weight:400;font-size:3.1em;margin:14px 0 2px;line-height:1.15}
#stickerBig{font-size:0.72em;vertical-align:baseline;margin-left:12px;cursor:pointer}
.year{text-align:center;color:var(--muted);font-size:1.05em;letter-spacing:2px}
.pagenav{display:flex;justify-content:center;align-items:center;gap:4px;margin:22px 0 6px}
.pagenav button{background:none;border:none;color:var(--accent);font-family:inherit;
     font-size:1.02em;cursor:pointer;padding:6px 10px}
.pagenav button:disabled{color:var(--faint);cursor:default}
.pagenav button:not(:disabled):hover{text-decoration:underline}
.pagenav .sep{color:var(--sepline)}
.stickrow{text-align:center;margin:12px 0 0;min-height:30px}
.stickerbtn{background:none;border:none;font-family:inherit;font-size:0.92em;
     color:var(--muted);cursor:pointer}
.stickerbtn:hover{color:var(--accent)}
.picker{display:flex;justify-content:center;gap:6px;margin-top:10px;flex-wrap:wrap}
.picker button{background:var(--card);border:1px solid var(--rule);border-radius:var(--radius);
     font-size:1.7em;padding:6px 10px;cursor:pointer;line-height:1.2}
.picker button:hover{border-color:var(--accent)}
.picker button.clear{font-size:0.85em;font-family:inherit;color:var(--muted);padding:6px 12px}
.themerow{position:absolute;top:20px;right:26px;margin:0;font-size:0.92em;
     color:var(--muted);text-align:right}
.themerow button{background:none;border:none;font-family:inherit;font-size:1em;
     color:var(--muted);cursor:pointer;padding:4px 8px}
.themerow button.on{color:var(--accent);font-weight:700}
.themerow button:hover{color:var(--accent)}
.themerow .sep{color:var(--sepline)}
.themerow .accentdots{margin-left:10px;white-space:nowrap}
.themerow .accentdots button{width:15px;height:15px;border-radius:50%;border:none;
     padding:0;margin:0 3px;cursor:pointer;vertical-align:-1px}
.themerow .accentdots button.on{outline:2px solid var(--ink);outline-offset:2px}
.tabs{display:flex;justify-content:center;gap:6px;margin:20px 0 0}
.tabs button{background:none;border:none;font-family:inherit;font-size:1.05em;
     color:var(--muted);cursor:pointer;padding:8px 20px;border-bottom:2px solid transparent}
.tabs button.on{color:var(--accent);border-bottom-color:var(--accent)}
.tabs button:hover{color:var(--accent)}
.searchrow{display:flex;gap:8px;justify-content:center;margin:26px 0 6px}
.searchrow input{font-family:inherit;font-size:1em;padding:10px 16px;width:min(420px,68%);
     border:1px solid var(--rule);border-radius:999px;background:var(--card);color:var(--ink)}
.searchrow input::placeholder{color:var(--placeholder);font-style:italic}
.searchrow button{font-family:inherit;font-size:1em;padding:10px 22px;border-radius:999px;
     border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.searchrow button:hover{background:var(--accent-hi)}
h2.sec{font-weight:400;font-size:1.6em;margin:54px 0 4px;padding-bottom:8px;
     border-bottom:1px solid var(--rule)}
h2.sec small{font-size:0.62em;color:var(--muted)}
.entry{font-size:1.1em}
.entry p{margin:0 0 1.1em;line-height:1.8}
.entry p:first-of-type::first-letter{font-size:3.3em;float:left;line-height:0.82;
     padding:6px 10px 0 0;color:var(--accent)}
.entry .note{font-size:0.85em;color:var(--muted);font-style:italic}
.entryctl{font-size:0.52em;vertical-align:middle;margin-left:16px;white-space:nowrap}
.entryctl button{background:none;border:none;font-family:inherit;font-size:1em;
     color:var(--muted);cursor:pointer;padding:4px 8px}
.entryctl button:hover{color:var(--accent)}
.entryctl button.danger:hover{color:#a33}
.writing{font-style:italic;color:var(--muted);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.55}50%{opacity:1}}
.placeholder{font-style:italic;color:var(--muted)}
.gentle{text-align:center;font-size:1.15em;margin:64px 0;color:var(--muted2)}
.bar{display:flex;align-items:baseline;gap:12px;margin:9px 0}
.bar .al{flex:none;width:190px;text-align:right;font-size:1.04em;white-space:nowrap;
     overflow:hidden;text-overflow:ellipsis}
.bar .track{flex:1;background:var(--track);border-radius:var(--radius);height:12px}
.bar .fill{background:var(--accent);height:12px;border-radius:var(--radius);opacity:0.88}
.bar .v{flex:none;width:70px;color:var(--muted);font-size:0.95em;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{border:1px solid var(--accent);color:var(--accent-ink);background:var(--chipbg);border-radius:999px;
     padding:3px 13px;font-size:0.98em}
/* "The hours" period headers are toggle buttons (chevron ▾/▸):
   collapsing hides the period's entries so content below pulls up. */
.periodhead{background:none;border:none;font-family:inherit;font-style:italic;
     font-size:1.18em;color:var(--muted2);margin:30px 0 2px;padding:0;
     cursor:pointer;display:flex;align-items:center;gap:10px;width:100%;text-align:left}
.periodhead:hover{color:var(--accent)}
.periodhead .chev{font-style:normal;font-size:0.8em;color:var(--accent);
     display:inline-block;width:1.1em;flex:none}
.periodbody.collapsed{display:none}
.hour-ev{display:flex;gap:18px;padding:8px 0;border-bottom:1px dotted var(--rule);font-size:1.02em}
.hour-ev .t{flex:none;width:78px;text-align:right;color:var(--muted);font-size:0.92em;
     padding-top:3px;font-variant-numeric:tabular-nums}
.hour-ev .b{min-width:0;flex:1}
.hour-ev .app{font-weight:700}
.hour-ev .dur{color:var(--muted);font-size:0.9em}
.hour-ev .clicks{color:var(--accent)}
.hour-ev .url{color:var(--placeholder);font-size:0.85em;display:block;white-space:nowrap;
     overflow:hidden;text-overflow:ellipsis}
#noteBody{width:100%;min-height:300px;font-family:inherit;font-size:1.05em;line-height:1.7;
     padding:18px 20px;border:1px solid var(--rule);border-radius:var(--radius);
     background:var(--card);color:var(--ink);resize:vertical;margin-top:12px}
#noteBody::placeholder{color:var(--placeholder);font-style:italic}
.noterow{display:flex;align-items:center;gap:14px;margin-top:12px}
.noterow button{font-family:inherit;font-size:1em;padding:9px 24px;border-radius:999px;
     border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.noterow button:hover{background:var(--accent-hi)}
.notesaved{color:var(--muted);font-style:italic;font-size:0.95em}
footer{margin-top:70px;text-align:center;color:var(--muted);font-size:0.95em}
footer .purgebtn{margin-top:14px;background:none;border:none;font-family:inherit;
     font-size:0.88em;color:var(--purge);cursor:pointer;text-decoration:underline}
footer .purgebtn:hover{color:var(--purge-hi)}
code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:0.9em}
@media (max-width:560px){
  .page{padding:36px 18px 70px}
  .themerow{position:static;text-align:center;margin:14px 0 0}
  h1.dateline{font-size:2.2em}
  .bar .al{width:118px}
  .hour-ev .t{width:64px}
}
</style></head>
<body>
<div class="page">
<header>
<div class="eyebrow" id="eyebrow">PrivateContext &middot; A Local Journal</div>
<h1 class="dateline"><span id="dowMonth">&hellip;</span><span id="stickerBig" title="mark this day" onclick="togglePicker(event)"></span></h1>
<div class="year" id="yearLine"></div>
<nav class="pagenav">
<button id="prevD" onclick="stepDate(-1)" title="previous day">&larr; Yesterday</button>
<span class="sep">&middot;</span>
<button id="todayB" onclick="setDate(todayISO())" title="back to today">Today</button>
<span class="sep">&middot;</span>
<button id="nextD" onclick="stepDate(1)" title="next day">Tomorrow &rarr;</button>
</nav>
<div class="stickrow"><button id="stickerBtn" class="stickerbtn" onclick="togglePicker(event)">&#10022; mark this day</button>
<div id="picker" class="picker" style="display:none"></div></div>
<div class="themerow"><button id="themeJournal" onclick="setTheme('journal')">Journal</button><span class="sep"> &middot; </span><button id="themeDiary" onclick="setTheme('diary')">Diary</button><span id="accentDots" class="accentdots" style="display:none"><!-- one row of dots covers the need; a Settings tab earns its place at 3+ settings --><button data-a="raspberry" style="background:#d6457b" title="raspberry" onclick="setAccent('raspberry')"></button><button data-a="ocean" style="background:#2563eb" title="ocean" onclick="setAccent('ocean')"></button><button data-a="forest" style="background:#16a34a" title="forest" onclick="setAccent('forest')"></button><button data-a="plum" style="background:#9333ea" title="plum" onclick="setAccent('plum')"></button><button data-a="tangerine" style="background:#ea580c" title="tangerine" onclick="setAccent('tangerine')"></button></span></div>
<nav class="tabs">
<button id="tabJournal" class="on" onclick="showTab('journal')">Journal</button>
<button id="tabNotes" onclick="showTab('notes')">Notes</button>
</nav>
</header>
<div class="searchrow">
<input id="q" placeholder="Search the journal&hellip;" autocomplete="off">
<button onclick="doSearch()">Search</button>
</div>
<div id="results"></div>
<div id="emptyDay" class="gentle" style="display:none">Nothing recorded this day.</div>
<div id="journalTab">
<section id="entrySec">
<h2 class="sec"><span id="entryH">Today&rsquo;s summary</span><span class="entryctl"><button id="regenBtn" onclick="regenEntry()" title="Ask the local model to write this summary again">&#8635; regenerate</button><button class="danger" onclick="removeEntry()" title="Delete this summary">remove</button></span></h2>
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
</div>
<div id="notesTab" style="display:none">
<section>
<h2 class="sec">Notes <small>for this day</small></h2>
<textarea id="noteBody" placeholder="Meeting notes, ideas, anything worth remembering&hellip;"></textarea>
<div class="noterow"><button onclick="saveNote()">Save note</button><span id="noteSaved" class="notesaved"></span></div>
</section>
</div>
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
    ? "Tonight's summary hasn't been written yet. Run <code>pctx summarize</code>."
    : 'No summary was written for this day.';
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
/* Collapse state for "The hours" periods: UI state only, persisted in
   localStorage keyed by viewed date + period (never server kv). Survives
   the 2s auto-refresh re-renders because hoursHTML reads it every time. */
function periodKey(name){ return 'pctx-period-'+curDate+'-'+name; }
function isPeriodCollapsed(name){
  try{ return typeof localStorage!=='undefined' &&
    localStorage.getItem(periodKey(name))==='1'; }catch(e){ return false; }
}
function setPeriodCollapsed(name, collapsed){
  try{ if(typeof localStorage!=='undefined')
    localStorage.setItem(periodKey(name), collapsed?'1':'0'); }catch(e){}
}
function togglePeriod(name){
  const body = document.getElementById('periodbody-'+name);
  const head = document.getElementById('period-'+name);
  if(!body || !head) return;
  const collapsed = body.classList.toggle('collapsed');
  head.setAttribute('aria-expanded', String(!collapsed));
  const chev = head.querySelector('.chev');
  if(chev) chev.textContent = collapsed ? '▸' : '▾';
  setPeriodCollapsed(name, collapsed);
}
function hoursHTML(tl, viewingToday){
  const chrono = tl.slice().reverse();  // API is newest-first
  const durs = entryDurations(chrono, viewingToday);
  const durOf = e => durs[chrono.indexOf(e)];
  // Periods read morning-to-night like a journal; entries WITHIN each
  // period read newest-first, so the latest activity is always on top
  // and live updates never push it below the fold. Each period header
  // is a keyboard-accessible toggle that collapses its entries.
  return groupByPeriod(chrono).map(g => {
    const collapsed = isPeriodCollapsed(g.name);
    // data-p carries the period name (event delegation below) — avoids
    // nested-quote escaping bugs in inline onclick handlers.
    return '<div class="periodwrap" id="periodwrap-'+g.name+'">'+
      '<button class="periodhead" id="period-'+g.name+'" aria-expanded="'+
      (!collapsed)+'" data-p="'+g.name+'">'+
      '<span class="chev">'+(collapsed?'▸':'▾')+'</span>'+
      '<span>'+g.name+'</span></button>'+
      '<div class="periodbody'+(collapsed?' collapsed':'')+'" id="periodbody-'+g.name+'">'+
      g.items.slice().reverse().map(e => hourEntryHTML(e, durOf(e))).join('')+
      '</div></div>';
  }).join('');
}
/* One-time smooth scroll to the current time period on initial load.
   Never fires on auto-refresh ticks, and never when viewing a past date. */
let firstLoad = true;
function scrollToNow(){
  if(curDate !== todayISO()) return;
  const name = periodName(new Date().getHours());
  // Never fight collapsed state: expand the current period first so the
  // scroll target is visible.
  if(isPeriodCollapsed(name)){
    setPeriodCollapsed(name, false);
    const body = document.getElementById('periodbody-'+name);
    const head = document.getElementById('period-'+name);
    if(body) body.classList.remove('collapsed');
    if(head){ head.setAttribute('aria-expanded','true');
      const c = head.querySelector('.chev'); if(c) c.textContent = '▾'; }
  }
  const el = document.getElementById('period-'+name);
  if(el && el.scrollIntoView) el.scrollIntoView({behavior:'smooth', block:'start'});
}
/* ---- Journal / Notes tabs ---- */
function showTab(which){
  document.getElementById('journalTab').style.display = which==='journal' ? '' : 'none';
  document.getElementById('notesTab').style.display = which==='notes' ? '' : 'none';
  document.getElementById('tabJournal').classList.toggle('on', which==='journal');
  document.getElementById('tabNotes').classList.toggle('on', which==='notes');
  if(which==='notes') loadNote();
}
/* ---- Per-day notes ---- */
async function loadNote(){
  try{
    const r = await j('/api/notes?date='+curDate);
    document.getElementById('noteBody').value = r.body || '';
    document.getElementById('noteSaved').textContent = '';
  }catch(e){ document.getElementById('noteSaved').textContent = 'error: '+e; }
}
async function saveNote(){
  const body = document.getElementById('noteBody').value;
  try{
    const r = await j('/api/notes?date='+curDate, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({body})});
    const now = new Date();
    document.getElementById('noteSaved').textContent =
      (r.body ? 'Saved \u2713 ' : 'Cleared \u2713 ') +
      now.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  }catch(e){ document.getElementById('noteSaved').textContent = 'error: '+e; }
}
/* ---- Stickers: awarded manually by the user, never auto-assigned ---- */
const STICKERS = ['⭐','🦄','🧜','🚀','🎨','🔥','💡','🌙'];
let curSticker = null;
async function loadSticker(){
  try{
    const r = await j('/api/sticker?date='+curDate);
    curSticker = r.sticker || null;
    renderSticker();
  }catch(e){}
}
function renderSticker(){
  const big = document.getElementById('stickerBig');
  const btn = document.getElementById('stickerBtn');
  if(curSticker){ big.textContent = curSticker; big.style.display = ''; btn.innerHTML = '\u2726 change'; }
  else { big.textContent = ''; big.style.display = 'none'; btn.innerHTML = '\u2726 mark this day'; }
}
function togglePicker(ev){
  if(ev) ev.stopPropagation();
  const p = document.getElementById('picker');
  if(p.style.display === 'none'){
    p.innerHTML = STICKERS.map(s =>
      '<button data-s="'+s+'" class="stk">'+s+'</button>').join('') +
      '<button class="clear" data-s="">clear</button>';
    p.querySelectorAll('button').forEach(b =>
      b.addEventListener('click', ()=>pickSticker(b.dataset.s || null)));
    p.style.display = '';
  } else p.style.display = 'none';
}
async function pickSticker(s){
  try{
    const r = await j('/api/sticker?date='+curDate, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({sticker:s})});
    curSticker = r.sticker || null;
    renderSticker();
  }catch(e){ alert('error: '+e); }
  document.getElementById('picker').style.display = 'none';
}
/* ---- Theme: journal (default monotone) vs diary (colorful) ---- */
let curTheme = 'journal';
async function loadTheme(){
  try{
    const r = await j('/api/theme');
    applyTheme(r.theme || 'journal');
  }catch(e){}
}
function applyTheme(t){
  curTheme = t;
  document.body.dataset.theme = t;  // server also renders it; no first-paint flash
  document.getElementById('themeJournal').classList.toggle('on', t==='journal');
  document.getElementById('themeDiary').classList.toggle('on', t==='diary');
  // Accent dots belong to diary mode only — journal stays monotone.
  document.getElementById('accentDots').style.display = (t==='diary') ? '' : 'none';
  // Theme-aware labels: diary mode reads "Diary | Notes" and "A Local Diary".
  const diary = (t === 'diary');
  document.getElementById('tabJournal').textContent = diary ? 'Diary' : 'Journal';
  document.getElementById('eyebrow').innerHTML =
    'PrivateContext &middot; A Local ' + (diary ? 'Diary' : 'Journal');
}
async function setTheme(t){
  try{
    const r = await j('/api/theme', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({theme:t})});
    applyTheme(r.theme);
  }catch(e){ alert('error: '+e); }
}
/* ---- Diary accent: raspberry (default), ocean, forest, plum, tangerine ----
   Server-rendered via data-accent on first paint, like data-theme. */
let curAccent = 'raspberry';
async function loadAccent(){
  try{
    const r = await j('/api/accent');
    applyAccent(r.accent || 'raspberry');
  }catch(e){}
}
function applyAccent(a){
  curAccent = a;
  document.body.dataset.accent = a;
  document.querySelectorAll('#accentDots button').forEach(
    b => b.classList.toggle('on', b.dataset.a === a));
}
async function setAccent(a){
  try{
    const r = await j('/api/accent', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({accent:a})});
    applyAccent(r.accent);
  }catch(e){ alert('error: '+e); }
}
/* ---- Entry controls: regenerate / remove ----
   UI contract for regeneration: POST starts a background summarize and
   returns immediately; poll /api/recap?date= every 3s until the summary
   TEXT differs from what was shown (or 120s elapse → error). A failed
   regeneration never deletes the existing summary — summarize_day only
   writes to the days table on success. */
let regenTimer = null;
async function regenEntry(){
  try{
    const r = await j('/api/entry/regenerate?date='+curDate, {method:'POST'});
    if(r.error){ alert(r.error); return; }
  }catch(e){ alert('error: '+e); return; }
  const before = document.getElementById('entry').dataset.summary || '';
  document.getElementById('entry').innerHTML =
    '<p class="writing">Writing today\u2019s summary\u2026</p>';
  const t0 = Date.now();
  if(regenTimer) clearInterval(regenTimer);
  regenTimer = setInterval(async ()=>{
    try{
      const r = await j('/api/recap?date='+curDate);
      const now = (r && r.summary) || '';
      if(now && now !== before){
        clearInterval(regenTimer); regenTimer = null;
        document.getElementById('entry').dataset.summary = now;
        document.getElementById('entry').innerHTML = entryHTML(r);
      } else if(Date.now()-t0 > 120000){
        clearInterval(regenTimer); regenTimer = null;
        document.getElementById('entry').innerHTML =
          '<p class="placeholder">Regeneration timed out \u2014 the previous summary was kept. Try again.</p>';
      }
    }catch(e){}
  }, 3000);
}
async function removeEntry(){
  if(!confirm("Delete today's summary? You can regenerate it later.")) return;
  try{
    await j('/api/entry/remove?date='+curDate, {method:'POST'});
    document.getElementById('entry').dataset.summary = '';
    refresh();
  }catch(e){ alert('error: '+e); }
}
function renderMasthead(){
  const d = new Date(curDate+'T12:00:00');
  document.getElementById('dowMonth').textContent =
    d.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric'});
  document.getElementById('yearLine').textContent = d.getFullYear();
  document.getElementById('entryH').textContent =
    (curDate === todayISO()) ? "Today\u2019s summary" : 'The summary';
  document.title = 'PrivateContext \u00b7 ' +
    d.toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'});
}
async function loadEntry(){
  try{
    // Viewing today: server falls back to yesterday's summary. A picked
    // past date shows exactly that date's summary, no fallback.
    const url = (curDate === todayISO()) ? '/api/recap' : '/api/recap?date='+curDate;
    const r = await j(url);
    document.getElementById('entry').innerHTML = entryHTML(r);
    // Track the raw summary text so regenEntry() can poll until it changes.
    document.getElementById('entry').dataset.summary = (r && r.summary) || '';
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
    // One-time scroll to the current period on initial load only —
    // never on auto-refresh ticks (scrollToNow guards past dates too).
    if(firstLoad){ firstLoad = false; scrollToNow(); }
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
  loadSticker();
  if(document.getElementById('notesTab').style.display !== 'none') loadNote();
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
document.getElementById('noteBody').addEventListener('keydown',
  e=>{ if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='s'){ e.preventDefault(); saveNote(); } });
document.addEventListener('click', e=>{
  const p = document.getElementById('picker');
  if(p.style.display !== 'none' && !p.contains(e.target) &&
     e.target.id !== 'stickerBtn' && e.target.id !== 'stickerBig') p.style.display = 'none';
  // "The hours" period toggles (event delegation — the timeline is
  // re-rendered every 2s, so inline handlers would rot).
  const head = e.target.closest ? e.target.closest('.periodhead') : null;
  if(head && head.dataset.p) togglePeriod(head.dataset.p);
});
loadTheme();
loadAccent();
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


def _date_or_today(qs: dict) -> str:
    """Validated ?date=, defaulting to today. Raises ValueError on bad
    format (caller turns it into HTTP 400)."""
    return _date_param(qs) or date.today().isoformat()


def _regen_entry(db_path: str, date_str: str) -> None:
    """Background summary regeneration. Opens its OWN sqlite connection —
    connections are never shared across threads. summarize_day only
    writes to the days table on success, so a failure (Ollama down,
    timeout, crash) can never delete the existing summary."""
    try:
        summarize_day(db_path, date_str, DEFAULT_MODEL)
    except Exception:
        pass  # the UI polls /api/recap and times out on its own


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

    def _read_json(self) -> dict:
        """Parse a JSON POST body. Raises ValueError on bad JSON."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("bad JSON body")
        if not isinstance(data, dict):
            raise ValueError("bad JSON body")
        return data

    def _dated(self, qs: dict) -> str | None:
        """Validated ?date= (default today). Sends 400 and returns None
        on bad format."""
        try:
            return _date_or_today(qs)
        except ValueError:
            self._json({"error": "bad date — use YYYY-MM-DD"}, 400)
            return None

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/":
            # Server-rendered theme + accent + labels: first paint is
            # already correct, no flash.
            theme = self._with_db(lambda db: get_theme(db))
            accent = self._with_db(lambda db: get_accent(db))
            body = PAGE.replace(
                "<body>",
                f'<body data-theme="{theme}" data-accent="{accent}">',
                1)
            if theme == "diary":
                body = body.replace("A Local Journal", "A Local Diary")
                body = body.replace(
                    "onclick=\"showTab('journal')\">Journal</button>",
                    "onclick=\"showTab('journal')\">Diary</button>", 1)
            body = body.encode()
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
        elif u.path == "/api/notes":
            d = self._dated(parse_qs(u.query))
            if d is None:
                return
            self._json(self._with_db(
                lambda db: {"body": get_note(db, d) or ""}))
        elif u.path == "/api/sticker":
            d = self._dated(parse_qs(u.query))
            if d is None:
                return
            self._json(self._with_db(
                lambda db: {"sticker": get_sticker(db, d)}))
        elif u.path == "/api/theme":
            self._json(self._with_db(lambda db: {"theme": get_theme(db)}))
        elif u.path == "/api/accent":
            self._json(self._with_db(lambda db: {"accent": get_accent(db)}))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        path = u.path
        if path == "/api/purge":
            def _purge(db):
                n = db.execute("DELETE FROM events").rowcount
                db.execute("DELETE FROM days")
                db.commit()
                db.execute("VACUUM")
                return {"deleted": n}
            self._json(self._with_db(_purge))
            return
        if path not in ("/api/notes", "/api/sticker", "/api/theme",
                        "/api/accent", "/api/entry/regenerate",
                        "/api/entry/remove"):
            self._json({"error": "not found"}, 404)
            return
        try:
            data = self._read_json()
        except ValueError:
            self._json({"error": "bad JSON body"}, 400)
            return
        if path in ("/api/notes", "/api/sticker",
                    "/api/entry/regenerate", "/api/entry/remove"):
            d = self._dated(parse_qs(u.query))
            if d is None:
                return
        if path == "/api/notes":
            body = data.get("body", "")
            if not isinstance(body, str):
                self._json({"error": "body must be a string"}, 400)
                return
            saved = self._with_db(lambda db: set_note(db, d, body))
            self._json({"saved": True, "body": saved})
        elif path == "/api/sticker":
            try:
                s = self._with_db(
                    lambda db: set_sticker(db, d, data.get("sticker")))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"sticker": s})
        elif path == "/api/theme":
            try:
                t = self._with_db(lambda db: set_theme(db, data.get("theme")))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"theme": t})
        elif path == "/api/accent":
            try:
                a = self._with_db(
                    lambda db: set_accent(db, data.get("accent")))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"accent": a})
        elif path == "/api/entry/remove":
            removed = self._with_db(lambda db: remove_day_entry(db, d))
            self._json({"removed": removed})
        elif path == "/api/entry/regenerate":
            # Fast pre-flight so a dead Ollama returns the hint, not a hang.
            if not ollama_reachable():
                self._json({"error": ollama_hint()}, 503)
                return
            t = threading.Thread(
                target=_regen_entry,
                args=(self.server.db_path, d),
                daemon=True)
            t.start()
            self._json({"started": True})

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
    server.db_path = db_path if db_path else default_db_path()  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    print(f"[privatecontext] dashboard at http://127.0.0.1:{port} — Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[privatecontext] dashboard stopped.")
