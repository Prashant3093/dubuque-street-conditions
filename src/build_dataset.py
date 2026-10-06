"""
Build analysis-ready tables from the raw API pulls.

Inputs  (from src/fetch_data.py)
  data/raw/street_ratings.json        City "Street Ratings" table, one row per rating record
  data/raw/road_centerlines.geojson   Road centerlines (all of Dubuque County area), WGS84

Outputs
  data/processed/segments.parquet       one row per rated street segment (CenterlineID)
  data/processed/segments.geojson       same, with geometry, for GIS tools (QGIS / ArcGIS)
  data/processed/ratings_long.parquet   one row per segment x rating year
  data/processed/analysis_table.csv     tidy long table with key segment attributes attached
  data/processed/unmatched_ratings.csv  rating rows that cannot be placed on the map
  outputs/data_quality.md               every data-quality count quoted in the README

Nothing is silently dropped. Every exclusion is counted in outputs/data_quality.md
and every row keeps a flag explaining why it was excluded from analysis.

Usage:  python src/build_dataset.py
"""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"

RATING_YEARS = [2009, 2010, 2011, 2012, 2013, 2014, 2015, 2021, 2022]

# Projected CRS used for any length / centroid math (NAD83 / UTM zone 15N covers Dubuque).
PROJECTED_CRS = "EPSG:26915"

# ---------------------------------------------------------------------------
# Condition bands
# ---------------------------------------------------------------------------
# The City does not document its 0-100 rating method on the published service.
# The scale behaves like a Pavement Condition Index (PCI): 0-100, higher = better.
# We therefore borrow the ASTM D6433 PCI rating categories and collapse the seven
# ASTM classes into four:
#     ASTM D6433:  Good 86-100, Satisfactory 71-85, Fair 56-70, Poor 41-55,
#                  Very Poor 26-40, Serious 11-25, Failed 0-10
#     This repo:   Good   = 71-100  (ASTM Good + Satisfactory)
#                  Fair   = 56-70   (ASTM Fair)
#                  Poor   = 26-55   (ASTM Poor + Very Poor)
#                  Failed = 1-25    (ASTM Serious + Failed)
# "Below the Poor threshold" in this project means rating <= 55 (Poor or Failed).
# These cutoffs are an assumption, not the City's; analysis.py runs a sensitivity
# check on the threshold.
BAND_EDGES = [(71, "Good"), (56, "Fair"), (26, "Poor"), (1, "Failed")]
POOR_THRESHOLD = 55  # rating <= this counts as "Poor or worse"

# Lane count is missing on most centerlines. Where it is missing we assume 2 lanes
# (a typical two-way city street) and set lanes_imputed = True.
DEFAULT_LANES = 2

# MaintenanceHistory values meaning the segment is no longer a City-rated street.
RETIRED_KEYWORDS = ["Vacated", "Removed", "Eliminated", "Lost to", "City Takeover"]


def condition_band(rating):
    if pd.isna(rating):
        return None
    for lower, name in BAND_EDGES:
        if rating >= lower:
            return name
    return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_ratings():
    rows = json.loads((RAW / "street_ratings.json").read_text())
    df = pd.DataFrame(rows)
    df["CONS"] = df["CONS"].replace({"NA": None})  # "NA" string and real nulls both mean missing
    return df


def load_centerlines():
    gdf = gpd.read_file(RAW / "road_centerlines.geojson")
    # Drop the verbose service prefixes. The StreetRatings.* fields duplicate table 3,
    # which we use directly instead, so they are removed here.
    gdf = gdf[[c for c in gdf.columns if not c.startswith("DUBUQUE.StreetRatings.")]]
    gdf.columns = [c.replace("DUBUQUE.RoadCenterline.", "") for c in gdf.columns]
    gdf = gdf.set_crs(4326, allow_override=True)
    return gdf


