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
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
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


# ---------------------------------------------------------------------------
# MLB Stats API helpers (season game logs + handedness splits)
# ---------------------------------------------------------------------------

_MLB_API = "https://statsapi.mlb.com/api/v1"
_pid_cache: dict = {}

_MLB_STAT_KEY = {
    "Batter Hits":          "hits",
    "Batter Total Bases":   "totalBases",
    "Batter Home Runs":     "homeRuns",
    "Batter Rbis":          "rbi",
    "Batter Walks":         "baseOnBalls",
    "Batter Strikeouts":    "strikeOuts",
    "Pitcher Strikeouts":   "strikeOuts",
    "Pitcher Outs":         "outs",
    "Pitcher Hits Allowed": "hits",
    "Pitcher Walks":        "baseOnBalls",
    "Pitcher Earned Runs":  "earnedRuns",
}

_PITCHER_STATS = {
    "Pitcher Strikeouts", "Pitcher Outs",
    "Pitcher Hits Allowed", "Pitcher Walks", "Pitcher Earned Runs",
}


def _mlb_get(path, params=None, timeout=7):
    url = _MLB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Python/urllib"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def _mlb_player_id(name):
    if name in _pid_cache:
        return _pid_cache[name]
    data = _mlb_get("/people/search", {"names": name, "sportId": 1})
    pid = None
    for p in (data.get("people") or []):
        if p.get("fullName", "").lower() == name.lower():
            pid = p["id"]
            break
    if pid is None and data.get("people"):
        pid = data["people"][0].get("id")
    _pid_cache[name] = pid
    return pid


def _mlb_game_log(pid, group, stat_key, season=2025):
    data = _mlb_get(f"/people/{pid}/stats", {
        "stats": "gameLog", "season": season,
        "group": group,     "gameType": "R",
    })
    games = []
    for blk in (data.get("stats") or []):
        for sp in (blk.get("splits") or []):
            s   = sp.get("stat", {})
            opp = (sp.get("opponent") or {})
            val = s.get(stat_key)
            games.append({
                "date": (sp.get("date") or "")[:10],
                "opp":  opp.get("abbreviation") or opp.get("teamName") or "",
                "home": sp.get("isHome", True),
                "val":  float(val) if val is not None else None,
            })
    return games  # chronological order oldest→newest


def _mlb_hand_splits(pid, group, stat_key, season=2025):
    data = _mlb_get(f"/people/{pid}/stats", {
        "stats": "statSplits", "season": season,
        "group": group, "sitCodes": "vr,vl", "gameType": "R",
    })
    out = {}
    for blk in (data.get("stats") or []):
        for sp in (blk.get("splits") or []):
            code = (sp.get("split") or {}).get("code", "")
            if code in ("vr", "vl"):
                s = sp.get("stat", {})
                label = "vs RHP" if code == "vr" else "vs LHP"
                out[label] = {
                    "g":    s.get("gamesPlayed", 0),
                    "avg":  s.get("avg", "---"),
                    "ops":  s.get("ops", "---"),
                    "stat": s.get(stat_key, 0),
                    "ab":   s.get("atBats") if s.get("atBats") is not None else s.get("battersFaced", 0),
                }
    return out


def fetch_player_seasons(prop_bets, season=2025):
    """For each unique player+stat in prop_bets, fetch MLB season game log + splits."""
    seen = {}
    for bet in prop_bets:
        player   = (bet.get("player") or "").strip()
        stat     = (bet.get("stat")   or "").strip()
        key      = f"{player}||{stat}"
        if not player or key in seen:
            continue
        is_pitcher = stat in _PITCHER_STATS
        group      = "pitching" if is_pitcher else "hitting"
        stat_key   = _MLB_STAT_KEY.get(stat)
        pid        = _mlb_player_id(player)
        if not pid or not stat_key:
            seen[key] = {"games": [], "splits": {}}
            continue
        games  = _mlb_game_log(pid, group, stat_key, season)
        splits = {} if is_pitcher else _mlb_hand_splits(pid, group, stat_key, season)
        seen[key] = {"pid": pid, "games": games, "splits": splits}
        time.sleep(0.12)
    return seen


_PITCHER_POS = {"P", "SP", "RP", "LRP", "MRP", "SU", "CL"}


def fetch_all_player_stats(season=2025):
    """Fetch every MLB player who appeared this season, then overlay season stats."""
    result = {}

    # Step 1 — full player roster for the season (one call, ~800-1000 players)
    data = _mlb_get("/sports/1/players", {"season": season, "gameType": "R"})
    for p in (data.get("people") or []):
        pid = p.get("id")
        if not pid:
            continue
        pos_abbr = (p.get("primaryPosition") or {}).get("abbreviation", "")
        is_pitcher = pos_abbr in _PITCHER_POS
        team = (p.get("currentTeam") or {}).get("abbreviation", "")
        pitch_hand = (p.get("pitchHand") or {}).get("code", "")
        bat_side   = (p.get("batSide")   or {}).get("code", "")
        result[str(pid)] = {
            "pid": pid, "name": p.get("fullName", ""),
            "team": team, "pos": "P" if is_pitcher else "B", "posDetail": pos_abbr,
            "pitchHand": pitch_hand, "batSide": bat_side,
            "g": 0, "pa": 0, "homeRuns": 0, "k": 0,
            "avg": "---", "ops": "---",
            "ip": "0.0", "pK": 0, "era": "---", "whip": "---",
        }

    def _upsert(pid_str, name, team, pos, posDetail=""):
        if pid_str not in result:
            result[pid_str] = {
                "pid": int(pid_str), "name": name, "team": team,
                "pos": pos, "posDetail": posDetail,
                "pitchHand": "", "batSide": "",
                "g": 0, "pa": 0, "homeRuns": 0, "k": 0,
                "avg": "---", "ops": "---",
                "ip": "0.0", "pK": 0, "era": "---", "whip": "---",
            }

    # Step 2 — overlay batter season stats
    data = _mlb_get("/stats", {"stats": "season", "group": "hitting",
                                "season": season, "sportId": 1, "limit": 1000, "gameType": "R"})
    for blk in (data.get("stats") or []):
        for sp in (blk.get("splits") or []):
            p = sp.get("player", {}); s = sp.get("stat", {}); t = (sp.get("team") or {})
            pid = p.get("id")
            if not pid:
                continue
            pid_str = str(pid)
            _upsert(pid_str, p.get("fullName", ""), t.get("abbreviation", ""), "B")
            result[pid_str].update({
                "team": t.get("abbreviation", "") or result[pid_str]["team"],
                "g": s.get("gamesPlayed", 0), "pa": s.get("plateAppearances", 0),
                "hits": s.get("hits", 0), "totalBases": s.get("totalBases", 0),
                "homeRuns": s.get("homeRuns", 0), "rbi": s.get("rbi", 0),
                "bb": s.get("baseOnBalls", 0), "k": s.get("strikeOuts", 0),
                "avg": s.get("avg", "---"), "ops": s.get("ops", "---"),
            })

    # Step 3 — overlay pitcher season stats
    data = _mlb_get("/stats", {"stats": "season", "group": "pitching",
                                "season": season, "sportId": 1, "limit": 1000, "gameType": "R"})
    for blk in (data.get("stats") or []):
        for sp in (blk.get("splits") or []):
            p = sp.get("player", {}); s = sp.get("stat", {}); t = (sp.get("team") or {})
            pid = p.get("id")
            if not pid:
                continue
            pid_str = str(pid)
            _upsert(pid_str, p.get("fullName", ""), t.get("abbreviation", ""), "P", "P")
            if result[pid_str]["pos"] == "P":
                result[pid_str].update({
                    "team": t.get("abbreviation", "") or result[pid_str]["team"],
                    "g": s.get("gamesPlayed", result[pid_str]["g"]),
                    "ip": s.get("inningsPitched", "0.0"),
                    "pK": s.get("strikeOuts", 0), "pH": s.get("hits", 0),
                    "pBB": s.get("baseOnBalls", 0), "pER": s.get("earnedRuns", 0),
                    "era": s.get("era", "---"), "whip": s.get("whip", "---"),
                })

    return result


