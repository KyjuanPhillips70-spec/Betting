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
import re
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

    # Player prop history (all MLB bets with Batter/Pitcher in market label)
    _prop_re = re.compile(
        r'^(.+?)\s+((?:Batter|Pitcher)(?:\s+\w+)+)\s+([OU])(\d+(?:\.\d+)?)$'
    )
    _proj_num_re = re.compile(r'Proj:\s*([\d.]+)')
    prop_rows = [dict(r) for r in conn.execute("""
        SELECT event, market, edge, stake_units,
               result, profit_units, logged_at, projected_score
        FROM bets_log
        WHERE sport = 'MLB'
          AND (market LIKE '% Batter %' OR market LIKE '% Pitcher %')
        ORDER BY logged_at
    """)]
    prop_bets = []
    for row in prop_rows:
        m = _prop_re.match((row.get("market") or "").strip())
        if not m:
            continue
        ps = row.get("projected_score") or ""
        pm = _proj_num_re.search(ps)
        proj_num = None
        if pm:
            try:
                proj_num = float(pm.group(1))
            except ValueError:
                pass
        prop_bets.append({
            "player":    m.group(1),
            "stat":      m.group(2),
            "side":      m.group(3),
            "threshold": float(m.group(4)),
            "event":     row.get("event") or "",
            "edge":      round(_safe_float(row.get("edge")) * 100, 1),
            "stake":     round(_safe_float(row.get("stake_units")), 2),
            "result":    row.get("result") or "",
            "profit":    round(_safe_float(row.get("profit_units") or 0), 2),
            "date":      (row.get("logged_at") or "")[:10],
            "proj_stat": proj_num,
        })

    conn.close()

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
        "today":        today_str,
        "today_picks":  today_picks,
        "recent_bets":  bets[:100],
        "daily_pnl":    daily_rows,
        "top_markets":  top_markets,
        "sports":       sports,
        "prop_bets":    prop_bets,
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
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
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

/* ── Sport chip (shared) ── */
.sport-chip{
  display:inline-flex;align-items:center;flex-shrink:0;
  font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  padding:.15rem .5rem;border-radius:4px;
  background:rgba(90,122,232,.1);color:var(--mlb);
  border:1px solid rgba(90,122,232,.18);
}
.sport-chip.soccer{
  background:rgba(24,168,138,.1);color:var(--soccer);
  border-color:rgba(24,168,138,.18);
}

/* ── Game accordion ── */
.game-list{display:flex;flex-direction:column;gap:.5rem;margin-bottom:.5rem}

.game-group{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r-lg);overflow:hidden;
}

.game-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:.85rem 1.1rem;cursor:pointer;user-select:none;
  transition:background .1s;gap:1rem;
}
.game-hdr:hover{background:rgba(255,255,255,.025)}