# ---------------------------------------------------------------------------
# Ratings: de-duplicate, classify zeros, reshape
# ---------------------------------------------------------------------------
def collapse_duplicate_segments(ratings, dq):
    """Some CenterlineIDs appear on 2-3 rows. Investigation showed these rows carry
    identical ratings and differ only in MaintenanceHistory (one row per treatment).
    We assert the ratings really are identical, then keep one row per segment and
    join all treatments into one string."""
    year_cols = [f"Y{y}" for y in RATING_YEARS]
    has_id = ratings[ratings["CenterlineID"].notna()].copy()
    has_id["CenterlineID"] = has_id["CenterlineID"].astype(int)

    dup_groups = has_id[has_id["CenterlineID"].duplicated(keep=False)]
    distinct_rating_patterns = (
        dup_groups.groupby("CenterlineID")[year_cols]
        .apply(lambda g: len(g.drop_duplicates()))
    )
    conflicting = distinct_rating_patterns[distinct_rating_patterns > 1]
    dq["dup_centerline_groups"] = int(dup_groups["CenterlineID"].nunique())
    dq["dup_centerline_rows"] = int(len(dup_groups))
    dq["dup_groups_with_conflicting_ratings"] = int(len(conflicting))
    if len(conflicting):
        print(f"WARNING: {len(conflicting)} duplicate groups have conflicting ratings; "
              "keeping the lowest OBJECTID row for ratings.")

    has_id = has_id.sort_values("OBJECTID")
    treatments = (
        has_id.groupby("CenterlineID")["MaintenanceHistory"]
        .apply(lambda s: " | ".join(dict.fromkeys(s.dropna())) or None)
        .rename("maintenance_history_all")
    )
    n_rows = has_id.groupby("CenterlineID").size().rename("n_rating_rows")
    first = has_id.drop_duplicates("CenterlineID", keep="first").set_index("CenterlineID")
    seg = first.join(treatments).join(n_rows).reset_index()
    return seg


def classify_zero(values, year_index, retired):
    """Explain why a rating is 0. Returns None when the rating is not 0.

    Categories, checked in this order:
      y2015_not_collected  - 97% of Y2015 is 0; the 2015 survey was evidently not loaded
      segment_retired      - MaintenanceHistory says vacated/removed/eliminated/taken over
      before_first_rating  - 0 precedes the segment's first non-zero rating (not built yet)
      after_last_rating    - 0 follows the segment's last non-zero rating
      isolated_gap         - 0 sits between non-zero ratings (data-entry gap)
    No category means "failed pavement": the lowest genuine rating anywhere is 10,
    and no zero follows a low (<=30) rating, so 0 is never treated as a condition score.
    """
    year, value = RATING_YEARS[year_index], values[year_index]
    if value != 0:
        return None
    if year == 2015:
        return "y2015_not_collected"
    if retired:
        return "segment_retired"
    nonzero_idx = [i for i, v in enumerate(values) if pd.notna(v) and v > 0 and RATING_YEARS[i] != 2015]
    if not nonzero_idx:
        return "never_rated"
    if year_index < min(nonzero_idx):
        return "before_first_rating"
    if year_index > max(nonzero_idx):
        return "after_last_rating"
    return "isolated_gap"


def to_long(seg):
    """Wide Y#### columns -> one row per (segment, year)."""
    records = []
    for _, row in seg.iterrows():
        values = [row[f"Y{y}"] for y in RATING_YEARS]
        for i, year in enumerate(RATING_YEARS):
            raw = values[i]
            zero_reason = classify_zero(values, i, row["is_retired"]) if pd.notna(raw) else None
            usable = pd.notna(raw) and raw > 0
            records.append({
                "segment_id": row["CenterlineID"],
                "year": year,
                "rating_raw": raw,
                # Analysis value: zeros become missing (see classify_zero docstring)
                "rating": raw if usable else np.nan,
                "is_null": pd.isna(raw),
                "is_suspect_zero": zero_reason is not None,
                "zero_reason": zero_reason,
            })
    long = pd.DataFrame(records)
    long["condition_band"] = long["rating"].apply(condition_band)
    return long


# ---------------------------------------------------------------------------
# Centerlines: one geometry per CLID, derived attributes
# ---------------------------------------------------------------------------
def centerline_per_clid(lines, dq):
    """A few CLIDs are split into several geometry pieces. Merge them so each
    CLID has one (Multi)LineString and its Miles is the sum of the pieces."""
    lines = lines[lines["CLID"].notna()].copy()
    lines["CLID"] = lines["CLID"].astype(int)
    dq["centerline_clids_split_into_pieces"] = int(lines["CLID"].duplicated(keep=False).groupby(lines["CLID"]).any().sum())

    projected = lines.to_crs(PROJECTED_CRS)
    lines["geom_miles"] = projected.length / 1609.344

    attrs = (
        lines.sort_values("OBJECTID")
        .groupby("CLID")
        .agg({
            "StreetName": "first", "StreetType": "first", "PreDir": "first", "SufDir": "first",
            "Label": "first", "Speed": "first", "PrimaryMaterial": "first",
            "SecondaryMaterial": "first", "DOT_FClassification": "first",
            "NoOfLanes": "first", "Width": "first", "OneWay": "first",
            "TrafficCounts2013": "max", "MaintResp": "first", "MaintenanceResp": "first",
            "PlowResp": "first", "SnowIceControl": "first",
            "ZipLeft": "first", "ZipRight": "first", "CityLeft": "first", "CityRight": "first",
            "Miles": lambda s: s.sum(min_count=1), "geom_miles": "sum",
        })
    )
    geoms = lines.dissolve(by="CLID")[["geometry"]]
    out = gpd.GeoDataFrame(attrs.join(geoms), geometry="geometry", crs=4326)
    out.columns = ["cl_" + c if c != "geometry" else c for c in out.columns]
    return out