def fetch_todays_lineups(today_str):
    """Return player IDs in today's lineups + per-game matchup detail."""
    data = _mlb_get("/schedule", {
        "sportId": 1, "date": today_str,
        "hydrate": "lineups,probablePitchers,team",
        "gameType": "R",
    })
    all_ids = []
    games_detail = []
    for date_entry in (data.get("dates") or []):
        for game in (date_entry.get("games") or []):
            teams = game.get("teams") or {}
            home = teams.get("home", {}); away = teams.get("away", {})
            home_team = (home.get("team") or {}).get("abbreviation", "")
            away_team = (away.get("team") or {}).get("abbreviation", "")
            lineup = game.get("lineups") or {}
            home_batters = [pl["id"] for pl in (lineup.get("homePlayers") or []) if pl.get("id")]
            away_batters = [pl["id"] for pl in (lineup.get("awayPlayers") or []) if pl.get("id")]
            home_pp_pid = (home.get("probablePitcher") or {}).get("id")
            away_pp_pid = (away.get("probablePitcher") or {}).get("id")
            all_ids.extend(home_batters + away_batters)
            if home_pp_pid: all_ids.append(home_pp_pid)
            if away_pp_pid: all_ids.append(away_pp_pid)
            games_detail.append({
                "home_team": home_team, "away_team": away_team,
                "home_batter_pids": home_batters,
                "away_batter_pids": away_batters,
                "home_pitcher_pid": home_pp_pid,
                "away_pitcher_pid": away_pp_pid,
            })
    return {
        "player_ids": list(set(all_ids)),
        "games": [{"home": g["home_team"], "away": g["away_team"]} for g in games_detail],
        "games_detail": games_detail,
        "matchups": {},  # filled in by read_db after all_player_stats is available
    }


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
        SELECT event, market, line, book, edge, stake_units,
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
            "line":      row.get("line") or "?",
            "book":      row.get("book") or "?",
            "edge":      round(_safe_float(row.get("edge")) * 100, 1),
            "stake":     round(_safe_float(row.get("stake_units")), 2),
            "result":    row.get("result") or "",
            "profit":    round(_safe_float(row.get("profit_units") or 0), 2),
            "date":      (row.get("logged_at") or "")[:10],
            "proj_stat": proj_num,
        })

    conn.close()

    # Fetch full-season game logs + handedness splits from MLB Stats API
    try:
        player_seasons = fetch_player_seasons(prop_bets)
    except Exception as exc:
        print(f"Warning: MLB API fetch failed: {exc}")
        player_seasons = {}

    # Bulk fetch all active MLB players for encyclopedia view
    try:
        all_player_stats = fetch_all_player_stats()
    except Exception as exc:
        print(f"Warning: bulk player stats fetch failed: {exc}")
        all_player_stats = {}

    # Fetch today's confirmed lineups + probable pitchers
    try:
        today_lineups = fetch_todays_lineups(today_str)
        # Build per-batter matchup dict: home pitcher faces away batters and vice versa
        matchups = {}
        for gd in today_lineups.get("games_detail", []):
            hp_pid = gd.get("home_pitcher_pid")
            ap_pid = gd.get("away_pitcher_pid")
            # Home team's pitcher faces away team's batters
            if hp_pid:
                hp = all_player_stats.get(str(hp_pid), {})
                for bpid in gd.get("away_batter_pids", []):
                    matchups[str(bpid)] = {
                        "pitcher_pid": hp_pid,
                        "pitcher_name": hp.get("name", ""),
                        "pitcher_hand": hp.get("pitchHand", ""),
                        "opp_team": gd.get("home_team", ""),
                    }
            # Away team's pitcher faces home team's batters
            if ap_pid:
                ap = all_player_stats.get(str(ap_pid), {})
                for bpid in gd.get("home_batter_pids", []):
                    matchups[str(bpid)] = {
                        "pitcher_pid": ap_pid,
                        "pitcher_name": ap.get("name", ""),
                        "pitcher_hand": ap.get("pitchHand", ""),
                        "opp_team": gd.get("away_team", ""),
                    }
        today_lineups["matchups"] = matchups
    except Exception as exc:
        print(f"Warning: today's lineups fetch failed: {exc}")
        today_lineups = {"player_ids": [], "games": [], "games_detail": [], "matchups": {}}

    return {
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
        "today":          today_str,
        "today_picks":    today_picks,
        "recent_bets":    bets[:100],
        "daily_pnl":      daily_rows,
        "top_markets":    top_markets,
        "sports":         sports,
        "prop_bets":      prop_bets,
        "player_seasons": player_seasons,
        "all_player_stats": all_player_stats,
        "today_lineups":  today_lineups,
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
/* Pick type badges */
.pick-type{display:inline-flex;align-items:center;padding:.1rem .38rem;border-radius:3px;font-size:.54rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;margin-right:.38rem;border:1px solid;vertical-align:middle;white-space:nowrap}
.pick-type.ml{background:rgba(90,122,232,.12);color:var(--mlb);border-color:rgba(90,122,232,.3)}
.pick-type.spread{background:rgba(24,168,138,.12);color:#18A88A;border-color:rgba(24,168,138,.3)}
.pick-type.total{background:rgba(200,128,15,.12);color:var(--amber);border-color:rgba(200,128,15,.3)}
.pick-type.nrfi{background:rgba(124,185,232,.12);color:#7CB9E8;border-color:rgba(124,185,232,.3)}
.pick-type.prop{background:rgba(248,113,113,.12);color:var(--loss);border-color:rgba(248,113,113,.3)}
.pick-type.other{background:rgba(139,158,196,.1);color:var(--t2);border-color:rgba(139,158,196,.2)}
/* Prob/fair row */
.pick-prob-row{font-size:.62rem;color:var(--tm);margin-top:.18rem;display:flex;gap:.6rem;flex-wrap:wrap}
.pick-prob-lbl{color:var(--t2);font-weight:600}
/* Daily Player Props tab */
.daily-props-grid{display:flex;flex-direction:column;gap:.5rem;margin-top:.25rem}
.dp-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--r-lg);padding:.85rem 1rem}
.dp-card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;margin-bottom:.55rem}
.dp-player{font-size:.95rem;font-weight:800;color:var(--t1)}
.dp-edge{font-size:1.1rem;font-weight:800;color:var(--win);font-family:'Consolas','Menlo',monospace;line-height:1}
.dp-stat{font-size:.72rem;color:var(--t2);margin-top:.2rem;display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
.dp-bottom{display:flex;align-items:center;justify-content:space-between;gap:.5rem;flex-wrap:wrap;padding-top:.5rem;border-top:1px solid var(--border)}
.dp-line{font-size:.92rem;font-weight:700;color:var(--odds);font-family:'Consolas','Menlo',monospace}
.dp-ou-badge{display:inline-flex;align-items:center;padding:.1rem .4rem;border-radius:3px;font-size:.62rem;font-weight:800;font-family:'Consolas','Menlo',monospace}
.dp-ou-badge.over{background:rgba(74,222,128,.12);color:var(--win);border:1px solid rgba(74,222,128,.25)}
.dp-ou-badge.under{background:rgba(248,113,113,.12);color:var(--loss);border:1px solid rgba(248,113,113,.25)}

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

/* ── Player props — two-view system ── */
.prop-filter-bar{display:flex;flex-direction:column;gap:.65rem;margin:1rem 0 .75rem}
.prop-search-wrap{position:relative}
.prop-search{width:100%;background:var(--surf);border:1px solid var(--border2);border-radius:var(--r);color:var(--t1);font-size:.82rem;font-family:inherit;padding:.55rem .9rem .55rem 2.2rem}
.prop-search::placeholder{color:var(--tm)}
.prop-search:focus{outline:none;border-color:var(--mlb)}
.prop-search-icon{position:absolute;left:.75rem;top:50%;transform:translateY(-50%);color:var(--tm);font-size:.85rem;pointer-events:none}
.prop-filter-lbl{font-size:.6rem;text-transform:uppercase;letter-spacing:.09em;color:var(--tm);margin-bottom:.35rem}
.prop-filter-chips{display:flex;gap:.35rem;flex-wrap:wrap}
.prop-tf-row{display:flex;gap:.4rem;flex-wrap:wrap}
.prop-tf-btn{background:var(--surf);border:1px solid var(--border);border-radius:var(--r);padding:.3rem .75rem;font-size:.68rem;font-weight:700;color:var(--t2);cursor:pointer;font-family:inherit;text-transform:uppercase;letter-spacing:.05em;transition:all .15s}
.prop-tf-btn:hover{border-color:var(--border2);color:var(--t1)}
.prop-tf-btn.active{background:rgba(90,122,232,.12);border-color:var(--mlb);color:var(--mlb)}
/* List rows */
.prop-rows{display:flex;flex-direction:column;background:var(--surf);border:1px solid var(--border);border-radius:var(--r-lg);overflow:hidden}
.prop-row{display:flex;align-items:center;gap:.75rem;padding:.8rem .9rem;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;width:100%;text-align:left;background:none;color:inherit;font:inherit;border-left:none;border-right:none;border-top:none;-webkit-tap-highlight-color:rgba(90,122,232,.15)}
.prop-row:last-child{border-bottom:none}
.prop-row:hover,.prop-row:active{background:rgba(255,255,255,.04)}
.prop-row-left{flex:1;min-width:0}
.prop-row-name{font-size:.9rem;font-weight:700;color:var(--t1)}
.prop-row-desc{font-size:.68rem;color:var(--t2);margin-top:.15rem;display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
.prop-row-odds{color:var(--tm)}
.stat-chip-sm{display:inline-flex;align-items:center;padding:.1rem .4rem;border-radius:3px;font-size:.55rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;background:rgba(200,128,15,.1);color:var(--amber);border:1px solid rgba(200,128,15,.2);white-space:nowrap}
.prop-row-mid{display:flex;flex-direction:column;align-items:center;gap:.05rem;flex-shrink:0;min-width:44px}
.prop-row-edge{font-size:.9rem;font-weight:800;color:var(--win);font-family:'Consolas','Menlo',monospace}
.prop-row-edge-lbl{font-size:.52rem;text-transform:uppercase;letter-spacing:.09em;color:var(--tm)}
.prop-row-right{display:flex;flex-direction:column;align-items:flex-end;gap:.25rem;flex-shrink:0}
.prop-row-hit{font-size:.72rem;font-weight:700;font-family:'Consolas','Menlo',monospace}
/* Detail view */
.detail-back-bar{padding:.75rem 0 .25rem}
.detail-back{background:none;border:none;color:var(--mlb);font-size:.82rem;font-weight:700;cursor:pointer;font-family:inherit;padding:0;display:inline-flex;align-items:center;gap:.35rem}
.detail-header{display:flex;align-items:flex-start;justify-content:space-between;margin:.5rem 0 .85rem;gap:1rem}
.detail-player{font-size:1.2rem;font-weight:800;color:var(--t1);line-height:1.2}
.detail-prop-meta{font-size:.8rem;color:var(--t2);margin-top:.25rem}
.detail-avg{background:var(--surf2);border:1px solid var(--border2);border-radius:var(--r);padding:.4rem .8rem;font-size:.82rem;font-weight:700;color:var(--amber);font-family:'Consolas','Menlo',monospace;white-space:nowrap;flex-shrink:0;align-self:flex-start}
.detail-chart-wrap{background:var(--surf);border:1px solid var(--border);border-radius:var(--r-lg);padding:.85rem .5rem .5rem 0;margin-bottom:.75rem;overflow-x:auto}
/* Splits */
.splits-row{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-bottom:.75rem}
.split-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--r);padding:.75rem .85rem}
.split-title{font-size:.58rem;text-transform:uppercase;letter-spacing:.1em;color:var(--tm);font-weight:700;margin-bottom:.45rem}
.split-avg{font-size:1.15rem;font-weight:800;color:var(--t1);font-family:'Consolas','Menlo',monospace;line-height:1}
.split-ops{font-size:.72rem;color:var(--t2);font-family:'Consolas','Menlo',monospace;margin-top:.15rem}
.split-stat{font-size:.82rem;font-weight:700;color:var(--amber);font-family:'Consolas','Menlo',monospace;margin-top:.25rem}
.split-meta{font-size:.62rem;color:var(--tm);margin-top:.2rem}
.split-support-badge{display:inline-block;font-size:.48rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;padding:.15rem .4rem;border-radius:3px;margin-bottom:.35rem;font-family:'Consolas','Menlo',monospace}
.split-support-yes{background:rgba(74,222,128,.15);color:var(--win)}
.split-support-no{background:rgba(248,113,113,.15);color:var(--loss)}
.detail-tf-row{display:flex;gap:0;border:1px solid var(--border);border-radius:var(--r-lg);overflow:hidden;margin-bottom:.75rem}
.detail-tf-btn{flex:1;background:none;border:none;border-right:1px solid var(--border);padding:.55rem .25rem;cursor:pointer;font-family:inherit;text-align:center;transition:background .1s}
.detail-tf-btn:last-child{border-right:none}
.detail-tf-btn.active{background:rgba(90,122,232,.1)}
.detail-tf-lbl{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--t2)}
.detail-tf-btn.active .detail-tf-lbl{color:var(--mlb)}
.detail-tf-rate{font-size:.8rem;font-weight:800;font-family:'Consolas','Menlo',monospace;margin-top:.15rem}
.detail-book{display:flex;align-items:center;gap:.75rem;background:var(--surf);border:1px solid var(--border);border-radius:var(--r);padding:.65rem .9rem;flex-wrap:wrap}
.detail-book-label{font-size:.7rem;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.05em}
.detail-book-line{font-size:1rem;font-weight:800;color:var(--odds);font-family:'Consolas','Menlo',monospace}
.detail-book-desc{font-size:.72rem;color:var(--tm)}
.detail-prop-select{background:var(--surf2);color:var(--t1);border:1px solid var(--border2);border-radius:var(--r);padding:.35rem .55rem;font-family:inherit;font-size:.72rem;cursor:pointer;max-width:180px}
.detail-prop-select option{background:var(--surf2)}
.h2h-opp-row{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.6rem}
.h2h-opp-btn{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r);padding:.28rem .6rem;font-size:.68rem;font-weight:700;color:var(--t2);cursor:pointer;font-family:'Consolas','Menlo',monospace;transition:all .1s}
.h2h-opp-btn.active{background:rgba(90,122,232,.15);border-color:var(--mlb);color:var(--mlb)}
.h2h-no-data{padding:.75rem;font-size:.75rem;color:var(--tm);text-align:center}

