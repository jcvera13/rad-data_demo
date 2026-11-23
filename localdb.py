"""localdb - read/validate the local exam database (one table, one row per exam) for examaudit_ui.py. No UI code."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

COLUMNS = ("CPT", "Modality", "Facility", "ExamCode", "ExamDescription", "Radiologist", "AccessionNumber",
           "ScheduledDateTime", "ObservationDateTime", "ReportDate", "ReportYearMonth", "JobTimeSecs",
           "JobTimeMins", "StudyDateTime", "WorkRVU", "WorkUnits")
REQUIRED = ("Modality", "Radiologist", "StudyDateTime", "WorkUnits")  # extra columns are fine; missing optional ones are filled empty
NUMERIC = ("JobTimeSecs", "JobTimeMins", "WorkRVU", "WorkUnits")


_TZ = r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:Z|[+-]\d{2}(?::?\d{2})?)$"
_PARTS = r"^\s*(?P<d>[^\sT]+)(?:[ T]+(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:\.(?P<f>\d+))?)?)?\s*$"
_YMD = r"^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?$"   # 2026-09-4, 2026-9-04, 2026/09/04, 2026-09 (month only)
_MDY = r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$"        # 9/29/26, 09/29/2026 (month first, US)


NULL_TOKENS = {"", "null", "none", "nan", "n/a", "na", "nat", "-", "--"}
FALLBACK_COLS = ("ObservationDateTime", "ReportDate")  # used, in this order, when StudyDateTime is null


def is_null(s: pd.Series) -> pd.Series:
    """True for real nulls and for null-like text (NULL, None, NaN, N/A, -, blank)."""
    return s.isna() | s.astype("string").str.strip().str.lower().isin(NULL_TOKENS).fillna(True)


def clean_nulls(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().mask(is_null(s))


def _strip(s: pd.Series) -> pd.Series:
    return clean_nulls(s).str.replace(_TZ, r"\1", regex=True)


_FAST = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%y %H:%M:%S.%f",
         "%m/%d/%y %H:%M:%S", "%m/%d/%y %H:%M", "%m/%d/%y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m")


def parse_dt(s: pd.Series) -> pd.Series:
    """Fast path: parse with the common exact formats (C speed) and send only the leftovers to the flexible parser."""
    t = _strip(s).str.replace("T", " ", regex=False)
    t = t.str.replace(r"\s+", " ", regex=True)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in _FAST:
        todo = out.isna() & t.notna()
        if not todo.any():
            break
        out[todo] = pd.to_datetime(t[todo], format=fmt, errors="coerce")
    rest = out.isna() & t.notna() & (t != "")
    if rest.any():
        out[rest] = _parse_flex(s[rest])
    return out


def _parse_flex(s: pd.Series) -> pd.Series:
    """Vectorised parse of mixed text dates/timestamps, read as stored (wall-clock).
    Dates: 2026-09-4, 2026-09-04, 2026/09/04, 9/29/26, 9/29/2026 (month first); a month-only value (2026-09) becomes the 1st.
    Times (optional): 14:20, 14:20:00, 14:20:00.00, with T or a space before them. A trailing UTC offset (-07:00, -0800, Z) is dropped.
    Anything else becomes NaT (see the Data quality tab)."""
    t = _strip(s)
    p = t.str.extract(_PARTS)
    a, b = p["d"].str.extract(_YMD), p["d"].str.extract(_MDY)
    num = lambda x: pd.to_numeric(x, errors="coerce")
    year = num(a[0]).fillna(num(b[2]))
    year = year.where(~(year < 100), year + 2000)
    month = num(a[1]).fillna(num(b[0]))
    day = num(a[2]).fillna(num(b[1])).mask(a[0].notna() & a[2].isna(), 1)
    ok = year.notna() & month.notna() & day.notna()
    ymd = pd.DataFrame({"year": year.where(ok, 2000), "month": month.where(ok, 1), "day": day.where(ok, 1)}).astype("int64")
    date = pd.to_datetime(ymd, errors="coerce").where(ok)
    secs = (num(p["h"]).fillna(0) * 3600 + num(p["m"]).fillna(0) * 60 + num(p["s"]).fillna(0)
            + num("0." + p["f"].fillna("0")).fillna(0))
    return date + pd.to_timedelta(secs, unit="s")


def month_only(s: pd.Series) -> pd.Series:
    """True where the value is year-month only (e.g. 2026-09); parse_dt treats those as the 1st of the month."""
    d = _strip(s).str.extract(_PARTS)["d"]
    return d.str.match(r"^\d{4}[-/]\d{1,2}$").fillna(False).astype(bool)


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Neutralise spreadsheet formula injection in text cells."""
    df = df.copy()
    for c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].map(lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v)
    return df


