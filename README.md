# rad-data (local prototype)

A runnable local version of the web app in the migration plan: a landing page with ten anonymised tiles, a
**My data** pathway (own exams only) and an **Admin** pathway (whole group, real reader IDs, no accession number).
It builds on the existing examaudit pieces: the 16-column exam table, `localdb.py` (date cleaning, fallback, quality checks)
and `examcalc.py` (lookup matching). The demo data is fabricated.

## Try it

```
pip install -r requirements.txt
python make_demo_db.py        # fabricated database and lookup in demo/
python checks.py              # access-rule and tile checks, all should PASS
streamlit run app.py
```

Pick who you are in the sidebar ("Sign in as"): `demo_rad01` is a member, `demo_rad02` is an admin who is also a reader,
`demo_office` is an admin with no reader ID, `unmapped.person` gets no data. Shortcut for testing: `?as=demo_rad01&tile=gaps&days=90`.

## Files

| File | Role |
| --- | --- |
| `app.py` | Pages: Home (tiles), My data, Admin. Admin is only in the menu for admins and refuses anyone else |
| `access.py` | Sign-in, config, and the one place that decides which rows each person gets; accession hashing; audit log |
| `tiles.py` | The tile queries as plain pandas, with the anonymity rules (also testable without Streamlit) |
| `rad_data.json` | Demo config. `rad_data.example.json` is the template for real use |
| `localdb.py`, `examcalc.py` | Unchanged from examaudit |
| `make_demo_db.py`, `checks.py` | Fabricated data and the checks |
| `.streamlit/` | Local server settings; `secrets.toml.example` for single sign-on later |

## Rules implemented

- **Member:** only rows where `Radiologist` equals their mapped reader ID. **Admin:** all rows, real IDs (anonymise toggle off by default). **Unmapped login:** no data at all.
- **Accession:** replaced by a keyed one-way hash when the file is read; no page can show it. Set `RAD_DATA_HASH_KEY` for real use.
- **My data (benchmarks against yourself):** your last 3 months (window ends at your newest exam, ignores the sidebar range). A drop-down sets the list of most-read exam descriptions to 5, 10, 20 or 30. Top 3 / Bottom 3 / Both exams by WU per hour can be shown per modality (top 3 CR, top 3 CT, top 3 MR...) or joined into one ranked table. Chest X-rays are excluded (`chest_xr_modalities`, default CR/DX/XR with "chest" in the description). An exam needs `min_reads` (5) timed reads to be ranked. "Vs my average" compares with your own WU/hr in the same modality (or in all included exams for the single table).
- **Tiles:** group totals only; readers shown as R01...; any view comparing readers needs `min_readers` (5); any cell under `min_cell` (10) exams is hidden or merged into Other.
- **Audit log** (`audit_log.csv`): sign-ins, admin page views, every export; a row-level admin export needs a typed reason.
- **Default range** is 180 days ending at the newest exam in the database (set `default_days`).

## What is not real yet

- **The filter is Python on a local file,** so it shows the rules and page behaviour but is not a security boundary. On a server the same rules go into database views or row-level security.
- **Single sign-on (`auth_mode: "oidc"`) is written but untested;** it needs IT's identity provider and `secrets.toml`.
- **Fluency extract:** the app reads a SQLite table named by `localdb.load`; map the CSV to the 16 columns first (`export_recent.py` from examaudit does this for the 3-month window).
- **Not built:** agentic report, snapshot comparison, the weekend/IsMinor options, and a facility split beyond the Mix tile.
## What the three timestamps mean
- `ScheduledDateTime`: the technologist completed the exam (last image obtained).
- `StudyDateTime`: the exam became available for review on PACS. This is "arrival" for the When exams arrive tile (weekday by hour) and the start of Turnaround.
- `ObservationDateTime`: the radiologist's report was finalized (the sign time). `ReportDate` is its date, and the date basis for ranges and trends.
- When `StudyDateTime` is empty, `localdb.load` borrows `ObservationDateTime`, then `ReportDate`, and records it in `StudySource`. Those rows are left out of Arrival and Turnaround because their time is not the availability time.

## Sign-date basis, Produced vs read, Assignment vs read
- **Sign date:** ranges, trends and My data use `ReportDate` (the sign date). Only the "produced" line in Produced vs read uses the study date.
- **Produced vs read (tile 1):** weekly WU by study date vs sign date, All modalities plus each modality with at least `min_cell` exams read, and a totals table (Produced, Read, Read minus produced). Produced is understated in the newest weeks (late signs not yet in).
- **Turnaround time (tile 7):** from *available for review* (`StudyDateTime`) to *report finalized* (`ObservationDateTime`), by modality, facility and the hour the exam became available (median, 90th percentile, % within 1 day), in hours. If either time of day is missing it falls back to calendar days from the study date to the sign date and says so in the note.
- **Cross-coverage (tile 8):** share of exams read by someone other than the assignee, overall and by modality, facility and week. Group level, no names, exams with no assignee are not counted.
- **Lookup check (tile 9, uses `examcalc.py`):** every exam description is matched to the reference with the examcalc rules. The tile shows exact, wildcard and unmatched counts, exams whose recorded WU differs from the reference value by more than `value_tolerance_pct` (default 5, optional key in `rad_data.json`), the wildcard rows doing the matching, and repeated names in the reference. The demo deliberately records two exams off the lookup (`mr knee w/o contrast`, `us thyroid`).
- **Find an exam (tile 10, interactive):** the work unit lookup as a searchable table. Search by words in the exam name (any order, `-word` leaves a word out), filter by Modality, Body part, Contrast, Log and a WU range, tick "Only exams read in the chosen dates", and see the count, lowest, median and highest WU, a strip chart by modality and a CSV download. "Reads in range" is the group's read count per exam, blank under the minimum cell size. Columns the reference lacks are worked out from the exam name (Modality from the prefix, Body part from keywords, Contrast from w/, w/o, w/wo) and labelled as such. The reference reader accepts the `ExamValue(WU)` header.
- **My data layout:** a Minimalist / Verbose toggle at the top of My data. Verbose is the full page (default). Minimalist shows only the five headline numbers, My work units per week and My modality mix; the choice lasts for the browser session.
- **Admin > Assignment vs read:** named table of exams read, WU read, picked up (read an exam assigned to someone else) and assigned to them but read by others. Uses the `AssignedTo` column (Merge PACS Assignment).
- **AssignedTo privacy:** never sent to members; admins see real IDs; group views map it with the same R01... aliases as Radiologist.
