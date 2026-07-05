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
# HTML template
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>+EV Betting Bot — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0F172A;--surface:#1E293B;--border:#334155;
  --t1:#F1F5F9;--t2:#CBD5E1;--tm:#64748B;
  --blue:#60A5FA;--teal:#34D399;--green:#4ADE80;
  --red:#F87171;--yellow:#FACC15;--purple:#A78BFA;
  --card-r:10px;
}
body{background:var(--bg);color:var(--t1);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  min-height:100vh;padding:1.5rem 2rem}

/* ── Header ── */
.hdr{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:2rem;flex-wrap:wrap;gap:.75rem}
.hdr-left{display:flex;align-items:center;gap:.75rem}
.logo{width:2.5rem;height:2.5rem;background:linear-gradient(135deg,#3B82F6,#10B981);
  border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:1.25rem;font-weight:700}
.hdr h1{font-size:1.25rem;font-weight:700;letter-spacing:-.02em}
.hdr-sub{color:var(--tm);font-size:.8rem;margin-top:.1rem}
.live-badge{display:flex;align-items:center;gap:.4rem;
  background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.3);
  border-radius:20px;padding:.3rem .75rem;font-size:.78rem;color:var(--green)}
.live-dot{width:6px;height:6px;background:var(--green);border-radius:50%;
  animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── KPI row ── */
.kpi-row{display:grid;
  grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1rem;margin-bottom:2rem}
.kpi{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--card-r);padding:1.1rem 1.25rem}
.kpi-label{color:var(--tm);font-size:.72rem;text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:.4rem}
.kpi-value{font-size:1.75rem;font-weight:700;line-height:1;letter-spacing:-.03em}
.kpi-sub{color:var(--tm);font-size:.75rem;margin-top:.35rem}
.up{color:var(--green)}.dn{color:var(--red)}.neu{color:var(--t2)}

/* ── Section heading ── */
.sec-head{font-size:1rem;font-weight:600;margin-bottom:1rem;
  display:flex;align-items:center;gap:.5rem}
.sec-head .badge{background:var(--border);border-radius:20px;
  padding:.15rem .55rem;font-size:.72rem;color:var(--t2);font-weight:500}

/* ── Today's picks ── */
.picks-grid{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem;
  margin-bottom:2rem}
.pick-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--card-r);padding:1rem 1.1rem;position:relative;
  overflow:hidden}
.pick-card::before{content:'';position:absolute;top:0;left:0;
  width:3px;height:100%;background:var(--blue)}
.pick-card.soccer::before{background:var(--teal)}
.pick-sport{font-size:.68rem;text-transform:uppercase;letter-spacing:.1em;
  color:var(--blue);font-weight:600;margin-bottom:.3rem}
.pick-card.soccer .pick-sport{color:var(--teal)}
.pick-event{font-size:.9rem;font-weight:600;margin-bottom:.2rem;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pick-market{color:var(--t2);font-size:.82rem;margin-bottom:.75rem;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pick-meta{display:flex;justify-content:space-between;align-items:flex-end}
.pick-odds{display:flex;align-items:baseline;gap:.5rem}
.pick-line{font-size:1.15rem;font-weight:700;color:var(--yellow)}
.pick-book{font-size:.72rem;color:var(--tm)}
.pick-stats{text-align:right}
.pick-edge{font-size:.78rem;color:var(--green);font-weight:600}
.pick-stake{font-size:.72rem;color:var(--tm);margin-top:.15rem}
.no-picks{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--card-r);padding:2rem;text-align:center;
  color:var(--tm);margin-bottom:2rem}

/* ── Charts row ── */
.charts-row{display:grid;
  grid-template-columns:2fr 1fr;gap:1rem;margin-bottom:2rem}
@media(max-width:800px){.charts-row{grid-template-columns:1fr}}
.chart-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--card-r);padding:1.25rem}
.chart-wrap{position:relative;height:220px}