def find_table(con: sqlite3.Connection, override: str | None = None) -> str:
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    if override:
        if override not in tabs:
            raise ValueError(f"table '{override}' not found (have: {', '.join(tabs) or 'none'})")
        return override
    if len(tabs) != 1:
        raise ValueError(f"expected exactly one table, found {len(tabs)}: {', '.join(tabs) or 'none'}")
    return tabs[0]


def validate(path: str | Path, table: str | None = None) -> str:
    """Return the table name, or raise ValueError saying what is wrong."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            t = find_table(con, table)
            have = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
        finally:
            con.close()
    except sqlite3.Error as e:
        raise ValueError(f"not a readable SQLite file: {e}") from e
    miss = [c for c in REQUIRED if c not in have]
    if miss:
        raise ValueError("missing required column(s): " + ", ".join(miss))
    return t


def _months(day: pd.Series) -> pd.Series:
    """'YYYY-MM' per day, formatting only the distinct days (far faster than strftime on every row)."""
    u = day.dropna().unique()
    return day.map({d: pd.Timestamp(d).strftime("%Y-%m") for d in u})


def load(path: str, table: str | None = None, fallback: bool = True) -> pd.DataFrame:
    """All rows, typed. Adds Study/Obs/Sched/Report (datetimes), StudyDay, ReportDay, JobMins.
    With fallback=True a null StudyDateTime takes ObservationDateTime, else ReportDate (column StudySource says which)."""
    t = validate(path, table)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(f'SELECT * FROM "{t}"', con)
    finally:
        con.close()
    for c in COLUMNS:
        if c not in df:
            df[c] = None
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for src, dst in (("StudyDateTime", "Study"), ("ObservationDateTime", "Obs"),
                     ("ScheduledDateTime", "Sched"), ("ReportDate", "Report")):
        df[dst] = parse_dt(df[src])
    df["StudyRaw"] = df["Study"]
    df["StudySource"] = pd.Series(pd.NA, index=df.index, dtype="string").mask(df["Study"].notna(), "StudyDateTime")
    if fallback:
        for c in FALLBACK_COLS:
            src = df["Obs"] if c == "ObservationDateTime" else df["Report"]
            use = df["Study"].isna() & src.notna() & is_null(df["StudyDateTime"])
            df["Study"] = df["Study"].mask(use, src)
            df["StudySource"] = df["StudySource"].mask(use, c)
    df["StudyDay"], df["ReportDay"] = df["Study"].dt.normalize(), df["Report"].dt.normalize()
    df["StudyMonth"], df["ReportMonth"] = _months(df["StudyDay"]), _months(df["ReportDay"])
    df["JobMins"] = (df["JobTimeSecs"] / 60).fillna(df["JobTimeMins"])  # secs is exact; JobTimeMins looks rounded (77 s -> 1.0)
    for c in ("Modality", "Radiologist", "Facility", "CPT", "ExamDescription", "ExamCode"):
        df[c] = clean_nulls(df[c])  # NULL / N/A / blank text become real nulls
    df["CPT"] = df["CPT"].str.strip("'\"").str.strip()  # CPT arrives as '70486' (Excel text prefix)
    return df


def with_basis(df: pd.DataFrame, basis: str) -> pd.DataFrame:
    """Add Day and Month from the chosen date basis ('StudyDateTime' or 'ReportDate')."""
    st_ = basis == "StudyDateTime"
    return df.assign(Day=df["StudyDay"] if st_ else df["ReportDay"], Month=df["StudyMonth"] if st_ else df["ReportMonth"])


def anonymize_map(names) -> dict:
    u = sorted({n for n in names if pd.notna(n)})
    w = max(2, len(str(len(u))))
    return {n: f"R{i:0{w}d}" for i, n in enumerate(u, 1)}


def month_flags(df: pd.DataFrame, basis: str) -> pd.DataFrame:
    """Month completeness. The pull is by StudyDateTime, so a month is partial if the study dates do not cover it;
    under a ReportDate basis the first study month is also partial (earlier studies signed in it are missing)."""
    s = df["StudyDay"].dropna()
    if s.empty:
        return pd.DataFrame(columns=["Month", "Complete", "Note"])
    lo, hi = s.min(), s.max()
    d = (df["StudyDay"] if basis == "StudyDateTime" else df["ReportDay"]).dropna()
    rows = []
    for m in sorted(d.dt.strftime("%Y-%m").unique()):
        a = pd.Period(m).start_time
        b = pd.Period(m).end_time.normalize()
        note = ""
        if basis == "StudyDateTime":
            if lo > a: note = f"data starts {lo.date()}"
            elif hi < b: note = f"data ends {hi.date()}"
        else:
            if a <= lo <= b: note = "studies from earlier months signed in this month are not in the database"
            elif hi < b: note = f"data ends {hi.date()}"
        rows.append((m, not note, note))
    return pd.DataFrame(rows, columns=["Month", "Complete", "Note"])


def quality(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Checks; each value is the offending rows (never dropped from the data)."""
    out = {}
    out["Null WorkUnits"] = df[df["WorkUnits"].isna()]
    out["Zero WorkUnits"] = df[df["WorkUnits"] == 0]
    out["Null WorkRVU"] = df[df["WorkRVU"].isna()]
    out["Null StudyDateTime"] = df[is_null(df["StudyDateTime"])]
    out["Null StudyDateTime, no fallback date available"] = df[df["Study"].isna() & is_null(df["StudyDateTime"])]
    out["Unparseable StudyDateTime"] = df[df["StudyRaw"].isna() & ~is_null(df["StudyDateTime"])]
    out["Month-only StudyDateTime (treated as the 1st)"] = df[month_only(df["StudyDateTime"])]
    out["Month-only ReportDate (treated as the 1st)"] = df[month_only(df["ReportDate"])]
    out["Unparseable ReportDate"] = df[df["Report"].isna() & ~is_null(df["ReportDate"])]
    ym = df["ReportMonth"]
    out["ReportYearMonth differs from ReportDate"] = df[ym.notna() & df["ReportYearMonth"].notna()
                                                        & (ym != df["ReportYearMonth"].astype(str).str[:7])]
    out["Blank Radiologist"] = df[df["Radiologist"].isna()]
    out["Blank Modality"] = df[df["Modality"].isna()]
    out["Exact duplicate rows"] = df[df.duplicated(subset=list(COLUMNS), keep=False)]
    key = ["AccessionNumber", "CPT", "Radiologist"]
    k = df.dropna(subset=["AccessionNumber"])
    out["Same accession + CPT + radiologist (may be legitimate)"] = k[k.duplicated(subset=key, keep=False)]
    return out


def coverage(df: pd.DataFrame, basis: str) -> pd.DataFrame:
    d = df["StudyDay"] if basis == "StudyDateTime" else df["ReportDay"]
    c = d.dropna().dt.date.value_counts().sort_index()
    if c.empty:
        return pd.DataFrame(columns=["Day", "Exams", "Weekday", "Flag"])
    idx = pd.date_range(c.index.min(), c.index.max()).date
    c = c.reindex(idx, fill_value=0)
    out = pd.DataFrame({"Day": c.index, "Exams": c.values})
    out["Weekday"] = pd.to_datetime(out["Day"]).dt.dayofweek < 5
    out["Flag"] = (out["Exams"] == 0) & out["Weekday"]
    return out
