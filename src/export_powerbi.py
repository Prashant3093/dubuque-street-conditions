"""
Export a small star schema for Power BI, plus a wide attribute CSV for joining in QGIS.

Writes
  outputs/powerbi/fact_ratings.csv    segment_id, year, rating (+ band and data-quality flags)
  outputs/powerbi/dim_segment.csv     one row per in-scope segment, with map lat/long
  outputs/powerbi/dim_year.csv        rating years with survey / trend flags
  outputs/powerbi/dim_band.csv        condition bands with sort order and cutoffs
  outputs/powerbi/fact_priority.csv   illustrative priority score and its components
  data/processed/segment_attributes.csv   wide table (one row per segment) for the QGIS join

Relationships (all single-direction, one-to-many):
  dim_segment[segment_id] 1 -> * fact_ratings[segment_id]
  dim_segment[segment_id] 1 -> 1 fact_priority[segment_id]
  dim_year[year]          1 -> * fact_ratings[year]
  dim_band[band]          1 -> * fact_ratings[condition_band]

Usage:  python src/export_powerbi.py
"""

from pathlib import Path

import pandas as pd

from analysis import PRIORITY_WEIGHTS, material_group, treatment_group
from build_dataset import BAND_EDGES, POOR_THRESHOLD

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
PBI = ROOT / "outputs" / "powerbi"
TABLES = ROOT / "outputs" / "tables"


def main():
    PBI.mkdir(parents=True, exist_ok=True)
    seg = pd.read_parquet(PROCESSED / "segments.parquet").drop(columns="geometry")
    metrics = pd.read_parquet(PROCESSED / "segment_metrics.parquet")
    long = pd.read_parquet(PROCESSED / "ratings_long.parquet")
    seg = seg.merge(metrics, on="CenterlineID", how="left")
    seg = seg[seg.in_scope].copy()

    # --- dim_segment --------------------------------------------------------
    dim_segment = pd.DataFrame({
        "segment_id": seg.CenterlineID,
        "street_name": seg.StreetName,
        "from_street": seg.FromStreet,
        "to_street": seg.ToStreet,
        "segment_label": seg.StreetName + " (" + seg.FromStreet.fillna("?") + " to " + seg.ToStreet.fillna("?") + ")",
        "route": seg.Route,
        "material_code": seg.CONS,
        "material_group": seg.CONS.apply(material_group),
        "functional_class": seg.cl_DOT_FClassification.fillna("Unclassified"),
        "last_work_year": seg.Year.astype("Int64"),
        "maintenance_history": seg.maintenance_history_all,
        "treatment_family": seg.maintenance_history_all.apply(treatment_group),
        "traffic_count_2013": seg.cl_TrafficCounts2013.astype("Int64"),
        "traffic_estimate": seg.traffic_est.round(0).astype("Int64"),
        "traffic_is_proxy": seg.traffic_imputed,
        "miles": seg.miles.round(4),
        "lanes": seg.lanes.astype(int),
        "lanes_assumed": seg.lanes_imputed,
        "lane_miles": seg.lane_miles.round(4),
        "rating_2021": seg.rating_2021.astype("Int64"),
        "band_2021": seg.band_2021,
        "deterioration_rate": seg.deterioration_rate.round(3),
        "is_brick": seg.is_brick,
        "latitude": seg.mid_lat,
        "longitude": seg.mid_lon,
    })
    dim_segment.to_csv(PBI / "dim_segment.csv", index=False)

    # --- fact_ratings -------------------------------------------------------
    # Zeros are already blank in `rating` (see data_quality.md), so DAX AVERAGE ignores them.
    f = long[long.segment_id.isin(seg.CenterlineID) & (long.year != 2022)]
    fact_ratings = pd.DataFrame({
        "segment_id": f.segment_id,
        "year": f.year,
        "rating": f.rating.astype("Int64"),
        "condition_band": f.condition_band,
        "rating_raw": f.rating_raw.astype("Int64"),
        "zero_reason": f.zero_reason,
    })
    fact_ratings.to_csv(PBI / "fact_ratings.csv", index=False)

    # --- dim_year -----------------------------------------------------------
    years = sorted(fact_ratings.year.unique())
    survey = [y for y in years if y != 2015]
    dim_year = pd.DataFrame({"year": years})
    dim_year["is_trend_year"] = dim_year.year.isin(survey)
    dim_year["previous_survey_year"] = dim_year.year.map(
        {y: (survey[i - 1] if i > 0 else None) for i, y in enumerate(survey)}).astype("Int64")
    dim_year["note"] = dim_year.year.map({2015: "95% placeholder zeros; excluded from trends",
                                          2021: "previous survey is 2014 (7-year gap)"}).fillna("")
    dim_year.to_csv(PBI / "dim_year.csv", index=False)

    # --- dim_band -----------------------------------------------------------
    uppers = [100] + [lo - 1 for lo, _ in BAND_EDGES[:-1]]
    dim_band = pd.DataFrame({
        "band": [name for _, name in BAND_EDGES],
        "sort_order": range(1, len(BAND_EDGES) + 1),
        "min_rating": [lo for lo, _ in BAND_EDGES],
        "max_rating": uppers,
        "is_poor_or_worse": [lo <= POOR_THRESHOLD for lo, _ in BAND_EDGES],
    })
    dim_band.to_csv(PBI / "dim_band.csv", index=False)

    # --- fact_priority ------------------------------------------------------
    p = pd.read_csv(TABLES / "priority_all.csv")
    fact_priority = pd.DataFrame({
        "segment_id": p.CenterlineID,
        "priority_rank": p.priority_rank,
        "priority_score": p.priority_score.round(2),
        "c_condition": p.c_condition.round(4),
        "c_deterioration": p.c_deterioration.round(4),
        "c_traffic": p.c_traffic.round(4),
        "c_size": p.c_size.round(4),
        "weights": "; ".join(f"{k}={v}" for k, v in PRIORITY_WEIGHTS.items()),
    })
    fact_priority.to_csv(PBI / "fact_priority.csv", index=False)

    # --- Wide CSV for the QGIS join ----------------------------------------
    wide = (long[long.year.isin([2009, 2010, 2011, 2012, 2013, 2014, 2021])]
            .pivot(index="segment_id", columns="year", values="rating"))
    wide.columns = [f"r{y}" for y in wide.columns]
    attrs = (dim_segment[["segment_id", "street_name", "material_group", "functional_class",
                          "rating_2021", "band_2021", "deterioration_rate"]]
             .merge(wide, left_on="segment_id", right_index=True, how="left")
             .merge(fact_priority[["segment_id", "priority_rank"]], on="segment_id", how="left")
             .rename(columns={"segment_id": "CenterlineID"}))
    attrs.to_csv(PROCESSED / "segment_attributes.csv", index=False)

    for name in ["fact_ratings", "dim_segment", "dim_year", "dim_band", "fact_priority"]:
        n = sum(1 for _ in open(PBI / f"{name}.csv")) - 1
        print(f"  {name}.csv: {n:,} rows")
    print(f"  segment_attributes.csv: {len(attrs):,} rows")


if __name__ == "__main__":
    main()