.game-hdr-left{display:flex;align-items:center;gap:.65rem;min-width:0}
.game-title{
  font-size:.9rem;font-weight:600;color:var(--t1);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.game-hdr-right{display:flex;align-items:center;gap:.75rem;flex-shrink:0}
.game-count{font-size:.68rem;color:var(--t2);font-variant-numeric:tabular-nums}
.game-chevron{
  color:var(--tm);font-size:.7rem;
  transition:transform .2s ease;display:inline-block;
}
.game-group.open .game-chevron{transform:rotate(180deg)}

.game-body{
  display:grid;grid-template-rows:0fr;
  transition:grid-template-rows .2s ease;
}
.game-group.open .game-body{grid-template-rows:1fr}
.game-body-inner{
  overflow:hidden;
  border-top:0 solid var(--border);
  transition:border-top-width 0s .2s, padding .2s ease;
  padding:0 1.1rem;
  display:flex;flex-direction:column;gap:.5rem;
}
.game-group.open .game-body-inner{
  border-top-width:1px;padding:.75rem 1.1rem 1rem;
  transition:padding .2s ease;
}

/* ── Pick rows inside accordion ── */
.pick-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:.6rem .8rem;background:var(--surf2);
  border-radius:var(--r);gap:1rem;
}
.pick-row-left{min-width:0;flex:1}
.pick-row-market{font-size:.83rem;font-weight:600;color:var(--t1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-row-proj{font-size:.64rem;color:var(--tm);margin-top:.2rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-row-right{display:flex;align-items:center;gap:1rem;flex-shrink:0}
.pick-row-edge{font-size:1.05rem;font-weight:800;color:var(--win);font-family:'Consolas','Menlo',monospace;letter-spacing:-.01em}
.pick-row-line{font-size:.95rem;font-weight:700;color:var(--odds);font-family:'Consolas','Menlo',monospace;font-variant-numeric:tabular-nums}
.pick-row-meta{text-align:right}
.pick-row-book{font-size:.6rem;color:var(--tm);text-transform:uppercase;letter-spacing:.05em}
.pick-row-stake{font-size:.68rem;color:var(--t2);font-family:'Consolas','Menlo',monospace}

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

/* ── Tabs ── */
.tabs{
  display:flex;margin:.75rem 0 0;
  border-bottom:1px solid var(--border);
  overflow-x:auto;scrollbar-width:none;
}
.tabs::-webkit-scrollbar{display:none}
.tab{
  background:none;border:none;border-bottom:2px solid transparent;
  color:var(--tm);font-size:.7rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.09em;
  padding:.65rem .9rem;cursor:pointer;white-space:nowrap;
  transition:color .15s,border-color .15s;font-family:inherit;
  margin-bottom:-1px;flex-shrink:0;
}
.tab:hover{color:var(--t2)}
.tab.active{color:var(--mlb);border-bottom-color:var(--mlb)}
.tab-badge{
  display:inline-flex;align-items:center;justify-content:center;
  min-width:16px;height:16px;border-radius:8px;padding:0 4px;
  background:var(--surf2);border:1px solid var(--border2);
  font-size:.55rem;color:var(--t2);font-family:inherit;
  font-variant-numeric:tabular-nums;vertical-align:middle;margin-left:3px;
}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ── Player props section ── */
.prop-controls{
  display:flex;align-items:center;gap:.75rem;
  margin-bottom:1rem;flex-wrap:wrap;
}
.prop-select{
  background:var(--surf);border:1px solid var(--border2);
  color:var(--t1);border-radius:var(--r);
  padding:.45rem .75rem;font-size:.78rem;font-family:inherit;
  cursor:pointer;flex:1;min-width:160px;max-width:300px;
  -webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='%234E6480' d='M5 6L0 0h10z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right .65rem center;
  padding-right:2rem;
}
.prop-select:focus{outline:none;border-color:var(--mlb)}
.prop-select:disabled{opacity:.4;cursor:default}
.prop-mini-strip{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.85rem}
.prop-mini-tile{
  background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r);padding:.5rem .85rem;
  display:flex;flex-direction:column;align-items:center;min-width:72px;
}
.prop-mini-val{
  font-size:1.05rem;font-weight:700;
  font-family:'Consolas','Menlo',monospace;font-variant-numeric:tabular-nums;
}
.prop-mini-lbl{font-size:.58rem;color:var(--tm);text-transform:uppercase;letter-spacing:.09em;margin-top:.2rem}
.prop-chart-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:.5rem}
.prop-chart-scroll canvas{display:block;min-height:220px}

/* ── Mobile (≤540px — iPhone 13 and similar) ── */
@media(max-width:540px){
  .shell{padding:.75rem .85rem 2.5rem}

  /* Header */
  .hdr-date{display:none}
  .brand-text h1{font-size:.85rem}

  /* KPI strip: tighter tiles */
  .kpi-strip{gap:.5rem}
  .kpi-tile{padding:.7rem .75rem}
  .kpi-value{font-size:1.2rem}
  .kpi-sub{font-size:.6rem}

  /* Game accordion: tighter horizontal padding */
  .game-hdr{padding:.75rem .85rem}
  .game-title{font-size:.82rem}
  .game-body-inner{padding:0 .85rem}
  .game-group.open .game-body-inner{padding:.6rem .85rem .85rem}

  /* Pick rows: stack vertically so nothing is cut off */
  .pick-row{flex-direction:column;align-items:flex-start;gap:.35rem}
  .pick-row-left{width:100%}
  .pick-row-market{white-space:normal;word-break:break-word;font-size:.82rem}
  .pick-row-proj{white-space:normal;word-break:break-word}
  .pick-row-right{width:100%;justify-content:space-between;flex-shrink:1}
  .pick-row-meta{text-align:left}
  .pick-row-edge{font-size:.95rem}
  .pick-row-line{font-size:.88rem}

  /* Charts: shorter on mobile */
  .chart-wrap{height:155px}

  /* Table: tighten cell padding so more fits before scroll kicks in */
  tbody td{padding:.45rem .65rem;font-size:.75rem}
  thead th{padding:.55rem .65rem}

  /* Tabs on mobile */
  .tab{padding:.55rem .7rem;font-size:.65rem}

  /* Prop controls full-width on mobile */
  .prop-select{max-width:100%;font-size:.75rem}
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

<div class="tabs" role="tablist">
  <button class="tab active" role="tab" data-tab="picks">Today&thinsp;<span class="tab-badge" id="tab-picks-badge">0</span></button>
  <button class="tab" role="tab" data-tab="performance">Performance</button>
  <button class="tab" role="tab" data-tab="props">Player Props</button>
  <button class="tab" role="tab" data-tab="history">History&thinsp;<span class="tab-badge" id="tab-history-badge">0</span></button>
</div>

<!-- Today's Picks -->
<div id="tab-picks" class="tab-panel active">
  <div class="sec" style="margin-top:1rem">
    <h2>Today's Picks</h2>
    <div class="sec-rule"></div>
  </div>
  <div id="picks-container"></div>
</div>

<!-- Performance -->
<div id="tab-performance" class="tab-panel">
  <div class="sec" style="margin-top:1rem">
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
</div>

<!-- Player Props -->
<div id="tab-props" class="tab-panel">
  <div class="sec" style="margin-top:1rem">
    <h2>Player Props History</h2>
    <div class="sec-rule"></div>
  </div>
  <div class="prop-controls">
    <select id="prop-player" class="prop-select"><option value="">Select player…</option></select>
    <select id="prop-stat" class="prop-select" disabled><option value="">Select stat…</option></select>
  </div>
  <div id="prop-mini-strip" class="prop-mini-strip" style="display:none"></div>
  <div class="chart-card" id="prop-chart-card" style="display:none">
    <div class="chart-title" id="prop-chart-title"></div>
    <div class="prop-chart-scroll"><canvas id="prop-chart"></canvas></div>
  </div>
  <div class="no-picks" id="prop-empty">Select a player and stat above to see their prop history.</div>
</div>

<!-- Bet History -->
<div id="tab-history" class="tab-panel">
  <div class="sec" style="margin-top:1rem">
    <h2>Bet History</h2>
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
</div>

<footer id="footer-text"></footer>
</div>

<script>
const D = __DATA__;

document.getElementById('updated-at').textContent = 'Updated ' + D.generated_at;
document.getElementById('hdr-date').textContent = D.today;

// ── KPI strip ────────────────────────────────────────────────
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

// ── Tabs ─────────────────────────────────────────────────────
let perfInited = false;
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'performance' && !perfInited) {
      perfInited = true;
      initPerfCharts();
    }
    if (tab.dataset.tab === 'props' && propChart) {
      setTimeout(() => propChart.resize(), 10);
    }
  });
});

