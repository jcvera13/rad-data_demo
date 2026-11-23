"""tiles - the six anonymised group views behind the landing page. Pure pandas, no Streamlit, so they are easy to test.

Every tile takes the group rows (readers already replaced by R01...), already limited to the chosen dates, and returns a Result.
Anonymity rules, from the plan (both numbers are in rad_data.json):
  min_readers  any view that depends on how readers differ needs at least this many readers, else it is blocked
  min_cell     any cell built from fewer exams than this is hidden or merged into 'Other'
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd


@dataclass
class Result:
    kind: str                                   # line | bars | range | heatmap | table | blocked
    frames: dict = field(default_factory=dict)  # name -> DataFrame
    note: str = ""


def _blocked(msg: str) -> Result:
    return Result("blocked", note=msg)


def _enough_readers(df: pd.DataFrame, cfg: dict) -> Result | None:
    n = df["Radiologist"].nunique()
    if n < cfg["min_readers"]:
        return _blocked(f"Only {n} reader(s) in this range; at least {cfg['min_readers']} are needed before a group view is shown.")
    return None


def in_range(df: pd.DataFrame, start, end) -> pd.DataFrame:
    return df[df["Day"].between(pd.Timestamp(start), pd.Timestamp(end))]


# 1 ---------------------------------------------------------------- workload trend
def workload_trend(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    if (b := _enough_readers(df, cfg)):
        return b
    w = (df.dropna(subset=["Day"]).set_index("Day").resample("W-SUN")
         .agg(Exams=("Modality", "size"), WorkUnits=("WorkUnits", "sum")))
    return Result("line", {"Exams per week": w[["Exams"]], "Work units per week": w[["WorkUnits"]]},
                  "Weeks end on Sunday; the first and last week of the range may be partial.")


# 2 ---------------------------------------------------------------- modality and facility mix
def mix(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    if (b := _enough_readers(df, cfg)):
        return b
    frames, hidden = {}, 0
    for key in ("Modality", "Facility"):
        g = df.dropna(subset=[key]).groupby(key).agg(Exams=("WorkUnits", "size"), WorkUnits=("WorkUnits", "sum"))
        small = g["Exams"] < cfg["min_cell"]
        if small.any():
            other = g[small].sum()
            g = g[~small]
            if other["Exams"] >= cfg["min_cell"]:
                g.loc["Other"] = other
            else:
                hidden += int(small.sum())
        frames[f"By {key.lower()}"] = g.sort_values("WorkUnits", ascending=False)
    note = f"{hidden} very small group(s) hidden." if hidden else ""
    return Result("bars", frames, note)


# 3 ---------------------------------------------------------------- productivity range
def productivity_range(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    t = df[df["JobMins"] > 0]
    per = t.groupby("Radiologist").agg(Exams=("JobMins", "size"), WU=("WorkUnits", "sum"), Mins=("JobMins", "sum"))
    per = per[per["Exams"] >= cfg["min_cell"]]
    if len(per) < cfg["min_readers"]:
        return _blocked(f"Only {len(per)} reader(s) have at least {cfg['min_cell']} timed exams in this range; "
                        f"at least {cfg['min_readers']} are needed.")
    per["WU per hour"] = per["WU"] / (per["Mins"] / 60)
    per["Min per WU"] = per["Mins"] / per["WU"].where(per["WU"] > 0)
    rows = [{"Metric": m, "Median": per[m].median(), "Lower quartile": per[m].quantile(.25),
             "Upper quartile": per[m].quantile(.75), "Readers": int(per[m].notna().sum())}
            for m in ("WU per hour", "Min per WU")]
    return Result("range", {"Spread": pd.DataFrame(rows).round(2)},
                  "Median and middle half of readers only; no individual is shown, and no minimum or maximum.")


def position(value: float, row) -> str:
    if pd.isna(value):
        return "not enough timed exams"
    if value < row["Lower quartile"]:
        return "below the middle half of readers"
    if value > row["Upper quartile"]:
        return "above the middle half of readers"
    return "inside the middle half of readers"


# 4 ---------------------------------------------------------------- when exams arrive
def arrival(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    if (b := _enough_readers(df, cfg)):
        return b
    o = df.dropna(subset=["Obs"])
    h = (o.assign(Weekday=o["Obs"].dt.day_name(), Hour=o["Obs"].dt.hour)
         .groupby(["Weekday", "Hour"]).size().reset_index(name="Exams"))
    hide = h["Exams"] < cfg["min_cell"]
    return Result("heatmap", {"Exams by weekday and hour": h[~hide]},
                  f"{int(hide.sum())} weekday-hour cell(s) with fewer than {cfg['min_cell']} exams are hidden." if hide.any() else "")


# 5 ---------------------------------------------------------------- late signs and corrections
def late_signs(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    if (b := _enough_readers(df, cfg)):
        return b
    d = df.dropna(subset=["StudyMonth", "ReportMonth"])
    d = d.assign(Later=d["ReportMonth"] > d["StudyMonth"])
    g = d.groupby("StudyMonth").agg(Exams=("Later", "size"), ReportedLater=("Later", "sum"))
    g = g[g["Exams"] >= cfg["min_cell"]]
    g["Percent later"] = (100 * g["ReportedLater"] / g["Exams"]).round(1)
    return Result("table", {"By study month": g.reset_index()},
                  "'Reported later' means the report month is after the study month. The first and last study month may be partial.")


# 6 ---------------------------------------------------------------- dictionary gaps
def dictionary_gaps(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    d = df.dropna(subset=["ExamDescription"]).assign(_null=lambda x: x["WorkUnits"].isna())
    g = d.groupby("ExamDescription").agg(Exams=("_null", "size"), NullWU=("_null", "sum"))
    if ref is not None:
        g["Lookup"] = ref.match(g.index)["match"].values
    else:
        g["Lookup"] = "no reference loaded"
    flag = (g["NullWU"] > 0) | (g["Lookup"] == "unmatched")
    g["Affected"] = g["Exams"].where(g["Lookup"] == "unmatched", g["NullWU"]).astype(int)   # exams hit by the problem
    out = g[flag & (g["Exams"] >= cfg["min_cell"])].sort_values(["Affected", "Exams"], ascending=False).head(25).reset_index()
    out["Problem"] = out.apply(lambda r: " and ".join(
        x for x in ("no lookup match" if r["Lookup"] == "unmatched" else "",
                    f"WU missing on {int(r['NullWU'])}" if r["NullWU"] else "") if x), axis=1)
    return Result("table", {"Exams to fix, most affected first": out[["ExamDescription", "Exams", "Affected", "Problem"]]},
                  "Exam descriptions only. 'Affected' counts the exams hit by the problem: all of them when there is no lookup match, "
                  "otherwise those with no WU. Add the exam to the lookup or backfill the WU in the source.")


# ---------------------------------------------------------------- personal benchmarks (My data page, one reader's own rows)
def is_chest_xr(df: pd.DataFrame, cfg: dict) -> pd.Series:
    mods = {m.upper() for m in cfg.get("chest_xr_modalities", ["CR", "DX", "XR"])}
    return df["Modality"].str.upper().isin(mods) & df["ExamDescription"].str.contains("chest", case=False, na=False)


def last_months(d: pd.DataFrame, months: int = 3) -> pd.DataFrame:
    """The user's own rows in the last N calendar months, ending at their newest exam."""
    end = d["Day"].max()
    return d[d["Day"] > end - pd.DateOffset(months=months)]


