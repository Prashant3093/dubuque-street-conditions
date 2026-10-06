"""
Render README.md and outputs/FINDINGS.md from templates, filling every number from
outputs/metrics.json and data/processed/data_quality.json.

No figure in either document is typed by hand: re-running the pipeline re-renders them.

Templates: src/templates/README.md, src/templates/FINDINGS.md  ({placeholder} syntax)
Usage:     python src/write_reports.py
"""

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
TEMPLATES = Path(__file__).resolve().parent / "templates"


def f1(v):
    return f"{v:.1f}"


def f2(v):
    return f"{v:.2f}"


def pct(v, digits=0):
    return f"{100 * v:.{digits}f}%"


def signed(v, digits=1):
    return f"{v:+.{digits}f}"


def top_table(n):
    t = pd.read_csv(OUTPUTS / "tables" / "priority_top50.csv").head(n)
    lines = ["| Rank | Street | Block | Road class | 2021 rating | Trend (pts/yr) | Traffic (2013) |",
             "|---:|---|---|---|---:|---:|---:|"]
    for _, r in t.iterrows():
        traffic = f"{r.traffic_est:,.0f}" + ("*" if r.traffic_imputed else "")
        cls = r.cl_DOT_FClassification if isinstance(r.cl_DOT_FClassification, str) else "—"
        lines.append(f"| {r.priority_rank} | {r.StreetName.title()} | {str(r.FromStreet).title()} → "
                     f"{str(r.ToStreet).title()} | {cls} | {r.rating_2021:.0f} | {r.slope_clean:.2f} | {traffic} |")
    return "\n".join(lines)


def top_repeat(n):
    """e.g. 'Rockdale Rd appears 5 times' for the most repeated street in the top n."""
    counts = pd.read_csv(OUTPUTS / "tables" / "priority_top50.csv").head(n).StreetName.value_counts()
    return f"{counts.index[0].title()} appears {counts.iloc[0]} times"


def fastest_table(n):
    t = pd.read_csv(OUTPUTS / "tables" / "fastest_declining_segments.csv")
    # one row per street + material so consecutive blocks of the same street do not crowd the list
    t = t.drop_duplicates(subset=["StreetName", "slope_clean"]).head(n)
    return ", ".join(f"{r.StreetName.title()} ({r.FromStreet.title()} → {r.ToStreet.title()}, "
                     f"{r.slope_clean:.1f}/yr)" for _, r in t.iterrows())