// ── Today's picks ─────────────────────────────────────────────
const picks = D.today_picks;
document.getElementById('tab-picks-badge').textContent = picks.length;
const pc = document.getElementById('picks-container');
if (!picks.length) {
  pc.innerHTML = '<div class="no-picks">No picks logged for today yet — check back after the next run.</div>';
} else {
  // Group picks by game event, sort groups by best edge desc
  const byGame={};
  picks.forEach(p=>{
    const ev=p.event||'?';
    if(!byGame[ev])byGame[ev]={sport:p.sport,picks:[]};
    byGame[ev].picks.push(p);
  });
  const gameList=Object.entries(byGame).sort((a,b)=>
    Math.max(...b[1].picks.map(p=>p.edge||0))-Math.max(...a[1].picks.map(p=>p.edge||0))
  );
  pc.innerHTML='<div class="game-list">'+gameList.map(([event,group],idx)=>{
    const isSoccer=(group.sport||'').toLowerCase()==='soccer';
    const rowsHtml=group.picks.map(p=>{
      const edge=((p.edge||0)*100).toFixed(1);
      const stake=(p.stake_units||0).toFixed(2);
      const proj=p.projected_score?`<div class="pick-row-proj">${p.projected_score}</div>`:'';
      return `<div class="pick-row">
        <div class="pick-row-left">
          <div class="pick-row-market">${p.market||'?'}</div>${proj}
        </div>
        <div class="pick-row-right">
          <span class="pick-row-edge">+${edge}%</span>
          <span class="pick-row-line">${p.line||'?'}</span>
          <div class="pick-row-meta">
            <div class="pick-row-book">${p.book||'?'}</div>
            <div class="pick-row-stake">${stake}u</div>
          </div>
        </div>
      </div>`;
    }).join('');
    const n=group.picks.length;
    return `<div class="game-group${idx===0?' open':''}">
      <div class="game-hdr" onclick="this.closest('.game-group').classList.toggle('open')">
        <div class="game-hdr-left">
          <span class="sport-chip${isSoccer?' soccer':''}">${group.sport||'?'}</span>
          <span class="game-title">${event}</span>
        </div>
        <div class="game-hdr-right">
          <span class="game-count">${n} pick${n!==1?'s':''}</span>
          <span class="game-chevron">▼</span>
        </div>
      </div>
      <div class="game-body"><div class="game-body-inner">${rowsHtml}</div></div>
    </div>`;
  }).join('')+'</div>';
}