def top_exams(d: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """The exam descriptions read most often."""
    g = d.dropna(subset=["ExamDescription"]).groupby("ExamDescription").agg(
        Reads=("WorkUnits", "size"), WU=("WorkUnits", "sum"), AvgMin=("JobMins", lambda s: s[s > 0].mean()))
    g = g.sort_values(["Reads", "WU"], ascending=False).head(n).round(1).reset_index()
    g.insert(0, "Rank", range(1, len(g) + 1))
    return g.rename(columns={"WU": "Work units", "AvgMin": "Avg read min"})


def exam_speed(d: pd.DataFrame, cfg: dict, k: int = 3, bottom: bool = False, by_modality: bool = True) -> pd.DataFrame:
    """The k exams with the highest (or lowest) own WU per hour.

    by_modality=True  -> k per modality (Rank restarts in each), modalities ordered by how much the reader reads them
    by_modality=False -> k across all exams in one table
    Chest X-rays are left out. An exam needs at least min_reads timed reads to be ranked.
    'Vs my average' compares with the reader's own WU/hr in the same group (that modality, or all included exams).
    """
    t = d[(d["JobMins"] > 0) & ~is_chest_xr(d, cfg)].dropna(subset=["ExamDescription", "Modality"])
    cols = ["Rank", "Modality", "ExamDescription", "Reads", "WU per hour", "Vs my average"]
    if t.empty:
        return pd.DataFrame(columns=cols)
    order = list(t["Modality"].value_counts().index)
    hrs = lambda x: x["WorkUnits"].sum() / (x["JobMins"].sum() / 60)
    base_mod = t.groupby("Modality").apply(hrs, include_groups=False)
    base_all = hrs(t)
    g = t.groupby(["Modality", "ExamDescription"]).agg(Reads=("JobMins", "size"), WU=("WorkUnits", "sum"), Mins=("JobMins", "sum")).reset_index()
    g = g[g["Reads"] >= cfg.get("min_reads", 5)]
    if g.empty:
        return pd.DataFrame(columns=cols)
    g["WU per hour"] = g["WU"] / (g["Mins"] / 60)
    g["_base"] = g["Modality"].map(base_mod) if by_modality else base_all
    g["Vs my average"] = (100 * (g["WU per hour"] / g["_base"] - 1)).round(0)
    g["_m"] = g["Modality"].map({m: i for i, m in enumerate(order)})
    g = g.sort_values(["_m", "WU per hour"] if by_modality else ["WU per hour"], ascending=[True, bottom] if by_modality else [bottom])
    g = g.groupby("Modality", sort=False).head(k) if by_modality else g.head(k)
    g["Rank"] = g.groupby("Modality").cumcount() + 1 if by_modality else range(1, len(g) + 1)
    return g[cols].round(2).reset_index(drop=True)


@dataclass(frozen=True)
class Tile:
    id: str
    title: str
    question: str
    compute: Callable


TILES = (
    Tile("workload", "Group workload trend", "Is volume and WU rising or falling, week by week, over the last {days} days?", workload_trend),
    Tile("mix", "Modality and facility mix", "Where does the work come from: WU by modality and by facility.", mix),
    Tile("productivity", "Productivity range", "What is the typical WU per hour and minutes per WU, and how wide is the spread?", productivity_range),
    Tile("arrival", "When exams arrive", "Which weekdays and hours are busiest, to inform scheduling?", arrival),
    Tile("late", "Late signs and corrections", "How many exams are reported in a later month than they were studied?", late_signs),
    Tile("gaps", "Dictionary gaps", "Which frequent exams have no WU value or no lookup match, ranked by how many exams they affect?", dictionary_gaps),
)
