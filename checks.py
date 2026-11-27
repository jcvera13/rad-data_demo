#!/usr/bin/env python3
"""checks - assertions for the access rules and the ten tiles, on the fabricated demo database.

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

check("member frame has no AssignedTo column", "AssignedTo" not in own.columns)
check("admin frame has AssignedTo with real IDs", everyone["AssignedTo"].notna().any() and everyone["AssignedTo"].dropna().str.startswith("demo_rad").all())
check("group frame aliases AssignedTo and never shows a real ID", grp["AssignedTo"].dropna().str.fullmatch(r"R\d+").all())
check("some exams were read by someone other than the assignee", (everyone["AssignedTo"].notna() & (everyone["AssignedTo"] != everyone["Radiologist"])).mean() > 0.03)
check("alias for the same reader matches in Radiologist and AssignedTo",
      grp.loc[grp["Radiologist"] == grp["AssignedTo"]].shape[0] == int((everyone["Radiologist"] == everyone["AssignedTo"]).sum()))

# ---- date handling carried over from examaudit
src = everyone["StudySource"].value_counts()
check("null StudyDateTime rows use the fallback date", src.get("ObservationDateTime", 0) > 0, str(dict(src)))

# ---- tiles on the full range
d = ldb.with_basis(grp, "ReportDate")   # the app uses sign date
ref = ec.Reference(ec.read_reference(cfg["reference"], ".csv"))
res = {t.id: t.compute(d, cfg, ref) for t in tiles.TILES}
check("all ten tiles produce a view on the demo data", all(r.kind != "blocked" for r in res.values()), str({k: v.kind for k, v in res.items()}))
wl = res["workload"]
w = wl.frames["All modalities"]
check("produced vs read: kind and series", wl.kind == "trend" and list(w.columns) == ["Produced", "Read"], str(list(wl.frames)))
check("produced vs read has about 26 weekly points", 24 <= len(w) <= 28, f"{len(w)}")
check("read WU adds up to the exams signed in range", abs(w["Read"].sum() - d["WorkUnits"].sum()) < 1.0, f"{w['Read'].sum():.1f} vs {d['WorkUnits'].sum():.1f}")
check("produced never exceeds what was signed overall (every exam is signed once)", w["Produced"].sum() <= d["WorkUnits"].sum() + 1.0)
tot = wl.frames["Totals by modality"]
check("per-modality frames exist and match the totals table", {"CT", "MR", "CR"} <= set(wl.frames) and set(tot["Modality"]) <= set(wl.frames))
check("modality totals add up to the all-modality series", abs(tot["Read"].sum() - w["Read"].sum()) < 1.0)
sp = res["productivity"].frames["Spread"]
check("productivity range leaves out the reader with too few exams", int(sp["Readers"].iloc[0]) == 12, f"{sp['Readers'].tolist()}")
check("productivity range has no min/max columns", not any(c in sp for c in ("Min", "Max", "Minimum", "Maximum")))
check("heatmap cells all meet the minimum cell size", (res["arrival"].frames["Exams by weekday and hour"]["Exams"] >= cfg["min_cell"]).all())
late = res["late"].frames["By study month"]
check("late signs: some but not all exams reported later (the newest month cannot have late signs yet)",
      (late["ReportedLater"].iloc[:-1] > 0).all() and (late["Percent later"] < 50).all(),
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
check("sign-date basis: no demo exam is signed after the pull date", d["Day"].max() <= pd.Timestamp("2026-10-30"))
# turnaround and cross-coverage
ta, cc = res["turnaround"], res["coverage"]
check("turnaround: demo ReportDate has no time, so it is reported in days", "Median (days)" in ta.frames["By modality"].columns)
check("turnaround: no cell below min_cell", all((f["Exams"] >= cfg["min_cell"]).all() for f in ta.frames.values()))
syn = d.head(3000).copy()
syn["Report"] = syn["Obs"] + pd.to_timedelta(2.5, unit="h")
check("turnaround: with a sign time it is reported in hours and the median is right",
      abs(tiles.turnaround(syn, cfg).frames["By modality"].filter(like="Median").iloc[:, 0].median() - 2.5) < 1e-9)
ov = cc.frames["Overall"].iloc[0]
raw = d.dropna(subset=["AssignedTo"])
check("coverage: overall share matches a direct count", abs(ov["Percent"] - round(100 * (raw["AssignedTo"] != raw["Radiologist"]).mean(), 1)) < 1e-9 and 3 < ov["Percent"] < 9, str(ov.to_dict()))
check("coverage and turnaround show no real reader IDs", not any(s.startswith("demo_") for r in (ta, cc) for f in r.frames.values() for col in f.select_dtypes("object") for s in f[col].astype(str)))
check("coverage and turnaround blocked below the minimum reader count", tiles.cross_coverage(few, cfg, ref).kind == "blocked" and tiles.turnaround(few, cfg, ref).kind == "blocked")
# lookup check
lk = res["lookup"]
ms = lk.frames["Match summary"]
check("lookup check: match summary adds up to the exams with a description", int(ms["Exams"].sum()) == int(d["ExamDescription"].notna().sum()))
check("lookup check: unmatched exams equal the demo's four missing exams",
      set(d[d["ExamDescription"].isin(["ct neck soft tissue w/ contrast", "mr abdomen w/ and w/o contrast", "us pelvis transvaginal", "nm bone scan whole body"])]["ExamDescription"]) and
      int(ms.loc[ms["Match"] == "Unmatched", "Descriptions"].iloc[0]) == 4)
offk = next(k for k in lk.frames if k.startswith("Recorded WU"))
check("lookup check: finds the two exams made to differ, and only those", set(lk.frames[offk]["ExamDescription"]) == {"mr knee w/o contrast", "us thyroid"}, str(set(lk.frames[offk]["ExamDescription"])))
check("lookup check: difference sign is right", lk.frames[offk].set_index("ExamDescription")["Difference %"].pipe(lambda s: s["mr knee w/o contrast"] > 15 and s["us thyroid"] < -15))
check("lookup check is blocked with no reference", tiles.lookup_check(d, cfg, None).kind == "blocked")
# find an exam
fl = res["find"].frames["Reference"]
check("find: one row per reference exam with derived columns", len(fl) == len(ref.ref) and {"Exam", "WU", "Modality", "Body part", "Contrast", "Reads in range"} <= set(fl.columns))
check("find: words match in any order, - excludes",
      len(tiles.filter_lookup(fl, "head ct")) == len(tiles.filter_lookup(fl, "ct head")) >= 2
      and len(tiles.filter_lookup(fl, "ct head -and")) == len(tiles.filter_lookup(fl, "ct head")) - 1
      and tiles.filter_lookup(fl, "CT HEAD")["Exam"].tolist() == tiles.filter_lookup(fl, "ct head")["Exam"].tolist())
check("find: filters and WU range combine", tiles.filter_lookup(fl, "", {"Modality": ["MR"]}, (4, 5)).pipe(lambda x: len(x) > 0 and x["WU"].between(4, 5).all() and (x["Modality"] == "MR").all()))
check("find: no filter returns everything", len(tiles.filter_lookup(fl)) == len(fl))
check("find: reads in range are blank under min_cell and never negative", ((fl["Reads in range"].isna()) | (fl["Reads in range"] == 0) | (fl["Reads in range"] >= cfg["min_cell"])).all())
check("find: contrast and modality guessed from the name",
      tiles._contrast_from_name("mr brain w/wo contrast") == "With and without" and tiles._contrast_from_name("ct head w/o contrast") == "Without"
      and tiles._contrast_from_name("ct chest w/ contrast") == "With" and tiles._modality_from_name("cta chest with contrast") == "CTA"
      and tiles._region_from_name("ct abdomen/pelvis w/o contrast") == "Abdomen/pelvis" and tiles._region_from_name("mr knee lt w/o contrast") == "Knee/leg")
import examcalc as _ec
_alias = _ec.read_reference(__import__("io").StringIO("Exam,ExamValue(WU)\ncr chest 1v,1.00\nmg ductogram lt,\n"), ".csv")
check("reference reader accepts the ExamValue(WU) header and blank WU", list(_alias.columns)[:2] == ["exam", "wrvu"] and _alias["wrvu"].isna().sum() == 1)
check("position text", tiles.position(1.0, {"Lower quartile": 2, "Upper quartile": 3}) == "below the middle half of readers")

# ---- personal benchmarks (My data)
mine = tiles.last_months(ldb.with_basis(own, "ReportDate"))
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
      tiles.exam_speed(access.own_rows(access.User("demo_rad13", "member", "demo_rad13"), cfg).pipe(ldb.with_basis, "ReportDate"), cfg).empty)

# ---- audit log
with tempfile.TemporaryDirectory() as td:
    c2 = {**cfg, "audit_log": str(Path(td) / "a.csv")}
    access.audit(c2, admin, "export", "=HYPERLINK(\"x\")")
    txt = Path(c2["audit_log"]).read_text()
    check("audit log is written with a header and guards formula text", txt.startswith("utc_time,login,role,action,detail") and "'=HYPERLINK" in txt)

print(f"\n{len(bad)} failed" if bad else "\nall checks passed")
sys.exit(1 if bad else 0)