/* ── Bet table ── */
.tbl-wrap{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--card-r);overflow:hidden;margin-bottom:2rem}
table{width:100%;border-collapse:collapse;font-size:.82rem}
thead th{background:rgba(51,65,85,.6);padding:.7rem 1rem;
  text-align:left;font-weight:500;color:var(--tm);
  font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;
  white-space:nowrap;cursor:pointer;user-select:none}
thead th:hover{color:var(--t2)}
thead th.sort-asc::after{content:' ▲'}
thead th.sort-desc::after{content:' ▼'}
tbody tr{border-top:1px solid var(--border)}
tbody tr:hover{background:rgba(51,65,85,.3)}
tbody td{padding:.65rem 1rem;vertical-align:middle;white-space:nowrap}
.cell-sport{display:inline-block;font-size:.65rem;text-transform:uppercase;
  letter-spacing:.08em;padding:.15rem .45rem;border-radius:4px;font-weight:600}
.sp-MLB{background:rgba(96,165,250,.15);color:var(--blue)}
.sp-Soccer{background:rgba(52,211,153,.15);color:var(--teal)}
.result-win{color:var(--green);font-weight:600}
.result-loss{color:var(--red);font-weight:600}
.result-pending{color:var(--tm)}
.tbl-scroll{overflow-x:auto}

/* ── Footer ── */
footer{color:var(--tm);font-size:.75rem;text-align:center;padding-top:1rem}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-left">
    <div class="logo">📈</div>
    <div>
      <h1>+EV Sports Betting Bot</h1>
      <div class="hdr-sub" id="updated-at"></div>
    </div>
  </div>
  <div class="live-badge"><span class="live-dot"></span>Live dashboard</div>
</header>

<!-- KPI tiles -->
<div class="kpi-row" id="kpi-row"></div>

<!-- Today's picks -->
<div class="sec-head">
  Today's Picks <span class="badge" id="picks-badge">0</span>
</div>
<div id="picks-container"></div>

<!-- Charts -->
<div class="charts-row">
  <div class="chart-card">
    <div class="sec-head">Cumulative P&amp;L (units)</div>
    <div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="sec-head">Top Markets</div>
    <div class="chart-wrap"><canvas id="mkt-chart"></canvas></div>
  </div>
</div>

<!-- Bet history -->
<div class="sec-head">
  Recent Bets <span class="badge" id="history-badge">0</span>
</div>
<div class="tbl-wrap">
  <div class="tbl-scroll">
    <table id="bet-table">
      <thead>
        <tr>
          <th onclick="sortTable(0)">Sport</th>
          <th onclick="sortTable(1)">Event</th>
          <th onclick="sortTable(2)">Market</th>
          <th onclick="sortTable(3)">Book</th>
          <th onclick="sortTable(4)">Line</th>
          <th onclick="sortTable(5)">Edge</th>
          <th onclick="sortTable(6)">Stake</th>
          <th onclick="sortTable(7)">Result</th>
          <th onclick="sortTable(8)">P&amp;L</th>
          <th onclick="sortTable(9)">Logged</th>
        </tr>
      </thead>
      <tbody id="bet-tbody"></tbody>
    </table>
  </div>
</div>

<footer>Generated by +EV Betting Bot · <span id="footer-date"></span></footer>

<script>
const D = __DATA__;

// ── Populate header ──────────────────────────────────────────────────────────
document.getElementById('updated-at').textContent = 'Updated ' + D.generated_at;
document.getElementById('footer-date').textContent = D.generated_at;

