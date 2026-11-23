#!/usr/bin/env python3
"""checks - assertions for the access rules and the six tiles, on the fabricated demo database.

Run:  python make_demo_db.py   (once)   then   python checks.py
Exit code 0 = everything passed. Streamlit warnings about 'missing ScriptRunContext' are expected outside the app.
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import pandas as pd

logging.disable(logging.WARNING)
import access  # noqa: E402
import examcalc as ec  # noqa: E402
import localdb as ldb  # noqa: E402
import tiles  # noqa: E402

cfg = access.load_config()
bad: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not ok:
        bad.append(name)


member = access.User("demo_rad01", "member", "demo_rad01")
admin = access.User("demo_rad02", "admin", "demo_rad02")
office = access.User("demo_office", "admin", None)

# ---- access rules
own = access.own_rows(member, cfg)
check("member sees only their own rows", len(own) > 0 and set(own["Radiologist"]) == {"demo_rad01"}, f"{len(own)} rows")
check("admin without a reader ID has no personal rows", access.own_rows(office, cfg).empty)
try:
    access.all_rows(member, cfg)
    check("member cannot load the whole table", False)
except PermissionError:
    check("member cannot load the whole table", True)
everyone = access.all_rows(admin, cfg)
check("admin sees every reader with real IDs", everyone["Radiologist"].nunique() == 13 and everyone["Radiologist"].str.startswith("demo_rad").all())
grp = access.group_rows(cfg)
check("group view replaces reader IDs", grp["Radiologist"].str.fullmatch(r"R\d+").all() and not grp["Radiologist"].str.contains("demo").any())
for label, df in (("own", own), ("admin", everyone), ("group", grp)):
    leaked = df.astype(str).apply(lambda c: c.str.contains("DEMO0")).any().any()
    check(f"no raw accession number in the {label} frame", not leaked)
check("accession hash is stable and 12 hex characters",
      everyone["AccessionNumber"].dropna().str.fullmatch(r"[0-9a-f]{12}").all()
      and access.all_rows(admin, cfg)["AccessionNumber"].equals(everyone["AccessionNumber"]))

# ---- date handling carried over from examaudit
src = everyone["StudySource"].value_counts()
check("null StudyDateTime rows use the fallback date", src.get("ObservationDateTime", 0) > 0, str(dict(src)))

# ---- tiles on the full range
d = ldb.with_basis(grp, "StudyDateTime")
ref = ec.Reference(ec.read_reference(cfg["reference"], ".csv"))
res = {t.id: t.compute(d, cfg, ref) for t in tiles.TILES}
check("all six tiles produce a view on the demo data", all(r.kind != "blocked" for r in res.values()), str({k: v.kind for k, v in res.items()}))
w = res["workload"].frames["Exams per week"]
check("workload trend has about 26 weekly points", 24 <= len(w) <= 28, f"{len(w)}")
check("workload exams add up to the database", int(w["Exams"].sum()) == int(d["Day"].notna().sum()))
sp = res["productivity"].frames["Spread"]
check("productivity range leaves out the reader with too few exams", int(sp["Readers"].iloc[0]) == 12, f"{sp['Readers'].tolist()}")
check("productivity range has no min/max columns", not any(c in sp for c in ("Min", "Max", "Minimum", "Maximum")))
check("heatmap cells all meet the minimum cell size", (res["arrival"].frames["Exams by weekday and hour"]["Exams"] >= cfg["min_cell"]).all())
late = res["late"].frames["By study month"]
check("late signs: some but not all exams reported later", (late["ReportedLater"] > 0).all() and (late["Percent later"] < 50).all(),
      f"{late['Percent later'].tolist()}")
gaps = res["gaps"].frames["Exams to fix, most affected first"]
for ex in ("ct neck soft tissue w/ contrast", "mr abdomen w/ and w/o contrast", "us pelvis transvaginal", "nm bone scan whole body"):
    check(f"dictionary gaps lists '{ex}'", ex in set(gaps["ExamDescription"]))
check("dictionary gaps does not list a matched exam without missing WU",
      not gaps[gaps["Problem"] == ""].shape[0])

# ---- anonymity rules
few = d[d["Radiologist"].isin(sorted(d["Radiologist"].unique())[:3])]
check("tiles are blocked below the minimum reader count",
      all(tiles.TILES[i].compute(few, cfg, ref).kind == "blocked" for i in (0, 1, 2, 3, 4)))
one_day = d[d["Day"] == d["Day"].max()]
r1 = tiles.arrival(one_day, cfg)
check("one-day range hides small heatmap cells", r1.kind != "heatmap" or len(r1.frames["Exams by weekday and hour"]) == 0 or
      (r1.frames["Exams by weekday and hour"]["Exams"] >= cfg["min_cell"]).all())
tiny = d.groupby("Modality").head(3)
check("mix merges or hides modalities under the cell size",
      tiles.mix(pd.concat([tiny, d.head(2000)]), cfg).kind in ("bars", "blocked"))
check("position text", tiles.position(1.0, {"Lower quartile": 2, "Upper quartile": 3}) == "below the middle half of readers")

# ---- personal benchmarks (My data)
mine = tiles.last_months(ldb.with_basis(own, "StudyDateTime"))
check("last 3 months window ends at the reader's newest exam and spans about 3 months",
      85 <= (mine["Day"].max() - mine["Day"].min()).days <= 92, f"{mine['Day'].min():%Y-%m-%d}..{mine['Day'].max():%Y-%m-%d}")
top10 = tiles.top_exams(mine)
check("top 10 exams: ten rows, most-read first", len(top10) == 10 and top10["Reads"].is_monotonic_decreasing and top10["Rank"].tolist() == list(range(1, 11)))
check("top 10 counts add up to at most the window", int(top10["Reads"].sum()) <= len(mine))
xr = tiles.is_chest_xr(mine, cfg)
check("chest X-ray rows are found in the demo data", xr.sum() > 0, f"{int(xr.sum())} rows")
for n in (5, 10, 20, 30):
    check(f"top {n}: at most {n} rows, most-read first", len(tiles.top_exams(mine, n)) == min(n, mine["ExamDescription"].nunique())
          and tiles.top_exams(mine, n)["Reads"].is_monotonic_decreasing)
for bottom in (False, True):
    nm = "bottom" if bottom else "top"
    bm = tiles.exam_speed(mine, cfg, 3, bottom=bottom, by_modality=True)
    check(f"{nm} 3 by modality: several modalities, at most 3 each, ranks restart", bm["Modality"].nunique() > 1
          and bm.groupby("Modality").size().max() <= 3 and bm.groupby("Modality")["Rank"].min().eq(1).all(), f"{sorted(bm['Modality'].unique())}")
    check(f"{nm} 3 by modality: sorted within each modality",
          all(p["WU per hour"].is_monotonic_increasing if bottom else p["WU per hour"].is_monotonic_decreasing for _, p in bm.groupby("Modality")))
    one = tiles.exam_speed(mine, cfg, 3, bottom=bottom, by_modality=False)
    check(f"{nm} 3 all together: exactly 3 rows, ranked 1-3", len(one) == 3 and one["Rank"].tolist() == [1, 2, 3])
    check(f"{nm} 3: no chest X-ray and every exam has enough reads",
          not (bm["ExamDescription"].str.contains("chest", case=False) & bm["Modality"].isin(cfg["chest_xr_modalities"])).any()
          and (bm["Reads"] >= cfg["min_reads"]).all())
hi_all = tiles.exam_speed(mine, cfg, 3, by_modality=False)["WU per hour"].iloc[0]
check("the single table's best exam is the best of the per-modality winners", abs(hi_all - tiles.exam_speed(mine, cfg, 3)["WU per hour"].max()) < 1e-9)
check("a reader with too few timed reads gets an empty ranking",
      tiles.exam_speed(access.own_rows(access.User("demo_rad13", "member", "demo_rad13"), cfg).pipe(ldb.with_basis, "StudyDateTime"), cfg).empty)

# ---- audit log
with tempfile.TemporaryDirectory() as td:
    c2 = {**cfg, "audit_log": str(Path(td) / "a.csv")}
    access.audit(c2, admin, "export", "=HYPERLINK(\"x\")")
    txt = Path(c2["audit_log"]).read_text()
    check("audit log is written with a header and guards formula text", txt.startswith("utc_time,login,role,action,detail") and "'=HYPERLINK" in txt)

print(f"\n{len(bad)} failed" if bad else "\nall checks passed")
sys.exit(1 if bad else 0)
