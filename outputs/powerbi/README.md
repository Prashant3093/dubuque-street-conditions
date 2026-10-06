# Power BI build spec

Power BI Desktop is Windows-only, so this folder contains the data model and an exact build spec rather than a `.pbix` file. The same KPIs are already live in the HTML fallback dashboard (`outputs/dashboard.html`), so you can check your Power BI numbers against it.

All files are produced by `python src/export_powerbi.py`. Validate your measures against `outputs/tables/kpi_by_year.csv` and `outputs/tables/kpi_condition_bands_2021.csv`. The Power BI numbers should match those to one decimal place.

## 1. Load the data

**Get data → Text/CSV** for each file. In Power Query, set these types:

| Table | Column | Type |
|---|---|---|
| fact_ratings | segment_id, year, rating, rating_raw | Whole number |
| fact_ratings | condition_band, zero_reason | Text |
| dim_segment | segment_id, last_work_year, rating_2021, lanes, traffic_* | Whole number |
| dim_segment | miles, lane_miles, deterioration_rate, latitude, longitude | Decimal number |
| dim_segment | traffic_is_proxy, lanes_assumed, is_brick | True/False |
| dim_year | year, previous_survey_year | Whole number |
| dim_band | min_rating, max_rating, sort_order | Whole number |
| fact_priority | priority_rank | Whole number; others Decimal |

In **Model view**, set `dim_segment[latitude]` to Data category **Latitude** and `dim_segment[longitude]` to **Longitude**. On `dim_band[band]`, choose **Sort by column → sort_order**.

## 2. Relationships

All are one-to-many with single-direction filtering:

```
dim_segment[segment_id]  1 ──► *  fact_ratings[segment_id]
dim_segment[segment_id]  1 ──► 1  fact_priority[segment_id]
dim_year[year]           1 ──► *  fact_ratings[year]
dim_band[band]           1 ──► *  fact_ratings[condition_band]
```

`fact_ratings[rating]` is **blank** wherever the source held a 0 placeholder (see `outputs/data_quality.md`), so `AVERAGE` and `SUMX` ignore those rows automatically. Never use `rating_raw` in a measure. It exists only for audit.

## 3. What-if parameters

**Modeling → New parameter → Numeric range:**

| Name | Min | Max | Increment | Default |
|---|---|---|---|---|
| Poor Threshold | 40 | 70 | 5 | 55 |
| W Condition | 0 | 1 | 0.05 | 0.40 |
| W Deterioration | 0 | 1 | 0.05 | 0.20 |
| W Traffic | 0 | 1 | 0.05 | 0.25 |
| W Size | 0 | 1 | 0.05 | 0.15 |

Power BI generates `[Poor Threshold Value]`, `[W Condition Value]` and so on.

## 4. DAX measures

Create a measures table first: **Enter data**, call it `_Measures`, then delete its column. Paste these:

```DAX
Rated Miles =
SUMX (
    FILTER ( fact_ratings, NOT ISBLANK ( fact_ratings[rating] ) ),
    RELATED ( dim_segment[miles] )
)
```

```DAX
Network Avg Condition =
-- Mile-weighted: a 1-mile arterial counts 10x a 0.1-mile cul-de-sac.
VAR RatedRows = FILTER ( fact_ratings, NOT ISBLANK ( fact_ratings[rating] ) )
RETURN
    DIVIDE (
        SUMX ( RatedRows, fact_ratings[rating] * RELATED ( dim_segment[miles] ) ),
        SUMX ( RatedRows, RELATED ( dim_segment[miles] ) )
    )
```

```DAX
Simple Avg Rating =
AVERAGE ( fact_ratings[rating] )
```

```DAX
Miles Poor or Worse =
VAR Threshold = [Poor Threshold Value]
RETURN
    SUMX (
        FILTER (
            fact_ratings,
            NOT ISBLANK ( fact_ratings[rating] ) && fact_ratings[rating] <= Threshold
        ),
        RELATED ( dim_segment[miles] )
    )
```