/* O/U side toggle */
.side-toggle{display:flex;gap:.3rem;margin-top:.45rem}
.side-btn{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r);padding:.28rem .7rem;font-size:.68rem;font-weight:700;cursor:pointer;font-family:inherit;text-transform:uppercase;letter-spacing:.05em;color:var(--t2);transition:all .15s}
.side-btn.over.active{background:rgba(74,222,128,.15);border-color:var(--win);color:var(--win)}
.side-btn.under.active{background:rgba(248,113,113,.15);border-color:var(--loss);color:var(--loss)}
/* Stats-only roster row */
.stats-row-team{display:inline-flex;align-items:center;padding:.1rem .35rem;border-radius:3px;font-size:.58rem;font-weight:700;background:rgba(90,122,232,.1);color:var(--mlb);border:1px solid rgba(90,122,232,.2);font-family:'Consolas','Menlo',monospace;margin-left:.3rem}
/* Async loading state */
.detail-loading{display:flex;align-items:center;justify-content:center;height:120px;color:var(--tm);font-size:.82rem;gap:.5rem}
.detail-spinner{width:16px;height:16px;border:2px solid var(--border2);border-top-color:var(--mlb);border-radius:50%;animation:spin .6s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
/* Today's lineup badge */
.today-badge{display:inline-flex;align-items:center;padding:.1rem .38rem;border-radius:3px;font-size:.58rem;font-weight:800;background:rgba(200,128,15,.18);color:var(--amber);border:1px solid rgba(200,128,15,.35);font-family:'Consolas','Menlo',monospace;margin-left:.35rem;letter-spacing:.04em;text-transform:uppercase}
/* vs LHP/RHP splits */
.hand-splits-wrap{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-bottom:.7rem}
.hand-split-card{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r-lg);padding:.65rem .75rem}
.hand-split-hand{font-size:.6rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.38rem}
.hand-split-hand.lhp{color:#7CB9E8}.hand-split-hand.rhp{color:#E87C7C}
.hand-split-avg{font-size:1.45rem;font-weight:800;color:var(--t1);font-family:'Consolas','Menlo',monospace;line-height:1}
.hand-split-ops{font-size:.67rem;color:var(--t2);margin:.18rem 0 .3rem}
.hand-split-stats{font-size:.65rem;color:var(--tm);display:flex;gap:.45rem;flex-wrap:wrap}
/* Today's matchup card */
.matchup-card{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r-lg);padding:.6rem .8rem;display:flex;align-items:center;gap:.7rem;margin-bottom:.75rem}
.matchup-hand-badge{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;font-size:.72rem;font-weight:900;font-family:'Consolas','Menlo',monospace;flex-shrink:0;letter-spacing:.03em}
.matchup-hand-badge.lhp{background:rgba(124,185,232,.15);color:#7CB9E8;border:1px solid rgba(124,185,232,.3)}
.matchup-hand-badge.rhp{background:rgba(232,124,124,.15);color:#E87C7C;border:1px solid rgba(232,124,124,.3)}
.matchup-hand-badge.switch{background:rgba(200,128,15,.15);color:var(--amber);border:1px solid rgba(200,128,15,.3)}
.matchup-pitcher-name{font-size:.88rem;font-weight:700;color:var(--t1);line-height:1.25}
.matchup-pitcher-meta{font-size:.67rem;color:var(--tm);margin-top:.1rem}

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

  /* Props on mobile */
  .prop-row{padding:.65rem .75rem;gap:.5rem}
  .prop-row-name{font-size:.82rem}
  .detail-player{font-size:1.05rem}
  .detail-chart-wrap{padding:.5rem .25rem .5rem 0}
}

/* ── P4.3 Flag banner ── */
.flag-banner{
  display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;
  padding:.4rem 1rem;
  background:#131f34;border:1px solid #1E2E46;
  border-radius:var(--r);margin:.5rem 0;
  font-size:.72rem;
}
.flag-banner-label{color:var(--t2);white-space:nowrap}
.flag-chip{
  background:#1a2c4a;color:var(--mlb);
  padding:.15rem .45rem;border-radius:4px;
  font-weight:600;letter-spacing:.02em;white-space:nowrap;
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
  <button class="tab" role="tab" data-tab="dailyprops">Daily Props&thinsp;<span class="tab-badge" id="tab-dailyprops-badge">0</span></button>
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
  <!-- List View -->
  <div id="props-list-view">
    <div class="prop-filter-bar">
      <div class="prop-search-wrap">
        <span class="prop-search-icon">&#9906;</span>
        <input id="prop-search" class="prop-search" type="text" placeholder="Search any MLB player…" oninput="renderPropList()">
      </div>
      <div style="font-size:.68rem;color:var(--tm);padding:.25rem .1rem 0;letter-spacing:.01em">Showing today's active players · type a name to search all MLB rosters</div>
      <div>
        <div class="prop-filter-lbl">Stat</div>
        <div class="prop-filter-chips" id="prop-stat-chips"></div>
      </div>
      <div class="prop-tf-row">
        <button class="prop-tf-btn" data-tf="L5"  onclick="setPropTf('L5')">L5</button>
        <button class="prop-tf-btn active" data-tf="L10" onclick="setPropTf('L10')">L10</button>
        <button class="prop-tf-btn" data-tf="L15" onclick="setPropTf('L15')">L15</button>
        <button class="prop-tf-btn" data-tf="all" onclick="setPropTf('all')">2025</button>
      </div>
    </div>
    <div id="prop-rows" class="prop-rows"></div>
    <div class="no-picks" id="prop-no-data" style="display:none">No player prop bets recorded yet — props appear after the next daily card run.</div>
  </div>
  <!-- Detail View -->
  <div id="props-detail-view" style="display:none">
    <div class="detail-back-bar">
      <button class="detail-back" onclick="closeDetail()">&#8592; All Props</button>
    </div>
    <div class="detail-header">
      <div>
        <div class="detail-player" id="detail-player-name"></div>
        <div class="detail-prop-meta" id="detail-prop-meta"></div>
        <div id="detail-prop-selector" style="margin-top:.5rem"></div>
        <div id="detail-side-toggle" class="side-toggle"></div>
      </div>
      <div class="detail-avg" id="detail-avg"></div>
    </div>
    <div class="detail-chart-wrap" id="detail-chart-wrap"></div>
    <div class="detail-tf-row" id="detail-tf-row"></div>
    <div id="detail-h2h-row" style="display:none"></div>
    <div id="detail-splits-row"></div>
    <div id="detail-hand-splits-row"></div>
    <div id="detail-book-row"></div>
  </div>
</div>

<!-- Daily Player Props -->
<div id="tab-dailyprops" class="tab-panel">
  <div class="sec" style="margin-top:1rem">
    <h2>Daily Player Props</h2>
    <div class="sec-rule"></div>
  </div>
  <div id="dailyprops-container"></div>
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

// P4.3 — Feature flag banner (shown only when at least one flag is ON)
(function(){
  const FLAG_LABELS = {
    USE_LOGIT_FACTORS:       'Logit Factors',
    USE_FULL_EXTRA_INNINGS:  'Full Extra Innings',
    ENABLE_EDGE_VERIFY_GATE: 'Edge Verify Gate',
    ENABLE_SAMPLE_GATE:      'Sample Gate',
  };
  const active = Object.entries(D.sim_flags || {})
    .filter(([,v]) => v)
    .map(([k]) => FLAG_LABELS[k] || k);
  if (active.length) {
    const banner = document.createElement('div');
    banner.className = 'flag-banner';
    banner.innerHTML = '<span class="flag-banner-label">Sim flags active:</span>' +
      active.map(f => `<span class="flag-chip">${f}</span>`).join('');
    const header = document.querySelector('header');
    header.parentNode.insertBefore(banner, header.nextSibling);
  }
})();

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
  });
});

