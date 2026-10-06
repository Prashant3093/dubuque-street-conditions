"""
Static and interactive maps of the Dubuque street network.

Writes
  outputs/figures/condition_map_2021.png     2021 condition band, print-ready
  outputs/figures/deterioration_map.png      per-segment deterioration rate
  outputs/dubuque_streets.html               interactive Leaflet map with hover tooltips
  data/processed/segments_poor_2021.geojson  Poor-or-worse segments only (for ArcGIS Online public accounts)
                                             (docs/index.html gets a copy for GitHub Pages)

Context layer: the service has no city-limits polygon, so every centerline that is
NOT in the City's rating program (county roads, neighbouring towns, unrated streets)
is drawn in light gray behind the rated network.

Usage:  python src/maps.py
"""

import json
import shutil
from pathlib import Path

import folium
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D

from build_dataset import POOR_THRESHOLD, RATING_YEARS

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"
FIGURES = OUTPUTS / "figures"
DOCS = ROOT / "docs"
PROJECTED_CRS = "EPSG:26915"

SURFACE = "#fcfcfb"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
CONTEXT = "#dcdbd5"

# Diverging blue <-> red around the Poor threshold: blue = above Poor, red = Poor or worse.
# Each arm validated as an ordinal ramp against the light surface.
BAND_COLORS = {"Good": "#256abf", "Fair": "#86b6ef", "Poor": "#e87471", "Failed": "#8a1f1f"}
BAND_LABELS = {"Good": "Good (71–100)", "Fair": "Fair (56–70)",
               "Poor": f"Poor (26–{POOR_THRESHOLD})", "Failed": "Failed (1–25)"}

# Deterioration rate classes (points per year). Red ramp = faster decline.
DET_CLASSES = [
    (-99, -1.5, "Losing > 1.5 pts/yr", "#8a1f1f"),
    (-1.5, -1.0, "1.0 – 1.5 pts/yr", "#c93535"),
    (-1.0, -0.5, "0.5 – 1.0 pts/yr", "#e0605d"),
    (-0.5, -1e-9, "< 0.5 pts/yr", "#ec9a97"),
    (-1e-9, 99, "Flat or improving", "#2a78d6"),
]
NO_ESTIMATE = "#b4b2a9"


def load():
    seg = gpd.read_parquet(PROCESSED / "segments.parquet")
    metrics = pd.read_parquet(PROCESSED / "segment_metrics.parquet")
    seg = seg.merge(metrics, on="CenterlineID", how="left")
    long = pd.read_parquet(PROCESSED / "ratings_long.parquet")

    context = gpd.read_file(RAW / "road_centerlines.geojson")
    context = context[~context["DUBUQUE.RoadCenterline.CLID"].isin(seg.loc[seg.in_scope, "CenterlineID"])]
    return seg[seg.in_scope].copy(), context, long


def line_width(fclass):
    """Thicker lines for arterials so the map hierarchy reads in print."""
    if fclass in ("OtherPrincipalArterial", "MinorArterial"):
        return 1.5
    if fclass in ("MajorCollector", "MinorCollector"):
        return 1.0
    return 0.6