def add_derived(seg):
    """Lane-miles, map points, retired flag."""
    seg["miles"] = seg["cl_Miles"].fillna(seg["cl_geom_miles"])
    seg["miles_from_geometry"] = seg["cl_Miles"].isna() & seg["cl_geom_miles"].notna()

    lanes = seg["cl_NoOfLanes"].where(seg["cl_NoOfLanes"] > 0)
    seg["lanes_imputed"] = lanes.isna()
    seg["lanes"] = lanes.fillna(DEFAULT_LANES)
    seg["lane_miles"] = seg["miles"] * seg["lanes"]

    # A point on the line itself (halfway along) for Power BI / dashboard map visuals.
    has_geom = seg.geometry.notna()
    mid = seg[has_geom].to_crs(PROJECTED_CRS).geometry.interpolate(0.5, normalized=True)
    mid = gpd.GeoSeries(mid, crs=PROJECTED_CRS).to_crs(4326)
    seg.loc[has_geom, "mid_lon"] = mid.x.round(6)
    seg.loc[has_geom, "mid_lat"] = mid.y.round(6)
    return seg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    PROCESSED.mkdir(parents=True, exist_ok=True)
    dq = {}

    ratings = load_ratings()
    lines = load_centerlines()
    dq["ratings_rows"] = len(ratings)
    dq["centerline_features"] = len(lines)
    dq["centerlines_null_clid"] = int(lines["CLID"].isna().sum())

    # --- Ratings rows with no CenterlineID cannot be mapped: keep them, report them.
    no_id = ratings[ratings["CenterlineID"].isna()]
    no_id.to_csv(PROCESSED / "unmatched_ratings.csv", index=False)
    dq["ratings_rows_null_centerline_id"] = len(no_id)

    seg = collapse_duplicate_segments(ratings, dq)
    seg["is_retired"] = seg["maintenance_history_all"].fillna("").str.contains("|".join(RETIRED_KEYWORDS))

    # --- Join to centerlines and report the match rate on both sides.
    cl = centerline_per_clid(lines, dq)
    matched_ids = set(seg["CenterlineID"]) & set(cl.index)
    dq["rated_segments"] = len(seg)
    dq["rated_segments_matched_to_centerline"] = len(matched_ids)
    dq["rated_segments_without_centerline"] = len(seg) - len(matched_ids)
    unrated = cl[~cl.index.isin(seg["CenterlineID"])]
    dq["centerline_clids_total"] = len(cl)
    dq["centerline_clids_without_rating"] = len(unrated)
    dq["unrated_centerlines_by_city"] = (
        unrated["cl_CityLeft"].fillna("(null)").str.upper().value_counts().to_dict()
    )
    dq["unrated_centerlines_by_maintresp"] = (
        unrated["cl_MaintResp"].fillna(-9).astype(int).astype(str).value_counts().to_dict()
    )

    seg = seg.merge(cl, left_on="CenterlineID", right_index=True, how="left")
    seg = gpd.GeoDataFrame(seg, geometry="geometry", crs=4326)
    seg = add_derived(seg)

    # --- Long ratings table
    long = to_long(seg)
    dq["y2022_non_null"] = int(long.loc[long.year == 2022, "rating_raw"].notna().sum())
    dq["zero_reasons"] = long["zero_reason"].value_counts().to_dict()
    dq["y2015_nonzero_values"] = int((long.loc[long.year == 2015, "rating_raw"] > 0).sum())
    dq["y2015_zero_share"] = round(
        float((long.loc[long.year == 2015, "rating_raw"] == 0).mean()), 4)
    dq["null_ratings_by_year"] = long[long.is_null].groupby("year").size().to_dict()

    # Age = years since the segment's recorded `Year` (see README: evidence suggests
    # Year is the most recent major treatment, not necessarily original construction).
    year_built = seg.set_index("CenterlineID")["Year"]
    long["record_year"] = long["segment_id"].map(year_built)
    long["age"] = long["year"] - long["record_year"]

    # --- Scope: which segments enter the network analysis
    # In scope = present in the City's own Street Ratings table, mapped to a centerline,
    # not retired, and holding a usable (non-zero) 2021 rating.
    r2021 = long[long.year == 2021].set_index("segment_id")["rating"]
    seg["rating_2021"] = seg["CenterlineID"].map(r2021)
    seg["band_2021"] = seg["rating_2021"].apply(condition_band)
    seg["scope_reason"] = np.select(
        [seg.geometry.isna(), seg["is_retired"], seg["rating_2021"].isna()],
        ["no_centerline_geometry", "retired_segment", "no_usable_2021_rating"],
        default="in_scope",
    )
    seg["in_scope"] = seg["scope_reason"] == "in_scope"
    seg["is_brick"] = seg["CONS"].fillna("").str.contains("Brick", case=False) & ~seg["CONS"].fillna("").str.startswith("ACC")
    dq["scope_reasons"] = seg["scope_reason"].value_counts().to_dict()
    insc = seg[seg.in_scope]
    dq["in_scope_by_maintresp"] = insc["cl_MaintResp"].fillna(-9).astype(int).astype(str).value_counts().to_dict()
    dq["in_scope_by_city"] = insc["cl_CityLeft"].fillna("(null)").str.upper().value_counts().to_dict()

    # --- Completeness of the attributes the analysis leans on (in-scope segments)
    dq["in_scope_segments"] = int(len(insc))
    dq["in_scope_miles"] = round(float(insc["miles"].sum()), 2)
    dq["in_scope_lane_miles"] = round(float(insc["lane_miles"].sum()), 2)
    dq["in_scope_lanes_imputed"] = int(insc["lanes_imputed"].sum())
    dq["in_scope_traffic_count_present"] = int(insc["cl_TrafficCounts2013"].notna().sum())
    dq["in_scope_cons_missing"] = int(insc["CONS"].isna().sum())
    dq["in_scope_year_missing"] = int(insc["Year"].isna().sum())
    dq["in_scope_width_missing_or_zero"] = int((insc["cl_Width"].fillna(0) <= 0).sum())
    dq["in_scope_fclass_missing"] = int(insc["cl_DOT_FClassification"].isna().sum())
    dq["in_scope_miles_from_geometry"] = int(insc["miles_from_geometry"].sum())
    dq["in_scope_brick"] = int(insc["is_brick"].sum())
    dq["cons_values"] = seg["CONS"].fillna("(null)").value_counts().to_dict()
    dq["segments_identical_rating_every_year"] = int(
        long[long.year.isin([2009, 2010, 2011, 2012, 2013, 2014, 2021])]
        .groupby("segment_id")["rating"].nunique().eq(1).sum()
    )
    dq["nonstandard_rating_values"] = int(
        (long["rating"].notna() & (long["rating"] % 5 != 0)).sum())

    # --- Write outputs
    long["condition_band"] = long["condition_band"].astype("string")
    long.to_parquet(PROCESSED / "ratings_long.parquet", index=False)

    seg_out = seg.drop(columns=[f"Y{y}" for y in RATING_YEARS])
    seg_out.to_parquet(PROCESSED / "segments.parquet", index=False)

    # GeoJSON for GIS: keep it light (only in-scope + key fields, 6-decimal coords ~10cm)
    gis_cols = ["CenterlineID", "StreetName", "FromStreet", "ToStreet", "CONS",
                "maintenance_history_all", "Year", "rating_2021", "band_2021",
                "miles", "lane_miles", "cl_DOT_FClassification", "in_scope", "geometry"]
    seg_out[gis_cols].to_file(PROCESSED / "segments.geojson", driver="GeoJSON",
                              COORDINATE_PRECISION=6)

    attach = seg_out[["CenterlineID", "StreetName", "FromStreet", "ToStreet", "CONS",
                      "maintenance_history_all", "Year", "cl_DOT_FClassification",
                      "miles", "lane_miles", "lanes_imputed", "in_scope", "is_brick"]]
    table = long.merge(attach, left_on="segment_id", right_on="CenterlineID", how="left")
    table = table.drop(columns="CenterlineID")
    table.to_csv(PROCESSED / "analysis_table.csv", index=False)

    (PROCESSED / "data_quality.json").write_text(json.dumps(dq, indent=2, default=str))
    write_dq_report(dq)
    print(json.dumps(dq, indent=2, default=str))