// ── Shared helpers (must be declared before any inline render code) ──
const TYPE_LABELS = {ml:'ML', spread:'Spread', total:'Total', nrfi:'NRFI', prop:'Prop', other:'Pick'};
function pickType(market) {
  const m = (market || '').toLowerCase();
  if (/\bnrfi\b/.test(m)) return 'nrfi';
  if (/\bmoneyline\b|\bml\b/.test(m)) return 'ml';
  if (/\bspread\b|\brunline\b|\bpuck.?line\b|\bgoal.?line\b/.test(m)) return 'spread';
  if (/batter|pitcher/.test(m)) return 'prop';
  if (/\btotal\b|\bover\b|\bunder\b/.test(m)) return 'total';
  return 'other';
}
const _propMarketRe = /^(.+?)\s+((?:Batter|Pitcher)(?:\s+\w+)+)\s+([OU])(\d+(?:\.\d+)?)$/;

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
      const edgeNum=((p.edge||0)*100);
      const edge=edgeNum.toFixed(1);
      const edgeStr=(edgeNum>0?'+':'')+edge+'%';
      const stake=(p.stake_units||0).toFixed(2);
      const type=pickType(p.market||'');
      const typeBadge=`<span class="pick-type ${type}">${TYPE_LABELS[type]}</span>`;
      const mProb=p.model_prob!=null?Math.round(p.model_prob*100)+'%':null;
      const fProb=p.fair_prob!=null?Math.round(p.fair_prob*100)+'%':null;
      const probRow=(mProb||fProb)?`<div class="pick-prob-row">${mProb?`<span><span class="pick-prob-lbl">Model:</span> ${mProb}</span>`:''} ${fProb?`<span><span class="pick-prob-lbl">Fair:</span> ${fProb}</span>`:''}</div>`:'';
      const proj=p.projected_score?`<div class="pick-row-proj">${p.projected_score}</div>`:'';
      return `<div class="pick-row">
        <div class="pick-row-left">
          <div class="pick-row-market">${typeBadge}${p.market||'?'}</div>
          ${probRow}${proj}
        </div>
        <div class="pick-row-right">
          <span class="pick-row-edge">${edgeStr}</span>
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

const STAT_LABELS = {
  'Batter Hits':'Hits','Batter Total Bases':'Total Bases','Batter Home Runs':'HR',
  'Batter Rbis':'RBI','Batter Walks':'Walks','Batter Strikeouts':'K (Bat)',
  'Pitcher Strikeouts':'K','Pitcher Outs':'Outs',
  'Pitcher Hits Allowed':'H Allow','Pitcher Walks':'BB Allow','Pitcher Earned Runs':'ER',
};
const BATTER_PROP_TYPES = ['Batter Hits','Batter Total Bases','Batter Home Runs','Batter Rbis','Batter Walks','Batter Strikeouts'];
const PITCHER_PROP_TYPES = ['Pitcher Strikeouts','Pitcher Outs','Pitcher Hits Allowed','Pitcher Walks','Pitcher Earned Runs'];
const STAT_KEYS = {
  'Batter Hits':'hits','Batter Total Bases':'totalBases','Batter Home Runs':'homeRuns',
  'Batter Rbis':'rbi','Batter Walks':'baseOnBalls','Batter Strikeouts':'strikeOuts',
  'Pitcher Strikeouts':'strikeOuts','Pitcher Outs':'outs',
  'Pitcher Hits Allowed':'hits','Pitcher Walks':'baseOnBalls','Pitcher Earned Runs':'earnedRuns',
};

// Build unique player+stat profiles
const propList = [];
const propByKey = {};
propBets.forEach(b => {
  const key = b.player + '||' + b.stat;
  if (!propByKey[key]) {
    const entry = {player:b.player,stat:b.stat,side:b.side,threshold:b.threshold,line:b.line,book:b.book,edge:b.edge,bets:[]};
    propByKey[key] = entry;
    propList.push(entry);
  }
  propByKey[key].bets.push(b);
});
// Sort each player's history oldest→newest (for chart left→right)
propList.forEach(pd => pd.bets.sort((a,b) => a.date.localeCompare(b.date)));

// Index props by player so detail view can switch between markets
const propsByPlayer = {};
propList.forEach((pd, idx) => {
  if (!propsByPlayer[pd.player]) propsByPlayer[pd.player] = [];
  propsByPlayer[pd.player].push(idx);
});

// Combined roster: bet-tracked props first, today's lineup players, then rest A-Z
const todayPids = new Set((D.today_lineups?.player_ids || []).map(String));
const masterDisplayList = [];
propList.forEach((pd, i) => masterDisplayList.push({type:'bet', propIdx:i, player:pd.player}));
const _trackedNames = new Set(propList.map(pd => pd.player.toLowerCase()));
const _allRosterPlayers = Object.values(D.all_player_stats || {})
  .filter(ps => ps.name && !_trackedNames.has(ps.name.toLowerCase()))
  .map(ps => Object.assign({type:'stats', player:ps.name, isToday: todayPids.has(String(ps.pid))}, ps));
const _todayRoster = _allRosterPlayers.filter(e => e.isToday).sort((a,b) => a.name.localeCompare(b.name));
const _restRoster  = _allRosterPlayers.filter(e => !e.isToday).sort((a,b) => a.name.localeCompare(b.name));
_todayRoster.forEach(e => masterDisplayList.push(e));
_restRoster.forEach(e => masterDisplayList.push(e));

let propStatFilter = 'all';
let propTf = 'L10';
let activeIdx = -1;
let activeStatsEntry = null;
let activeStatType = null;
let activeGameLog = null;
let activeBatterSplits = null;
let h2hOpp = null;
let viewSide = null;
const _glCache = {};

// Name → player entry lookup for finding pid of bet-tracked players
const playerByName = {};
Object.values(D.all_player_stats || {}).forEach(p => { playerByName[p.name.toLowerCase()] = p; });

// Build stat filter chips
(function(){
  const el = document.getElementById('prop-stat-chips');
  if (!el) return;
  const stats = [...new Set(propBets.map(p => p.stat))].sort();
  el.innerHTML = `<button class="filter-chip active" data-pstat="all" onclick="setPropStat('all')">All</button>` +
    stats.map(s => `<button class="filter-chip" data-pstat="${s}" onclick="setPropStat('${s}')">${STAT_LABELS[s]||s.replace(/^(Batter|Pitcher)\s+/,'')}</button>`).join('');
})();

function setPropStat(v) {
  propStatFilter = v;
  document.querySelectorAll('[data-pstat]').forEach(c => c.classList.toggle('active', c.dataset.pstat === v));
  renderPropList();
}
function setPropTf(tf) {
  propTf = tf;
  document.querySelectorAll('.prop-tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));
  renderPropList();
}

function getSlice(bets, tf) {
  if (tf === 'L5')  return bets.slice(-5);
  if (tf === 'L10') return bets.slice(-10);
  if (tf === 'L15') return bets.slice(-15);
  return bets;
}
function hitRate(bets) {
  const s = bets.filter(b => b.result === 'win' || b.result === 'loss');
  if (!s.length) return null;
  return Math.round(s.filter(b => b.result === 'win').length / s.length * 100);
}
function avgProj(bets) {
  const a = bets.filter(b => b.proj_stat != null);
  if (!a.length) return null;
  return a.reduce((s,b) => s + b.proj_stat, 0) / a.length;
}
function rateColor(hr) {
  if (hr === null) return 'var(--tm)';
  return hr >= 60 ? 'var(--win)' : hr >= 45 ? 'var(--t2)' : 'var(--loss)';
}

function miniSpark(bets, tf) {
  const sl = getSlice(bets, tf);
  if (!sl.length) return '';
  const n = sl.length, W = 80, H = 28, gap = 1.5;
  const bw = Math.max(3, (W - gap*(n-1)) / n);
  const thresh = sl[0].threshold;
  const vals = sl.map(b => b.proj_stat != null ? b.proj_stat : thresh);
  const maxV = Math.max(...vals, thresh * 1.3) || 1;
  const lineY = H - (thresh / maxV) * H;
  let s = sl.map((b, i) => {
    const x = i * (bw + gap);
    const val = b.proj_stat != null ? b.proj_stat : thresh;
    const bh = Math.max(3, (val / maxV) * H);
    const col = b.result==='win' ? '#4ADE80' : b.result==='loss' ? '#F87171' : '#4E6480';
    return `<rect x="${x.toFixed(1)}" y="${(H-bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="1.5" fill="${col}" fill-opacity=".9"/>`;
  }).join('');
  s += `<line x1="0" y1="${lineY.toFixed(1)}" x2="${W}" y2="${lineY.toFixed(1)}" stroke="rgba(255,255,255,.35)" stroke-width="1" stroke-dasharray="2,1.5"/>`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${s}</svg>`;
}

async function fetchGameLogClient(pid, statType) {
  const key = pid + '||' + statType;
  if (_glCache[key]) return _glCache[key];
  const isPitcher = statType.startsWith('Pitcher');
  const group = isPitcher ? 'pitching' : 'hitting';
  const statKey = STAT_KEYS[statType];
  try {
    const url = `https://statsapi.mlb.com/api/v1/people/${pid}/stats?stats=gameLog&season=2025&group=${group}&gameType=R`;
    const res = await fetch(url);
    const data = await res.json();
    const games = [];
    for (const blk of (data.stats || [])) {
      for (const sp of (blk.splits || [])) {
        const s = sp.stat || {};
        const opp = sp.opponent || {};
        const val = s[statKey];
        games.push({date:(sp.date||'').slice(0,10), opp:opp.abbreviation||opp.teamName||'', val:val!=null?parseFloat(val):null});
      }
    }
    games.sort((a, b) => a.date.localeCompare(b.date));
    _glCache[key] = games;
    return games;
  } catch(e) {
    _glCache[key] = [];
    return [];
  }
}

