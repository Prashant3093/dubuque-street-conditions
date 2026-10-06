"""
Six analyses of Dubuque street condition ratings.

  1. Deterioration rate per segment (OLS slope of rating vs. year)
  2. Material comparison (asphalt vs. concrete vs. composite)
  3. Lifecycle curve (rating vs. years since recorded work) and time-to-Poor
  4. Maintenance effectiveness (rating change across recorded work, vs. untreated streets)
  5. Capital prioritization score (transparent, illustrative; NOT a City methodology)
  6. Network KPIs (condition by band, average condition by year, Poor backlog)

Reads   data/processed/segments.parquet, data/processed/ratings_long.parquet
Writes  outputs/analysis_report.md        human-readable report
        outputs/metrics.json              every number quoted in README / FINDINGS
        outputs/tables/*.csv              supporting tables
        outputs/figures/*.png             lifecycle curve and network trend charts
        data/processed/segment_metrics.parquet   per-segment slope + priority, used by maps and Power BI

Usage:  python src/analysis.py
"""

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy import stats
from statsmodels.duration.survfunc import SurvfuncRight
from statsmodels.nonparametric.smoothers_lowess import lowess

from build_dataset import POOR_THRESHOLD, condition_band

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"
TABLES = OUTPUTS / "tables"
FIGURES = OUTPUTS / "figures"

# Years with a real condition survey. 2015 is excluded from trends: 95% of its
# values are 0 placeholders (see outputs/data_quality.md). 2022 is empty.
SURVEY_YEARS = [2009, 2010, 2011, 2012, 2013, 2014, 2021]

# Minimum evidence for a per-segment deterioration slope.
MIN_OBS = 3
MIN_SPAN_YEARS = 5
# A rise of this many points between consecutive ratings with no recorded work
# suggests unrecorded maintenance (or a rating inconsistency), which would
# contaminate a deterioration slope.
UNRECORDED_JUMP = 10

# ---------------------------------------------------------------------------
# Capital prioritization weights (analysis 5).
# Each component is scaled 0-1 so the weights read as shares of the score.
#   condition     0.40  Current need: how far below 100 the 2021 rating is.
#   deterioration 0.20  Momentum: segments losing points fastest will need work soonest.
#   traffic       0.25  Users affected: more vehicles -> more benefit per repair.
#   size          0.15  Network impact: longer/wider segments carry more of the network.
# Weights are a judgment call made for this portfolio project. They are NOT the
# City of Dubuque's methodology, and the sensitivity check below shows how much
# the ranking depends on them.
# ---------------------------------------------------------------------------
PRIORITY_WEIGHTS = {"condition": 0.40, "deterioration": 0.20, "traffic": 0.25, "size": 0.15}

# Chart colors (reference palette from the project's dataviz guide)
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
BLUE = "#2a78d6"
BLUE_LIGHT = "#86b6ef"
ORANGE = "#eb6834"
CRITICAL = "#d03b3b"

METRICS = {}


def metric(key, value):
    """Record a number for metrics.json and return it, so every quoted figure is traceable."""
    if isinstance(value, (np.floating, float)):
        value = None if np.isnan(value) else round(float(value), 4)
    elif isinstance(value, np.integer):
        value = int(value)
    METRICS[key] = value
    return value


def mean_ci(x, conf=0.95):
    x = pd.Series(x).dropna()
    if len(x) < 2:
        return np.nan, np.nan, np.nan
    m = x.mean()
    half = stats.t.ppf((1 + conf) / 2, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))
    return m, m - half, m + half


def material_group(cons):
    """Collapse the City's CONS codes into groups large enough to compare.
    ACC = asphalt cement concrete (asphalt); PCC = Portland cement concrete.
    'ACCoPCC' / 'ACCPCC' / 'PCCoACC' / 'ACCoBRICK' = asphalt over another base (composite)."""
    if pd.isna(cons):
        return "Unknown"
    if cons == "ACC":
        return "Asphalt (ACC)"
    if cons == "PCC":
        return "Concrete (PCC)"
    if cons == "Brick":
        return "Brick"
    return "Composite"


def treatment_group(history):
    """Group free-text MaintenanceHistory into treatment families, most intensive first."""
    if pd.isna(history) or not history:
        return None
    h = history.lower()
    if "reconstruct" in h or h.startswith("new"):
        return "Reconstruction / new"
    if "overlay" in h or "resurface" in h or "final lift" in h:
        return "Overlay / resurface"
    if "seal" in h or "level" in h or "band" in h:  # "Banded" = crack banding
        return "Preventive seal"
    if "repair" in h:
        return "Section repair"
    return "Other"


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ["top", "right"]:
        ax.spines[side].set_visible(False)
    for side in ["left", "bottom"]:
        ax.spines[side].set_color("#c3c2b7")
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def load():
    seg = pd.read_parquet(PROCESSED / "segments.parquet")
    seg = seg.drop(columns="geometry", errors="ignore")
    long = pd.read_parquet(PROCESSED / "ratings_long.parquet")
    seg["material"] = seg["CONS"].apply(material_group)
    seg["treatment"] = seg["maintenance_history_all"].apply(treatment_group)
    return seg, long