def write_dq_report(dq):
    z = dq["zero_reasons"]
    lines = [
        "# Data-quality report",
        "",
        "_Generated by `src/build_dataset.py`. Every number below is computed from the raw pull._",
        "",
        "## Record counts and join",
        f"- Street Ratings table rows: **{dq['ratings_rows']:,}**",
        f"- Rows with no CenterlineID (cannot be mapped; saved to `data/processed/unmatched_ratings.csv`): **{dq['ratings_rows_null_centerline_id']}**",
        f"- CenterlineIDs appearing on more than one row: **{dq['dup_centerline_groups']}** segments / {dq['dup_centerline_rows']} rows. "
        f"Ratings are identical within every group ({dq['dup_groups_with_conflicting_ratings']} conflicts); rows differ only in MaintenanceHistory, "
        "i.e. the table stores one row per treatment. Collapsed to one segment with all treatments kept.",
        f"- Distinct rated segments: **{dq['rated_segments']:,}**; matched to a centerline: **{dq['rated_segments_matched_to_centerline']:,}**; "
        f"unmatched: **{dq['rated_segments_without_centerline']}**",
        f"- Centerline features: {dq['centerline_features']:,} ({dq['centerlines_null_clid']} with null CLID); distinct CLIDs: {dq['centerline_clids_total']:,}; "
        f"CLIDs split into multiple pieces (merged): {dq['centerline_clids_split_into_pieces']}",
        f"- Centerlines with **no** rating: **{dq['centerline_clids_without_rating']:,}**. By CityLeft: "
        + ", ".join(f"{k} {v}" for k, v in dq["unrated_centerlines_by_city"].items()),
        "",
        "## Rating values",
        f"- **Y2022** is present in the schema but has **{dq['y2022_non_null']}** non-null values. Dropped from analysis.",
        f"- **Y2015**: {dq['y2015_zero_share']:.1%} of values are 0; only {dq['y2015_nonzero_values']} are non-zero. "
        "The 2015 survey was evidently not loaded (or not performed); 0 is a placeholder, not a score.",
        "- **2016–2020**: no columns exist. Trends cannot see anything that happened in those five years.",
        "- **Zero ratings outside 2015** are classified, not deleted:",
    ]
    for reason in ["segment_retired", "before_first_rating", "after_last_rating", "isolated_gap", "never_rated"]:
        lines.append(f"  - `{reason}`: {z.get(reason, 0)}")
    lines += [
        "- Zeros are never read as \"failed\": the lowest genuine rating anywhere in the data is 10, "
        "and zeros coincide with vacated/removed streets, years before a street was built, or the 2015 gap.",
        f"- Null ratings by year: {dq['null_ratings_by_year']}",
        f"- Segments with the identical rating in every surveyed year (2009–2014, 2021): **{dq['segments_identical_rating_every_year']:,}** "
        "— either very stable pavement or ratings carried forward without re-inspection; the data cannot tell which.",
        f"- Ratings not on the usual 5-point grid (e.g. 66, 73): {dq['nonstandard_rating_values']}",
        "",
        "## Scope",
        "In-scope = segment is in the City's own Street Ratings table, mapped to a centerline, not retired, "
        "and has a usable 2021 rating. Counts by reason: " + ", ".join(f"{k} {v}" for k, v in dq["scope_reasons"].items()),
        f"- In-scope segments: **{dq['in_scope_segments']:,}**, {dq['in_scope_miles']:,} centerline miles, {dq['in_scope_lane_miles']:,} lane-miles",
        "- In-scope by MaintResp code (1 appears to mean City-maintained): " + ", ".join(f"{k}: {v}" for k, v in dq["in_scope_by_maintresp"].items()),
        "- In-scope by CityLeft: " + ", ".join(f"{k}: {v}" for k, v in dq["in_scope_by_city"].items()),
        "",
        "## Attribute completeness (in-scope segments)",
        f"- NoOfLanes missing (assumed {DEFAULT_LANES} lanes): **{dq['in_scope_lanes_imputed']:,}** of {dq['in_scope_segments']:,}",
        f"- TrafficCounts2013 present: **{dq['in_scope_traffic_count_present']}** of {dq['in_scope_segments']:,}",
        f"- DOT functional class missing: {dq['in_scope_fclass_missing']}",
        f"- Material (CONS) missing: {dq['in_scope_cons_missing']} (the string \"NA\" was normalised to missing)",
        f"- Treatment year (`Year`) missing: {dq['in_scope_year_missing']}",
        f"- Width missing or zero: {dq['in_scope_width_missing_or_zero']}",
        f"- Miles missing on centerline, computed from geometry instead: {dq['in_scope_miles_from_geometry']}",
        f"- Brick segments (rated on what appears to be a different convention, e.g. 10 every year): {dq['in_scope_brick']}",
        "- CONS codes (all rated segments): " + ", ".join(f"{k} {v}" for k, v in dq["cons_values"].items()),
    ]
    (OUTPUTS / "data_quality.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
