#!/usr/bin/env python3
"""make_demo_db - build a FABRICATED demo database for trying rad-data locally.

Nothing here is real: reader IDs are demo_rad01.., accession numbers are DEMO########, exam values are invented.
Writes demo/rad_data_demo.db (one table, the 16-column examaudit schema) and demo/ReferenceData_demo.csv
(a small lookup with four exams deliberately missing so the 'Dictionary gaps' tile has something to show).

Run:  python make_demo_db.py
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "demo"

# (description, modality, work units, typical read minutes)
EXAMS = [
    ("cr chest 1v", "CR", 0.6, 1.0), ("cr chest 2v", "CR", 1.0, 2.0), ("cr abdomen 1v", "CR", 1.0, 1.5),
    ("cr knee 3v rt", "CR", 1.0, 1.5), ("cr hand 3v lt", "CR", 1.0, 1.5),
    ("ct head w/o contrast", "CT", 3.88, 5.0), ("ct head w/ and w/o contrast", "CT", 5.2, 7.0),
    ("ct chest w/ contrast", "CT", 5.5, 7.0), ("ct chest w/o contrast lung screening", "CT", 3.2, 5.0),
    ("ct abdomen pelvis w/ contrast", "CT", 6.28, 9.0), ("ct abdomen pelvis w/ and w/o contrast", "CT", 8.44, 12.0),
    ("ct cervical spine w/o contrast", "CT", 3.9, 5.0), ("ct angio head neck", "CT", 7.0, 11.0),
    ("ct neck soft tissue w/ contrast", "CT", 4.8, 7.0),
    ("mr brain w/o contrast", "MR", 3.92, 9.0), ("mr brain w/ and w/o contrast", "MR", 5.4, 12.0),
    ("mr lumbar spine w/o contrast", "MR", 4.67, 10.0), ("mr cervical spine w/o contrast", "MR", 4.67, 10.0),
    ("mr knee w/o contrast", "MR", 2.8, 8.0), ("mr shoulder w/o contrast", "MR", 2.8, 8.0),
    ("mr pelvis w/ and w/o contrast", "MR", 6.1, 13.0), ("mr abdomen w/ and w/o contrast", "MR", 6.4, 14.0),
    ("us abdomen complete", "US", 1.9, 6.0), ("us renal", "US", 1.4, 5.0), ("us thyroid", "US", 1.5, 5.0),
    ("us venous duplex lower ext bilat", "US", 1.6, 6.0), ("us pelvis transvaginal", "US", 1.5, 6.0),
    ("mg screening 3d bilat", "MG", 1.7, 3.0), ("mg diagnostic bilat", "MG", 2.9, 5.0),
    ("nm bone scan whole body", "NM", 3.5, 10.0), ("nm myocardial perfusion", "NM", 4.2, 12.0),
    ("pt fdg skull base to mid thigh", "PT", 10.49, 15.0), ("dexa bone density", "BD", 0.89, 3.0),
]
NOT_IN_LOOKUP = {"ct neck soft tissue w/ contrast", "mr abdomen w/ and w/o contrast", "us pelvis transvaginal",
                 "nm bone scan whole body"}
WEIGHT = {"CR": 30, "CT": 24, "MR": 14, "US": 14, "MG": 8, "NM": 3, "PT": 2, "BD": 3}
HOURS = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
HOUR_W = [4, 8, 10, 10, 9, 6, 8, 9, 8, 6, 4, 2]


def main(seed: int = 7) -> None:
    rnd = random.Random(seed)
    OUT.mkdir(exist_ok=True)
    pace = [0.85, 0.92, 1.0, 1.0, 1.05, 1.1, 0.95, 0.9, 1.2, 1.0, 1.15, 0.8]  # read-time factor per reader
    readers = {f"demo_rad{i:02d}": p for i, p in enumerate(pace, 1)}
    names = list(readers)
    readers["demo_rad13"] = 1.0   # visiting reader, added after the roster so weekdays never pick them
    facilities = ["Facility A", "Facility B", "Facility C"]
    by_mod: dict[str, list] = {}
    for e in EXAMS:
        by_mod.setdefault(e[1], []).append(e)
    mods = list(WEIGHT)
    rows = []

    def add(day: pd.Timestamp, who: str) -> None:
        mod = rnd.choices(mods, [WEIGHT[m] for m in mods])[0]
        desc, _, wu, mins = rnd.choice(by_mod[mod])
        h = rnd.choices(HOURS, HOUR_W)[0]
        st = day + pd.Timedelta(hours=h, minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59))
        secs = max(30, int(rnd.gauss(mins * 60 * readers[who], mins * 60 * 0.3)))
        late = rnd.random() < 0.05
        rep = st + pd.Timedelta(days=rnd.randint(10, 40) if late else rnd.randint(0, 2), minutes=rnd.randint(5, 240))
        r = rnd.random()
        stamp = (st.strftime("%Y-%m-%d %H:%M:%S") if r < 0.78 else st.strftime("%Y-%m-%d %H:%M") if r < 0.90
                 else st.strftime("%m/%d/%y %H:%M") if r < 0.99 else "NULL")
        rows.append(dict(
            CPT=f"'7{rnd.randint(0, 9999):04d}'", Modality=mod, Facility=rnd.choice(facilities),
            ExamCode=f"X{rnd.randint(0, 99999):05d}", ExamDescription=desc, Radiologist=who,
            AccessionNumber=f"DEMO{len(rows):08d}",
            ScheduledDateTime=(st - pd.Timedelta(hours=rnd.randint(1, 6))).strftime("%Y-%m-%d %H:%M"),
            ObservationDateTime=(st + pd.Timedelta(minutes=rnd.randint(5, 30))).strftime("%Y-%m-%d %H:%M:%S"),
            ReportDate=rep.strftime("%m/%d/%y"), ReportYearMonth=rep.strftime("%Y-%m"),
            JobTimeSecs=secs, JobTimeMins=round(secs / 60), StudyDateTime=stamp,
            WorkRVU=round(wu * 0.93, 2), WorkUnits=None if rnd.random() < 0.007 else round(wu * rnd.uniform(0.95, 1.05), 2)))

    for day in pd.bdate_range("2026-05-04", "2026-10-30"):
        present = rnd.sample(names, rnd.randint(8, 12))
        for _ in range(rnd.randint(55, 75)):
            add(day, rnd.choice(present))
    for day in pd.date_range("2026-05-04", "2026-10-30"):  # weekend call: one reader per weekend
        if day.dayofweek >= 5:
            who = names[(day.isocalendar().week) % len(names)]
            for _ in range(rnd.randint(18, 30)):
                add(day, who)
    for _ in range(6):  # a visiting reader with too few exams to show in any group view
        add(pd.Timestamp("2026-09-15"), "demo_rad13")

    df = pd.DataFrame(rows)
    con = sqlite3.connect(OUT / "rad_data_demo.db")
    df.to_sql("exams", con, index=False, if_exists="replace")
    con.close()

    ref = [(d, "demo", wu, round(wu * 10), m.lower(), "xr" if m == "CR" else m.lower())
           for d, m, wu, _ in EXAMS if d not in NOT_IN_LOOKUP]
    ref.append(("cr hand*", "demo", 1.0, 10, "cr", "xr"))
    pd.DataFrame(ref, columns=["exam", "part", "wrvu", "ppc", "modality", "log"]).to_csv(OUT / "ReferenceData_demo.csv", index=False)
    print(f"{len(df):,} fabricated exams, {df['Radiologist'].nunique()} readers -> {OUT}")


if __name__ == "__main__":
    main()