def values():
    m = json.loads((OUTPUTS / "metrics.json").read_text())
    dq = json.loads((ROOT / "data" / "processed" / "data_quality.json").read_text())
    manifest = json.loads((ROOT / "data" / "raw" / "_manifest.json").read_text())
    zr = dq["zero_reasons"]
    v = {
        # provenance
        "pulled": manifest["pulled_at_utc"][:10],
        "n_ratings_rows": f"{dq['ratings_rows']:,}",
        "n_centerlines": f"{dq['centerline_features']:,}",
        # scope
        "n_segments": f"{dq['in_scope_segments']:,}",
        "miles": f"{dq['in_scope_miles']:.0f}",
        "lane_miles": f"{dq['in_scope_lane_miles']:.0f}",
        "n_rated_segments": f"{dq['rated_segments']:,}",
        "n_matched": f"{dq['rated_segments_matched_to_centerline']:,}",
        "n_unrated_centerlines": f"{dq['centerline_clids_without_rating']:,}",
        "n_unrated_dubuque": f"{dq['unrated_centerlines_by_city'].get('DUBUQUE', 0):,}",
        "n_retired": dq["scope_reasons"].get("retired_segment", 0),
        "n_no_2021": dq["scope_reasons"].get("no_usable_2021_rating", 0),
        "n_inscope_maintresp1": f"{dq['in_scope_by_maintresp'].get('1', 0):,}",
        "n_null_clid_rows": dq["ratings_rows_null_centerline_id"],
        # data quality
        "y2015_zero_share": pct(dq["y2015_zero_share"]),
        "y2015_nonzero": dq["y2015_nonzero_values"],
        "y2022_nonnull": dq["y2022_non_null"],
        "dup_groups": dq["dup_centerline_groups"],
        "dup_rows": dq["dup_centerline_rows"],
        "zero_retired": zr.get("segment_retired", 0),
        "zero_before": zr.get("before_first_rating", 0),
        "zero_isolated": zr.get("isolated_gap", 0),
        "zero_2015": f"{zr.get('y2015_not_collected', 0):,}",
        "lanes_missing": f"{dq['in_scope_lanes_imputed']:,}",
        "lanes_missing_share": pct(dq["in_scope_lanes_imputed"] / dq["in_scope_segments"]),
        "traffic_present": dq["in_scope_traffic_count_present"],
        "traffic_present_share": pct(dq["in_scope_traffic_count_present"] / dq["in_scope_segments"], 1),
        "year_missing": dq["in_scope_year_missing"],
        "cons_missing": dq["in_scope_cons_missing"],
        "width_missing": dq["in_scope_width_missing_or_zero"],
        "fclass_missing": dq["in_scope_fclass_missing"],
        "n_brick": dq["in_scope_brick"],
        "n_constant": f"{dq['segments_identical_rating_every_year']:,}",
        "n_split": dq["centerline_clids_split_into_pieces"],
        "n_nonstandard": dq["nonstandard_rating_values"],
        "join_dupes": manifest["files"]["road_centerlines.geojson"]["rows_returned_before_dedup"]
                      - manifest["files"]["road_centerlines.geojson"]["records_fetched"],
        # 1 deterioration
        "det_n": f"{m['det_segments_clean_slope']:,}",
        "det_excluded": m["det_segments_excluded_unrecorded_jump"],
        "det_median": f2(-m["det_slope_median"]),
        "det_p10": f2(-m["det_slope_p10"]),
        "det_share_declining": pct(m["det_share_declining"]),
        "det_median_incl": f2(-m["det_slope_median_incl_jumps"]),
        "fastest_list": fastest_table(4),
        # 2 material
        "mat_acc": f2(-m["mat_asphalt_mean_slope"]),
        "mat_acc_lo": f2(-m["mat_asphalt_ci_high"]), "mat_acc_hi": f2(-m["mat_asphalt_ci_low"]),
        "mat_pcc": f2(-m["mat_concrete_mean_slope"]),
        "mat_pcc_lo": f2(-m["mat_concrete_ci_high"]), "mat_pcc_hi": f2(-m["mat_concrete_ci_low"]),
        "mat_acc_n": f"{m['mat_asphalt_n']:,}", "mat_pcc_n": m["mat_concrete_n"],
        "mat_diff": f2(m["mat_pcc_minus_acc"]),
        "mat_adj": f2(m["mat_adj_pcc_coef"]),
        "mat_adj_lo": f2(m["mat_adj_pcc_ci_low"]), "mat_adj_hi": f2(m["mat_adj_pcc_ci_high"]),
        "mat_ratio": f"{m['mat_concrete_mean_slope'] / m['mat_asphalt_mean_slope']:.0%}",
        "mat_acc_r21": f1(m["mat_asphalt_mean_rating_2021"]),
        "mat_pcc_r21": f1(m["mat_concrete_mean_rating_2021"]),
        # 3 lifecycle
        "life_fast_age": f"{m['life_asphalt_fastest_decline_age']:.0f}",
        "life_fast_rate": f1(-m["life_asphalt_fastest_decline_rate"]),
        "life_acc_0": f"{m['life_asphalt_fit_at_age_0']:.0f}",
        "life_acc_10": f"{m['life_asphalt_fit_at_age_10']:.0f}",
        "life_pcc_0": f"{m['life_concrete_fit_at_age_0']:.0f}",
        "life_pcc_10": f"{m['life_concrete_fit_at_age_10']:.0f}",
        "life_km_acc": f"{m['life_km_median_asphalt']:.0f}",
        "life_km_events": m["life_km_events"],
        "life_km_n": f"{m['life_km_segments']:,}",
        "life_above_20": pct(m["life_km_share_above_poor_at_20"]),
        # 4 maintenance
        "mnt_n": m["mnt_segments"],
        "mnt_pre": f1(m["mnt_mean_pre"]), "mnt_post": f1(m["mnt_mean_post"]),
        "mnt_change": signed(m["mnt_mean_change"]),
        "mnt_change_lo": f1(m["mnt_change_ci_low"]), "mnt_change_hi": f1(m["mnt_change_ci_high"]),
        "mnt_did": signed(m["mnt_mean_did"]),
        "mnt_did_lo": f1(m["mnt_did_ci_low"]), "mnt_did_hi": f1(m["mnt_did_ci_high"]),
        "mnt_share": pct(m["mnt_share_improved"]),
        "mnt_overlay_n": m["mnt_overlay_n"], "mnt_overlay": signed(m["mnt_overlay_mean_change"]),
        "mnt_recon_n": m["mnt_reconstruction_n"], "mnt_recon": signed(m["mnt_reconstruction_mean_change"]),
        "mnt_prev_n": m["mnt_preventive_n"], "mnt_prev": signed(m["mnt_preventive_mean_change"]),
        # 5 priority
        "w_cond": pct(m["pri_weights"]["condition"]), "w_det": pct(m["pri_weights"]["deterioration"]),
        "w_traf": pct(m["pri_weights"]["traffic"]), "w_size": pct(m["pri_weights"]["size"]),
        "pri_proxy": pct(m["pri_traffic_imputed_share"]),
        "pri_counts": m["pri_traffic_counts_used"],
        "pri_ov_equal": m["pri_top50_overlap_equal"],
        "pri_ov_cond": m["pri_top50_overlap_condition_heavy"],
        "pri_ov_notraffic": m["pri_top50_overlap_no_traffic"],
        "pri_top50_lm": f1(m["pri_top50_lane_miles"]),
        "pri_top50_real": m["pri_top50_with_real_traffic_count"],
        "class_medians": ", ".join(f"{k} {v:,.0f}" for k, v in m["pri_traffic_class_medians"].items()),
        "top10": top_table(10),
        "top10_repeat": top_repeat(10),
        "mat_acc_nonlocal": pct(m["mat_acc_nonlocal_share"]),
        "mat_pcc_nonlocal": pct(m["mat_pcc_nonlocal_share"]),
        "mat_acc_year": f"{m['mat_acc_median_work_year']:.0f}",
        "mat_pcc_year": f"{m['mat_pcc_median_work_year']:.0f}",
        # 6 KPIs
        "kpi_good": pct(m["kpi_2021_share_miles_good"]),
        "kpi_fair": pct(m["kpi_2021_share_miles_fair"]),
        "kpi_poor_share": pct(m["kpi_2021_share_miles_poor_or_worse"], 1),
        "kpi_poor_miles": f1(m["kpi_2021_miles_poor_or_worse"]),
        "kpi_avg_2009": f1(m["kpi_avg_rating_2009"]), "kpi_avg_2014": f1(m["kpi_avg_rating_2014"]),
        "kpi_avg_2021": f1(m["kpi_avg_rating_2021"]),
        "kpi_panel_n": f"{m['kpi_panel_segments']:,}",
        "kpi_panel_change": signed(m["kpi_panel_change_2009_2021"]),
        "kpi_bl_2009": f1(m["kpi_backlog_lane_miles_2009"]),
        "kpi_bl_2014": f1(m["kpi_backlog_lane_miles_2014"]),
        "kpi_bl_2021": f1(m["kpi_backlog_lane_miles_2021"]),
        "kpi_bl_50": f1(m["kpi_2021_backlog_lane_miles_le_50"]),
        "kpi_bl_60": f1(m["kpi_2021_backlog_lane_miles_le_60"]),
        "kpi_bl_lanes_assumed": pct(m["kpi_2021_lanes_imputed_share_of_backlog"]),
    }
    return v


def main():
    v = values()
    for name, dest in [("README.md", ROOT / "README.md"), ("FINDINGS.md", OUTPUTS / "FINDINGS.md")]:
        text = (TEMPLATES / name).read_text().format_map(v)
        dest.write_text(text)
        print(f"wrote {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
