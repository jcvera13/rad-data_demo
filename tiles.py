"""tiles - the anonymised group views behind the landing page. Pure pandas, no Streamlit, so they are easy to test.

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


# 1 ---------------------------------------------------------------- produced vs read
def _weekly(df: pd.DataFrame, daycol: str, lo, hi) -> pd.DataFrame:
    x = df[df[daycol].between(lo, hi)]
    wk = x[daycol].dt.to_period("W-SUN").dt.end_time.dt.normalize()
    return x.assign(Week=wk).groupby(["Week", "Modality"]).agg(WU=("WorkUnits", "sum"), Exams=("WorkUnits", "size"))


def workload_trend(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    """Weekly WU produced (study date) against read (sign date), all modalities and per modality.
    The rows are the exams signed in the range, so 'produced' is understated for the newest weeks."""
    if (b := _enough_readers(df, cfg)):
        return b
    d = df.dropna(subset=["Day", "Modality"])
    lo, hi = d["Day"].min(), d["Day"].max()
    prod, read = _weekly(d, "StudyDay", lo, hi), _weekly(d, "Day", lo, hi)
    frames, hidden = {}, 0

    def both(mod=None):
        p = prod if mod is None else prod[prod.index.get_level_values("Modality") == mod]
        r = read if mod is None else read[read.index.get_level_values("Modality") == mod]
        out = pd.DataFrame({"Produced": p.groupby("Week")["WU"].sum(), "Read": r.groupby("Week")["WU"].sum()}).fillna(0).sort_index()
        return out.round(1)

    frames["All modalities"] = both()
    total = pd.DataFrame({"Produced": prod.groupby("Modality")["WU"].sum(), "Read": read.groupby("Modality")["WU"].sum(),
                          "Exams read": read.groupby("Modality")["Exams"].sum()}).fillna(0)
    for mod in total.sort_values("Read", ascending=False).index:
        if total.loc[mod, "Exams read"] < cfg["min_cell"]:
            hidden += 1
        else:
            frames[mod] = both(mod)
    shown = total[total["Exams read"] >= cfg["min_cell"]].drop(columns="Exams read")
    shown = shown.assign(**{"Read minus produced": shown["Read"] - shown["Produced"]}).sort_values("Read", ascending=False).round(1)
    frames["Totals by modality"] = shown.reset_index()
    note = ("Weeks end on Sunday. Read = work units by sign date; produced = by study date. Only exams signed in this range are included, "
            "so produced is understated in the newest weeks and exams studied before the range appear as read only.")
    if hidden:
        note += f" {hidden} modality(ies) with fewer than {cfg['min_cell']} exams are hidden."
    return Result("trend", frames, note)


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
    """When exams become available for review on PACS (StudyDateTime), by weekday and hour. Needs a StudyDateTime with a time of day;
    rows whose study date came from a fallback column, or has only a day, are left out."""
    if (b := _enough_readers(df, cfg)):
        return b
    o = df[(df["StudySource"] == "StudyDateTime") & df["Study"].notna() & (df["Study"] != df["Study"].dt.normalize())]
    if o.empty:
        return _blocked("StudyDateTime has no time of day in this data, so the hour an exam became available is unknown.")
    h = (o.assign(Weekday=o["Study"].dt.day_name(), Hour=o["Study"].dt.hour)
         .groupby(["Weekday", "Hour"]).size().reset_index(name="Exams"))
    hide = h["Exams"] < cfg["min_cell"]
    note = f"Hour the exam became available for review on PACS (StudyDateTime); {len(df) - len(o):,} of {len(df):,} exams have no study time of day and are not counted. "
    if hide.any():
        note += f"{int(hide.sum())} weekday-hour cell(s) with fewer than {cfg['min_cell']} exams are hidden."
    return Result("heatmap", {"Exams by weekday and hour": h[~hide]}, note)


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


# 7 ---------------------------------------------------------------- turnaround
def _tat_table(d: pd.DataFrame, by, cfg: dict, unit: str) -> pd.DataFrame:
    g = d.groupby(by)["TAT"]
    out = pd.DataFrame({"Exams": g.size(), f"Median ({unit})": g.median().round(1),
                        f"90th percentile ({unit})": g.quantile(0.9).round(1),
                        "% within 1 day": (100 * g.apply(lambda s: (s <= (24 if unit == "hours" else 1)).mean())).round(1)})
    return out[out["Exams"] >= cfg["min_cell"]].reset_index()


def turnaround(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    """Available for review (StudyDateTime) to report finalized (ObservationDateTime), by modality, facility and the hour the exam became available.
    Hours when both have a time of day; otherwise calendar days from the study date to the sign date (ReportDate)."""
    if (b := _enough_readers(df, cfg)):
        return b
    d = df[df["StudySource"] == "StudyDateTime"].dropna(subset=["Study", "Modality"]).copy()
    timed = d["Obs"].notna() & (d["Study"] != d["Study"].dt.normalize())
    has_time = bool(len(d) and timed.mean() > 0.5)
    if has_time:
        d = d[timed]
        d["TAT"] = (d["Obs"] - d["Study"]).dt.total_seconds() / 3600
        unit = "hours"
    else:
        d = d.dropna(subset=["Report"])
        d["TAT"] = (d["Report"].dt.normalize() - d["Study"].dt.normalize()).dt.days
        unit = "days"
    if d.empty:
        return _blocked("No exams with both a study time and a report time in this range.")
    neg = int((d["TAT"] < 0).sum())
    d = d[d["TAT"] >= 0]
    d["Hour"] = d["Study"].dt.hour
    frames = {"By modality": _tat_table(d, "Modality", cfg, unit).sort_values("Exams", ascending=False),
              "By facility": _tat_table(d.dropna(subset=["Facility"]), "Facility", cfg, unit).sort_values("Exams", ascending=False),
              "By hour available": _tat_table(d.dropna(subset=["Study"]), "Hour", cfg, unit)}
    note = f"Turnaround = available for review (StudyDateTime) to report finalized (ObservationDateTime), in {unit}. "
    if not has_time:
        note += "Study or report times of day are missing, so it is counted in calendar days from the study date to the sign date (0 = signed the same day). "
    note += "Includes late signs and corrections, so read the 90th percentile with care. "
    if neg:
        note += f"{neg:,} exam(s) finalized before they were available were left out. "
    note += f"Rows with fewer than {cfg['min_cell']} exams are hidden."
    return Result("table", frames, note)


# 8 ---------------------------------------------------------------- cross-coverage
def _cov(d: pd.DataFrame, by, cfg: dict) -> pd.DataFrame:
    g = d.groupby(by)["Other"]
    out = pd.DataFrame({"Exams": g.size(), "Read by someone else": g.sum().astype(int)})
    out["Percent"] = (100 * out["Read by someone else"] / out["Exams"]).round(1)
    return out[out["Exams"] >= cfg["min_cell"]].reset_index()


def cross_coverage(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    """Share of exams read by someone other than the one Merge PACS assigned them to. Group level, no names."""
    if (b := _enough_readers(df, cfg)):
        return b
    d = df.dropna(subset=["AssignedTo", "Radiologist"]).copy()
    if d.empty:
        return _blocked("No exams in this range have an assignee recorded.")
    d["Other"] = d["AssignedTo"] != d["Radiologist"]
    d["Week"] = d["Day"].dt.to_period("W-SUN").dt.end_time.dt.normalize().dt.strftime("%Y-%m-%d")
    frames = {"Overall": pd.DataFrame({"Exams with an assignee": [len(d)], "Read by someone else": [int(d["Other"].sum())],
                                       "Percent": [round(100 * d["Other"].mean(), 1)]}),
              "By modality": _cov(d, "Modality", cfg).sort_values("Exams", ascending=False),
              "By facility": _cov(d.dropna(subset=["Facility"]), "Facility", cfg).sort_values("Exams", ascending=False),
              "By week (sign date)": _cov(d, "Week", cfg)}
    return Result("table", frames, f"Only exams with an assignee recorded are counted ({len(d):,} of {len(df):,}). "
                  f"Weeks end on Sunday. Rows with fewer than {cfg['min_cell']} exams are hidden. Names are not shown; admins see them in Admin.")


# 9 ---------------------------------------------------------------- lookup check (examcalc)
def lookup_check(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    """Match every exam description to the reference (examcalc rules) and compare the work units recorded on the exam with
    the reference value. Group level, exam descriptions only, no readers."""
    if ref is None:
        return _blocked("No reference table is loaded, so exams cannot be matched.")
    d = df.dropna(subset=["ExamDescription"])
    if d.empty:
        return _blocked("No exams with a description in this range.")
    m = ref.match(pd.Series(d["ExamDescription"].unique())).set_index("exam")
    d = d.assign(Match=d["ExamDescription"].map(m["match"]), RefWU=d["ExamDescription"].map(m["wrvu"]),
                 MatchedTo=d["ExamDescription"].map(m["matched_to"]))
    total = len(d)
    rows = []
    for kind in ("exact", "wildcard", "unmatched"):
        x = d[d["Match"] == kind]
        rows.append({"Match": kind.title(), "Exams": len(x), "% of exams": round(100 * len(x) / total, 1),
                     "Descriptions": x["ExamDescription"].nunique(), "Recorded WU": round(x["WorkUnits"].sum(), 1)})
    summary = pd.DataFrame(rows)
    frames = {"Match summary": summary}
    both = d[d["RefWU"].notna() & d["WorkUnits"].notna() & (d["RefWU"] > 0)].copy()
    tol = float(cfg.get("value_tolerance_pct", 5))
    both["Diff"] = both["WorkUnits"] - both["RefWU"]
    g = both.groupby("ExamDescription").agg(Exams=("Diff", "size"), Recorded=("WorkUnits", "median"), Reference=("RefWU", "first"),
                                            Impact=("Diff", "sum"))
    g["Difference %"] = (100 * (g["Recorded"] - g["Reference"]) / g["Reference"]).round(1)
    off = g[(g["Exams"] >= cfg["min_cell"]) & (g["Difference %"].abs() > tol)]
    off = (off.assign(_k=off["Impact"].abs()).sort_values("_k", ascending=False).drop(columns="_k").head(25).reset_index()
           .rename(columns={"Recorded": "Recorded WU (median)", "Reference": "Reference WU", "Impact": "Total WU difference"}).round(2))
    frames[f"Recorded WU more than {tol:g}% from the reference"] = off
    w = d[d["Match"] == "wildcard"].groupby("MatchedTo").agg(Exams=("Match", "size"), Descriptions=("ExamDescription", "nunique"))
    w = w[w["Exams"] >= cfg["min_cell"]].sort_values("Exams", ascending=False).head(15).reset_index().rename(columns={"MatchedTo": "Wildcard row"})
    if not w.empty:
        frames["Wildcard rows doing the matching"] = w
    dup = len(ref.duplicates())
    note = (f"Exams are matched to the reference with the examcalc rules (exact name first, then a trailing * as a prefix). "
            f"The comparison is the WU recorded on each exam against the reference value, for exams with at least {cfg['min_cell']} reads. "
            "Descriptions only; no readers.")
    if dup:
        note += f" The reference has {dup} row(s) with a repeated exam name (the first one wins)."
    if off.empty:
        note += " No exam differs from the reference by more than the tolerance."
    return Result("table", frames, note)


# 10 --------------------------------------------------------------- exam lookup (interactive; the filtering lives here so it can be tested)
import re

KNOWN_MODS = {"CR", "CT", "CTA", "DEXA", "MG", "MR", "MRA", "MRI", "NM", "PET", "PT", "RF", "US", "XR", "DX", "FL", "BD"}
REGIONS = [("Head/brain", r"brain|head|pituitary|cerebral"), ("Face/sinus", r"orbits?|maxillofacial|facial|sinus|mandible|maxilla|temporal|tmj|temporomandibular|mastoid|nasal|skull|nose"),
           ("Neck", r"neck|thyroid|carotid|parathyroid"), ("Chest", r"chest|lung|ribs?|sternum|sternoclavicular|thoracic outlet|brachial plexus"),
           ("Abdomen", r"abdomen|abd|liver|renal|kidney|bladder|retroperitoneal|hepatobiliary|gastric|small bowel|aorta"), ("Pelvis", r"pelvis|pelvic|scrotum|prostate|rectal|hysterosalpingogram|ob|bony pelvis"),
           ("Spine", r"spine|sacrum|coccyx|scoliosis|sacroiliac|myelogram|lumbar|cervical"), ("Shoulder/clavicle", r"shoulder|clavicle|scapula|acromioclavicular"),
           ("Arm", r"humerus|elbow|forearm"), ("Hand/wrist", r"hand|wrist|fingers?"), ("Hip/femur", r"hip|femur|femoral"),
           ("Knee/leg", r"knee|tibia|fibula|patella|leg|lower extremity"), ("Ankle/foot", r"ankle|foot|heel|toes?|hindfoot"),
           ("Breast", r"breast|mg|mammo"), ("Extremity", r"extremity")]
LOOKUP_LABELS = {"part": "Body part", "ppc": "PPC", "log": "Log"}


def _modality_from_name(name: str) -> str:
    t = re.split(r"[\s/]+", str(name).strip().lower())[0].upper() if str(name).strip() else ""
    return t if t in KNOWN_MODS else "Other"


def _region_from_name(name: str) -> str:
    n = str(name).lower()
    hit = [lab for lab, rx in REGIONS if re.search(rf"\b(?:{rx})\b", n)]
    hit = [h for h in hit if h != "Extremity" or len(hit) == 1]
    if not hit:
        return "Other"
    if len(hit) == 1:
        return hit[0]
    return "Abdomen/pelvis" if set(hit) <= {"Abdomen", "Pelvis"} else "Multiple regions"


def _contrast_from_name(name: str) -> str:
    n = str(name).lower()
    if re.search(r"w/\s?wo\b|\bwo/w\b|w/ and w/o|w and w/o|with/without|without and with|with or without|wwo|w/ wo|wo/w", n):
        return "With and without"
    if re.search(r"w/o\b|\bwo\b|without", n):
        return "Without"
    if re.search(r"w/\s?(?:iv\s)?contrast|w/contrast|w contrast|w/c\b|\bwith\b|\bw/ ", n):
        return "With"
    return "Not stated"


def lookup_table(df: pd.DataFrame, cfg: dict, ref=None) -> Result:
    """The reference table as one searchable table: exam, WU and every other reference column, plus Modality / Body part / Contrast
    worked out from the exam name when the reference has no such column, plus how often each exam was read in the chosen range."""
    if ref is None:
        return _blocked("No reference table is loaded.")
    r = ref.ref
    out = pd.DataFrame({"Exam": r["exam"].astype(str).str.strip(), "WU": r["wrvu"]})
    derived = []
    if "modality" in r:
        out["Modality"] = r["modality"].astype("string").str.upper().fillna("Other")
    else:
        out["Modality"] = r["exam"].map(_modality_from_name)
        derived.append("Modality")
    if "part" in r:
        out["Body part"] = r["part"].astype("string").fillna("Not stated")
    else:
        out["Body part"] = r["exam"].map(_region_from_name)
        derived.append("Body part")
    out["Contrast"] = r["exam"].map(_contrast_from_name)
    derived.append("Contrast")
    for c in r.columns:
        if c in ("exam", "wrvu", "modality", "part"):
            continue
        out[LOOKUP_LABELS.get(c, str(c).title())] = r[c]
    # reads in the chosen range, per reference row (exact or wildcard match), small counts hidden
    d = df.dropna(subset=["ExamDescription"])
    reads = pd.Series(0, index=out.index, dtype="float")
    if not d.empty:
        m = ref.match(pd.Series(d["ExamDescription"].unique())).set_index("exam")
        counts = d["ExamDescription"].map(m["matched_to"]).dropna().map(lambda x: re.sub(r"\s+", " ", str(x)).strip().lower()).value_counts()
        reads = out["Exam"].map(lambda x: counts.get(re.sub(r"\s+", " ", x).strip().lower(), 0)).astype(float)
    out["Reads in range"] = reads.where((reads == 0) | (reads >= cfg["min_cell"]))
    note = (f"{len(out):,} exams in the reference. " + ("; ".join(derived) + " are worked out from the exam name (the reference has no such column), so check them against the name. " if derived else "")
            + f"'Reads in range' counts group reads in the chosen dates; counts under {cfg['min_cell']} are hidden (blank).")
    return Result("lookup", {"Reference": out}, note)


def filter_lookup(t: pd.DataFrame, text: str = "", filters: dict | None = None, wu: tuple | None = None) -> pd.DataFrame:
    """Every word in `text` must appear in the exam name (any order, any case); a word starting with - must not appear.
    `filters` maps a column to the values to keep (empty = no filter); `wu` is (low, high), applied only when given."""
    x = t
    for w in str(text or "").lower().split():
        neg = w.startswith("-") and len(w) > 1
        term = w[1:] if neg else w
        has = x["Exam"].str.lower().str.contains(term, regex=False)
        x = x[~has] if neg else x[has]
    for col, vals in (filters or {}).items():
        if vals:
            x = x[x[col].isin(vals)]
    if wu is not None:
        x = x[x["WU"].between(wu[0], wu[1])]
    return x.sort_values("Exam")


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
    Tile("workload", "Produced vs read", "Is the group reading as much as it produces? Weekly WU by study date vs sign date, by modality, over the last {days} days.", workload_trend),
    Tile("mix", "Modality and facility mix", "Where does the work come from: WU by modality and by facility.", mix),
    Tile("productivity", "Productivity range", "What is the typical WU per hour and minutes per WU, and how wide is the spread?", productivity_range),
    Tile("arrival", "When exams arrive", "Which weekdays and hours do exams become available for review, to inform scheduling?", arrival),
    Tile("late", "Late signs and corrections", "How many exams are reported in a later month than they were studied?", late_signs),
    Tile("gaps", "Dictionary gaps", "Which frequent exams have no WU value or no lookup match, ranked by how many exams they affect?", dictionary_gaps),
    Tile("turnaround", "Turnaround time", "How long from available for review to report finalized, by modality, facility and hour of day?", turnaround),
    Tile("coverage", "Cross-coverage", "How often is an exam read by someone other than the reader it was assigned to?", cross_coverage),
    Tile("lookup", "Lookup check", "Do exams match the reference table, and does the work unit recorded on each exam agree with it?", lookup_check),
    Tile("find", "Find an exam", "Search the work unit lookup by name, or filter by modality, body part, contrast and WU, and see how often each exam is read.", lookup_table),
)
