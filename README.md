# rad-data_demo
A runnable local version of the web app in the migration plan: a landing page with six anonymised tiles, a
**My data** pathway (own exams only) and an **Admin** pathway (whole group, real reader IDs, no accession number).
It builds on the existing examaudit pieces: the 16-column exam table, `localdb.py` (date cleaning, fallback, quality checks)
and `examcalc.py` (lookup matching). The demo data is fabricated.

## Try it (about two minutes)

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
| `tiles.py` | The six tile queries as plain pandas, with the anonymity rules (testable without Streamlit) |
| `rad_data.json` | Demo config. `rad_data.example.json` is the template for real use |
| `localdb.py`, `examcalc.py` | Unchanged from examaudit |
| `make_demo_db.py`, `checks.py` | Fabricated data and the checks |
| `.streamlit/` | Local server settings; `secrets.toml.example` for single sign-on later |

## Rules implemented

- **Member:** only rows where `Radiologist` equals their mapped reader ID. **Admin:** all rows, real IDs (anonymise toggle off by default). **Unmapped login:** no data at all.
- **Accession:** replaced by a keyed one-way hash when the file is read; no page can show it. Set `RAD_DATA_HASH_KEY` for real use.
- **Tiles:** group totals only; readers shown as R01...; any view comparing readers needs `min_readers` (5); any cell under `min_cell` (10) exams is hidden or merged into Other.
- **Audit log** (`audit_log.csv`): sign-ins, admin page views, every export; a row-level admin export needs a typed reason.
- **Default range** is 180 days ending at the newest exam in the database (set `default_days`).

## What is not real yet

- **The filter is Python on a local file,** so it shows the rules and page behaviour but is not a security boundary. On a server the same rules go into database views or row-level security.
- **Single sign-on (`auth_mode: "oidc"`) is written but untested;** it needs IT's identity provider and `secrets.toml`.
- **Fluency extract:** the app reads a SQLite table named by `localdb.load`; map the CSV to the 16 columns first (`export_recent.py` from examaudit does this for the 3-month window).
- **Not built:** the agentic report, snapshot comparison, the weekend/IsMinor options, and a facility split beyond the Mix tile.
- The tile 1 question in the plan still says 90 days; the app follows your edit (180).