def base_axes(seg, context, title, subtitle):
    fig, ax = plt.subplots(figsize=(11, 11), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    proj = seg.to_crs(PROJECTED_CRS)
    ctx = context.to_crs(PROJECTED_CRS)
    ctx.plot(ax=ax, color=CONTEXT, linewidth=0.4, zorder=1)
    # Frame the map on the rated network, with a margin
    xmin, ymin, xmax, ymax = proj.total_bounds
    pad = 0.04 * max(xmax - xmin, ymax - ymin)
    ax.set_xlim(xmin - pad, xmax + pad)
    ax.set_ylim(ymin - pad, ymax + pad)
    ax.set_axis_off()
    ax.set_title(title, loc="left", fontsize=16, color=INK, pad=24)
    ax.text(0, 1.01, subtitle, transform=ax.transAxes, fontsize=10, color=INK_2)
    ax.text(0, -0.02,
             "Independent analysis of public data; not affiliated with or endorsed by the City of Dubuque. "
             "Data: City of Dubuque Engineering and Public Works Departments, GIS Office (Street Ratings for Web).",
             fontsize=7, color=MUTED, transform=ax.transAxes, va="top")
    # North arrow + scale bar (1 mile)
    ax.annotate("N", xy=(0.97, 0.97), xytext=(0.97, 0.91), xycoords="axes fraction",
                ha="center", fontsize=11, color=INK_2,
                arrowprops=dict(arrowstyle="-|>", color=INK_2, lw=1.2))
    x0, y0 = xmin, ymin - pad * 0.6
    ax.plot([x0, x0 + 1609.344], [y0, y0], color=INK_2, lw=2)
    ax.text(x0 + 804, y0 + pad * 0.15, "1 mile", ha="center", fontsize=8, color=INK_2)
    return fig, ax, proj


def fmt_miles(v):
    return f"{v:.1f} mi" if v < 10 else f"{v:.0f} mi"


def condition_map(seg, context):
    fig, ax, proj = base_axes(
        seg, context, "Dubuque street condition, 2021",
        f"{len(seg):,} City-rated segments, {seg.miles.sum():.0f} centerline miles. "
        "Gray: roads outside the City rating program.")
    for band in ["Good", "Fair", "Poor", "Failed"]:  # draw bad streets last so they sit on top
        sub = proj[proj.band_2021 == band]
        sub.plot(ax=ax, color=BAND_COLORS[band], zorder=3,
                 linewidth=sub.cl_DOT_FClassification.map(line_width).fillna(0.6))
    miles = seg.groupby("band_2021").miles.sum()
    handles = [Line2D([0], [0], color=BAND_COLORS[b], lw=3,
                      label=f"{BAND_LABELS[b]} — {fmt_miles(miles.get(b, 0))} ({miles.get(b, 0) / miles.sum():.0%})")
               for b in BAND_COLORS]
    handles.append(Line2D([0], [0], color=CONTEXT, lw=2, label="Not in City rating program"))
    leg = ax.legend(handles=handles, loc="lower right", frameon=True, fontsize=9,
                    title="2021 rating band", title_fontsize=9.5, labelcolor=INK_2)
    leg.get_frame().set_edgecolor("#e1e0d9")
    leg.get_frame().set_facecolor(SURFACE)
    fig.savefig(FIGURES / "condition_map_2021.png", facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def det_class(rate):
    if pd.isna(rate):
        return None
    for lo, hi, label, _ in DET_CLASSES:
        if lo < rate <= hi:
            return label
    return None


def deterioration_map(seg, context):
    seg = seg.copy()
    seg["det_class"] = seg.deterioration_rate.apply(det_class)
    fig, ax, proj = base_axes(
        seg, context, "How fast are Dubuque streets losing condition?",
        "Per-segment trend in rating since last recorded work (OLS slope, 2009–2014 & 2021 surveys).")
    proj["det_class"] = seg["det_class"].values
    proj[proj.det_class.isna()].plot(ax=ax, color=NO_ESTIMATE, linewidth=0.6, zorder=2, linestyle=(0, (2, 1)))
    for lo, hi, label, color in reversed(DET_CLASSES):
        sub = proj[proj.det_class == label]
        sub.plot(ax=ax, color=color, zorder=3,
                 linewidth=sub.cl_DOT_FClassification.map(line_width).fillna(0.6))
    counts = seg.det_class.value_counts()
    handles = [Line2D([0], [0], color=c, lw=3, label=f"{lab} ({counts.get(lab, 0):,} segments)")
               for _, _, lab, c in DET_CLASSES]
    handles.append(Line2D([0], [0], color=NO_ESTIMATE, lw=2, linestyle=(0, (2, 1)),
                          label=f"No reliable estimate ({seg.det_class.isna().sum():,})"))
    leg = ax.legend(handles=handles, loc="lower right", frameon=True, fontsize=9,
                    title="Rating change per year", title_fontsize=9.5, labelcolor=INK_2)
    leg.get_frame().set_edgecolor("#e1e0d9")
    leg.get_frame().set_facecolor(SURFACE)
    fig.savefig(FIGURES / "deterioration_map.png", facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def interactive_map(seg, context, long):
    """Leaflet map. Geometry is simplified to ~1 m and properties trimmed to keep the
    file small enough to host on GitHub Pages."""
    hist = (long[long.year.isin([y for y in RATING_YEARS if y not in (2015, 2022)])]
            .pivot(index="segment_id", columns="year", values="rating"))
    history = hist.apply(lambda r: " · ".join(f"{y}: {'—' if pd.isna(v) else int(v)}" for y, v in r.items()), axis=1)

    g = seg[["CenterlineID", "StreetName", "FromStreet", "ToStreet", "CONS", "maintenance_history_all",
             "Year", "rating_2021", "band_2021", "deterioration_rate", "priority_rank",
             "cl_DOT_FClassification", "geometry"]].copy()
    g["history"] = g.CenterlineID.map(history)
    g["cross"] = g.FromStreet.fillna("?") + " → " + g.ToStreet.fillna("?")
    g["rate"] = g.deterioration_rate.map(lambda v: "no reliable estimate" if pd.isna(v) else f"{v:+.2f} pts/yr")
    g["rank"] = g.priority_rank.map(lambda v: "—" if pd.isna(v) else f"#{int(v):,}")
    g["work"] = g.maintenance_history_all.fillna("none recorded") + g.Year.map(
        lambda y: "" if pd.isna(y) else f" ({int(y)})")
    g["rating"] = g.rating_2021.map(lambda v: f"{int(v)}") + " (" + g.band_2021 + ")"
    g["material"] = g.CONS.fillna("unknown")
    g["color"] = g.band_2021.map(BAND_COLORS)
    g["weight"] = g.cl_DOT_FClassification.map(line_width).fillna(0.6) * 2.2
    g["top50"] = g.priority_rank <= 50

    proj = g.to_crs(PROJECTED_CRS)
    proj["geometry"] = proj.geometry.simplify(1.0)
    g = proj.to_crs(4326)
    keep = ["StreetName", "cross", "rating", "history", "rate", "material", "work", "rank", "color", "weight", "geometry"]

    center = [g.geometry.union_all().centroid.y, g.geometry.union_all().centroid.x]
    # Basemap-free by default (no API key or third-party tile dependency): the gray
    # context roads give orientation. OpenStreetMap is available as an optional layer.
    m = folium.Map(location=center, zoom_start=13, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap (optional reference)", show=False,
                     overlay=True, opacity=0.6).add_to(m)
    ctx = context.to_crs(PROJECTED_CRS)
    ctx["geometry"] = ctx.geometry.simplify(3.0)
    ctx = ctx[["geometry"]].to_crs(4326)
    folium.GeoJson(json.loads(ctx.to_json(drop_id=True)), name="Roads outside the City rating program",
                   style_function=lambda f: {"color": CONTEXT, "weight": 1.2, "opacity": 1},
                   interactive=False).add_to(m)

    tooltip = folium.GeoJsonTooltip(
        fields=["StreetName", "cross", "rating", "history", "rate", "material", "work", "rank"],
        aliases=["Street", "Block", "2021 rating", "Rating history", "Trend", "Material",
                 "Recorded work", "Priority rank"],
        sticky=True, style="font-family: system-ui, sans-serif; font-size: 12px;")
    folium.GeoJson(
        json.loads(g[keep].to_json(drop_id=True)),
        name="2021 condition",
        style_function=lambda f: {"color": f["properties"]["color"], "weight": f["properties"]["weight"], "opacity": 0.95},
        highlight_function=lambda f: {"weight": 6, "color": "#0b0b0b"},
        tooltip=tooltip,
    ).add_to(m)

    top = g[g.top50]
    if len(top):
        folium.GeoJson(
            json.loads(top[keep].to_json(drop_id=True)),
            name="Top 50 priority segments (illustrative score)",
            style_function=lambda f: {"color": "#0b0b0b", "weight": 7, "opacity": 0.35},
            tooltip=folium.GeoJsonTooltip(fields=["StreetName", "cross", "rank", "rating"],
                                          aliases=["Street", "Block", "Priority rank", "2021 rating"]),
            show=False,
        ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    legend = "".join(
        f'<div><span style="display:inline-block;width:18px;height:4px;background:{c};'
        f'vertical-align:middle;margin-right:6px;border-radius:2px"></span>{BAND_LABELS[b]}</div>'
        for b, c in BAND_COLORS.items())
    m.get_root().html.add_child(folium.Element(f"""
      <div style="position:fixed;bottom:24px;left:12px;z-index:9999;background:#fcfcfb;padding:10px 12px;
                  border:1px solid #e1e0d9;border-radius:6px;font:12px system-ui,sans-serif;color:#52514e;
                  max-width:300px">
        <div style="font-weight:600;color:#0b0b0b;margin-bottom:4px">Dubuque street condition, 2021</div>
        {legend}
        <div style="margin-top:6px;font-size:10.5px;color:#898781">Hover a street for its rating history.
        Independent analysis of public City of Dubuque GIS data; not affiliated with or endorsed by the City.</div>
      </div>"""))
    m.get_root().header.add_child(folium.Element(
        "<title>Dubuque Street Conditions</title><style>.leaflet-container{background:#fcfcfb !important}</style>"))
    out = OUTPUTS / "dubuque_streets.html"
    m.save(out)
    DOCS.mkdir(exist_ok=True)
    shutil.copy(out, DOCS / "index.html")
    print(f"interactive map: {out.stat().st_size / 1e6:.1f} MB")


def poor_subset(seg):
    """Poor-or-worse segments only. Small enough (< 1,000 features) to add directly to an
    ArcGIS Online web map from a free public account."""
    cols = ["CenterlineID", "StreetName", "FromStreet", "ToStreet", "CONS", "rating_2021",
            "band_2021", "deterioration_rate", "priority_rank", "geometry"]
    sub = seg[seg.rating_2021 <= POOR_THRESHOLD][cols]
    sub.to_file(PROCESSED / "segments_poor_2021.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    print(f"poor subset: {len(sub)} features")


def main():
    FIGURES.mkdir(parents=True, exist_ok=True)
    seg, context, long = load()
    poor_subset(seg)
    condition_map(seg, context)
    deterioration_map(seg, context)
    interactive_map(seg, context, long)
    print("maps written")


if __name__ == "__main__":
    main()