```DAX
% Miles Poor or Worse =
DIVIDE ( [Miles Poor or Worse], [Rated Miles] )
```

```DAX
Backlog Lane-Miles =
-- Lane-miles at or below the Poor threshold. 94% of segments use an assumed 2 lanes,
-- so treat this as approximate (see dim_segment[lanes_assumed]).
VAR Threshold = [Poor Threshold Value]
RETURN
    SUMX (
        FILTER (
            fact_ratings,
            NOT ISBLANK ( fact_ratings[rating] ) && fact_ratings[rating] <= Threshold
        ),
        RELATED ( dim_segment[lane_miles] )
    )
```

```DAX
Change vs Previous Survey =
-- "YoY" in this dataset means vs. the previous *survey* year: 2015 is excluded, and
-- 2021's previous survey is 2014. Returns blank when no single year is selected.
VAR PrevYear = SELECTEDVALUE ( dim_year[previous_survey_year] )
VAR Current = [Network Avg Condition]
VAR Previous =
    CALCULATE ( [Network Avg Condition], REMOVEFILTERS ( dim_year ), dim_year[year] = PrevYear )
RETURN
    IF ( NOT ISBLANK ( PrevYear ), Current - Previous )
```

```DAX
Latest Condition (2021) =
CALCULATE ( [Network Avg Condition], REMOVEFILTERS ( dim_year ), dim_year[year] = 2021 )
```

```DAX
Change 2014 to 2021 =
[Latest Condition (2021)]
    - CALCULATE ( [Network Avg Condition], REMOVEFILTERS ( dim_year ), dim_year[year] = 2014 )
```

```DAX
Backlog Lane-Miles (2021) =
CALCULATE ( [Backlog Lane-Miles], REMOVEFILTERS ( dim_year ), dim_year[year] = 2021 )
```

```DAX
% Miles Poor or Worse (2021) =
CALCULATE ( [% Miles Poor or Worse], REMOVEFILTERS ( dim_year ), dim_year[year] = 2021 )
```

```DAX
Segments Rated =
CALCULATE ( DISTINCTCOUNT ( fact_ratings[segment_id] ), NOT ISBLANK ( fact_ratings[rating] ) )
```

```DAX
Priority Score (dynamic) =
-- Re-weights the illustrative score live from the What-if sliders. Weights are
-- normalised so they always sum to 1. Not a City of Dubuque methodology.
VAR wC = [W Condition Value]
VAR wD = [W Deterioration Value]
VAR wT = [W Traffic Value]
VAR wS = [W Size Value]
VAR Total = wC + wD + wT + wS
RETURN
    IF (
        HASONEVALUE ( fact_priority[segment_id] ) && Total > 0,
        100 * DIVIDE (
            wC * MAX ( fact_priority[c_condition] )
          + wD * MAX ( fact_priority[c_deterioration] )
          + wT * MAX ( fact_priority[c_traffic] )
          + wS * MAX ( fact_priority[c_size] ),
            Total
        )
    )
```

```DAX
Priority Rank (dynamic) =
IF (
    HASONEVALUE ( fact_priority[segment_id] ),
    RANKX ( ALLSELECTED ( fact_priority[segment_id] ), [Priority Score (dynamic)], , DESC, DENSE )
)
```

```DAX
Share Lanes Assumed =
DIVIDE (
    CALCULATE ( SUM ( dim_segment[miles] ), dim_segment[lanes_assumed] = TRUE () ),
    SUM ( dim_segment[miles] )
)
```

```DAX
Share Traffic Proxy =
DIVIDE (
    CALCULATE ( COUNTROWS ( dim_segment ), dim_segment[traffic_is_proxy] = TRUE () ),
    COUNTROWS ( dim_segment )
)
```

Formats: the condition measures use 1 decimal; the `%` measures are Percentage with 1 decimal; lane-miles use 1 decimal.

## 5. Report pages

Use a 16:9 canvas. Each page has a slicer row along the top (60 px tall) with these slicers: **material_group**, **functional_class**, **treatment_family** (all from `dim_segment`), and on page 1 also **dim_year[year]**. Use the dropdown style and enable *Select all*.