# ---------------------------------------------------------------------------
# 1. Deterioration rate
# ---------------------------------------------------------------------------
def analysis_deterioration(seg, long, out):
    """OLS slope of rating on year for each segment, using only ratings taken in or
    after the segment's recorded work year (so a repair does not masquerade as
    'improvement'). Zeros are already missing in `rating`."""
    insc = seg[seg.in_scope & ~seg.is_brick].set_index("CenterlineID")
    obs = long[long.segment_id.isin(insc.index) & long.year.isin(SURVEY_YEARS) & long.rating.notna()].copy()
    obs["record_year"] = obs.segment_id.map(insc["Year"])
    obs = obs[obs.record_year.isna() | (obs.year >= obs.record_year)]

    rows = []
    for sid, g in obs.groupby("segment_id"):
        g = g.sort_values("year")
        n, span = len(g), g.year.max() - g.year.min()
        jump = bool((g.rating.diff() >= UNRECORDED_JUMP).any())
        slope = np.nan
        if n >= MIN_OBS and span >= MIN_SPAN_YEARS:
            fit = sm.OLS(g.rating.values, sm.add_constant(g.year.values)).fit()
            slope = fit.params[1]
        rows.append({"segment_id": sid, "n_obs": n, "span_years": span,
                     "unrecorded_jump": jump, "slope": slope,
                     "first_rating": g.rating.iloc[0], "last_rating": g.rating.iloc[-1]})
    slopes = pd.DataFrame(rows)
    slopes["slope_clean"] = slopes.slope.where(~slopes.unrecorded_jump)

    s_all = slopes.slope.dropna()
    s = slopes.slope_clean.dropna()
    metric("det_segments_in_scope_non_brick", len(insc))
    metric("det_segments_with_slope", len(s_all))
    metric("det_segments_excluded_unrecorded_jump", int(slopes.loc[slopes.slope.notna(), "unrecorded_jump"].sum()))
    metric("det_segments_clean_slope", len(s))
    metric("det_slope_median", s.median())
    metric("det_slope_mean", s.mean())
    metric("det_slope_p10", s.quantile(0.10))
    metric("det_slope_p90", s.quantile(0.90))
    metric("det_share_declining", (s < 0).mean())
    metric("det_share_flat", (s == 0).mean())
    metric("det_share_improving", (s > 0).mean())
    metric("det_slope_median_incl_jumps", s_all.median())

    names = insc[["StreetName", "FromStreet", "ToStreet", "CONS", "Year", "rating_2021", "miles"]]
    worst = (slopes.dropna(subset=["slope_clean"]).sort_values("slope_clean")
             .head(15).join(names, on="segment_id"))
    worst.to_csv(TABLES / "fastest_declining_segments.csv", index=False)
    slopes.to_csv(TABLES / "segment_slopes.csv", index=False)

    out.append("## 1. Deterioration rate per segment\n")
    out.append(
        f"Method: for each in-scope, non-brick segment, an OLS line of rating on year, using survey years "
        f"{SURVEY_YEARS[0]}–2014 and 2021 (2015 excluded), only ratings taken in or after the segment's recorded "
        f"`Year`, requiring ≥{MIN_OBS} ratings spanning ≥{MIN_SPAN_YEARS} years. Segments whose rating rose "
        f"≥{UNRECORDED_JUMP} points between surveys with no recorded work are excluded as contaminated.\n")
    out.append(f"- Segments with a slope: {len(s_all):,}; excluded for unrecorded jumps: "
               f"{METRICS['det_segments_excluded_unrecorded_jump']:,}; used: **{len(s):,}**")
    out.append(f"- Median slope: **{s.median():.2f} points/year** (mean {s.mean():.2f}; "
               f"10th pct {s.quantile(.1):.2f}, 90th pct {s.quantile(.9):.2f})")
    out.append(f"- Declining: {(s < 0).mean():.1%} · flat: {(s == 0).mean():.1%} · improving: {(s > 0).mean():.1%}")
    out.append(f"- Median slope if contaminated segments are kept: {s_all.median():.2f}\n")
    out.append("Fastest-declining segments:\n")
    out.append("| Street | From | To | Material | Slope (pts/yr) | 2021 rating | Ratings used |")
    out.append("|---|---|---|---|---|---|---|")
    for _, r in worst.iterrows():
        out.append(f"| {r.StreetName} | {r.FromStreet} | {r.ToStreet} | {r.CONS} | {r.slope_clean:.2f} | "
                   f"{r.rating_2021:.0f} | {r.n_obs} |")
    out.append("")
    return slopes


