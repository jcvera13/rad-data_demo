#!/usr/bin/env python3
"""rad-data - local prototype of the web app described in the migration plan.

Run:   streamlit run app.py
Pages: Home (ten anonymised tiles), My data (own exams only), Admin (whole group, real IDs; admins only).
Config: rad_data.json (paths, thresholds, who is member/admin). Demo data: python make_demo_db.py
Builds on the examaudit pieces: localdb.py (schema, date cleaning, quality checks) and examcalc.py (lookup matching).
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import access
import examcalc as ec
import localdb as ldb
import re
import tiles

st.set_page_config(page_title="rad-data", layout="wide")
cfg = access.load_config()
if cfg.get("demo"):
    st.warning("DEMO: every exam, reader and accession number here is fabricated. Never upload real data to this app.", icon=":material/science:")
user = access.resolve_user(cfg)
if user.role == "none":
    st.title("rad-data")
    st.error("Your sign-in is not linked to a radiologist ID or an admin role, so no data is shown. Please contact an admin.")
    st.stop()
st.sidebar.caption(f"Signed in as **{user.login}** ({user.role})")
access.audit_once(cfg, user, "sign-in")
BASIS = "ReportDate"   # every date range and trend uses the sign (report) date; study date appears only in "produced"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------- helpers
@st.cache_resource(show_spinner=False)
def _reference(path: str, stamp: float):
    return ec.Reference(ec.read_reference(path, Path(path).suffix.lower()))


def reference():
    p = Path(cfg["reference"]) if cfg.get("reference") else None
    return _reference(str(p), p.stat().st_mtime) if p and p.is_file() else None


def pick_range(days: pd.Series, key: str):
    """Date range ending at the newest exam in the database (the data is complete through the last month end)."""
    d = days.dropna().dt.date
    if d.empty:
        st.info("No dated exams in this database.")
        st.stop()
    lo, hi = d.min(), d.max()
    qp = st.query_params.get("days")
    default = int(qp) if qp and qp.isdigit() else int(cfg["default_days"])
    presets = sorted({*map(int, cfg["range_presets"]), default})
    labels = [*(f"Last {n} days" for n in presets), "All data", "Custom"]
    st.sidebar.caption("Dates are sign (report) dates.")
    choice = st.sidebar.selectbox("Date range", labels, index=labels.index(f"Last {default} days"), key=f"preset_{key}")
    if choice == "All data":
        return lo, hi
    if choice == "Custom":
        rng = st.sidebar.date_input("Dates", (lo, hi), min_value=lo, max_value=hi, key=f"dates_{key}")
        if len(rng) != 2:
            st.info("Pick an end date.")
            st.stop()
        return rng
    return max(lo, hi - timedelta(days=int(choice.split()[1]) - 1)), hi


def csv_button(label: str, df: pd.DataFrame, name: str, detail: str):
    st.download_button(label, ldb.csv_safe(df).to_csv(index=False).encode("utf-8-sig"), f"{name}.csv", "text/csv",
                       key=f"dl_{name}", on_click=access.audit, args=(cfg, user, "export", detail))


def show_lookup(res: tiles.Result, name: str):
    """Interactive reference lookup: word search on the exam name plus filters on the other columns."""
    t = res.frames["Reference"]
    text = st.text_input("Search exam name", key=f"lk_text_{name}", placeholder="e.g. ct knee, mr brain -research (words in any order, - leaves a word out)")
    cats = [c for c in t.columns if c not in ("Exam", "WU", "Reads in range") and t[c].dtype in (object, "string") and 1 < t[c].nunique() <= 60]
    chosen = {}
    with st.expander("Filters", expanded=True):
        cols = st.columns(min(4, max(1, len(cats) + 1)))
        for i, c in enumerate(cats):
            chosen[c] = cols[i % len(cols)].multiselect(c, sorted(t[c].dropna().unique()), key=f"lk_{c}_{name}")
        lo, hi = float(t["WU"].min()), float(t["WU"].max())
        wu = cols[len(cats) % len(cols)].slider("WU range", lo, hi, (lo, hi), step=0.01, key=f"lk_wu_{name}") if hi > lo else (lo, hi)
        only_read = st.checkbox("Only exams read in the chosen dates", key=f"lk_read_{name}")
    x = tiles.filter_lookup(t, text, chosen, None if wu == (lo, hi) else wu)
    if only_read:
        x = x[x["Reads in range"].isna() | (x["Reads in range"] > 0)]
    k = st.columns(4)
    k[0].metric("Exams found", f"{len(x):,}")
    k[1].metric("Lowest WU", f"{x['WU'].min():.2f}" if x["WU"].notna().any() else "-")
    k[2].metric("Median WU", f"{x['WU'].median():.2f}" if x["WU"].notna().any() else "-")
    k[3].metric("Highest WU", f"{x['WU'].max():.2f}" if x["WU"].notna().any() else "-")
    if x.empty:
        st.info("No exams match. Loosen the search or a filter.")
    else:
        st.dataframe(x, hide_index=True, width="stretch", height=380,
                     column_config={"WU": st.column_config.NumberColumn(format="%.2f"),
                                    "Reads in range": st.column_config.NumberColumn(help="Group reads in the chosen dates; blank when fewer than the minimum cell size")})
        p = x.dropna(subset=["WU"])
        if len(p) > 1:
            st.caption("Work units of the exams found, by modality (hover for the exam name)")
            if len(p) > 1500:
                p = p.sample(1500, random_state=0)
                st.caption("Showing a random 1,500 of the exams found.")
            ch = (alt.Chart(p).mark_circle(size=55, opacity=0.5)
                  .transform_calculate(jitter="sqrt(-2*log(random()))*cos(2*PI*random())")
                  .encode(x=alt.X("WU:Q", title="WU"), y=alt.Y("Modality:N", title=None),
                          yOffset=alt.YOffset("jitter:Q", scale=alt.Scale(domain=[-4, 4])),
                          tooltip=["Exam", "WU", "Modality", "Body part", "Contrast"]))
            st.altair_chart(ch.properties(height=max(120, 36 * p["Modality"].nunique())), width="stretch")
        csv_button("Download these exams (.csv)", x, f"{name}_exams", f"tile:{name}")
    if res.note:
        st.caption(res.note)


def show_result(res: tiles.Result, name: str):
    if res.kind == "blocked":
        st.info(res.note)
        return
    if res.kind == "lookup":
        show_lookup(res, name)
        return
    if res.kind == "trend":
        totals = res.frames["Totals by modality"]
        weekly = {k: v for k, v in res.frames.items() if k != "Totals by modality"}
        pick = st.selectbox("Modality", list(weekly), key=f"trend_{name}")
        st.caption(f"Weekly work units, {pick}: produced (study date) against read (sign date)")
        st.line_chart(weekly[pick])
        csv_button("Download this series (.csv)", weekly[pick].reset_index(), f"{name}_weekly_{pick.split()[0].lower()}", f"tile:{name}")
        st.caption("Totals by modality")
        st.dataframe(totals, hide_index=True, width="stretch")
        csv_button("Download totals (.csv)", totals, f"{name}_totals", f"tile:{name}")
        if res.note:
            st.caption(res.note)
        return
    for title, f in res.frames.items():
        st.caption(title)
        if res.kind == "line":
            st.line_chart(f)
        elif res.kind == "bars":
            a, b = st.columns([2, 1])
            a.bar_chart(f[["WorkUnits"]])
            b.dataframe(f.round(1), width="stretch")
        elif res.kind == "range":
            st.dataframe(f, hide_index=True, width="stretch")
            for _, r in f.iterrows():
                one = pd.DataFrame([r])
                band = alt.Chart(one).mark_bar(height=14, opacity=0.35).encode(
                    y=alt.Y("Metric:N", title=None), x=alt.X("Lower quartile:Q", title=r["Metric"]), x2=alt.X2("Upper quartile:Q"))
                mid = alt.Chart(one).mark_tick(thickness=3, size=22).encode(y="Metric:N", x="Median:Q")
                st.altair_chart((band + mid).properties(height=60), width="stretch")
        elif res.kind == "heatmap":
            st.altair_chart(alt.Chart(f).mark_rect().encode(
                x=alt.X("Hour:O"), y=alt.Y("Weekday:O", sort=DAYS), color=alt.Color("Exams:Q", scale=alt.Scale(scheme="blues")),
                tooltip=["Weekday", "Hour", "Exams"]), width="stretch")
        else:
            st.dataframe(f, hide_index=True, width="stretch")
        csv_button(f"Download '{title}' (.csv)", f.reset_index() if f.index.name else f, f"{name}_{re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')[:40]}", f"tile:{name}")
    if res.note:
        st.caption(res.note)


# ---------------------------------------------------------------- page: home
def _close_tile():
    st.query_params.pop("tile", None)


def home():
    st.title("rad-data")
    st.caption(f"Group views, anonymised: readers are never named, any view that compares readers needs at least "
               f"{cfg['min_readers']} of them, and any cell with fewer than {cfg['min_cell']} exams is hidden.")
    d = ldb.with_basis(access.group_rows(cfg), BASIS)
    start, end = pick_range(d["Day"], "home")
    d = tiles.in_range(d, start, end)
    sel = st.query_params.get("tile")
    ndays = (end - start).days + 1
    for row in (tiles.TILES[i:i + 3] for i in range(0, len(tiles.TILES), 3)):
        for col, t in zip(st.columns(3), row):
            with col.container(border=True):
                st.markdown(f"**{t.title}**")
                st.caption(t.question.format(days=ndays))
                if st.button("Open", key=f"open_{t.id}", width="stretch", type="primary" if sel == t.id else "secondary"):
                    st.query_params["tile"] = t.id
                    st.rerun()
    tile = next((t for t in tiles.TILES if t.id == sel), None)
    if tile:
        st.divider()
        a, b = st.columns([6, 1])
        a.subheader(tile.title)
        b.button("Close", on_click=_close_tile, width="stretch")
        st.caption(f"{start:%Y-%m-%d} to {end:%Y-%m-%d} ({ndays} days), {len(d):,} exams")
        if d.empty:
            st.info("No exams in this range.")
        else:
            show_result(tile.compute(d, cfg, reference()), tile.id)


# ---------------------------------------------------------------- page: my data
def _summary_workbook(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            ldb.csv_safe(df).to_excel(w, sheet_name=name[:31], index=False)
    return buf.getvalue()


def _week_and_mix(d: pd.DataFrame) -> pd.DataFrame:
    """My work units per week (chart) and my modality mix (table), side by side. Returns the mix table."""
    left, right = st.columns(2)
    left.caption("My work units per week")
    left.line_chart(d.set_index("Day").resample("W-SUN")["WorkUnits"].sum())
    mod = (d.groupby("Modality").agg(Exams=("WorkUnits", "size"), WorkUnits=("WorkUnits", "sum"), Hours=("JobMins", lambda s: s.sum() / 60))
           .sort_values("WorkUnits", ascending=False))
    mod["Share of WU %"] = (100 * mod["WorkUnits"] / mod["WorkUnits"].sum()).round(1)
    right.caption("My modality mix")
    right.dataframe(mod.round(1), width="stretch")
    return mod


def my_data():
    st.title("My data")
    mode = st.segmented_control("Layout", ["Minimalist", "Verbose"], default="Verbose", key="my_layout",
                                help="Minimalist: headline numbers, weekly work units and modality mix. Verbose: everything.") or "Verbose"
    if not user.reader:
        st.info("Your account is not linked to a radiologist ID, so there is no personal data to show.")
        st.stop()
    own = access.own_rows(user, cfg)
    if own.empty:
        st.info("No exams were found for your ID in this database.")
        st.stop()
    all_own = ldb.with_basis(own, BASIS)
    start, end = pick_range(all_own["Day"], "mine")
    d = tiles.in_range(all_own, start, end)
    if d.empty:
        st.info("No exams in this range.")
        st.stop()
    st.caption(f"Only your own exams ({user.reader}), {start:%Y-%m-%d} to {end:%Y-%m-%d}. Patient identifiers are never shown.")
    t = d[d["JobMins"] > 0]
    hours = t["JobMins"].sum() / 60
    wu_hr = t["WorkUnits"].sum() / hours if hours else float("nan")
    min_wu = t["JobMins"].sum() / t["WorkUnits"].sum() if t["WorkUnits"].sum() else float("nan")
    m = st.columns(5)
    m[0].metric("Exams", f"{len(d):,}")
    m[1].metric("Work units", f"{d['WorkUnits'].sum():,.1f}")
    m[2].metric("Work RVU", f"{d['WorkRVU'].sum():,.1f}")
    m[3].metric("Read time (hrs)", f"{d['JobMins'].sum() / 60:,.1f}")
    m[4].metric("Days worked", f"{d['Day'].nunique():,}")
    if mode == "Minimalist":
        _week_and_mix(d)
        return

    st.subheader("My last 3 months")
    recent = tiles.last_months(all_own)
    st.caption(f"Compared with my own history, not the group: {recent['Day'].min():%Y-%m-%d} to {recent['Day'].max():%Y-%m-%d}, "
               f"{len(recent):,} exams. This section ignores the date range in the sidebar. Exams need at least {cfg['min_reads']} timed reads to be ranked.")
    h1, h2 = st.columns([4, 1])
    n_top = h2.selectbox("Show", [5, 10, 20, 30], index=1, key="top_n", format_func=lambda v: f"Top {v}")
    h1.markdown(f"**Top {n_top} exams I read**")
    st.dataframe(tiles.top_exams(recent, n_top), hide_index=True, width="stretch")

    st.markdown("**Exams by WU per hour**")
    c1, c2 = st.columns(2)
    which = c1.segmented_control("Show", ["Top 3", "Bottom 3", "Both"], default="Both", key="speed_which")
    split = c2.segmented_control("Group", ["By modality", "All exams together"], default="By modality", key="speed_group")
    which, split = which or "Both", split or "By modality"
    by_mod = split == "By modality"
    st.caption("Chest X-rays are left out. 'Vs my average' compares with my own WU per hour in the same modality"
               if by_mod else "Chest X-rays are left out. 'Vs my average' compares with my own WU per hour across all these exams")
    for bottom, title in ((False, "Top 3"), (True, "Bottom 3")):
        if which != "Both" and which != title:
            continue
        tab = tiles.exam_speed(recent, cfg, 3, bottom=bottom, by_modality=by_mod)
        st.markdown(f"**{title} exams by WU per hour{' in each modality' if by_mod else ''}**")
        if tab.empty:
            st.info("Not enough timed reads to rank exams.")
            continue
        tab = tab.assign(**{"Vs my average": tab["Vs my average"].map(lambda v: f"{v:+.0f}%")})
        if by_mod:
            for m, part in tab.groupby("Modality", sort=False):
                st.caption(m)
                st.dataframe(part.drop(columns="Modality"), hide_index=True, width="stretch")
        else:
            st.dataframe(tab, hide_index=True, width="stretch")

    mod = _week_and_mix(d)

    st.subheader("My exams needing attention")
    fix = d[d["WorkUnits"].isna()][["Day", "Modality", "Facility", "CPT", "ExamDescription"]].assign(Problem="WU missing")
    fix["Day"] = fix["Day"].dt.date
    if fix.empty:
        st.success("None of your exams in this range is missing a work-unit value.")
    else:
        st.caption("Exams with no work-unit value; worth reporting so the source can be corrected.")
        st.dataframe(fix, hide_index=True, width="stretch")

    monthly = d.groupby("Month").agg(Exams=("WorkUnits", "size"), WorkUnits=("WorkUnits", "sum"), WorkRVU=("WorkRVU", "sum"),
                                     Hours=("JobMins", lambda s: s.sum() / 60)).round(2).reset_index()
    st.download_button("Download my summary (.xlsx)", _summary_workbook({"Summary": pd.DataFrame(
        [("Reader", user.reader), ("From", f"{start}"), ("To", f"{end}"), ("Exams", len(d)), ("Work units", round(d["WorkUnits"].sum(), 2)),
         ("Work RVU", round(d["WorkRVU"].sum(), 2)), ("WU per hour", round(wu_hr, 2)), ("Min per WU", round(min_wu, 2))],
        columns=["Item", "Value"]), "By month": monthly, "By modality": mod.reset_index(), "Needs attention": fix}),
        "my_summary.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        on_click=access.audit, args=(cfg, user, "export", "my-summary"))


# ---------------------------------------------------------------- page: admin
def admin():
    st.title("Admin")
    if not user.is_admin:
        st.error("Admin access is required.")
        st.stop()
    df = access.all_rows(user, cfg)
    if st.sidebar.checkbox("Anonymise readers (R01, R02...)", value=False,
                           help="For screen-sharing. Off by default because admins need real IDs."):
        m = access.alias_map(df)
        df = df.assign(Radiologist=df["Radiologist"].map(m), AssignedTo=df["AssignedTo"].map(m))
    d = ldb.with_basis(df, BASIS)
    start, end = pick_range(d["Day"], "admin")
    d = tiles.in_range(d, start, end)
    view = st.radio("View", ["Readers", "Assignment vs read", "Group trend", "Data coverage", "Data quality", "Export"], horizontal=True,
                    label_visibility="collapsed")
    access.audit_once(cfg, user, "view", f"admin:{view}")
    st.caption("Whole group, real reader IDs. AccessionNumber is replaced by a one-way hash and never shown. Page views and exports are logged.")
    if d.empty and view != "Data coverage":
        st.info("No exams in this range.")
        return

    if view == "Readers":
        t = d[d["JobMins"] > 0]
        g = d.groupby("Radiologist").agg(Exams=("WorkUnits", "size"), WU=("WorkUnits", "sum"), RVU=("WorkRVU", "sum"),
                                         Days=("Day", "nunique"))
        g["Weekend exam %"] = (100 * d.assign(w=d["Day"].dt.dayofweek >= 5).groupby("Radiologist")["w"].mean()).round(1)
        tt = t.groupby("Radiologist").agg(Hours=("JobMins", lambda s: s.sum() / 60), TWU=("WorkUnits", "sum"))
        g = g.join(tt)
        g["WU / hour"] = g["TWU"] / g["Hours"]
        g["Min / WU"] = g["Hours"] * 60 / g["TWU"].where(g["TWU"] > 0)
        g = g.drop(columns="TWU").round(2).sort_values("WU", ascending=False).reset_index()
        st.dataframe(g, hide_index=True, width="stretch")
        csv_button("Download readers (.csv)", g, "admin_readers", "admin:readers")
    elif view == "Assignment vs read":
        st.caption("Exams signed in this range, by who read them and who Merge PACS assigned them to. Admin only: it names the people involved.")
        if d["AssignedTo"].notna().sum() == 0:
            st.info("This database has no AssignedTo values.")
            return
        k = d.dropna(subset=["Radiologist", "AssignedTo"]).assign(
            _same=lambda x: x["Radiologist"].str.lower().str.strip() == x["AssignedTo"].str.lower().str.strip())
        read = k.groupby("Radiologist").agg(Read=("WorkUnits", "size"), ReadWU=("WorkUnits", "sum"), Own=("_same", "sum"))
        took = k[~k["_same"]].groupby("Radiologist").agg(PickedUp=("WorkUnits", "size"), PickedUpWU=("WorkUnits", "sum"))
        gave = k[~k["_same"]].groupby("AssignedTo").agg(AssignedAway=("WorkUnits", "size"), AssignedAwayWU=("WorkUnits", "sum"))
        g = read.join(took, how="left").join(gave, how="outer").fillna(0)
        g.index.name = "Reader"
        g["Own assignment %"] = (100 * g["Own"] / g["Read"].where(g["Read"] > 0)).round(1)
        g = g.rename(columns={"Read": "Exams read", "ReadWU": "WU read", "PickedUp": "Picked up (exams)", "PickedUpWU": "Picked up (WU)",
                              "AssignedAway": "Assigned to them, read by others (exams)",
                              "AssignedAwayWU": "Assigned to them, read by others (WU)"}).drop(columns="Own")
        g = g.round(1).sort_values("Exams read", ascending=False).reset_index()
        st.dataframe(g, hide_index=True, width="stretch")
        csv_button("Download assignment vs read (.csv)", g, "admin_assignment", "admin:assignment")
        st.caption("Picked up = they read an exam assigned to someone else. Assigned to them, read by others = the exam went to a different reader.")
    elif view == "Group trend":
        a, b = st.columns(2)
        a.caption("Daily work units")
        a.line_chart(d.groupby("Day")["WorkUnits"].sum())
        b.caption("Work units by modality")
        b.bar_chart(d.groupby("Modality")["WorkUnits"].sum())
        mo = d.groupby("Month").agg(Exams=("WorkUnits", "size"), WU=("WorkUnits", "sum"), RVU=("WorkRVU", "sum"),
                                    Readers=("Radiologist", "nunique")).round(1).reset_index()
        st.dataframe(mo, hide_index=True, width="stretch")
    elif view == "Data coverage":
        st.dataframe(ldb.month_flags(df, BASIS), hide_index=True, width="stretch")
        cv = ldb.coverage(df, BASIS)
        gaps = cv[cv["Flag"]]
        st.caption(f"{len(gaps)} weekday(s) with no exams signed between the first and last sign date.")
        if len(gaps):
            st.dataframe(gaps[["Day", "Exams"]], hide_index=True)
    elif view == "Data quality":
        q = ldb.quality(df)
        st.dataframe(pd.DataFrame({"Check": list(q), "Rows": [len(v) for v in q.values()]}), hide_index=True, width="stretch")
        pick = st.selectbox("Show rows for", [k for k, v in q.items() if len(v)] or ["(none)"])
        if pick in q:
            st.dataframe(q[pick].drop(columns=["AccessionNumber"]).head(200), hide_index=True)
    else:
        by = d.groupby(["Month", "Radiologist"]).agg(Exams=("WorkUnits", "size"), WU=("WorkUnits", "sum"), RVU=("WorkRVU", "sum")).round(2).reset_index()
        st.subheader("Aggregates")
        csv_button("Download reader by month (.csv)", by, "admin_reader_month", "admin:aggregates")
        st.subheader("Row-level exams")
        st.caption("No AccessionNumber. A reason is required and is written to the audit log.")
        why = st.text_input("Reason for the row-level export (at least 10 characters)")
        if len(why.strip()) < 10:
            st.info("Enter a reason to enable the row-level download.")
        else:
            cols = [c for c in ldb.COLUMNS if c != "AccessionNumber"]
            csv_button("Download exams (.csv)", d[cols], "admin_exams", f"admin:rows:{why.strip()}")


pages = [st.Page(home, title="Home", icon=":material/home:", default=True),
         st.Page(my_data, title="My data", icon=":material/person:", url_path="my-data")]
if user.is_admin:
    pages.append(st.Page(admin, title="Admin", icon=":material/admin_panel_settings:", url_path="admin"))
st.navigation(pages).run()