### Page 1: Network overview

```
┌──────────────────────────── slicers ────────────────────────────┐
│ [Avg cond. 2021] [% miles Poor+ 2021] [Backlog lane-mi 2021] [Δ 2014→2021] │
├───────────────────────────────┬─────────────────────────────────┤
│ Line: Network Avg Condition    │ Map: segments by band_2021      │
│ by dim_year[year]              │                                 │
├───────────────────────────────┤                                 │
│ 100% stacked bar: Rated Miles  │                                 │
│ by year, legend = dim_band     │                                 │
└───────────────────────────────┴─────────────────────────────────┘
```

- **4 cards:** `Latest Condition (2021)`, `% Miles Poor or Worse (2021)`, `Backlog Lane-Miles (2021)` and `Change 2014 to 2021`.
- **Line chart:** X = `dim_year[year]` (set the axis type to *Categorical* so the 2015–2020 gap isn't drawn as continuous time). Y = `Network Avg Condition`. Add a visual-level filter `dim_year[is_trend_year] = True`. Start the Y axis at 50, not at the data minimum. A ~1-point change shouldn't look like a collapse.
- **100% stacked bar:** Y = `dim_year[year]`, X = `Rated Miles`, Legend = `dim_band[band]`. Filter it to trend years. Colors: Good `#256abf`, Fair `#86b6ef`, Poor `#e87471`, Failed `#8a1f1f`.
- **Map** (Azure Maps, or the classic Map visual): Latitude = `dim_segment[latitude]`, Longitude = `dim_segment[longitude]`, Legend = `dim_segment[band_2021]`, Size = `dim_segment[lane_miles]`. Put `segment_label`, `rating_2021`, `deterioration_rate` and `material_code` in the tooltips. The points are the midpoint of each segment; Power BI can't draw the line geometry without a custom visual, which the HTML map covers.

### Page 2: Deterioration & lifecycle

- **Column chart:** X = `dim_segment[deterioration_rate]` (create bins of 0.25), Y = count of `segment_id`.
- **Clustered bar:** average `deterioration_rate` by `material_group`, with a caption: *"Association, not a material effect. Concrete is concentrated on arterials and newer reconstructions."*
- **Line chart:** X = `fact_ratings[year] - dim_segment[last_work_year]`. Add this calculated column to `fact_ratings`: `Age = fact_ratings[year] - RELATED ( dim_segment[last_work_year] )`. Y = `Simple Avg Rating`, Legend = `material_group`, with a visual filter of Age ≥ 0 and Age ≤ 25.
- **Table:** the 15 fastest-declining segments (`segment_label`, `material_code`, `deterioration_rate`, `rating_2021`), sorted ascending by `deterioration_rate`.

### Page 3: Capital priority (illustrative)

- Put the **four weight sliders** from the What-if parameters down the left side.
- **Table:** `priority_rank`, `Priority Rank (dynamic)`, `segment_label`, `functional_class`, `rating_2021`, `deterioration_rate`, `traffic_estimate`, `traffic_is_proxy`, `lane_miles`, `Priority Score (dynamic)`. Add a Top N filter of 50 by `Priority Score (dynamic)`.
- Add a text box: *"Illustrative scoring for discussion. Not a City of Dubuque methodology. 96% of traffic values are a functional-class proxy."*
- **Map:** the same as page 1, filtered to the Top 50.

### Page 4: Data quality

- **Cards:** `Share Lanes Assumed` and `Share Traffic Proxy`.
- **Table:** `fact_ratings[year]` × `fact_ratings[zero_reason]` with count of rows. Filter out blank `zero_reason`.
- **Card:** count of `fact_ratings[rating]` where `year = 2015`, to show how little 2015 data exists.
- Add a text box linking to `outputs/data_quality.md`.

## 6. Publish

If you have a Power BI Pro or Premium Per User license, publish to a workspace. **Publish to web** creates a public link, and your tenant admin may disable it. Without a license, export the report to PDF and add it to the repo alongside this README.