// ── KPI tiles ───────────────────────────────────────────────────────────────
const s = D.stats;
const kpis = [
  { label:'Total Picks',   value: s.total_picks.toLocaleString(),
    sub:'all time', cls:'neu' },
  { label:'Win Rate',      value: s.n_resolved > 0 ? s.win_rate+'%' : 'N/A',
    sub: s.n_resolved + ' resolved', cls: s.win_rate >= 52 ? 'up' : s.win_rate > 0 ? 'neu' : 'dn' },
  { label:'ROI',           value: s.n_resolved > 0 ? (s.roi>0?'+':'')+s.roi+'%' : 'N/A',
    sub:'on resolved bets', cls: s.roi > 0 ? 'up' : s.roi < 0 ? 'dn' : 'neu' },
  { label:'Total P&L',     value: s.n_resolved > 0 ? (s.total_profit>0?'+':'')+s.total_profit+'u' : 'N/A',
    sub:'units', cls: s.total_profit > 0 ? 'up' : s.total_profit < 0 ? 'dn' : 'neu' },
  { label:'Avg Edge',      value: s.avg_edge+'%',
    sub:'model vs market', cls: s.avg_edge >= 3 ? 'up' : 'neu' },
  { label:'Avg CLV',       value: s.avg_clv !== null ? (s.avg_clv>0?'+':'')+s.avg_clv+'%' : 'N/A',
    sub:'closing line value', cls: s.avg_clv > 0 ? 'up' : 'dn' },
];
document.getElementById('kpi-row').innerHTML = kpis.map(k => `
  <div class="kpi">
    <div class="kpi-label">${k.label}</div>
    <div class="kpi-value ${k.cls}">${k.value}</div>
    <div class="kpi-sub">${k.sub}</div>
  </div>`).join('');

// ── Today's picks ────────────────────────────────────────────────────────────
const picks = D.today_picks;
document.getElementById('picks-badge').textContent = picks.length;
const pc = document.getElementById('picks-container');
if (!picks.length) {
  pc.innerHTML = '<div class="no-picks">No picks logged for today yet — run the bot or check back later.</div>';
} else {
  pc.innerHTML = '<div class="picks-grid">' + picks.map(p => {
    const sport = (p.sport||'').toLowerCase();
    const edge  = ((p.edge||0)*100).toFixed(1);
    const stake = (p.stake_units||0).toFixed(2);
    const proj  = p.projected_score ? `<div style="color:var(--tm);font-size:.7rem;margin-top:.5rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${p.projected_score}</div>` : '';
    return `<div class="pick-card ${sport==='soccer'?'soccer':''}">
      <div class="pick-sport">${p.sport||'?'}</div>
      <div class="pick-event" title="${p.event||''}">${p.event||'?'}</div>
      <div class="pick-market" title="${p.market||''}">${p.market||'?'}</div>
      <div class="pick-meta">
        <div class="pick-odds">
          <span class="pick-line">${p.line||'?'}</span>
          <span class="pick-book">${p.book||'?'}</span>
        </div>
        <div class="pick-stats">
          <div class="pick-edge">+${edge}% edge</div>
          <div class="pick-stake">${stake}u stake</div>
        </div>
      </div>${proj}
    </div>`;
  }).join('') + '</div>';
}

// ── P&L chart (line) ─────────────────────────────────────────────────────────
// Form: change-over-time → line. One series. Color: green if cumulative > 0.
const pnlData = D.daily_pnl;
if (pnlData.length > 0) {
  const labels = pnlData.map(r => r.day);
  const vals   = pnlData.map(r => r.cumulative);
  const lastVal = vals[vals.length - 1] || 0;
  const lineColor = lastVal >= 0 ? '#4ADE80' : '#F87171';
  const fillColor = lastVal >= 0 ? 'rgba(74,222,128,.12)' : 'rgba(248,113,113,.12)';

  new Chart(document.getElementById('pnl-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: vals,
        borderColor: lineColor,
        backgroundColor: fillColor,
        borderWidth: 2,
        fill: true,
        tension: 0.3,
        pointRadius: vals.length > 30 ? 0 : 4,
        pointHoverRadius: 6,
        pointBackgroundColor: lineColor,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => (ctx.raw >= 0 ? '+' : '') + ctx.raw.toFixed(2) + 'u'
          }
        }
      },
      scales: {
        x: { ticks:{color:'#64748B',font:{size:10}}, grid:{color:'#1E293B'} },
        y: {
          ticks:{color:'#64748B',font:{size:10},
            callback: v => (v>0?'+':'')+v+'u'},
          grid:{color:'#334155'},
          border:{dash:[4,4]}
        }
      }
    }
  });
} else {
  document.getElementById('pnl-chart').parentElement.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.85rem">No resolved bets yet</div>';
}