async function fetchBatterHandSplits(pid) {
  const key = 'hs||' + pid;
  if (_glCache[key]) return _glCache[key];
  try {
    const url = `https://statsapi.mlb.com/api/v1/people/${pid}/stats?stats=statSplits&season=2025&group=hitting&sitCodes=vr,vl&gameType=R`;
    const res = await fetch(url);
    const data = await res.json();
    const out = {};
    for (const blk of (data.stats || [])) {
      for (const sp of (blk.splits || [])) {
        const code = (sp.split || {}).code || '';
        if (code === 'vr' || code === 'vl') {
          const s = sp.stat || {};
          const k = code === 'vr' ? 'vsR' : 'vsL';
          out[k] = {
            avg: s.avg || '---', ops: s.ops || '---',
            obp: s.obp || '---', slg: s.slg || '---',
            hr: s.homeRuns || 0, rbi: s.rbi || 0,
            k: s.strikeOuts || 0, bb: s.baseOnBalls || 0,
            ab: s.atBats || 0, g: s.gamesPlayed || 0,
          };
        }
      }
    }
    _glCache[key] = out;
    return out;
  } catch(e) { _glCache[key] = {}; return {}; }
}

function setViewSide(s) {
  viewSide = s;
  renderDetailView();
}

async function switchStatsProp(statType) {
  if (!activeStatsEntry) return;
  const entry = activeStatsEntry;
  activeStatType = statType;
  activeGameLog = null;
  viewSide = null;
  document.getElementById('detail-chart-wrap').innerHTML = '<div class="detail-loading"><div class="detail-spinner"></div>Loading game log…</div>';
  document.getElementById('detail-avg').textContent = '';
  document.getElementById('detail-tf-row').innerHTML = '';
  document.getElementById('detail-h2h-row').style.display = 'none';
  document.getElementById('detail-hand-splits-row').innerHTML = '';
  // Fetch game log; also refresh hand splits if switching to a batter type
  if (!statType.startsWith('Pitcher')) {
    [activeGameLog, activeBatterSplits] = await Promise.all([
      fetchGameLogClient(entry.pid, statType),
      fetchBatterHandSplits(entry.pid),
    ]);
  } else {
    activeBatterSplits = null;
    activeGameLog = await fetchGameLogClient(entry.pid, statType);
  }
  if (activeStatsEntry === entry && activeStatType === statType) renderDetailView();
}

function renderPropList() {
  const query = (document.getElementById('prop-search')?.value || '').toLowerCase().trim();
  const rowsEl = document.getElementById('prop-rows');
  const noData = document.getElementById('prop-no-data');
  const filtered = masterDisplayList.filter(entry => {
    // Without a search query, only show bet-tracked props and today's lineup players
    if (!query && entry.type === 'stats' && !entry.isToday) return false;
    // Name search — reveals full roster
    if (query && !entry.player.toLowerCase().includes(query)) return false;
    // Stat filter applies to bet entries only
    if (entry.type === 'bet' && propStatFilter !== 'all') {
      if (propList[entry.propIdx].stat !== propStatFilter) return false;
    }
    return true;
  });
  if (!filtered.length) {
    noData.style.display = '';
    noData.textContent = query
      ? `No players found matching "${query}".`
      : 'No active lineup data yet — check back closer to game time.';
    rowsEl.innerHTML = '';
    return;
  }
  noData.style.display = 'none';
  rowsEl.innerHTML = filtered.map(entry => {
    const midx = masterDisplayList.indexOf(entry);
    if (entry.type === 'bet') {
      const pd = propList[entry.propIdx];
      const sl = STAT_LABELS[pd.stat] || pd.stat.replace(/^(Batter|Pitcher)\s+/, '');
      const slice = getSlice(pd.bets, propTf);
      const hr = hitRate(slice);
      const hrTxt = hr !== null ? hr + '%' : '—';
      const spark = miniSpark(pd.bets, propTf);
      const recent = pd.bets[pd.bets.length - 1];
      return `<button class="prop-row" onclick="openDetailMaster(${midx})">
        <div class="prop-row-left">
          <div class="prop-row-name">${pd.player}</div>
          <div class="prop-row-desc">
            <span class="stat-chip-sm">${sl}</span>
            ${pd.side==='O'?'Over':'Under'} ${pd.threshold}
            <span class="prop-row-odds">${recent ? recent.line : ''}</span>
          </div>
        </div>
        <div class="prop-row-mid">
          <div class="prop-row-edge">${pd.edge > 0 ? '+' : ''}${pd.edge}%</div>
          <div class="prop-row-edge-lbl">EDGE</div>
        </div>
        <div class="prop-row-right">
          ${spark}
          <div class="prop-row-hit" style="color:${rateColor(hr)}">${hrTxt}</div>
        </div>
      </button>`;
    } else {
      const isPitcher = entry.pos === 'P';
      const posLabel = entry.posDetail || (isPitcher ? 'P' : 'B');
      const keyStats = isPitcher
        ? `ERA ${entry.era||'---'} · ${entry.pK||0}K · WHIP ${entry.whip||'---'}`
        : `AVG ${entry.avg||'---'} · ${entry.homeRuns||0}HR · ${entry.k||0}K`;
      const vol = isPitcher ? `${entry.g||0}G · ${entry.ip||'0'}IP` : `${entry.g||0}G · ${entry.pa||0}PA`;
      const todayBadge = entry.isToday ? `<span class="today-badge">TODAY ⚡</span>` : '';
      return `<button class="prop-row" onclick="openDetailMaster(${midx})">
        <div class="prop-row-left">
          <div class="prop-row-name">${entry.player}${todayBadge}<span class="stats-row-team">${entry.team||''} · ${posLabel}</span></div>
          <div class="prop-row-desc">
            <span style="font-family:'Consolas','Menlo',monospace;font-size:.7rem">${keyStats}</span>
            <span class="prop-row-odds">${vol}</span>
          </div>
        </div>
        <div class="prop-row-mid">
          <div style="font-size:.58rem;font-weight:800;color:${entry.isToday?'var(--amber)':'var(--mlb)'};letter-spacing:.06em;text-transform:uppercase">${entry.isToday?'Playing Today':'Roster'}</div>
        </div>
        <div class="prop-row-right">
          <div class="prop-row-hit" style="color:var(--tm)">—</div>
        </div>
      </button>`;
    }
  }).join('');
}

// Chart from actual MLB season game log data
function renderSeasonChart(games, thresh, side) {
  // Sort chronologically so bars always go oldest→newest left→right
  const sorted = [...games].sort((a, b) => a.date.localeCompare(b.date));
  const valid = sorted.filter(g => g.val != null);
  if (!valid.length) return '<div style="padding:2rem;text-align:center;color:var(--tm);font-size:.8rem">No game data available</div>';
  const n = valid.length;
  // Extra bottom margin for 45° rotated date labels
  const H=200, ML=32, MR=8, MT=22, MB=58;
  const cH=H-MT-MB;
  const minBw = n > 20 ? 14 : Math.max(14, 320/n);
  const W = ML + MR + n*minBw + (n-1)*3;
  const cW = W - ML - MR;
  const bw = (cW - (n-1)*3) / n;
  const vals = valid.map(g => g.val);
  const maxV = Math.max(Math.max(...vals, thresh) * 1.28, 1);
  function sy(v) { return MT + cH - (v / maxV * cH); }
  const gridVals = [0, +(thresh/2).toFixed(1), thresh, +(maxV*0.78).toFixed(1)];
  const grid = [...new Set(gridVals)].map(v => {
    const y = sy(v);
    return `<line x1="${ML}" y1="${y.toFixed(1)}" x2="${ML+cW}" y2="${y.toFixed(1)}" stroke="rgba(30,46,70,.7)" stroke-width=".5"/>
    <text x="${(ML-4)}" y="${(y+3).toFixed(1)}" text-anchor="end" fill="#3A506A" font-size="8" font-family="Consolas,Menlo,monospace">${v}</text>`;
  }).join('');
  // Date interval: show every Nth label so they don't collide at smaller bar widths
  const dateEvery = n > 50 ? 10 : n > 30 ? 5 : n > 18 ? 3 : n > 10 ? 2 : 1;
  const bars = valid.map((g, i) => {
    const x = ML + i*(bw+3);
    const barTop = sy(g.val), barH = Math.max(2, cH-(barTop-MT));
    const hit = side==='O' ? g.val >= thresh : g.val <= thresh;
    const col = hit ? '#4ADE80' : '#F87171';
    const showLbl = n <= 25;
    const showOpp = n <= 12;
    const v = g.val % 1 === 0 ? g.val : g.val.toFixed(1);
    const valLbl = showLbl ? `<text x="${(x+bw/2).toFixed(1)}" y="${(barTop-4).toFixed(1)}" text-anchor="middle" fill="#E8EDF6" font-size="9" font-family="Consolas,Menlo,monospace">${v}</text>` : '';
    // Rotated date label — anchored at top of label, rotated -45° so it fans down-left
    const cx = (x+bw/2).toFixed(1), cy = (MT+cH+10).toFixed(1);
    const dateLbl = (i % dateEvery === 0) ? `<text x="${cx}" y="${cy}" text-anchor="start" fill="#4E6480" font-size="8" font-family="Consolas,Menlo,monospace" transform="rotate(-45,${cx},${cy})">${g.date.slice(5)}</text>` : '';
    const oppLbl = showOpp && g.opp ? `<text x="${(x+bw/2).toFixed(1)}" y="${(MT+cH+46).toFixed(1)}" text-anchor="middle" fill="#2D4060" font-size="7" font-family="Consolas,Menlo,monospace">${g.opp}</text>` : '';
    return `<rect x="${x.toFixed(1)}" y="${barTop.toFixed(1)}" width="${bw.toFixed(1)}" height="${barH.toFixed(1)}" rx="3" fill="${col}" fill-opacity=".9"/>${valLbl}${dateLbl}${oppLbl}`;
  }).join('');
  const lineY = sy(thresh);
  const thrLine = `<line x1="${ML}" y1="${lineY.toFixed(1)}" x2="${ML+cW}" y2="${lineY.toFixed(1)}" stroke="white" stroke-width="1.5"/>
  <text x="${(ML-4)}" y="${(lineY+3).toFixed(1)}" text-anchor="end" fill="#E8A830" font-size="9" font-weight="bold" font-family="Consolas,Menlo,monospace">${thresh}</text>`;
  const svgW = Math.max(W, 350);
  return `<svg viewBox="0 0 ${svgW} ${H}" width="${svgW}" style="display:block;overflow:visible;min-width:100%">${grid}${bars}${thrLine}</svg>`;
}

