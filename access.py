"""access - who is signed in, and the single place that decides which rows each person may see.

Rules (same as the migration plan):
  member        -> only rows whose Radiologist equals their mapped reader ID
  admin         -> every row, real reader IDs
  everyone      -> AccessionNumber is replaced by a keyed one-way hash the moment the file is read,
                   so no page can show it (the hash only lets duplicate checks keep working)
  unmapped user -> no data at all

In this local prototype the filter runs in Python on a file anyone on this machine can open, so it shows the
rules and the page behaviour; it is not a security boundary. On a server the same rules move into database
views / row-level security and this module shrinks to 'query the database as this user'.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import localdb as ldb

HERE = Path(__file__).resolve().parent
DEFAULTS = {"auth_mode": "dev", "default_days": 180, "range_presets": [30, 90, 180, 365], "min_readers": 5,
            "min_cell": 10, "min_reads": 5, "demo": False, "chest_xr_modalities": ["CR", "DX", "XR"], "fallback": True, "users": {}, "hash_key": "change-me"}


# ---------------------------------------------------------------- configuration
def load_config(path: str | Path | None = None) -> dict:
    p = Path(path or os.environ.get("RAD_DATA_CONFIG") or HERE / "rad_data.json")
    cfg = {**DEFAULTS, **json.loads(p.read_text(encoding="utf-8"))}
    for k in ("database", "reference", "audit_log"):
        if cfg.get(k) and not Path(cfg[k]).is_absolute():
            cfg[k] = str((p.parent / cfg[k]).resolve())
    cfg["users"] = {str(k).strip().lower(): v for k, v in cfg["users"].items()}
    cfg["hash_key"] = os.environ.get("RAD_DATA_HASH_KEY") or cfg["hash_key"]
    return cfg


# ---------------------------------------------------------------- identity
@dataclass(frozen=True)
class User:
    login: str
    role: str            # member | admin | none
    reader: str | None   # the Radiologist value this person owns

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def resolve_user(cfg: dict) -> User:
    """Dev mode: a sidebar picker (clearly labelled). oidc mode: Streamlit single sign-on (st.login), needs secrets.toml."""
    users = cfg["users"]
    if cfg["auth_mode"] == "oidc":
        if not getattr(st.user, "is_logged_in", False):
            st.title("rad-data")
            st.write("Sign in with your organisation account to continue.")
            st.button("Sign in", on_click=st.login)
            st.stop()
        login = str(getattr(st.user, "email", "") or "").strip().lower()
    else:
        st.sidebar.warning("Development sign-in. In production this is single sign-on.")
        opts = [*users, "unmapped.person"]
        want = st.query_params.get("as")
        login = st.sidebar.selectbox("Sign in as", opts, index=opts.index(want) if want in opts else 0, key="dev_user")
    entry = users.get(login)
    if not entry or entry.get("role") not in ("member", "admin"):
        return User(login, "none", None)
    return User(login, entry["role"], entry.get("reader"))


# ---------------------------------------------------------------- data (the access layer)
def _hash_ids(s: pd.Series, key: str) -> pd.Series:
    t = s.astype("string")
    m = {v: hmac.new(key.encode(), v.encode(), hashlib.sha256).hexdigest()[:12] for v in t.dropna().unique()}
    return t.map(m)


@st.cache_data(show_spinner="Reading database...")
def _all_rows(path: str, stamp: float, fallback: bool, _key: str) -> pd.DataFrame:
    df = ldb.load(path, fallback=fallback)
    df["AccessionNumber"] = _hash_ids(df["AccessionNumber"], _key)   # identifiers never leave this function
    return df


def alias_map(df: pd.DataFrame) -> dict:
    """R01, R02... for every reader seen as Radiologist or AssignedTo, so both columns use the same alias."""
    return ldb.anonymize_map(pd.concat([df["Radiologist"], df["AssignedTo"]]))


@st.cache_data(show_spinner=False)
def _group_rows(path: str, stamp: float, fallback: bool, _key: str) -> pd.DataFrame:
    df = _all_rows(path, stamp, fallback, _key)
    m = alias_map(df)
    return df.assign(Radiologist=df["Radiologist"].map(m), AssignedTo=df["AssignedTo"].map(m))


def _db(cfg: dict) -> tuple:
    p = Path(cfg["database"])
    if not p.is_file() and cfg.get("demo"):
        import make_demo_db   # fabricated data only; lets a fresh clone (e.g. Streamlit Community Cloud) start without a committed database
        make_demo_db.main()
    if not p.is_file():
        st.error(f"Database not found: {p}")
        st.stop()
    return str(p), p.stat().st_mtime, bool(cfg["fallback"]), cfg["hash_key"]


def own_rows(user: User, cfg: dict) -> pd.DataFrame:
    """The signed-in person's own exams, whatever their role. Empty when the login has no reader ID."""
    df = _all_rows(*_db(cfg))
    if not user.reader:
        return df.iloc[0:0].drop(columns="AssignedTo")
    # AssignedTo would name other readers (who an exam was assigned to), so members never receive it
    return df[df["Radiologist"].str.lower().eq(user.reader.lower()).fillna(False)].drop(columns="AssignedTo")


def all_rows(user: User, cfg: dict) -> pd.DataFrame:
    """Every exam with real reader IDs. Admin only."""
    if not user.is_admin:
        raise PermissionError("admin access required")
    return _all_rows(*_db(cfg))


def group_rows(cfg: dict) -> pd.DataFrame:
    """Every exam with readers replaced by R01, R02..., for the anonymised group views."""
    return _group_rows(*_db(cfg))


# ---------------------------------------------------------------- audit log
def _safe(v: str) -> str:
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def audit(cfg: dict, user: User, action: str, detail: str = "") -> None:
    p = cfg.get("audit_log")
    if not p:
        return
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc_time", "login", "role", "action", "detail"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), _safe(user.login), user.role,
                    _safe(action), _safe(detail)])


def audit_once(cfg: dict, user: User, action: str, detail: str = "") -> None:
    """Log a page view once per browser session, not on every rerun."""
    seen = st.session_state.setdefault("_audit_seen", set())
    if (action, detail) not in seen:
        seen.add((action, detail))
        audit(cfg, user, action, detail)