// ── Performance charts (lazy — init on first tab click) ───────
function initPerfCharts() {
  const pnlData = D.daily_pnl;
  if (pnlData.length > 0) {
    const labels = pnlData.map(r => r.day);
    const vals = pnlData.map(r => r.cumulative);
    const lastVal = vals[vals.length-1] || 0;
    const lc = lastVal >= 0 ? '#4ADE80' : '#F87171';
    const fc = lastVal >= 0 ? 'rgba(74,222,128,.07)' : 'rgba(248,113,113,.07)';
    new Chart(document.getElementById('pnl-chart'), {
      type: 'line',
      data: {labels, datasets: [{
        data: vals, borderColor: lc, backgroundColor: fc,
        borderWidth: 1.5, fill: true, tension: .35,
        pointRadius: vals.length > 20 ? 0 : 3,
        pointHoverRadius: 5, pointBackgroundColor: lc,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => (ctx.raw>=0?'+':'')+ctx.raw.toFixed(2)+'u'}}},
        scales: {
          x: {ticks: {color:'#4E6480',font:{size:9,family:'Consolas,Menlo,monospace'}}, grid: {color:'rgba(30,46,70,.5)'}},
          y: {ticks: {color:'#4E6480',font:{size:9,family:'Consolas,Menlo,monospace'},callback:v=>(v>0?'+':'')+v+'u'}, grid: {color:'rgba(30,46,70,.8)',borderDash:[3,3]}}
        }
      }
    });
  } else {
    document.getElementById('pnl-chart').parentElement.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.8rem">No resolved bets yet</div>';
  }
  const mkts = D.top_markets;
  if (mkts.length > 0) {
    const cats = ['#5A7AE8','#18A88A','#C8800F','#E04868','#7B9BFF','#1EC09E','#D4960F','#F0607A'];
    new Chart(document.getElementById('mkt-chart'), {
      type: 'bar',
      data: {
        labels: mkts.map(m => m.market),
        datasets: [{
          data: mkts.map(m => m.count),
          backgroundColor: mkts.map((_,i) => cats[i%cats.length]+'BB'),
          borderColor: mkts.map((_,i) => cats[i%cats.length]),
          borderWidth: 1, borderRadius: 3, borderSkipped: 'start',
        }]
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => ctx.raw+' picks'}}},
        scales: {
          x: {ticks: {color:'#4E6480',font:{size:9}}, grid: {color:'rgba(30,46,70,.8)'}},
          y: {ticks: {color:'#8B9EC4',font:{size:10,family:'Consolas,Menlo,monospace'}}, grid: {display:false}}
        }
      }
    });
  } else {
    document.getElementById('mkt-chart').parentElement.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.8rem">No data yet</div>';
  }
}

// ── Player Props ──────────────────────────────────────────────
const propBets = D.prop_bets || [];
const playerSel = document.getElementById('prop-player');
const statSel   = document.getElementById('prop-stat');
const propEmpty = document.getElementById('prop-empty');
const propCard  = document.getElementById('prop-chart-card');
const propMini  = document.getElementById('prop-mini-strip');
let propChart   = null;

if (propBets.length) {
  const players = [...new Set(propBets.map(p => p.player))].sort();
  players.forEach(p => playerSel.add(new Option(p, p)));
} else {
  playerSel.disabled = true;
  propEmpty.textContent = 'No player prop bets recorded yet — props will appear here after the next daily card run.';
}

function updateStatOptions(player) {
  statSel.innerHTML = '<option value="">Select stat…</option>';
  if (!player) { statSel.disabled = true; return; }
  const stats = [...new Set(propBets.filter(p => p.player === player).map(p => p.stat))].sort();
  stats.forEach(s => statSel.add(new Option(s.replace(/^(Batter|Pitcher)\s+/,''), s)));
  statSel.disabled = false;
}