// Fallback chart from our bet history (used when no season data available)
function renderDetailChart(bets, thresh) {
  if (!bets.length) return '<div style="padding:2rem;text-align:center;color:var(--tm);font-size:.8rem">No data for this timeframe</div>';
  const W=400, H=185, ML=32, MR=8, MT=22, MB=42;
  const cW=W-ML-MR, cH=H-MT-MB;
  const vals = bets.map(b => b.proj_stat!=null ? b.proj_stat : thresh);
  const maxV = Math.max(...vals, thresh) * 1.28;
  const n = bets.length;
  const bw = Math.max(8, (cW - (n-1)*3) / n);
  function sy(v) { return MT + cH - (v / maxV * cH); }
  const gridVals = [0, Math.round(thresh/2*10)/10, thresh, Math.round(maxV*0.78*10)/10];
  let grid = gridVals.map(v => {
    const y = sy(v);
    return `<line x1="${ML}" y1="${y.toFixed(1)}" x2="${ML+cW}" y2="${y.toFixed(1)}" stroke="rgba(30,46,70,.7)" stroke-width=".5"/>
    <text x="${(ML-4)}" y="${(y+3).toFixed(1)}" text-anchor="end" fill="#3A506A" font-size="8" font-family="Consolas,Menlo,monospace">${v}</text>`;
  }).join('');
  let bars = bets.map((b, i) => {
    const x = ML + i*(bw+3);
    const val = b.proj_stat!=null ? b.proj_stat : thresh;
    const barTop = sy(val), barH = Math.max(2, cH-(barTop-MT));
    const col = b.result==='win'?'#4ADE80':b.result==='loss'?'#F87171':'#4E6480';
    const valLbl = `<text x="${(x+bw/2).toFixed(1)}" y="${(barTop-4).toFixed(1)}" text-anchor="middle" fill="#E8EDF6" font-size="9" font-family="Consolas,Menlo,monospace">${val.toFixed(1)}</text>`;
    const dateLbl = `<text x="${(x+bw/2).toFixed(1)}" y="${(MT+cH+13).toFixed(1)}" text-anchor="middle" fill="#4E6480" font-size="8" font-family="Consolas,Menlo,monospace">${b.date.slice(5)}</text>`;
    const opp = b.event ? (b.event.includes(' @ ') ? b.event.split(' @ ')[1] : b.event).split(' ').pop() : '';
    const oppLbl = opp ? `<text x="${(x+bw/2).toFixed(1)}" y="${(MT+cH+24).toFixed(1)}" text-anchor="middle" fill="#2D4060" font-size="7" font-family="Consolas,Menlo,monospace">${opp}</text>` : '';
    return `<rect x="${x.toFixed(1)}" y="${barTop.toFixed(1)}" width="${bw.toFixed(1)}" height="${barH.toFixed(1)}" rx="3" fill="${col}" fill-opacity=".9"/>${valLbl}${dateLbl}${oppLbl}`;
  }).join('');
  const lineY = sy(thresh);
  const thrLine = `<line x1="${ML}" y1="${lineY.toFixed(1)}" x2="${ML+cW}" y2="${lineY.toFixed(1)}" stroke="white" stroke-width="1.5"/>
  <text x="${(ML-4)}" y="${(lineY+3).toFixed(1)}" text-anchor="end" fill="#E8A830" font-size="9" font-weight="bold" font-family="Consolas,Menlo,monospace">${thresh}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;overflow:visible">${grid}${bars}${thrLine}</svg>`;
}