// ── Market chart (horizontal bar) ────────────────────────────────────────────
// Form: magnitude + identity → horizontal bar. Categorical colors: use border (neutral).
const mkts = D.top_markets;
if (mkts.length > 0) {
  new Chart(document.getElementById('mkt-chart'), {
    type: 'bar',
    data: {
      labels: mkts.map(m => m.market),
      datasets: [{
        data: mkts.map(m => m.count),
        backgroundColor: mkts.map((_,i) => [
          'rgba(96,165,250,.75)','rgba(52,211,153,.75)',
          'rgba(167,139,250,.75)','rgba(250,204,21,.75)',
          'rgba(251,146,60,.75)','rgba(248,113,113,.75)',
          'rgba(96,165,250,.5)','rgba(52,211,153,.5)',
        ][i % 8]),
        borderWidth: 0,
        borderRadius: 4,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ctx.raw + ' picks' } } },
      scales: {
        x: { ticks:{color:'#64748B',font:{size:10}}, grid:{color:'#334155'} },
        y: { ticks:{color:'#CBD5E1',font:{size:11}}, grid:{display:false} }
      }
    }
  });
} else {
  document.getElementById('mkt-chart').parentElement.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--tm);font-size:.85rem">No bet history yet</div>';
}

// ── Bet history table ─────────────────────────────────────────────────────────
const bets = D.recent_bets;
document.getElementById('history-badge').textContent = bets.length;
const tbody = document.getElementById('bet-tbody');
tbody.innerHTML = bets.map(b => {
  const edge  = b.edge  != null ? '+' + (b.edge*100).toFixed(1)+'%' : '—';
  const stake = b.stake_units != null ? (b.stake_units).toFixed(2)+'u' : '—';
  const resultCls = b.result === 'win'  ? 'result-win' :
                    b.result === 'loss' ? 'result-loss' : 'result-pending';
  const resultTxt = b.result || '⏳';
  const pnl = b.profit_units != null
    ? `<span class="${b.profit_units>=0?'up':'dn'}">${b.profit_units>=0?'+':''}${(+b.profit_units).toFixed(2)}u</span>`
    : '—';
  const logged = (b.logged_at||'').split(' ')[0];
  const sp = b.sport || '?';
  const spCls = sp === 'MLB' ? 'sp-MLB' : sp === 'Soccer' ? 'sp-Soccer' : '';
  return `<tr>
    <td><span class="cell-sport ${spCls}">${sp}</span></td>
    <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${b.event||''}">${b.event||'?'}</td>
    <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis" title="${b.market||''}">${b.market||'?'}</td>
    <td>${b.book||'?'}</td>
    <td style="font-weight:600;color:var(--yellow)">${b.line||'?'}</td>
    <td style="color:var(--green)">${edge}</td>
    <td>${stake}</td>
    <td class="${resultCls}">${resultTxt}</td>
    <td>${pnl}</td>
    <td style="color:var(--tm)">${logged}</td>
  </tr>`;
}).join('');

// ── Sortable table ────────────────────────────────────────────────────────────
let sortCol = -1, sortDir = 1;
function sortTable(col) {
  const ths = document.querySelectorAll('thead th');
  ths.forEach((th, i) => { th.classList.remove('sort-asc','sort-desc'); });
  if (sortCol === col) { sortDir *= -1; } else { sortDir = 1; sortCol = col; }
  ths[col].classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
  const rows = [...document.querySelectorAll('#bet-tbody tr')];
  rows.sort((a, b) => {
    const av = a.cells[col].textContent.trim();
    const bv = b.cells[col].textContent.trim();
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return (an - bn) * sortDir;
    return av.localeCompare(bv) * sortDir;
  });
  tbody.append(...rows);
}
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