# ---------------------------------------------------------------------------
# 2. Material comparison
# ---------------------------------------------------------------------------
def analysis_material(seg, slopes, out):
    d = slopes.dropna(subset=["slope_clean"]).merge(
        seg[["CenterlineID", "material", "cl_DOT_FClassification", "rating_2021", "Year"]],
        left_on="segment_id", right_on="CenterlineID")
    d["fclass"] = d.cl_DOT_FClassification.fillna("Unknown")
    d["age_2021"] = 2021 - d.Year

    out.append("## 2. Material comparison\n")
    out.append("| Material | Segments | Mean slope (pts/yr) | 95% CI | Median | Mean 2021 rating |")
    out.append("|---|---|---|---|---|---|")
    summary = []
    for mat, g in d.groupby("material"):
        m, lo, hi = mean_ci(g.slope_clean)
        summary.append({"material": mat, "n": len(g), "mean_slope": m, "ci_low": lo, "ci_high": hi,
                        "median_slope": g.slope_clean.median(), "mean_rating_2021": g.rating_2021.mean()})
        out.append(f"| {mat} | {len(g):,} | {m:.2f} | [{lo:.2f}, {hi:.2f}] | {g.slope_clean.median():.2f} | "
                   f"{g.rating_2021.mean():.1f} |")
        key = mat.split()[0].lower()
        metric(f"mat_{key}_n", len(g))
        metric(f"mat_{key}_mean_slope", m)
        metric(f"mat_{key}_ci_low", lo)
        metric(f"mat_{key}_ci_high", hi)
        metric(f"mat_{key}_mean_rating_2021", g.rating_2021.mean())
    pd.DataFrame(summary).to_csv(TABLES / "material_comparison.csv", index=False)

    acc = d.loc[d.material == "Asphalt (ACC)", "slope_clean"]
    pcc = d.loc[d.material == "Concrete (PCC)", "slope_clean"]
    t = stats.ttest_ind(pcc, acc, equal_var=False)
    u = stats.mannwhitneyu(pcc, acc, alternative="two-sided")
    diff = pcc.mean() - acc.mean()
    metric("mat_pcc_minus_acc", diff)
    metric("mat_welch_t_p", t.pvalue)
    metric("mat_mannwhitney_p", u.pvalue)

    # Adjusted model: does the material gap survive controlling for road class and age?
    sub = d[d.material.isin(["Asphalt (ACC)", "Concrete (PCC)", "Composite"])].dropna(subset=["age_2021"])
    model = smf.ols("slope_clean ~ C(material, Treatment('Asphalt (ACC)')) + C(fclass) + age_2021",
                    data=sub).fit(cov_type="HC3")
    pcc_term = [k for k in model.params.index if "Concrete" in k][0]
    metric("mat_adj_pcc_coef", model.params[pcc_term])
    metric("mat_adj_pcc_ci_low", model.conf_int().loc[pcc_term, 0])
    metric("mat_adj_pcc_ci_high", model.conf_int().loc[pcc_term, 1])
    metric("mat_adj_pcc_p", model.pvalues[pcc_term])
    metric("mat_adj_n", int(model.nobs))

    out.append("")
    out.append(f"- Concrete minus asphalt, mean slope: **{diff:+.2f} points/year** "
               f"(Welch t-test p = {t.pvalue:.3g}; Mann-Whitney p = {u.pvalue:.3g}).")
    out.append(f"- Adjusted for functional class and years since recorded work (OLS, HC3 robust SEs, n = {int(model.nobs):,}): "
               f"concrete coefficient {model.params[pcc_term]:+.2f} "
               f"[{model.conf_int().loc[pcc_term, 0]:.2f}, {model.conf_int().loc[pcc_term, 1]:.2f}], "
               f"p = {model.pvalues[pcc_term]:.3g}.")
    # How different are the two populations? (quoted in the caveat)
    pop = seg[seg.in_scope & seg.material.isin(["Asphalt (ACC)", "Concrete (PCC)"])]
    nonlocal_share = (pop.assign(nonlocal_mi=pop.miles.where(pop.cl_DOT_FClassification.notna()
                                                             & (pop.cl_DOT_FClassification != "Local"), 0))
                      .groupby("material")[["nonlocal_mi", "miles"]].sum())
    nonlocal_share = nonlocal_share.nonlocal_mi / nonlocal_share.miles
    work_year = pop.groupby("material").Year.median()
    metric("mat_acc_nonlocal_share", nonlocal_share["Asphalt (ACC)"])
    metric("mat_pcc_nonlocal_share", nonlocal_share["Concrete (PCC)"])
    metric("mat_acc_median_work_year", work_year["Asphalt (ACC)"])
    metric("mat_pcc_median_work_year", work_year["Concrete (PCC)"])
    out.append(f"- **Caveat:** material is not randomly assigned. Concrete carries more collector/arterial mileage "
               f"({nonlocal_share['Concrete (PCC)']:.0%} of its miles vs {nonlocal_share['Asphalt (ACC)']:.0%} for asphalt) "
               f"and its last recorded work is older (median {work_year['Concrete (PCC)']:.0f} vs "
               f"{work_year['Asphalt (ACC)']:.0f}). Traffic counts exist for too few segments to control for load. "
               "This is an association, not a material effect.\n")
    out.append("<details><summary>Adjusted model output</summary>\n\n```\n" + model.summary().as_text() + "\n```\n</details>\n")