function renderPropChart() {
  const player = playerSel.value;
  const stat   = statSel.value;
  if (!player || !stat) {
    propCard.style.display = 'none'; propMini.style.display = 'none';
    propEmpty.style.display = ''; return;
  }
  const rows = propBets.filter(p => p.player === player && p.stat === stat);
  rows.sort((a, b) => a.date.localeCompare(b.date));

  propEmpty.style.display = 'none';
  propCard.style.display  = '';
  propMini.style.display  = '';

  // Mini summary strip
  const won    = rows.filter(r => r.result === 'win').length;
  const lost   = rows.filter(r => r.result === 'loss').length;
  const pend   = rows.filter(r => !r.result).length;
  const profit = rows.reduce((s, r) => s + r.profit, 0);
  propMini.innerHTML = [
    {v: rows.length,  l:'Bets',    c:'kpi-neu'},
    {v: won,          l:'Won',     c:'kpi-up'},
    {v: lost,         l:'Lost',    c:'kpi-dn'},
    {v: pend,         l:'Pending', c:'kpi-neu'},
    {v: (profit>=0?'+':'')+profit.toFixed(2)+'u', l:'P&L', c: profit>=0?'kpi-up':'kpi-dn'},
  ].map(t => `<div class="prop-mini-tile"><div class="prop-mini-val ${t.c}">${t.v}</div><div class="prop-mini-lbl">${t.l}</div></div>`).join('');

  const statLabel = stat.replace(/^(Batter|Pitcher)\s+/, '');
  document.getElementById('prop-chart-title').textContent =
    player + ' — ' + statLabel + ' (' + rows.length + ' bet' + (rows.length !== 1 ? 's' : '') + ')';

  const labels = rows.map(r => {
    const parts = (r.event || '').split(' @ ');
    const short = parts.length === 2
      ? parts.map(t => t.trim().split(' ').slice(-1)[0]).join('@')
      : (r.event || '').slice(0, 12);
    return r.date.slice(5) + ' ' + short;
  });
  const barBg = rows.map(r =>
    r.result==='win'  ? 'rgba(74,222,128,.75)'  :
    r.result==='loss' ? 'rgba(248,113,113,.75)' : 'rgba(78,100,128,.45)'
  );
  const barBd = rows.map(r =>
    r.result==='win'  ? '#4ADE80' :
    r.result==='loss' ? '#F87171' : '#4E6480'
  );

  const canvas = document.getElementById('prop-chart');
  canvas.style.width  = Math.max(600, rows.length * 38) + 'px';
  canvas.style.height = '230px';

  if (propChart) propChart.destroy();
  propChart = new Chart(canvas, {
    data: {
      labels,
      datasets: [
        {
          type: 'bar', label: 'Bet line',
          data: rows.map(r => r.threshold),
          backgroundColor: barBg, borderColor: barBd,
          borderWidth: 1.5, borderRadius: 4, order: 2,
        },
        {
          type: 'line', label: 'Projected',
          data: rows.map(r => r.proj_stat),
          borderColor: '#C8800F', backgroundColor: 'transparent',
          borderWidth: 2, borderDash: [4, 3],
          pointRadius: 4, pointBackgroundColor: '#C8800F',
          pointBorderColor: '#0B1120', pointBorderWidth: 1,
          tension: 0.2, spanGaps: true, order: 1,
        }
      ]
    },
    options: {
      responsive: false, maintainAspectRatio: false,
      interaction: {mode:'index', intersect:false},
      plugins: {
        legend: {
          display: true, position: 'top', align: 'start',
          labels: {color:'#8B9EC4', font:{size:10}, boxWidth:14, padding:14}
        },
        tooltip: {
          callbacks: {
            title: ctx => rows[ctx[0].dataIndex].event || '',
            label: ctx => {
              if (ctx.dataset.label === 'Projected')
                return ctx.raw !== null ? 'Projected: ' + ctx.raw : null;
              const r = rows[ctx.dataIndex];
              return [
                r.side + r.threshold + '  Edge: +' + r.edge + '%  Stake: ' + r.stake + 'u',
                r.result ? 'Result: ' + (r.result==='win' ? 'Win ✓' : 'Loss ✗') : 'Result: Pending',
              ];
            }
          }
        }
      },
      scales: {
        x: {
          ticks: {color:'#4E6480', font:{size:8,family:'Consolas,Menlo,monospace'}, maxRotation:40, minRotation:30},
          grid: {display:false}
        },
        y: {
          title: {display:true, text:statLabel, color:'#8B9EC4', font:{size:10}},
          ticks: {color:'#4E6480', font:{size:9}},
          grid: {color:'rgba(30,46,70,.5)', borderDash:[3,3]},
          beginAtZero: true,
        }
      }
    }
  });
}

playerSel.addEventListener('change', () => { updateStatOptions(playerSel.value); renderPropChart(); });
statSel.addEventListener('change', renderPropChart);

// ── Bet History ───────────────────────────────────────────────
const bets = D.recent_bets;
document.getElementById('tab-history-badge').textContent = bets.length;
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
