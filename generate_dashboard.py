#!/usr/bin/env python3
"""
Generate a self-contained HTML dashboard from the betting bot's SQLite database.

Usage:
    python generate_dashboard.py [--db betting_bot.db] [--out dashboard.html]

Free hosting options for the output file:
  - GitHub Pages  : push dashboard.html to the docs/ folder and enable Pages
  - Netlify Drop  : drag dashboard.html to app.netlify.com/drop
  - Vercel        : connect this repo; a workflow commits the file each run
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_db(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    bets = [dict(r) for r in conn.execute("""
        SELECT id, sport, event, market, book, line,
               model_prob, fair_prob, edge, stake_units, ev,
               result, profit_units, logged_at,
               closing_line, clv, projected_score
        FROM bets_log
        ORDER BY logged_at DESC
        LIMIT 500
    """)]

    today_str = date.today().isoformat()
    today_picks = [dict(r) for r in conn.execute("""
        SELECT sport, event, market, book, line,
               model_prob, fair_prob, edge, stake_units, projected_score
        FROM bets_log
        WHERE DATE(logged_at) = ?
        ORDER BY edge DESC
    """, (today_str,))]

    daily_rows = [dict(r) for r in conn.execute("""
        SELECT DATE(logged_at) AS day,
               SUM(profit_units) AS pnl,
               COUNT(*)          AS n_bets,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS n_wins,
               SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS n_resolved
        FROM bets_log
        GROUP BY day
        ORDER BY day
    """)]

    conn.close()

    # Cumulative P&L
    cum = 0.0
    for row in daily_rows:
        cum += _safe_float(row["pnl"])
        row["cumulative"] = round(cum, 2)

    # Summary stats
    resolved   = [b for b in bets if b.get("result")]
    n_resolved = len(resolved)
    n_wins     = sum(1 for b in resolved if b["result"] == "win")
    staked     = sum(_safe_float(b["stake_units"]) for b in resolved)
    profit     = sum(_safe_float(b["profit_units"]) for b in resolved)
    clv_vals   = [_safe_float(b["clv"]) for b in resolved if b.get("clv") is not None]

    win_rate   = n_wins / n_resolved if n_resolved else 0.0
    roi        = profit / staked if staked else 0.0
    avg_edge   = sum(_safe_float(b["edge"]) for b in bets) / len(bets) if bets else 0.0
    avg_clv    = sum(clv_vals) / len(clv_vals) if clv_vals else None

    # Market distribution (top 8)
    mkt_counter: Counter = Counter()
    for b in bets:
        raw = (b.get("market") or "").strip()
        # Strip the point from "Over 8.5" → "Over/Under"
        label = raw.split(" O")[0].split(" U")[0].split(" -")[0].split(" +")[0].strip()
        if label:
            mkt_counter[label] += 1
    top_markets = [{"market": m, "count": c} for m, c in mkt_counter.most_common(8)]

    # Sport breakdown
    sport_counter: Counter = Counter(b.get("sport", "?") for b in bets)
    sport_wins:    Counter = Counter(b.get("sport", "?") for b in resolved if b["result"] == "win")
    sports = []
    for sport, cnt in sport_counter.most_common():
        res_cnt = sum(1 for b in resolved if b.get("sport") == sport)
        sports.append({
            "sport":    sport,
            "total":    cnt,
            "resolved": res_cnt,
            "wins":     sport_wins.get(sport, 0),
        })

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
        "today":        today_str,
        "today_picks":  today_picks,
        "recent_bets":  bets[:100],
        "daily_pnl":    daily_rows,
        "top_markets":  top_markets,
        "sports":       sports,
        "stats": {
            "total_picks":    len(bets),
            "n_resolved":     n_resolved,
            "n_wins":         n_wins,
            "win_rate":       round(win_rate * 100, 1),
            "roi":            round(roi * 100, 1),
            "total_profit":   round(profit, 2),
            "avg_edge":       round(avg_edge * 100, 1),
            "avg_clv":        round(avg_clv * 100, 1) if avg_clv is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# HTML template  (redesigned — validated palette, terminal typography, structured layout)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>+EV Bot — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
/* ── Reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* ── Design tokens — validated categorical palette (OKLCH dark mode) ── */
:root{
  --bg:#0B1120; --surf:#111928; --surf2:#182236;
  --border:#1E2E46; --border2:#253A56;
  --t1:#E8EDF6; --t2:#8B9EC4; --tm:#4E6480;
  --mlb:#5A7AE8; --soccer:#18A88A;
  --amber:#C8800F; --rose:#E04868;
  --win:#4ADE80; --loss:#F87171;
  --odds:#E8A830;
  --r:8px; --r-lg:12px;
}

html{scroll-behavior:smooth}
body{
  background:var(--bg);color:var(--t1);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  font-size:14px;line-height:1.5;min-height:100vh;
}

.mono{font-family:'Consolas','Menlo','Monaco','Courier New',monospace}
.tabnum{font-variant-numeric:tabular-nums}

/* ── Shell ── */
.shell{max-width:1400px;margin:0 auto;padding:1.25rem 1.5rem 3rem}

/* ── Header ── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:1.25rem 0 1rem;
  border-bottom:1px solid rgba(200,128,15,.2);
  gap:1rem;flex-wrap:wrap;
}
.hdr-brand{display:flex;align-items:center;gap:.75rem}
.brand-icon{
  width:36px;height:36px;flex-shrink:0;border-radius:var(--r);
  background:linear-gradient(135deg,#5A7AE8,#18A88A);
  display:flex;align-items:center;justify-content:center;
  font-size:.7rem;font-weight:800;color:#fff;
  font-family:'Consolas','Menlo',monospace;letter-spacing:-.02em;
}
.brand-text h1{font-size:.95rem;font-weight:700;letter-spacing:-.01em;color:var(--t1)}
.brand-text .updated{
  font-size:.68rem;color:var(--tm);margin-top:1px;
  font-family:'Consolas','Menlo',monospace;
}
.hdr-right{display:flex;align-items:center;gap:1rem}
.hdr-date{font-size:.72rem;color:var(--t2);font-family:'Consolas','Menlo',monospace;letter-spacing:.04em}
.live-chip{
  display:flex;align-items:center;gap:5px;
  background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.18);
  border-radius:20px;padding:.2rem .65rem;
  font-size:.65rem;font-weight:700;letter-spacing:.09em;color:#4ADE80;text-transform:uppercase;
}
.live-dot{
  width:5px;height:5px;background:#4ADE80;border-radius:50%;
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}

/* ── KPI strip ── */
.kpi-strip{
  display:grid;
  grid-template-columns:repeat(6,1fr);
  gap:.75rem;margin:1.25rem 0;
}
@media(max-width:1000px){.kpi-strip{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.kpi-strip{grid-template-columns:repeat(2,1fr)}}

.kpi-tile{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r);padding:.9rem 1rem;
  position:relative;overflow:hidden;
}
.kpi-tile::after{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--kpi-accent,var(--border2));
  border-radius:var(--r) var(--r) 0 0;
}
.kpi-label{
  font-size:.62rem;text-transform:uppercase;letter-spacing:.1em;
  color:var(--tm);margin-bottom:.5rem;font-weight:600;
}
.kpi-value{
  font-size:1.55rem;font-weight:700;line-height:1;
  letter-spacing:-.03em;
  font-family:'Consolas','Menlo',monospace;
  font-variant-numeric:tabular-nums;
}
.kpi-sub{font-size:.65rem;color:var(--tm);margin-top:.35rem;font-variant-numeric:tabular-nums}
.kpi-up{color:var(--win)}.kpi-dn{color:var(--loss)}.kpi-neu{color:var(--t2)}

/* ── Section rule ── */
.sec{display:flex;align-items:center;gap:.6rem;margin:1.5rem 0 .85rem}
.sec h2{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--tm);white-space:nowrap}
.sec-rule{flex:1;height:1px;background:var(--border)}
.badge{
  background:var(--surf2);border:1px solid var(--border2);border-radius:20px;
  padding:.1rem .5rem;font-size:.65rem;color:var(--t2);font-weight:600;
  font-variant-numeric:tabular-nums;
}

/* ── Pick cards ── */
.picks-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:.75rem;margin-bottom:.5rem;
}
.pick-card{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:1rem 1.1rem;
  position:relative;overflow:hidden;
  display:flex;flex-direction:column;gap:.55rem;
  transition:border-color .15s;
}
.pick-card:hover{border-color:var(--border2)}
.pick-card::before{
  content:'';position:absolute;top:0;left:0;width:3px;height:100%;
  background:var(--card-accent,var(--mlb));
  border-radius:var(--r-lg) 0 0 var(--r-lg);
}
.pick-card.soccer{--card-accent:var(--soccer)}

.pick-top{display:flex;justify-content:space-between;align-items:flex-start}
.pick-sport-chip{
  display:inline-flex;align-items:center;
  font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  padding:.15rem .5rem;border-radius:4px;
  background:rgba(90,122,232,.1);color:var(--mlb);
  border:1px solid rgba(90,122,232,.18);
}
.pick-card.soccer .pick-sport-chip{
  background:rgba(24,168,138,.1);color:var(--soccer);
  border-color:rgba(24,168,138,.18);
}
.pick-edge-hero{
  font-size:1.4rem;font-weight:800;color:var(--win);line-height:1;
  font-family:'Consolas','Menlo',monospace;letter-spacing:-.02em;text-align:right;
}
.pick-edge-label{font-size:.56rem;text-transform:uppercase;letter-spacing:.08em;color:var(--tm);text-align:right;margin-top:1px}

.pick-event{font-size:.88rem;font-weight:600;color:var(--t1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-market{font-size:.76rem;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.pick-bottom{
  display:flex;justify-content:space-between;align-items:center;
  border-top:1px solid var(--border);padding-top:.55rem;margin-top:.1rem;
}
.pick-odds-wrap{display:flex;align-items:baseline;gap:.4rem}
.pick-line{
  font-size:1.05rem;font-weight:700;color:var(--odds);
  font-family:'Consolas','Menlo',monospace;font-variant-numeric:tabular-nums;
}
.pick-book{font-size:.65rem;color:var(--tm);text-transform:uppercase;letter-spacing:.05em}
.pick-stake{font-size:.7rem;color:var(--t2);font-family:'Consolas','Menlo',monospace}
.pick-proj{font-size:.65rem;color:var(--tm);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.no-picks{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:2.5rem;
  text-align:center;color:var(--tm);font-size:.82rem;
}

/* ── Charts ── */
.charts-row{display:grid;grid-template-columns:7fr 4fr;gap:.75rem}
@media(max-width:800px){.charts-row{grid-template-columns:1fr}}
.chart-card{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:1.1rem 1.25rem 1rem;
}
.chart-title{
  font-size:.65rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.1em;color:var(--tm);margin-bottom:.85rem;
}
.chart-wrap{position:relative;height:200px}

/* ── Filter chips ── */
.filter-row{display:flex;gap:.5rem;margin-bottom:.75rem;flex-wrap:wrap}
.filter-chip{
  background:var(--surf);border:1px solid var(--border);
  border-radius:20px;padding:.25rem .75rem;
  font-size:.68rem;font-weight:600;color:var(--t2);
  cursor:pointer;transition:all .15s;
  text-transform:uppercase;letter-spacing:.05em;
  font-family:inherit;
}
.filter-chip:hover{border-color:var(--border2);color:var(--t1)}
.filter-chip.active{background:var(--surf2);border-color:var(--mlb);color:var(--mlb)}
.filter-chip[data-sport="Soccer"].active{border-color:var(--soccer);color:var(--soccer)}

/* ── Table ── */
.tbl-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--r-lg);overflow:hidden}
.tbl-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.79rem;font-variant-numeric:tabular-nums}
thead th{
  padding:.65rem .9rem;text-align:left;
  font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;
  color:var(--tm);white-space:nowrap;cursor:pointer;user-select:none;
  border-bottom:1px solid var(--border);background:var(--surf2);
}
thead th:hover{color:var(--t2)}
thead th.sort-asc::after{content:' ▲';font-size:.55em}
thead th.sort-desc::after{content:' ▼';font-size:.55em}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,.02)}
tbody td{padding:.58rem .9rem;vertical-align:middle;white-space:nowrap;color:var(--t1)}

.sp-chip{
  display:inline-flex;align-items:center;
  font-size:.56rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  padding:.15rem .45rem;border-radius:4px;
}
.sp-MLB{background:rgba(90,122,232,.1);color:var(--mlb);border:1px solid rgba(90,122,232,.18)}
.sp-Soccer{background:rgba(24,168,138,.1);color:var(--soccer);border:1px solid rgba(24,168,138,.18)}
.r-win{color:var(--win);font-weight:600}
.r-loss{color:var(--loss);font-weight:600}
.r-pend{color:var(--tm)}
.pnl-up{color:var(--win)}.pnl-dn{color:var(--loss)}

/* ── Footer ── */
footer{
  text-align:center;color:var(--tm);font-size:.65rem;
  padding-top:2rem;
  font-family:'Consolas','Menlo',monospace;letter-spacing:.04em;
}
</style>
</head>
<body>
<div class="shell">

<header>
  <div class="hdr-brand">
    <div class="brand-icon">+EV</div>
    <div class="brand-text">
      <h1>Sports Betting Dashboard</h1>
      <div class="updated" id="updated-at"></div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="hdr-date" id="hdr-date"></div>
    <div class="live-chip"><span class="live-dot"></span>Live</div>
  </div>
</header>

<div class="kpi-strip" id="kpi-strip"></div>

<div class="sec">
  <h2>Today's Picks</h2>
  <span class="badge" id="picks-badge">0</span>
  <div class="sec-rule"></div>
</div>
<div id="picks-container"></div>

<div class="sec">
  <h2>Performance</h2>
  <div class="sec-rule"></div>
</div>
<div class="charts-row">
  <div class="chart-card">
    <div class="chart-title">Cumulative P&amp;L (units)</div>
    <div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="chart-title">Top Markets</div>
    <div class="chart-wrap"><canvas id="mkt-chart"></canvas></div>
  </div>
</div>

<div class="sec" style="margin-top:1.75rem">
  <h2>Bet History</h2>
  <span class="badge" id="history-badge">0</span>
  <div class="sec-rule"></div>
</div>
<div class="filter-row" id="filter-row"></div>
<div class="tbl-card">
  <div class="tbl-scroll">
    <table>
      <thead>
        <tr>
          <th onclick="sortTable(0)">Sport</th>
          <th onclick="sortTable(1)">Event</th>
          <th onclick="sortTable(2)">Market</th>
          <th onclick="sortTable(3)">Line</th>
          <th onclick="sortTable(4)">Book</th>
          <th onclick="sortTable(5)">Edge</th>
          <th onclick="sortTable(6)">Stake</th>
          <th onclick="sortTable(7)">Result</th>
          <th onclick="sortTable(8)">P&amp;L</th>
          <th onclick="sortTable(9)">Date</th>
        </tr>
      </thead>
      <tbody id="bet-tbody"></tbody>
    </table>
  </div>
</div>

<footer id="footer-text"></footer>
</div>

<script>
const D = __DATA__;

document.getElementById('updated-at').textContent = 'Updated ' + D.generated_at;
document.getElementById('hdr-date').textContent = D.today;

// KPI strip
const s = D.stats;
const kpis = [
  { label:'Total Picks', sub:'all time', accent:'#5A7AE8',
    value:s.total_picks.toLocaleString(), cls:'kpi-neu' },
  { label:'Win Rate', sub:s.n_resolved+' resolved',
    accent:s.win_rate>=52?'#4ADE80':'#8B9EC4',
    value:s.n_resolved>0?s.win_rate+'%':'N/A',
    cls:s.win_rate>=52?'kpi-up':'kpi-neu' },
  { label:'ROI', sub:'on resolved bets',
    accent:s.roi>0?'#4ADE80':s.roi<0?'#F87171':'#8B9EC4',
    value:s.n_resolved>0?(s.roi>0?'+':'')+s.roi+'%':'N/A',
    cls:s.roi>0?'kpi-up':s.roi<0?'kpi-dn':'kpi-neu' },
  { label:'Total P&L', sub:'units profit',
    accent:s.total_profit>0?'#4ADE80':s.total_profit<0?'#F87171':'#8B9EC4',
    value:s.n_resolved>0?(s.total_profit>0?'+':'')+s.total_profit+'u':'N/A',
    cls:s.total_profit>0?'kpi-up':s.total_profit<0?'kpi-dn':'kpi-neu' },
  { label:'Avg Edge', sub:'model vs market', accent:'#C8800F',
    value:s.avg_edge+'%', cls:s.avg_edge>=3?'kpi-up':'kpi-neu' },
  { label:'Avg CLV', sub:'closing line value',
    accent:s.avg_clv>0?'#4ADE80':'#8B9EC4',
    value:s.avg_clv!==null?(s.avg_clv>0?'+':'')+s.avg_clv+'%':'N/A',
    cls:s.avg_clv>0?'kpi-up':s.avg_clv<0?'kpi-dn':'kpi-neu' },
];
document.getElementById('kpi-strip').innerHTML = kpis.map(k=>`
  <div class="kpi-tile" style="--kpi-accent:${k.accent}">
    <div class="kpi-label">${k.label}</div>
    <div class="kpi-value ${k.cls} mono tabnum">${k.value}</div>
    <div class="kpi-sub">${k.sub}</div>
  </div>`).join('');

// Today's picks
const picks = D.today_picks;
document.getElementById('picks-badge').textContent = picks.length;
const pc = document.getElementById('picks-container');
if (!picks.length) {
  pc.innerHTML = '<div class="no-picks">No picks logged for today yet — check back after the next run.</div>';
} else {
  pc.innerHTML = '<div class="picks-grid">'+picks.map(p=>{
    const isSoccer=(p.sport||'').toLowerCase()==='soccer';
    const edge=((p.edge||0)*100).toFixed(1);
    const stake=(p.stake_units||0).toFixed(2);
    const proj=p.projected_score?`<div class="pick-proj">${p.projected_score}</div>`:'';
    return `<div class="pick-card ${isSoccer?'soccer':''}">
      <div class="pick-top">
        <span class="pick-sport-chip">${p.sport||'?'}</span>
        <div><div class="pick-edge-hero">+${edge}%</div><div class="pick-edge-label">edge</div></div>
      </div>
      <div class="pick-event" title="${p.event||''}">${p.event||'?'}</div>
      <div class="pick-market" title="${p.market||''}">${p.market||'?'}</div>
      <div class="pick-bottom">
        <div class="pick-odds-wrap">
          <span class="pick-line mono">${p.line||'?'}</span>
          <span class="pick-book">${p.book||'?'}</span>
        </div>
        <span class="pick-stake mono">${stake}u</span>
      </div>${proj}
    </div>`;
  }).join('')+'</div>';
}

// P&L chart
const pnlData=D.daily_pnl;
if(pnlData.length>0){
  const labels=pnlData.map(r=>r.day);
  const vals=pnlData.map(r=>r.cumulative);
  const lastVal=vals[vals.length-1]||0;
  const lc=lastVal>=0?'#4ADE80':'#F87171';
  const fc=lastVal>=0?'rgba(74,222,128,.07)':'rgba(248,113,113,.07)';
  new Chart(document.getElementById('pnl-chart'),{
    type:'line',
    data:{labels,datasets:[{
      data:vals,borderColor:lc,backgroundColor:fc,
      borderWidth:1.5,fill:true,tension:.35,
      pointRadius:vals.length>20?0:3,pointHoverRadius:5,pointBackgroundColor:lc,
    }]},
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>(ctx.raw>=0?'+':'')+ctx.raw.toFixed(2)+'u'}}},
      scales:{
        x:{ticks:{color:'#4E6480',font:{size:9,family:'Consolas,Menlo,monospace'}},grid:{color:'rgba(30,46,70,.5)'}},
        y:{ticks:{color:'#4E6480',font:{size:9,family:'Consolas,Menlo,monospace'},callback:v=>(v>0?'+':'')+v+'u'},grid:{color:'rgba(30,46,70,.8)',borderDash:[3,3]}}
      }
    }
  });
} else {
  document.getElementById('pnl-chart').parentElement.innerHTML=
    '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.8rem">No resolved bets yet</div>';
}

// Markets chart — categorical: fixed slot order, validated palette
const mkts=D.top_markets;
if(mkts.length>0){
  const cats=['#5A7AE8','#18A88A','#C8800F','#E04868','#7B9BFF','#1EC09E','#D4960F','#F0607A'];
  new Chart(document.getElementById('mkt-chart'),{
    type:'bar',
    data:{
      labels:mkts.map(m=>m.market),
      datasets:[{
        data:mkts.map(m=>m.count),
        backgroundColor:mkts.map((_,i)=>cats[i%cats.length]+'BB'),
        borderColor:mkts.map((_,i)=>cats[i%cats.length]),
        borderWidth:1,borderRadius:3,borderSkipped:'start',
      }]
    },
    options:{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>ctx.raw+' picks'}}},
      scales:{
        x:{ticks:{color:'#4E6480',font:{size:9}},grid:{color:'rgba(30,46,70,.8)'}},
        y:{ticks:{color:'#8B9EC4',font:{size:10,family:'Consolas,Menlo,monospace'}},grid:{display:false}}
      }
    }
  });
} else {
  document.getElementById('mkt-chart').parentElement.innerHTML=
    '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.8rem">No data yet</div>';
}

// Filter chips + table
const bets=D.recent_bets;
document.getElementById('history-badge').textContent=bets.length;
const sports=['All',...new Set(bets.map(b=>b.sport||'?').filter(Boolean))];
let activeSport='All';
document.getElementById('filter-row').innerHTML=sports.map(sp=>
  `<button class="filter-chip${sp==='All'?' active':''}" data-sport="${sp}" onclick="filterTable('${sp}')">${sp}</button>`
).join('');

function renderTable(sportFilter){
  const rows=sportFilter==='All'?bets:bets.filter(b=>b.sport===sportFilter);
  document.getElementById('bet-tbody').innerHTML=rows.map(b=>{
    const edge=b.edge!=null?'+'+(b.edge*100).toFixed(1)+'%':'—';
    const stake=b.stake_units!=null?( +b.stake_units).toFixed(2)+'u':'—';
    const rCls=b.result==='win'?'r-win':b.result==='loss'?'r-loss':'r-pend';
    const rTxt=b.result==='win'?'Win':b.result==='loss'?'Loss':'—';
    const pnl=b.profit_units!=null
      ?`<span class="${+b.profit_units>=0?'pnl-up':'pnl-dn'}">${+b.profit_units>=0?'+':''}${(+b.profit_units).toFixed(2)}u</span>`:'—';
    const logged=(b.logged_at||'').split(' ')[0];
    const sp=b.sport||'?';
    const spCls=sp==='MLB'?'sp-MLB':sp==='Soccer'?'sp-Soccer':'';
    return `<tr data-sport="${sp}">
      <td><span class="sp-chip ${spCls}">${sp}</span></td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${b.event||''}">${b.event||'?'}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;color:var(--t2)" title="${b.market||''}">${b.market||'?'}</td>
      <td class="mono" style="color:var(--odds)">${b.line||'?'}</td>
      <td style="color:var(--tm)">${b.book||'?'}</td>
      <td class="mono" style="color:var(--win)">${edge}</td>
      <td class="mono">${stake}</td>
      <td class="${rCls}">${rTxt}</td>
      <td class="mono">${pnl}</td>
      <td class="mono" style="color:var(--tm)">${logged}</td>
    </tr>`;
  }).join('');
}

function filterTable(sport){
  activeSport=sport;
  document.querySelectorAll('.filter-chip').forEach(c=>c.classList.remove('active'));
  document.querySelector(`.filter-chip[data-sport="${sport}"]`).classList.add('active');
  sortCol=-1;sortDir=1;
  renderTable(sport);
}

renderTable('All');

let sortCol=-1,sortDir=1;
function sortTable(col){
  const ths=document.querySelectorAll('thead th');
  ths.forEach(th=>th.classList.remove('sort-asc','sort-desc'));
  if(sortCol===col){sortDir*=-1}else{sortDir=1;sortCol=col}
  ths[col].classList.add(sortDir===1?'sort-asc':'sort-desc');
  const rows=[...document.querySelectorAll('#bet-tbody tr')];
  rows.sort((a,b)=>{
    const av=a.cells[col].textContent.trim(),bv=b.cells[col].textContent.trim();
    const an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn))return(an-bn)*sortDir;
    return av.localeCompare(bv)*sortDir;
  });
  document.getElementById('bet-tbody').append(...rows);
}

document.getElementById('footer-text').textContent='+EV Betting Bot · '+D.generated_at;
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate(db_path: str, out_path: str) -> None:
    try:
        data = read_db(db_path)
    except Exception as exc:
        print(f"Warning: could not read {db_path}: {exc}")
        data = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
            "today": date.today().isoformat(),
            "today_picks": [], "recent_bets": [], "daily_pnl": [],
            "top_markets": [], "sports": [],
            "stats": {"total_picks":0,"n_resolved":0,"n_wins":0,"win_rate":0,
                      "roi":0,"total_profit":0,"avg_edge":0,"avg_clv":None},
        }
    html = HTML.replace("__DATA__", json.dumps(data, default=str))
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"Dashboard written → {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate betting bot HTML dashboard")
    parser.add_argument("--db",  default="betting_bot.db", help="SQLite database path")
    parser.add_argument("--out", default="dashboard.html",  help="Output HTML file path")
    args = parser.parse_args()
    generate(args.db, args.out)