# ---------------------------------------------------------------------------
# 3. Lifecycle curve
# ---------------------------------------------------------------------------
def analysis_lifecycle(seg, long, out):
    insc = seg[seg.in_scope & ~seg.is_brick].set_index("CenterlineID")
    obs = long[long.segment_id.isin(insc.index) & long.year.isin(SURVEY_YEARS)
               & long.rating.notna() & long.age.notna() & (long.age >= 0)].copy()
    obs["material"] = obs.segment_id.map(insc["material"])

    # The pooled curve mixes asphalt (mostly young, resurfaced often) with concrete
    # (older, slower to deteriorate), so its tail rises with age. Fit each material
    # separately as well, and only trust ages with >= 30 observations.
    def curve(o):
        b = o.groupby("age").rating.agg(["count", "mean", "median"]).reset_index()
        b = b[b["count"] >= 30]
        sm_ = pd.DataFrame(lowess(o.rating, o.age, frac=0.3, return_sorted=True),
                           columns=["age", "fit"]).drop_duplicates("age")
        sm_ = sm_[sm_.age <= b.age.max()].copy()
        sm_["rate"] = np.gradient(sm_.fit, sm_.age)
        return b.merge(sm_, on="age", how="left"), sm_

    curves = {"All": curve(obs)}
    for mat in ["Asphalt (ACC)", "Concrete (PCC)"]:
        curves[mat] = curve(obs[obs.material == mat])
    pd.concat([c[0].assign(material=k) for k, c in curves.items()]).to_csv(
        TABLES / "lifecycle_curve.csv", index=False)

    metric("life_obs", len(obs))
    for k, (b, sm_) in curves.items():
        key = "all" if k == "All" else k.split()[0].lower()
        fastest_k = sm_.loc[sm_.rate.idxmin()]
        metric(f"life_{key}_max_trusted_age", b.age.max())
        metric(f"life_{key}_fastest_decline_age", fastest_k.age)
        metric(f"life_{key}_fastest_decline_rate", fastest_k.rate)
        for a in [0, 5, 10, 15, 20]:
            near = sm_.iloc[(sm_.age - a).abs().argmin()]
            metric(f"life_{key}_fit_at_age_{a}", near.fit if abs(near.age - a) <= 1 else np.nan)
    smooth = curves["All"][1]
    fastest = curves["Asphalt (ACC)"][1].loc[curves["Asphalt (ACC)"][1].rate.idxmin()]

    # Time to Poor: Kaplan-Meier on "first observed rating <= POOR_THRESHOLD".
    # Segments enter only if their first observed rating after the work year is above
    # the threshold; they are censored at their last observed age otherwise.
    events = []
    for sid, g in obs.sort_values("age").groupby("segment_id"):
        if g.rating.iloc[0] <= POOR_THRESHOLD:
            continue
        hit = g[g.rating <= POOR_THRESHOLD]
        if len(hit):
            events.append((sid, hit.age.iloc[0], 1))
        else:
            events.append((sid, g.age.iloc[-1], 0))
    ev = pd.DataFrame(events, columns=["segment_id", "age", "event"])
    ev["material"] = ev.segment_id.map(insc["material"])
    km = SurvfuncRight(ev.age, ev.event)
    median_age = km.quantile(0.5)
    metric("life_km_segments", len(ev))
    metric("life_km_events", int(ev.event.sum()))
    metric("life_km_median_age_to_poor", median_age)
    for a in [10, 15, 20, 25]:
        idx = np.searchsorted(km.surv_times, a, side="right") - 1
        metric(f"life_km_share_above_poor_at_{a}", km.surv_prob[idx] if idx >= 0 else 1.0)
    km_by_mat = {}
    for mat in ["Asphalt (ACC)", "Concrete (PCC)"]:
        e = ev[ev.material == mat]
        kmm = SurvfuncRight(e.age, e.event)
        km_by_mat[mat] = kmm
        metric(f"life_km_median_{mat.split()[0].lower()}", kmm.quantile(0.5))
        metric(f"life_km_events_{mat.split()[0].lower()}", int(e.event.sum()))

    # Figure: one line per material, direct-labeled
    colors = {"Asphalt (ACC)": BLUE, "Concrete (PCC)": ORANGE}
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)
    ax.axhspan(0, POOR_THRESHOLD, color="#f0efec", zorder=0)
    ax.text(0.3, POOR_THRESHOLD - 1.5, f"Poor or worse (≤{POOR_THRESHOLD})", color=INK_2, fontsize=8, va="top")
    for mat, color in colors.items():
        b, sm_ = curves[mat]
        ax.scatter(b.age, b["mean"], s=np.clip(b["count"] / 10, 8, 60), color=color, alpha=0.35,
                   edgecolor="none", zorder=2)
        ax.plot(sm_.age, sm_.fit, color=color, linewidth=2, zorder=3)
        ax.text(sm_.age.iloc[-1] + 0.4, sm_.fit.iloc[-1], mat, color=INK, fontsize=8, va="center")
    ax.annotate(f"Asphalt loses points fastest\naround age {fastest.age:.0f} ({fastest.rate:.1f} pts/yr)",
                xy=(fastest.age, fastest.fit), xytext=(fastest.age + 3, 58),
                fontsize=8, color=INK_2, arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8))
    ax.set_xlabel("Years since recorded work (`Year` field)")
    ax.set_ylabel("Condition rating (0–100)")
    ax.set_ylim(40, 100)
    ax.set_xlim(-0.5, max(curves[m][1].age.max() for m in colors) + 7)
    ax.set_title("Pavement lifecycle: rating vs. years since recorded work", loc="left", fontsize=11, color=INK)
    fig.text(0.01, 0.005, "Lines: LOWESS trend. Dots: mean rating at each age (size = observations; ages with ≥30 obs). "
             "Source: City of Dubuque GIS Office, Street Ratings.", fontsize=6.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(FIGURES / "lifecycle_curve.png", facecolor=SURFACE)
    plt.close(fig)

    out.append("## 3. Lifecycle curve\n")
    out.append("Age here is years since the segment's `Year` field. Section 4 shows ratings jump in that year, "
               "so `Year` behaves like *most recent recorded work*, not original construction.\n")
    out.append(f"- Observations used: {len(obs):,}\n")
    out.append("| Smoothed rating at age | 0 | 5 | 10 | 15 | 20 | Steepest decline |")
    out.append("|---|---|---|---|---|---|---|")
    for k in ["all", "asphalt", "concrete"]:
        cells = " | ".join("—" if METRICS[f"life_{k}_fit_at_age_{a}"] is None
                           else f"{METRICS[f'life_{k}_fit_at_age_{a}']:.0f}" for a in [0, 5, 10, 15, 20])
        out.append(f"| {k.title()} | {cells} | age {METRICS[f'life_{k}_fastest_decline_age']:.0f} "
                   f"({METRICS[f'life_{k}_fastest_decline_rate']:.2f} pts/yr) |")
    out.append("")
    out.append("- The pooled curve flattens and even rises after ~age 10. That is not pavement healing: older segments "
               "are disproportionately concrete, and the ones still unrepaired at age 20+ are the survivors that never "
               "needed work. Read the material-specific curves, and only the first ~10–15 years with confidence.")
    if median_age is None or np.isnan(median_age):
        out.append(f"- Median age to Poor (Kaplan-Meier, {len(ev):,} segments, {int(ev.event.sum())} reached Poor): "
                   "**not reached** — fewer than half of segments are observed falling to Poor within the ages the data covers.")
    else:
        out.append(f"- Median age to Poor (Kaplan-Meier, {len(ev):,} segments, {int(ev.event.sum())} reached Poor): **{median_age:.0f} years**")
    out.append("- Share of segments still above Poor at age: " + ", ".join(
        f"{a} yrs {METRICS[f'life_km_share_above_poor_at_{a}']:.0%}" for a in [10, 15, 20, 25]))
    for mat in km_by_mat:
        k = mat.split()[0].lower()
        mv = METRICS[f"life_km_median_{k}"]
        out.append(f"  - {mat}: median {('not reached' if mv is None else f'{mv:.0f} years')} "
                   f"({METRICS[f'life_km_events_{k}']} reached Poor)")
    out.append("- Caveats: the 2015–2020 gap means the first observed Poor rating is an upper bound on when it happened. "
               "Ages beyond ~20 come mostly from segments that have *not* yet been repaired, i.e. survivors.\n")


# ---------------------------------------------------------------------------
# 4. Maintenance effectiveness
# ---------------------------------------------------------------------------
def analysis_maintenance(seg, long, out):
    """For segments whose recorded work year falls inside the survey window, compare
    the last rating before the work year with the first rating after it. Compare that
    change to untreated segments (recorded work before 2009) over the same two years,
    i.e. a simple difference-in-differences."""
    insc = seg[seg.in_scope & ~seg.is_brick].set_index("CenterlineID")
    obs = long[long.segment_id.isin(insc.index) & long.year.isin(SURVEY_YEARS) & long.rating.notna()]
    wide = obs.pivot(index="segment_id", columns="year", values="rating")

    control = wide[wide.index.isin(insc.index[insc.Year < 2009])]

    rows = []
    for sid, work_year in insc.Year.dropna().items():
        if not (2010 <= work_year <= 2020) or sid not in wide.index:
            continue
        r = wide.loc[sid].dropna()
        before, after = r[r.index < work_year], r[r.index > work_year]
        if before.empty or after.empty:
            continue
        y0, y1 = before.index.max(), after.index.min()
        change = after.iloc[0] - before.iloc[-1]
        ctrl = (control[y1] - control[y0]).dropna()
        rows.append({"segment_id": sid, "work_year": int(work_year), "pre_year": y0, "post_year": y1,
                     "pre": before.iloc[-1], "post": after.iloc[0], "change": change,
                     "control_change": ctrl.mean(), "did": change - ctrl.mean(),
                     "treatment": insc.loc[sid, "treatment"]})
    m = pd.DataFrame(rows)
    m.to_csv(TABLES / "maintenance_effect_segments.csv", index=False)

    mean_c, lo_c, hi_c = mean_ci(m.change)
    mean_d, lo_d, hi_d = mean_ci(m.did)
    paired = stats.ttest_rel(m.post, m.pre)
    did_t = stats.ttest_1samp(m.did, 0)
    metric("mnt_segments", len(m))
    metric("mnt_control_segments", len(control))
    metric("mnt_mean_change", mean_c)
    metric("mnt_change_ci_low", lo_c)
    metric("mnt_change_ci_high", hi_c)
    metric("mnt_paired_t_p", paired.pvalue)
    metric("mnt_mean_did", mean_d)
    metric("mnt_did_ci_low", lo_d)
    metric("mnt_did_ci_high", hi_d)
    metric("mnt_did_p", did_t.pvalue)
    metric("mnt_share_improved", (m.change > 0).mean())
    metric("mnt_mean_pre", m.pre.mean())
    metric("mnt_mean_post", m.post.mean())

    out.append("## 4. Maintenance effectiveness\n")
    out.append("Segments whose recorded work year (`Year`) is 2010–2020 and which have a rating before and after "
               "that year. Control = segments whose recorded work predates 2009, over the same pair of survey years.\n")
    out.append(f"- Treated segments: **{len(m):,}**; control pool: {len(control):,}")
    out.append(f"- Mean rating before → after: {m.pre.mean():.1f} → {m.post.mean():.1f}; "
               f"change **{mean_c:+.1f} points** [95% CI {lo_c:.1f}, {hi_c:.1f}], paired t-test p = {paired.pvalue:.3g}")
    out.append(f"- Net of what untreated streets did over the same years (difference-in-differences): "
               f"**{mean_d:+.1f} points** [95% CI {lo_d:.1f}, {hi_d:.1f}], p = {did_t.pvalue:.3g}")
    out.append(f"- Share of treated segments whose rating rose: {(m.change > 0).mean():.0%}\n")
    out.append("| Treatment family | Segments | Mean change | 95% CI | Net of control |")
    out.append("|---|---|---|---|---|")
    by = []
    for tr, g in m.groupby("treatment"):
        mc, lo, hi = mean_ci(g.change)
        md, _, _ = mean_ci(g.did)
        by.append({"treatment": tr, "n": len(g), "mean_change": mc, "ci_low": lo, "ci_high": hi, "mean_did": md})
        if len(g) < 2:
            continue  # kept in the CSV, too few to summarise
        out.append(f"| {tr} | {len(g):,} | {mc:+.1f} | [{lo:.1f}, {hi:.1f}] | {md:+.1f} |")
        key = tr.split()[0].lower().replace("/", "")
        metric(f"mnt_{key}_n", len(g))
        metric(f"mnt_{key}_mean_change", mc)
        metric(f"mnt_{key}_mean_did", md)
    pd.DataFrame(by).to_csv(TABLES / "maintenance_effect_by_treatment.csv", index=False)
    out.append("")
    out.append("- A segment can appear with a single treatment family even when its blocks are counted separately "
               "(e.g. 12 blocks of one street), so family-level n overstates independent evidence.")
    out.append("- Caveats: `Year` is one year per segment, so for segments with several recorded treatments we cannot "
               "tell which treatment happened in that year; the treatment family is the most intensive one listed. "
               "Work years 2016–2020 are compared 2014 → 2021, a long window. No cost data, so this measures "
               "points gained, not points per dollar.\n")
    return m


# ---------------------------------------------------------------------------
# 5. Capital prioritization
# ---------------------------------------------------------------------------
def pct_rank(s):
    return s.rank(pct=True, method="average")


def analysis_priority(seg, slopes, out):
    d = seg[seg.in_scope & ~seg.is_brick].copy()
    d = d.merge(slopes[["segment_id", "slope_clean"]], left_on="CenterlineID", right_on="segment_id", how="left")

    # Traffic: 2013 counts exist for very few segments. Where missing, use the median
    # observed count for the segment's DOT functional class (from all centerlines that
    # have a count), and flag it. This is a proxy, and the README says so.
    # Segments with no functional class are treated as Local streets for this purpose
    # (most unclassified city streets are local), which is the conservative choice.
    d["fclass"] = d.cl_DOT_FClassification.fillna("Local")
    counts = d.dropna(subset=["cl_TrafficCounts2013"])
    class_median = counts.groupby("fclass").cl_TrafficCounts2013.median()
    class_n = counts.groupby("fclass").size()
    usable = class_median[class_n >= 5]
    overall = counts.cl_TrafficCounts2013.median()
    d["traffic_imputed"] = d.cl_TrafficCounts2013.isna()
    d["traffic_est"] = d.cl_TrafficCounts2013.fillna(d.fclass.map(usable)).fillna(overall)
    metric("pri_traffic_class_medians", {k: float(v) for k, v in usable.items()})
    metric("pri_traffic_counts_used", len(counts))
    metric("pri_traffic_imputed_share", d.traffic_imputed.mean())

    d["deterioration_imputed"] = d.slope_clean.isna()
    d["c_condition"] = (100 - d.rating_2021) / 100
    # Faster decline (more negative slope) -> higher component. Missing slope -> 0.5 (neutral).
    d["c_deterioration"] = pct_rank(-d.slope_clean).fillna(0.5)
    d["c_traffic"] = pct_rank(d.traffic_est)
    d["c_size"] = pct_rank(d.lane_miles)

    def score(weights):
        return 100 * sum(w * d[f"c_{k}"] for k, w in weights.items())

    d["priority_score"] = score(PRIORITY_WEIGHTS)
    d["priority_rank"] = d.priority_score.rank(ascending=False, method="first").astype(int)
    d = d.sort_values("priority_rank")

    # Sensitivity: how stable is the top 50 under other reasonable weightings?
    top = set(d.head(50).CenterlineID)
    alt = {
        "equal": {"condition": .25, "deterioration": .25, "traffic": .25, "size": .25},
        "condition_heavy": {"condition": .60, "deterioration": .15, "traffic": .15, "size": .10},
        "no_traffic": {"condition": .55, "deterioration": .25, "traffic": 0, "size": .20},
    }
    for name, w in alt.items():
        alt_top = set(d.assign(s=score(w)).nlargest(50, "s").CenterlineID)
        metric(f"pri_top50_overlap_{name}", len(top & alt_top))

    cols = ["priority_rank", "priority_score", "CenterlineID", "StreetName", "FromStreet", "ToStreet",
            "CONS", "cl_DOT_FClassification", "rating_2021", "band_2021", "slope_clean",
            "cl_TrafficCounts2013", "traffic_est", "traffic_imputed", "lane_miles", "lanes_imputed",
            "c_condition", "c_deterioration", "c_traffic", "c_size"]
    d[cols].head(50).to_csv(TABLES / "priority_top50.csv", index=False)
    d[cols].to_csv(TABLES / "priority_all.csv", index=False)

    top50 = d.head(50)
    metric("pri_top50_lane_miles", top50.lane_miles.sum())
    metric("pri_top50_mean_rating_2021", top50.rating_2021.mean())
    metric("pri_top50_with_real_traffic_count", int((~top50.traffic_imputed).sum()))
    metric("pri_weights", PRIORITY_WEIGHTS)

    out.append("## 5. Capital prioritization (illustrative score)\n")
    out.append("**Not a City methodology.** A transparent, re-weightable score for discussion. Components (each 0–1):")
    out.append("condition = (100 − 2021 rating)/100; deterioration = percentile of decline rate (neutral 0.5 if unknown); "
               "traffic = percentile of 2013 count, or class-median proxy; size = percentile of lane-miles.\n")
    out.append("Weights: " + ", ".join(f"{k} {v:.0%}" for k, v in PRIORITY_WEIGHTS.items()))
    out.append(f"- Traffic counts available for {len(counts)} in-scope segments; "
               f"{d.traffic_imputed.mean():.0%} of segments use a class-based proxy "
               "(median 2013 count of counted segments in the same DOT functional class; unclassified = Local).")
    out.append("- Several top entries are consecutive blocks of the same street; a real program would group them into projects.")
    out.append(f"- Top-50 overlap under alternative weights — equal: {METRICS['pri_top50_overlap_equal']}/50, "
               f"condition-heavy: {METRICS['pri_top50_overlap_condition_heavy']}/50, "
               f"no traffic: {METRICS['pri_top50_overlap_no_traffic']}/50")
    out.append("- The ranking is sensitive to the weights, especially traffic: treat it as a conversation starter, "
               "not a list. The condition and deterioration inputs are solid; the traffic input is mostly a proxy.")
    out.append(f"- Top 50 total {top50.lane_miles.sum():.1f} lane-miles, mean 2021 rating {top50.rating_2021.mean():.1f}\n")
    out.append("| Rank | Street | From | To | Class | 2021 | Slope | Traffic | Lane-mi | Score |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in top50.iterrows():
        traffic = f"{r.traffic_est:,.0f}{'*' if r.traffic_imputed else ''}"
        slope = "—" if pd.isna(r.slope_clean) else f"{r.slope_clean:.2f}"
        out.append(f"| {r.priority_rank} | {r.StreetName} | {r.FromStreet} | {r.ToStreet} | "
                   f"{r.cl_DOT_FClassification if pd.notna(r.cl_DOT_FClassification) else '—'} | {r.rating_2021:.0f} | {slope} | {traffic} | "
                   f"{r.lane_miles:.2f} | {r.priority_score:.1f} |")
    out.append("\n\\* traffic is a functional-class proxy, not a count.\n")
    return d


# ---------------------------------------------------------------------------
# 6. Network KPIs
# ---------------------------------------------------------------------------
def analysis_kpis(seg, long, out):
    insc = seg[seg.in_scope].set_index("CenterlineID")

    # 2021 condition distribution
    band_order = ["Good", "Fair", "Poor", "Failed"]
    dist = (insc.groupby("band_2021").agg(segments=("miles", "size"), miles=("miles", "sum"),
                                          lane_miles=("lane_miles", "sum"))
            .reindex(band_order).fillna(0))
    dist["share_miles"] = dist.miles / dist.miles.sum()
    dist["share_lane_miles"] = dist.lane_miles / dist.lane_miles.sum()
    dist.to_csv(TABLES / "kpi_condition_bands_2021.csv")
    for b in band_order:
        metric(f"kpi_2021_share_miles_{b.lower()}", dist.loc[b, "share_miles"])
        metric(f"kpi_2021_lane_miles_{b.lower()}", dist.loc[b, "lane_miles"])
    metric("kpi_2021_share_miles_poor_or_worse", dist.loc[["Poor", "Failed"], "share_miles"].sum())
    metric("kpi_2021_miles_poor_or_worse", dist.loc[["Poor", "Failed"], "miles"].sum())
    metric("kpi_2021_network_miles", insc.miles.sum())
    metric("kpi_2021_network_lane_miles", insc.lane_miles.sum())

    # Condition by year (mile-weighted), all in-scope segments rated that year
    obs = long[long.segment_id.isin(insc.index) & long.year.isin(SURVEY_YEARS + [2015]) & long.rating.notna()].copy()
    obs["miles"] = obs.segment_id.map(insc.miles)
    obs["lane_miles"] = obs.segment_id.map(insc.lane_miles)

    def by_year(o):
        g = o.groupby("year")
        res = pd.DataFrame({
            "segments_rated": g.size(),
            "avg_rating_mile_weighted": g.apply(lambda x: np.average(x.rating, weights=x.miles)),
            "avg_rating_simple": g.rating.mean(),
            "backlog_lane_miles_poor": g.apply(lambda x: x.loc[x.rating <= POOR_THRESHOLD, "lane_miles"].sum()),
            "share_miles_poor": g.apply(lambda x: x.loc[x.rating <= POOR_THRESHOLD, "miles"].sum() / x.miles.sum()),
        })
        return res

    kpi = by_year(obs)
    # Change vs. the previous *survey* year (2015 skipped), so 2021 is compared with 2014.
    kpi["yoy_change"] = kpi.loc[SURVEY_YEARS, "avg_rating_mile_weighted"].diff()
    # Fixed panel: segments rated in every survey year, so year-to-year changes are not
    # caused by streets entering or leaving the inventory.
    survey_obs = obs[obs.year.isin(SURVEY_YEARS)]
    full = survey_obs.groupby("segment_id").year.nunique()
    panel_ids = full[full == len(SURVEY_YEARS)].index
    panel = by_year(survey_obs[survey_obs.segment_id.isin(panel_ids)])
    kpi["panel_avg_rating_mile_weighted"] = panel.avg_rating_mile_weighted
    kpi["panel_backlog_lane_miles_poor"] = panel.backlog_lane_miles_poor
    kpi["note"] = np.where(kpi.index == 2015, "only partial 2015 data; excluded from trend", "")
    kpi.to_csv(TABLES / "kpi_by_year.csv")

    metric("kpi_panel_segments", len(panel_ids))
    for y in SURVEY_YEARS:
        metric(f"kpi_avg_rating_{y}", kpi.loc[y, "avg_rating_mile_weighted"])
        metric(f"kpi_backlog_lane_miles_{y}", kpi.loc[y, "backlog_lane_miles_poor"])
        metric(f"kpi_panel_avg_rating_{y}", kpi.loc[y, "panel_avg_rating_mile_weighted"])
    metric("kpi_avg_rating_change_2009_2021", kpi.loc[2021, "avg_rating_mile_weighted"] - kpi.loc[2009, "avg_rating_mile_weighted"])
    metric("kpi_panel_change_2009_2021", panel.loc[2021, "avg_rating_mile_weighted"] - panel.loc[2009, "avg_rating_mile_weighted"])
    metric("kpi_panel_change_2014_2021", panel.loc[2021, "avg_rating_mile_weighted"] - panel.loc[2014, "avg_rating_mile_weighted"])

    # Threshold sensitivity for the 2021 backlog
    r21 = insc.rating_2021
    sens = {t: float(insc.loc[r21 <= t, "lane_miles"].sum()) for t in [50, 55, 60]}
    for t, v in sens.items():
        metric(f"kpi_2021_backlog_lane_miles_le_{t}", v)
    metric("kpi_2021_lanes_imputed_share_of_backlog",
           insc.loc[r21 <= POOR_THRESHOLD, "lanes_imputed"].mean())

    # Figure: network average condition by year
    fig, ax = plt.subplots(figsize=(8, 4), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)
    trend = kpi.loc[SURVEY_YEARS]
    early, late = trend.loc[:2014], trend.loc[[2014, 2021]]
    series = [("avg_rating_mile_weighted", BLUE, "All in-scope segments rated that year"),
              ("panel_avg_rating_mile_weighted", MUTED, f"Fixed panel ({len(panel_ids):,} segments rated every year)")]
    for col, color, label in series:
        ax.plot(early.index, early[col], color=color, linewidth=2, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=label)
        # Dotted across 2015-2020: there is no data there, only the two endpoints.
        ax.plot(late.index, late[col], color=color, linewidth=1.5, linestyle=":")
        ax.plot([2021], [trend.loc[2021, col]], color=color, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1.5)
    # Honest scale: a 1-point move should look like 1 point, not a collapse.
    ax.set_ylim(55, 85)
    ax.axvspan(2014.5, 2020.5, color="#f0efec", zorder=0)
    ax.text(2017.5, 57, "No usable ratings\n2015–2020", ha="center", fontsize=8, color=INK_2)
    last = trend.iloc[-1]
    ax.annotate(f"{last.avg_rating_mile_weighted:.1f}", (2021, last.avg_rating_mile_weighted),
                xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.set_ylabel("Mile-weighted average rating")
    ax.set_xticks(SURVEY_YEARS)
    ax.set_title("Network average condition by survey year", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left", labelcolor=INK_2)
    fig.text(0.01, 0.005, "Source: City of Dubuque GIS Office, Street Ratings (public ArcGIS service).",
             fontsize=6.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(FIGURES / "network_condition_by_year.png", facecolor=SURFACE)
    plt.close(fig)

    out.append("## 6. Network KPIs\n")
    out.append(f"Network in scope: {insc.miles.sum():.1f} centerline miles, {insc.lane_miles.sum():.1f} lane-miles "
               f"({insc.lanes_imputed.mean():.0%} of segments use an assumed 2 lanes).\n")
    out.append("**2021 condition distribution**\n")
    out.append("| Band | Rating | Segments | Miles | Share of miles | Lane-miles |")
    out.append("|---|---|---|---|---|---|")
    ranges = {"Good": "71–100", "Fair": "56–70", "Poor": "26–55", "Failed": "1–25"}
    for b in band_order:
        r = dist.loc[b]
        out.append(f"| {b} | {ranges[b]} | {int(r.segments):,} | {r.miles:.1f} | {r.share_miles:.1%} | {r.lane_miles:.1f} |")
    out.append("")
    out.append("**Condition by year** (mile-weighted; 2015 shown for completeness only)\n")
    out.append("| Year | Segments rated | Avg rating | YoY | Panel avg | % miles Poor+ | Backlog lane-mi (≤55) |")
    out.append("|---|---|---|---|---|---|---|")
    for y, r in kpi.iterrows():
        yoy = "" if pd.isna(r.yoy_change) else f"{r.yoy_change:+.1f}"
        pa = "" if pd.isna(r.panel_avg_rating_mile_weighted) else f"{r.panel_avg_rating_mile_weighted:.1f}"
        flag = " †" if y == 2015 else ""
        out.append(f"| {y}{flag} | {int(r.segments_rated):,} | {r.avg_rating_mile_weighted:.1f} | {yoy} | {pa} | "
                   f"{r.share_miles_poor:.1%} | {r.backlog_lane_miles_poor:.1f} |")
    out.append("\n† 2015: only partial data, excluded from trend. YoY is change vs. the previous survey year, so the 2021 figure spans seven years (2014→2021).\n")
    out.append("**Backlog sensitivity (2021 lane-miles at or below threshold):** " +
               ", ".join(f"≤{t}: {v:.1f}" for t, v in sens.items()))
    out.append(f"\n{METRICS['kpi_2021_lanes_imputed_share_of_backlog']:.0%} of backlog segments use the assumed lane count, "
               "so lane-mile totals are approximate.\n")


# ---------------------------------------------------------------------------
def main():
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    seg, long = load()
    dq = json.loads((PROCESSED / "data_quality.json").read_text())

    out = ["# Analysis report", "",
           "_Generated by `src/analysis.py`. Independent analysis of public City of Dubuque GIS data; "
           "not affiliated with or endorsed by the City._", "",
           f"In-scope segments: {dq['in_scope_segments']:,}. Condition bands: Good ≥71, Fair 56–70, Poor 26–55, "
           f"Failed 1–25 (collapsed from ASTM D6433 PCI categories; the City's own scale is not documented on the service). "
           f"\"Poor threshold\" = rating ≤ {POOR_THRESHOLD}.", ""]

    slopes = analysis_deterioration(seg, long, out)
    analysis_material(seg, slopes, out)
    analysis_lifecycle(seg, long, out)
    analysis_maintenance(seg, long, out)
    prio = analysis_priority(seg, slopes, out)
    analysis_kpis(seg, long, out)

    # Per-segment metrics for maps and Power BI
    seg_metrics = (seg[["CenterlineID"]]
                   .merge(slopes[["segment_id", "slope_clean", "n_obs", "unrecorded_jump"]],
                          left_on="CenterlineID", right_on="segment_id", how="left")
                   .merge(prio[["CenterlineID", "priority_score", "priority_rank", "traffic_est", "traffic_imputed"]],
                          on="CenterlineID", how="left")
                   .drop(columns="segment_id")
                   .rename(columns={"slope_clean": "deterioration_rate"}))
    seg_metrics.to_parquet(PROCESSED / "segment_metrics.parquet", index=False)

    (OUTPUTS / "analysis_report.md").write_text("\n".join(out) + "\n")
    (OUTPUTS / "metrics.json").write_text(json.dumps(METRICS, indent=2, default=str))
    print("\n".join(out))


if __name__ == "__main__":
    main()