function renderDetailView() {
  const isStatsOnly = activeStatsEntry !== null;
  if (!isStatsOnly && activeIdx < 0) return;
  if (isStatsOnly && !activeGameLog) return; // still loading

  // ── Resolve context ──
  let pd, seasonGames, seasonData;
  if (isStatsOnly) {
    const validVals = activeGameLog.filter(g=>g.val!=null).map(g=>g.val);
    const seasonAvg = validVals.length ? validVals.reduce((a,b)=>a+b,0)/validVals.length : 0;
    const thresh = Math.max(0.5, Math.round(seasonAvg * 2) / 2);
    pd = {player:activeStatsEntry.player, stat:activeStatType, side:'O', threshold:thresh, bets:[], line:'—', book:'—'};
    seasonData = {games: activeGameLog, splits: {}};
    seasonGames = activeGameLog;
  } else {
    pd = propList[activeIdx];
    const key = pd.player + '||' + pd.stat;
    seasonData = (D.player_seasons || {})[key] || {games:[],splits:{}};
    seasonGames = seasonData.games || [];
  }

  const effSide = viewSide || pd.side;
  const hasSeasonData = seasonGames.length > 0;
  const sl = STAT_LABELS[pd.stat] || pd.stat.replace(/^(Batter|Pitcher)\s+/, '');

  // ── O/U toggle ──
  const toggleEl = document.getElementById('detail-side-toggle');
  if (toggleEl) {
    toggleEl.innerHTML =
      `<button class="side-btn over${effSide==='O'?' active':''}" onclick="setViewSide('O')">Over</button>` +
      `<button class="side-btn under${effSide==='U'?' active':''}" onclick="setViewSide('U')">Under</button>`;
  }

  // ── Prop selector dropdown ──
  const selectorEl = document.getElementById('detail-prop-selector');
  if (selectorEl) {
    if (isStatsOnly) {
      const propTypes = activeStatsEntry.pos === 'P' ? PITCHER_PROP_TYPES : BATTER_PROP_TYPES;
      selectorEl.innerHTML = `<select class="detail-prop-select" onchange="switchStatsProp(this.value)">` +
        propTypes.map(st => {
          const lbl = STAT_LABELS[st] || st.replace(/^(Batter|Pitcher)\s+/,'');
          return `<option value="${st}"${st===activeStatType?' selected':''}>${lbl}</option>`;
        }).join('') + `</select>`;
    } else {
      const playerIdxs = propsByPlayer[pd.player] || [activeIdx];
      selectorEl.innerHTML = playerIdxs.length > 1
        ? `<select class="detail-prop-select" onchange="switchProp(this.value)">` +
            playerIdxs.map(i => {
              const p = propList[i];
              const lbl = STAT_LABELS[p.stat] || p.stat.replace(/^(Batter|Pitcher)\s+/, '');
              return `<option value="${i}"${i===activeIdx?' selected':''}>${lbl} · ${p.side==='O'?'Over':'Under'} ${p.threshold}</option>`;
            }).join('') + `</select>`
        : '';
    }
  }

  // Update prop-meta to reflect effective side
  if (!isStatsOnly) {
    document.getElementById('detail-prop-meta').textContent = (effSide==='O'?'Over':'Under') + ' ' + pd.threshold + ' ' + sl;
  }

  // Pre-compute opps and init h2hOpp BEFORE TF buttons so H2H rate is correct on first render
  const opps = hasSeasonData ? [...new Set(seasonGames.filter(g=>g.opp).map(g=>g.opp))].sort() : [];
  if (propTf === 'h2h' && !h2hOpp && opps.length) h2hOpp = opps[0];

  // ── Timeframe buttons (L5/L10/L15/2025/H2H) ──
  const tfs = ['L5','L10','L15','all','h2h'];
  const tfN  = {L5:5,L10:10,L15:15,all:9999};
  const tfLbls = {L5:'L5',L10:'L10',L15:'L15',all:'2025',h2h:'H2H'};
  document.getElementById('detail-tf-row').innerHTML = tfs.map(tf => {
    let hr = null, hrTxt = '—';
    if (tf === 'h2h') {
      if (hasSeasonData && h2hOpp) {
        const oppGames = seasonGames.filter(g => g.opp === h2hOpp && g.val != null);
        const hits = oppGames.filter(g => effSide==='O' ? g.val >= pd.threshold : g.val <= pd.threshold).length;
        if (oppGames.length) { hr = Math.round(hits/oppGames.length*100); hrTxt = hr+'%'; }
      }
    } else if (isStatsOnly && hasSeasonData) {
      const limit = tfN[tf] || 9999;
      const slice = limit < 9999 ? seasonGames.slice(-limit) : seasonGames;
      const valid = slice.filter(g=>g.val!=null);
      if (valid.length) {
        const hits = valid.filter(g => effSide==='O' ? g.val >= pd.threshold : g.val <= pd.threshold).length;
        hr = Math.round(hits/valid.length*100); hrTxt = hr+'%';
      }
    } else {
      const betSlice = getSlice(pd.bets, tf);
      hr = hitRate(betSlice);
      hrTxt = hr !== null ? hr + '%' : '—';
    }
    return `<button class="detail-tf-btn${tf===propTf?' active':''}" onclick="setDetailTf('${tf}')">
      <div class="detail-tf-lbl">${tfLbls[tf]}</div>
      <div class="detail-tf-rate" style="color:${rateColor(hr)}">${hrTxt}</div>
    </button>`;
  }).join('');

  // ── H2H opponent chip row ──
  const h2hEl = document.getElementById('detail-h2h-row');
  if (propTf === 'h2h' && hasSeasonData) {
    h2hEl.style.display = '';
    h2hEl.innerHTML = opps.length
      ? `<div class="h2h-opp-row">` +
          opps.map(opp => {
            const oppGames = seasonGames.filter(g => g.opp === opp && g.val != null);
            return `<button class="h2h-opp-btn${opp===h2hOpp?' active':''}" data-opp="${opp}" onclick="setH2hOpp(this.dataset.opp)">${opp} <span style="opacity:.55;font-weight:400">${oppGames.length}G</span></button>`;
          }).join('') + `</div>`
      : `<div class="h2h-no-data">No opponent data available</div>`;
  } else {
    h2hEl.style.display = 'none';
  }

  // ── Chart + AVG ──
  if (hasSeasonData) {
    let chartGames;
    if (propTf === 'h2h') {
      chartGames = h2hOpp ? seasonGames.filter(g => g.opp === h2hOpp) : [];
    } else {
      const limit = tfN[propTf] || 9999;
      chartGames = limit < 9999 ? seasonGames.slice(-limit) : seasonGames;
    }
    const validVals = chartGames.filter(g=>g.val!=null).map(g=>g.val);
    const avg = validVals.length ? (validVals.reduce((a,b)=>a+b,0)/validVals.length) : null;
    document.getElementById('detail-avg').textContent = avg!=null ? 'AVG: '+avg.toFixed(1) : '';
    document.getElementById('detail-chart-wrap').innerHTML = renderSeasonChart(chartGames, pd.threshold, effSide);
  } else {
    const betSlice = getSlice(pd.bets, propTf === 'h2h' ? 'all' : propTf);
    const avg = avgProj(betSlice);
    document.getElementById('detail-avg').textContent = avg!=null ? 'AVG: '+avg.toFixed(1) : '';
    document.getElementById('detail-chart-wrap').innerHTML = renderDetailChart(betSlice, pd.threshold);
  }

  // ── Bat vs Pitch splits (bet-tracked only) — enriched with hand-split batting stats ──
  const isBatter = isStatsOnly ? activeStatsEntry.pos !== 'P' : !propList[activeIdx].stat.startsWith('Pitcher');
  const splitsEl = document.getElementById('detail-splits-row');
  const splits = isStatsOnly ? {} : (seasonData.splits || {});
  const splitEntries = Object.entries(splits);
  if (splitsEl && splitEntries.length) {
    splitsEl.innerHTML = `<div class="sec" style="margin:.85rem 0 .5rem"><h2>Bat vs Pitch</h2><div class="sec-rule"></div></div>
      <div class="splits-row">` +
      splitEntries.map(([hand, s]) => {
        const perGNum = s.g > 0 ? s.stat / s.g : null;
        const perG    = perGNum !== null ? perGNum.toFixed(2) : '—';
        const supports = perGNum !== null
          ? (effSide === 'O' ? perGNum >= pd.threshold : perGNum <= pd.threshold)
          : null;
        const borderCol = supports === true ? 'var(--win)' : supports === false ? 'var(--loss)' : 'var(--border)';
        const badge = supports === true
          ? `<div class="split-support-badge split-support-yes">&#10003; Supports ${effSide==='O'?'Over':'Under'}</div>`
          : supports === false
          ? `<div class="split-support-badge split-support-no">&#8593; Opposes ${effSide==='O'?'Over':'Under'}</div>`
          : '';
        // Enrich with hand-split general batting stats (AVG, OPS, OBP, SLG, HR, RBI, K, BB, AB)
        const hsKey = hand === 'vs RHP' ? 'vsR' : hand === 'vs LHP' ? 'vsL' : null;
        const hs = (hsKey && activeBatterSplits) ? activeBatterSplits[hsKey] : null;
        const avgRow = hs
          ? `<div class="split-avg">${hs.avg}</div><div class="split-ops">OPS ${hs.ops} &middot; OBP ${hs.obp} &middot; SLG ${hs.slg}</div>`
          : (s.avg && s.avg!=='---' ? `<div class="split-avg">${s.avg}</div><div class="split-ops">OPS ${s.ops||'---'}</div>` : '');
        const extraStats = hs
          ? `<div class="hand-split-stats" style="margin-top:.28rem">${hs.hr} HR &middot; ${hs.rbi} RBI &middot; ${hs.k} K &middot; ${hs.bb} BB &middot; ${hs.ab} AB &middot; ${hs.g}G</div>`
          : '';
        return `<div class="split-card" style="box-shadow:inset 3px 0 0 ${borderCol};border-color:${borderCol}">
          <div class="split-title">${hand}</div>
          ${badge}
          ${avgRow}
          <div class="split-stat">${s.stat} ${sl}</div>
          <div class="split-meta">${s.g} G &middot; ${perG}/G</div>
          ${extraStats}
        </div>`;
      }).join('') + `</div>`;
  } else if (splitsEl) {
    splitsEl.innerHTML = '';
  }

  // ── vs LHP / RHP + today's matchup ──
  // For stats-only batters: show full hand-split cards here (no bet splits above)
  // For bet-tracked batters: show only today's matchup (stats already in splits-row above)
  const handSplitsEl = document.getElementById('detail-hand-splits-row');
  if (handSplitsEl) {
    if (isBatter) {
      const pid = isStatsOnly ? String(activeStatsEntry.pid) : String((playerByName[pd.player.toLowerCase()] || {}).pid || '');
      const hs = activeBatterSplits;
      const matchup = pid ? ((D.today_lineups && D.today_lineups.matchups) || {})[pid] : null;
      let html = '';
      if (isStatsOnly) {
        // Stats-only: full vs LHP/RHP section
        html += `<div class="sec" style="margin:.85rem 0 .4rem"><h2>vs LHP &amp; RHP</h2><div class="sec-rule"></div></div>`;
        if (hs && (hs.vsL || hs.vsR)) {
          html += `<div class="hand-splits-wrap">`;
          [['vsL','vs LHP','lhp'],['vsR','vs RHP','rhp']].forEach(([k,label,cls]) => {
            const s = hs[k];
            if (s) {
              html += `<div class="hand-split-card">
                <div class="hand-split-hand ${cls}">${label}</div>
                <div class="hand-split-avg">${s.avg}</div>
                <div class="hand-split-ops">OPS ${s.ops} &middot; OBP ${s.obp} &middot; SLG ${s.slg}</div>
                <div class="hand-split-stats">${s.hr} HR &middot; ${s.rbi} RBI &middot; ${s.k} K &middot; ${s.bb} BB &middot; ${s.ab} AB &middot; ${s.g}G</div>
              </div>`;
            } else {
              html += `<div class="hand-split-card"><div class="hand-split-hand ${cls}">${label}</div><div style="color:var(--tm);font-size:.75rem;margin-top:.5rem">No data</div></div>`;
            }
          });
          html += `</div>`;
        } else if (hs !== null) {
          html += `<div style="color:var(--tm);font-size:.78rem;padding:.4rem 0 .7rem">No split data available for 2025.</div>`;
        } else {
          html += `<div class="detail-loading" style="height:70px"><div class="detail-spinner"></div>Loading splits…</div>`;
        }
      }
      // Always show today's matchup card if available
      if (matchup && matchup.pitcher_name) {
        const hand = matchup.pitcher_hand || '';
        const handCls = hand === 'L' ? 'lhp' : hand === 'R' ? 'rhp' : 'switch';
        const handLbl = hand === 'L' ? 'LHP' : hand === 'R' ? 'RHP' : hand || '—';
        html += `<div class="matchup-card">
          <div class="matchup-hand-badge ${handCls}">${handLbl}</div>
          <div>
            <div class="matchup-pitcher-name">${matchup.pitcher_name}</div>
            <div class="matchup-pitcher-meta">Today's Matchup &middot; ${matchup.opp_team || ''}</div>
          </div>
        </div>`;
      }
      handSplitsEl.innerHTML = html;
    } else {
      handSplitsEl.innerHTML = '';
    }
  }

  // ── Book row (bet-tracked only) ──
  if (!isStatsOnly) {
    const recent = pd.bets[pd.bets.length-1];
    document.getElementById('detail-book-row').innerHTML = `<div class="detail-book">
      <span class="detail-book-label">${recent ? recent.book : '?'}</span>
      <span class="detail-book-line">${recent ? recent.line : '?'}</span>
      <span class="detail-book-desc">${effSide==='O'?'Over':'Under'} ${pd.threshold} ${sl}</span>
    </div>`;
  } else {
    document.getElementById('detail-book-row').innerHTML = '';
  }
}

