"""
Build a self-contained interactive HTML dashboard (no external libraries, works offline).

This is the fallback for the Power BI report: same KPIs, same definitions, computed in
the browser from the embedded segment data so the filters work. The KPI math mirrors
src/analysis.py (mile-weighted average of non-zero ratings over in-scope segments).

Writes  outputs/dashboard.html  (and docs/dashboard.html for GitHub Pages)

Usage:  python src/dashboard.py
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd

from analysis import material_group, treatment_group
from build_dataset import POOR_THRESHOLD

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"
DOCS = ROOT / "docs"
YEARS = [2009, 2010, 2011, 2012, 2013, 2014, 2021]


def txt(value, default):
    """Text field or a default. (Missing values arrive as NaN, which is truthy, so `or` is not enough.)"""
    return value if isinstance(value, str) and value else default


def build_payload():
    seg = gpd.read_parquet(PROCESSED / "segments.parquet")
    seg = seg[seg.in_scope].copy()
    metrics = pd.read_parquet(PROCESSED / "segment_metrics.parquet")
    seg = seg.merge(metrics, on="CenterlineID", how="left")
    long = pd.read_parquet(PROCESSED / "ratings_long.parquet")
    wide = long[long.year.isin(YEARS)].pivot(index="segment_id", columns="year", values="rating")

    # Simplify geometry (~3 m) and round coordinates to keep the file small.
    geom = seg.to_crs("EPSG:26915").geometry.simplify(3.0).to_crs(4326)

    def coords(g):
        parts = [g] if g.geom_type == "LineString" else list(g.geoms)
        return [[[round(x, 5), round(y, 5)] for x, y in p.coords] for p in parts]

    rows = []
    for (_, r), g in zip(seg.iterrows(), geom):
        ratings = wide.loc[r.CenterlineID] if r.CenterlineID in wide.index else None
        rows.append({
            "id": int(r.CenterlineID),
            "name": txt(r.StreetName, "?"),
            "from": txt(r.FromStreet, "?"),
            "to": txt(r.ToStreet, "?"),
            "mat": material_group(r.CONS),
            "cons": txt(r.CONS, "unknown"),
            "cls": txt(r.cl_DOT_FClassification, "Unclassified"),
            "trt": txt(treatment_group(r.maintenance_history_all), "None recorded"),
            "mi": round(float(r.miles), 4),
            "lm": round(float(r.lane_miles), 4),
            "r": [None if ratings is None or pd.isna(ratings.get(y)) else int(ratings.get(y)) for y in YEARS],
            "slope": None if pd.isna(r.deterioration_rate) else round(float(r.deterioration_rate), 2),
            "rank": None if pd.isna(r.priority_rank) else int(r.priority_rank),
            "g": coords(g),
        })
    manifest = json.loads((ROOT / "data" / "raw" / "_manifest.json").read_text())
    return {"years": YEARS, "poor": POOR_THRESHOLD, "segments": rows,
            "pulled": manifest["pulled_at_utc"][:10]}


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dubuque Street Dashboard</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --context: #e4e3dd;
  --series-1: #2a78d6; --series-2: #898781;
  --good: #256abf; --fair: #86b6ef; --poor: #e87471; --failed: #8a1f1f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --context: #2c2c2a;
    --series-1: #3987e5; --good: #3987e5; --fair: #1c5cab; --poor: #e66767; --failed: #f0a3a3;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --context: #2c2c2a;
  --series-1: #3987e5; --good: #3987e5; --fair: #1c5cab; --poor: #e66767; --failed: #f0a3a3;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1240px; margin: 0 auto; padding: 20px 16px 40px; }
h1 { font-size: 22px; margin: 0 0 2px; font-weight: 600; }
.sub { color: var(--ink-2); margin: 0 0 16px; font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: end; margin-bottom: 16px; }
.filters label { display: flex; flex-direction: column; font-size: 12px; color: var(--ink-2); gap: 4px; }
select { font: inherit; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--border);
         background: var(--surface); color: var(--ink); min-width: 170px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.tile .k { font-size: 12px; color: var(--ink-2); }
.tile .v { font-size: 28px; font-weight: 600; margin-top: 2px; }
.tile .n { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 12px; }
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
.card h2 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
.card .note { font-size: 11.5px; color: var(--muted); margin-top: 6px; }
svg text { fill: var(--muted); font-size: 11px; }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,.12); max-width: 300px; display: none; z-index: 10; }
.tip b { font-weight: 600; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: var(--ink-2); margin-top: 6px; }
.sw { display: inline-block; width: 16px; height: 4px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { text-align: left; color: var(--ink-2); font-weight: 600; border-bottom: 1px solid var(--grid); padding: 6px 6px; }
td { border-bottom: 1px solid var(--grid); padding: 5px 6px; font-variant-numeric: tabular-nums; }
td.num, th.num { text-align: right; }
.tablewrap { overflow-x: auto; }
details summary { cursor: pointer; font-size: 12px; color: var(--ink-2); margin-top: 6px; }
#map path { fill: none; stroke-linecap: round; }
#map path.seg:hover { stroke: var(--ink); stroke-width: 4; }
footer { color: var(--muted); font-size: 11.5px; margin-top: 18px; }
</style>
</head>
<body>
<main>
  <h1>Dubuque street condition dashboard</h1>
  <p class="sub">City-rated streets, survey years 2009–2014 and 2021. Independent analysis of public City of Dubuque GIS data
  (pulled __PULLED__); not affiliated with or endorsed by the City.</p>

  <div class="filters" role="group" aria-label="Filters">
    <label>Material <select id="f-mat"></select></label>
    <label>Functional class <select id="f-cls"></select></label>
    <label>Recorded treatment <select id="f-trt"></select></label>
    <label>"Poor" threshold <select id="f-thr">
      <option value="50">≤ 50</option><option value="55" selected>≤ 55 (default)</option><option value="60">≤ 60</option>
    </select></label>
  </div>

  <div class="tiles" id="tiles"></div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>Network average condition by survey year</h2>
        <svg id="trend" viewBox="0 0 520 230" width="100%" role="img" aria-label="Line chart of mile-weighted average rating by year"></svg>
        <div class="note">Mile-weighted average of non-zero ratings. No usable ratings exist for 2015–2020; the dotted segment only joins 2014 to 2021.</div>
        <details><summary>Show as table</summary><div class="tablewrap"><table id="trend-table"></table></div></details>
      </div>
      <div class="card" style="margin-top:12px">
        <h2>2021 network miles by condition band</h2>
        <svg id="bands" viewBox="0 0 520 70" width="100%" role="img" aria-label="Stacked bar of 2021 miles by condition band"></svg>
        <div class="legend" id="band-legend"></div>
      </div>
    </div>
    <div class="card">
      <h2>2021 condition map</h2>
      <svg id="map" viewBox="0 0 600 560" width="100%" role="img" aria-label="Map of street segments colored by 2021 condition band"></svg>
      <div class="legend" id="map-legend"></div>
      <div class="note">Hover a street for its rating history. Gray = outside the current filter.</div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <h2>Top 25 by illustrative priority score (within filter)</h2>
    <div class="tablewrap"><table id="prio"></table></div>
    <div class="note">Score = 40% condition + 20% deterioration + 25% traffic + 15% lane-miles. A transparent, re-weightable
    score for discussion, not a City methodology; 96% of traffic inputs are a functional-class proxy.</div>
  </div>

  <footer>Data: City of Dubuque Engineering and Public Works Departments, GIS Office — "Street Ratings for Web" ArcGIS REST service.
  Bands: Good 71–100, Fair 56–70, Poor 26–55, Failed 1–25 (collapsed from ASTM D6433 PCI classes; the City's scale is undocumented).</footer>
</main>
<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;
const YEARS = DATA.years, SEGS = DATA.segments;
const BANDS = [["Good", 71, "--good"], ["Fair", 56, "--fair"], ["Poor", 26, "--poor"], ["Failed", 1, "--failed"]];
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const band = r => r == null ? null : (BANDS.find(b => r >= b[1]) || [null])[0];
const bandVar = name => (BANDS.find(b => b[0] === name) || [, , "--muted"])[2];
const fmt1 = v => v == null || isNaN(v) ? "—" : v.toFixed(1);
const pct = v => v == null || isNaN(v) ? "—" : (100 * v).toFixed(1) + "%";
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };

// ---- filters -------------------------------------------------------------
const FILTERS = [["f-mat", "mat", "All materials"], ["f-cls", "cls", "All classes"], ["f-trt", "trt", "All treatments"]];
for (const [id, key, all] of FILTERS) {
  const sel = document.getElementById(id);
  const vals = [...new Set(SEGS.map(s => s[key]))].sort();
  sel.innerHTML = `<option value="">${all}</option>` + vals.map(v => `<option>${esc(v)}</option>`).join("");
  sel.addEventListener("change", render);
}
document.getElementById("f-thr").addEventListener("change", render);
const selected = () => SEGS.filter(s => FILTERS.every(([id, key]) => {
  const v = document.getElementById(id).value; return !v || s[key] === v; }));

// ---- KPI math (mirrors src/analysis.py) ----------------------------------
function yearStats(segs, yi, thr) {
  let w = 0, sum = 0, poorMi = 0, backlog = 0, n = 0;
  for (const s of segs) { const r = s.r[yi]; if (r == null) continue;
    n++; w += s.mi; sum += r * s.mi; if (r <= thr) { poorMi += s.mi; backlog += s.lm; } }
  return { n, avg: w ? sum / w : NaN, poorShare: w ? poorMi / w : NaN, backlog, miles: w };
}

// ---- tooltip -------------------------------------------------------------
const tip = document.getElementById("tip");
function showTip(evt, html) { tip.innerHTML = html; tip.style.display = "block";
  const x = Math.min(evt.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(evt.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px"; tip.style.top = y + "px"; }
const hideTip = () => tip.style.display = "none";

// ---- trend chart ---------------------------------------------------------
function drawTrend(stats) {
  const svg = document.getElementById("trend"); svg.innerHTML = "";
  const W = 520, H = 230, L = 40, R = 20, T = 12, B = 28, lo = 50, hi = 90;
  const x = i => L + i * (W - L - R) / (YEARS.length - 1);
  const y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  for (let v = lo; v <= hi; v += 10) {
    el("line", {x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: css("--grid")}, svg);
    el("text", {x: L - 6, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = v; }
  YEARS.forEach((yr, i) => el("text", {x: x(i), y: H - 8, "text-anchor": "middle"}, svg).textContent = yr);
  const pts = stats.map((s, i) => [x(i), y(s.avg)]);
  const path = pts.slice(0, 6).map((p, i) => (i ? "L" : "M") + p.join(",")).join("");
  el("path", {d: path, stroke: css("--series-1"), "stroke-width": 2, fill: "none"}, svg);
  el("path", {d: `M${pts[5]}L${pts[6]}`, stroke: css("--series-1"), "stroke-width": 1.5, fill: "none",
              "stroke-dasharray": "2 4"}, svg);
  pts.forEach((p, i) => {
    el("circle", {cx: p[0], cy: p[1], r: 4.5, fill: css("--series-1"), stroke: css("--surface"), "stroke-width": 2}, svg);
    const hit = el("rect", {x: p[0] - 18, y: T, width: 36, height: H - T - B, fill: "transparent"}, svg);
    hit.addEventListener("mousemove", e => showTip(e, `<b>${YEARS[i]}</b><br>Avg rating ${fmt1(stats[i].avg)}<br>` +
      `Miles Poor or worse ${pct(stats[i].poorShare)}<br>Segments rated ${stats[i].n.toLocaleString()}`));
    hit.addEventListener("mouseleave", hideTip);
  });
  const last = pts[6]; el("text", {x: last[0] - 4, y: last[1] - 10, "text-anchor": "end",
    style: `fill:${css("--ink")};font-size:12px`}, svg).textContent = fmt1(stats[6].avg);
  document.getElementById("trend-table").innerHTML =
    "<tr><th>Year</th><th class=num>Segments rated</th><th class=num>Avg rating</th><th class=num>% miles Poor+</th><th class=num>Backlog lane-mi</th></tr>" +
    stats.map((s, i) => `<tr><td>${YEARS[i]}</td><td class=num>${s.n.toLocaleString()}</td><td class=num>${fmt1(s.avg)}</td>` +
      `<td class=num>${pct(s.poorShare)}</td><td class=num>${fmt1(s.backlog)}</td></tr>`).join("");
}

// ---- band bar ------------------------------------------------------------
function drawBands(segs) {
  const svg = document.getElementById("bands"); svg.innerHTML = "";
  const miles = {}; let total = 0;
  for (const s of segs) { const b = band(s.r[6]); if (!b) continue; miles[b] = (miles[b] || 0) + s.mi; total += s.mi; }
  let x = 0; const W = 520;
  for (const [name, , v] of BANDS) {
    const w = total ? (miles[name] || 0) / total * W : 0; if (w <= 0) continue;
    const r = el("rect", {x: x + 1, y: 8, width: Math.max(w - 2, 1), height: 26, rx: 4, fill: css(v)}, svg);
    r.addEventListener("mousemove", e => showTip(e, `<b>${name}</b><br>${fmt1(miles[name])} miles (${pct(miles[name] / total)})`));
    r.addEventListener("mouseleave", hideTip);
    if (w > 60) el("text", {x: x + 8, y: 56, style: `fill:${css("--ink-2")};font-size:12px`}, svg)
      .textContent = `${name} ${pct(miles[name] / total)}`;
    x += w;
  }
  document.getElementById("band-legend").innerHTML = BANDS.map(([n, , v]) =>
    `<span><span class="sw" style="background:var(${v})"></span>${n}: ${fmt1(miles[n] || 0)} mi</span>`).join("");
}

// ---- map -----------------------------------------------------------------
let project;
function setupProjection() {
  let x0 = 180, x1 = -180, y0 = 90, y1 = -90;
  for (const s of SEGS) for (const part of s.g) for (const [x, y] of part) {
    x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  const k = Math.cos((y0 + y1) / 2 * Math.PI / 180), W = 600, H = 560, pad = 10;
  const sc = Math.min((W - 2 * pad) / ((x1 - x0) * k), (H - 2 * pad) / (y1 - y0));
  const ox = (W - (x1 - x0) * k * sc) / 2, oy = (H - (y1 - y0) * sc) / 2;
  project = ([x, y]) => [(ox + (x - x0) * k * sc).toFixed(1), (oy + (y1 - y) * sc).toFixed(1)];
}
const pathD = s => s.g.map(part => part.map((p, i) => (i ? "L" : "M") + project(p).join(",")).join("")).join("");
function drawMap(segs) {
  const svg = document.getElementById("map"); svg.innerHTML = "";
  const inFilter = new Set(segs.map(s => s.id));
  const ctx = el("g", {}, svg), main = el("g", {}, svg);
  const order = ["Good", "Fair", "Poor", "Failed"];
  const sorted = [...SEGS].sort((a, b) => order.indexOf(band(a.r[6])) - order.indexOf(band(b.r[6])));
  for (const s of sorted) {
    const on = inFilter.has(s.id), b = band(s.r[6]);
    const wide = s.cls.includes("Arterial") ? 2.2 : s.cls.includes("Collector") ? 1.6 : 1.1;
    const p = el("path", {d: pathD(s), class: on ? "seg" : "",
      stroke: on ? css(bandVar(b)) : css("--context"), "stroke-width": on ? wide : 0.8}, on ? main : ctx);
    if (!on) continue;
    p.addEventListener("mousemove", e => showTip(e,
      `<b>${esc(s.name)}</b><br>${esc(s.from)} → ${esc(s.to)}<br>2021 rating <b>${s.r[6] ?? "—"}</b> (${b})<br>` +
      `History: ${YEARS.map((y, i) => `${String(y).slice(2)}:${s.r[i] ?? "—"}`).join(" ")}<br>` +
      `Trend: ${s.slope == null ? "no reliable estimate" : (s.slope > 0 ? "+" : "") + s.slope + " pts/yr"}<br>` +
      `Material: ${esc(s.cons)} · ${esc(s.cls)}<br>Priority rank: ${s.rank ? "#" + s.rank : "—"}`));
    p.addEventListener("mouseleave", hideTip);
  }
  document.getElementById("map-legend").innerHTML = BANDS.map(([n, lo, v]) =>
    `<span><span class="sw" style="background:var(${v})"></span>${n}</span>`).join("");
}

// ---- priority table ------------------------------------------------------
function drawPriority(segs) {
  const top = segs.filter(s => s.rank).sort((a, b) => a.rank - b.rank).slice(0, 25);
  document.getElementById("prio").innerHTML =
    "<tr><th class=num>Rank</th><th>Street</th><th>Block</th><th>Class</th><th class=num>2021</th><th class=num>Trend (pts/yr)</th><th class=num>Lane-mi</th></tr>" +
    top.map(s => `<tr><td class=num>${s.rank}</td><td>${esc(s.name)}</td><td>${esc(s.from)} → ${esc(s.to)}</td>` +
      `<td>${esc(s.cls)}</td><td class=num>${s.r[6] ?? "—"}</td><td class=num>${s.slope ?? "—"}</td><td class=num>${s.lm.toFixed(2)}</td></tr>`).join("") ||
    "<tr><td colspan=7>No segments in this filter.</td></tr>";
}

// ---- tiles ---------------------------------------------------------------
function drawTiles(stats, segs, thr) {
  const s21 = stats[6], s14 = stats[5];
  const tiles = [
    ["Average condition, 2021", fmt1(s21.avg), "mile-weighted, 0–100"],
    [`Miles rated Poor or worse (≤${thr})`, pct(s21.poorShare), `${fmt1(s21.poorShare * s21.miles)} of ${fmt1(s21.miles)} miles`],
    ["Backlog, 2021", fmt1(s21.backlog) + " lane-mi", "lane counts mostly assumed (2)"],
    ["Change 2014 → 2021", (s21.avg - s14.avg >= 0 ? "+" : "") + fmt1(s21.avg - s14.avg), "points, over a 7-year gap"],
    ["Segments in filter", segs.length.toLocaleString(), `${fmt1(segs.reduce((a, s) => a + s.mi, 0))} centerline miles`],
  ];
  document.getElementById("tiles").innerHTML = tiles.map(([k, v, n]) =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("");
}

function render() {
  const segs = selected(), thr = +document.getElementById("f-thr").value;
  const stats = YEARS.map((_, i) => yearStats(segs, i, thr));
  drawTiles(stats, segs, thr); drawTrend(stats); drawBands(segs); drawMap(segs); drawPriority(segs);
}
setupProjection(); render();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
</script>
</body>
</html>
"""


def main():
    payload = build_payload()
    html = (TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"), allow_nan=False))
            .replace("__PULLED__", payload["pulled"]))
    out = OUTPUTS / "dashboard.html"
    out.write_text(html)
    DOCS.mkdir(exist_ok=True)
    shutil.copy(out, DOCS / "dashboard.html")
    print(f"dashboard: {out.stat().st_size / 1e6:.1f} MB, {len(payload['segments']):,} segments")


if __name__ == "__main__":
    main()