async function openDetailMaster(midx) {
  const entry = masterDisplayList[midx];
  h2hOpp = null;
  viewSide = null;
  activeBatterSplits = null;
  if (propTf === 'h2h') propTf = 'L10';
  document.getElementById('props-list-view').style.display = 'none';
  document.getElementById('props-detail-view').style.display = '';
  document.getElementById('detail-hand-splits-row').innerHTML = '';
  if (entry.type === 'bet') {
    activeIdx = entry.propIdx;
    activeStatsEntry = null;
    activeStatType = null;
    activeGameLog = null;
    const pd = propList[activeIdx];
    const sl = STAT_LABELS[pd.stat] || pd.stat.replace(/^(Batter|Pitcher)\s+/, '');
    document.getElementById('detail-player-name').textContent = pd.player;
    document.getElementById('detail-prop-meta').textContent = (pd.side==='O'?'Over':'Under') + ' ' + pd.threshold + ' ' + sl;
    renderDetailView();
    // Async: fetch hand splits for batter props
    if (!pd.stat.startsWith('Pitcher')) {
      const pEntry = playerByName[pd.player.toLowerCase()];
      if (pEntry?.pid) {
        activeBatterSplits = await fetchBatterHandSplits(pEntry.pid);
        if (activeIdx === entry.propIdx && activeStatsEntry === null) renderDetailView();
      }
    }
  } else {
    activeIdx = -1;
    activeStatsEntry = entry;
    activeStatType = entry.pos === 'P' ? 'Pitcher Strikeouts' : 'Batter Hits';
    activeGameLog = null;
    document.getElementById('detail-player-name').textContent = entry.player;
    document.getElementById('detail-prop-meta').textContent = (entry.team||'') + (entry.pos==='P'?' · Pitcher':' · Batter');
    document.getElementById('detail-avg').textContent = '';
    document.getElementById('detail-chart-wrap').innerHTML = '<div class="detail-loading"><div class="detail-spinner"></div>Loading game log…</div>';
    document.getElementById('detail-tf-row').innerHTML = '';
    document.getElementById('detail-h2h-row').style.display = 'none';
    document.getElementById('detail-splits-row').innerHTML = '';
    document.getElementById('detail-book-row').innerHTML = '';
    document.getElementById('detail-side-toggle').innerHTML = '';
    const propTypes = entry.pos === 'P' ? PITCHER_PROP_TYPES : BATTER_PROP_TYPES;
    document.getElementById('detail-prop-selector').innerHTML =
      `<select class="detail-prop-select" onchange="switchStatsProp(this.value)">` +
      propTypes.map(st => {
        const lbl = STAT_LABELS[st] || st.replace(/^(Batter|Pitcher)\s+/,'');
        return `<option value="${st}"${st===activeStatType?' selected':''}>${lbl}</option>`;
      }).join('') + `</select>`;
    if (entry.pos !== 'P') {
      // Fetch game log and batter splits in parallel
      [activeGameLog, activeBatterSplits] = await Promise.all([
        fetchGameLogClient(entry.pid, activeStatType),
        fetchBatterHandSplits(entry.pid),
      ]);
    } else {
      activeGameLog = await fetchGameLogClient(entry.pid, activeStatType);
    }
    if (activeStatsEntry === entry) renderDetailView();
  }
}

function closeDetail() {
  activeIdx = -1;
  activeStatsEntry = null;
  activeStatType = null;
  activeGameLog = null;
  activeBatterSplits = null;
  h2hOpp = null;
  viewSide = null;
  if (propTf === 'h2h') propTf = 'L10';
  document.getElementById('props-list-view').style.display = '';
  document.getElementById('props-detail-view').style.display = 'none';
}

function setDetailTf(tf) {
  propTf = tf;
  if (tf !== 'h2h') h2hOpp = null;
  renderDetailView();
}

function switchProp(idxStr) {
  activeIdx = +idxStr;
  activeStatsEntry = null;
  h2hOpp = null;
  viewSide = null;
  const pd = propList[activeIdx];
  const sl = STAT_LABELS[pd.stat] || pd.stat.replace(/^(Batter|Pitcher)\s+/, '');
  document.getElementById('detail-prop-meta').textContent = (pd.side==='O'?'Over':'Under') + ' ' + pd.threshold + ' ' + sl;
  renderDetailView();
}

function setH2hOpp(opp) {
  h2hOpp = opp;
  renderDetailView();
}

renderPropList();

// ── Daily Player Props ────────────────────────────────────────
function renderDailyProps() {
  const dpc = document.getElementById('dailyprops-container');
  if (!dpc) return;
  const propPicks = (D.today_picks || []).filter(p => _propMarketRe.test((p.market || '').trim()));
  const badge = document.getElementById('tab-dailyprops-badge');
  if (badge) badge.textContent = propPicks.length;
  if (!propPicks.length) {
    dpc.innerHTML = '<div class="no-picks">No player prop picks for today yet — check back after the next daily card run.</div>';
    return;
  }
  dpc.innerHTML = '<div class="daily-props-grid">' + propPicks.map(p => {
    const raw = (p.market || '').trim();
    const m = _propMarketRe.exec(raw);
    if (!m) return '';
    const [, player, stat, side, threshStr] = m;
    const thresh = parseFloat(threshStr);
    const sl = STAT_LABELS[stat] || stat.replace(/^(Batter|Pitcher)\s+/, '');
    const edgeNum = (p.edge || 0) * 100;
    const edgeStr = (edgeNum > 0 ? '+' : '') + edgeNum.toFixed(1) + '%';
    const mProb = p.model_prob != null ? Math.round(p.model_prob * 100) + '%' : null;
    const fProb = p.fair_prob != null ? Math.round(p.fair_prob * 100) + '%' : null;
    const ouCls = side === 'O' ? 'over' : 'under';
    const ouLbl = side === 'O' ? 'O' : 'U';
    const isPitcher = stat.startsWith('Pitcher');
    const pEntry = playerByName[player.toLowerCase()];
    const pid = pEntry ? String(pEntry.pid) : '';
    const matchup = pid ? ((D.today_lineups && D.today_lineups.matchups) || {})[pid] : null;
    let matchupHtml = '';
    if (matchup && matchup.pitcher_name) {
      const hand = matchup.pitcher_hand || '';
      const hCls = hand === 'L' ? 'lhp' : hand === 'R' ? 'rhp' : 'switch';
      const hLbl = hand === 'L' ? 'LHP' : hand === 'R' ? 'RHP' : hand || '?';
      matchupHtml = `<span class="matchup-hand-badge ${hCls}" style="width:auto;height:auto;border-radius:4px;padding:.1rem .38rem;font-size:.6rem">${hLbl}</span>&thinsp;<span style="color:var(--t2)">${matchup.pitcher_name}</span>`;
    }
    const probRow = (mProb || fProb)
      ? `<div class="pick-prob-row" style="justify-content:flex-end;margin-top:.22rem">${mProb?`<span><span class="pick-prob-lbl">Model:</span> ${mProb}</span>`:''} ${fProb?`<span><span class="pick-prob-lbl">Fair:</span> ${fProb}</span>`:''}</div>` : '';
    const projScore = p.projected_score ? `<div style="font-size:.67rem;color:var(--tm);margin-top:.22rem">${p.projected_score}</div>` : '';
    return `<div class="dp-card">
      <div class="dp-card-top">
        <div style="min-width:0;flex:1">
          <div class="dp-player">${player}</div>
          <div class="dp-stat">
            <span class="stat-chip-sm">${sl}</span>
            <span>${isPitcher ? 'Pitcher' : 'Batter'}</span>
            ${matchupHtml ? '&middot;&nbsp;' + matchupHtml : ''}
          </div>
          ${projScore}
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div class="dp-edge">${edgeStr}</div>
          <div style="font-size:.58rem;color:var(--tm);margin-top:.12rem;letter-spacing:.07em;text-transform:uppercase">Edge</div>
          ${probRow}
        </div>
      </div>
      <div class="dp-bottom">
        <div style="display:flex;align-items:center;gap:.5rem">
          <span class="dp-ou-badge ${ouCls}">${ouLbl}&thinsp;${thresh}</span>
          <span class="dp-line">${p.line || '?'}</span>
        </div>
        <div style="display:flex;align-items:center;gap:.65rem">
          <span style="font-size:.65rem;color:var(--tm);text-transform:uppercase;letter-spacing:.05em">${p.book || '?'}</span>
          <span style="font-size:.78rem;font-weight:700;color:var(--t2);font-family:'Consolas','Menlo',monospace">${(p.stake_units || 0).toFixed(2)}u</span>
        </div>
      </div>
    </div>`;
  }).join('') + '</div>';
}
renderDailyProps();

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
            "top_markets": [], "sports": [], "prop_bets": [], "player_seasons": {}, "all_player_stats": {},
            "today_lineups": {"player_ids": [], "games": [], "games_detail": [], "matchups": {}},
            "stats": {"total_picks":0,"n_resolved":0,"n_wins":0,"win_rate":0,
                      "roi":0,"total_profit":0,"avg_edge":0,"avg_clv":None},
        }
    def _flag(name: str) -> bool:
        return os.getenv(name, "0").strip().lower() in ("1", "true", "yes")

    data["sim_flags"] = {
        "USE_LOGIT_FACTORS":       _flag("USE_LOGIT_FACTORS"),
        "USE_FULL_EXTRA_INNINGS":  _flag("USE_FULL_EXTRA_INNINGS"),
        "ENABLE_EDGE_VERIFY_GATE": _flag("ENABLE_EDGE_VERIFY_GATE"),
        "ENABLE_SAMPLE_GATE":      _flag("ENABLE_SAMPLE_GATE"),
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
