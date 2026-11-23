#!/usr/bin/env python3
"""examcalc - match exam descriptions to the reference table (port of the Excel RunExamCalculator macro).

Rules, as in the macro: trim and lowercase both sides; an exact match wins (first reference row if the name
repeats); otherwise a reference exam ending in '*' is a prefix wildcard (first one in file order wins);
otherwise the exam is unmatched.
Differences: runs of spaces are collapsed before comparing, and a wildcard prefix is compared literally
(VBA's Like would treat ? # [ in the prefix as pattern characters). No UI code here, so it is easy to test.
"""
from __future__ import annotations

import difflib
import io
import re
from pathlib import Path

import pandas as pd

ALIASES = {"exam": ("exam", "exam name", "exam description", "description"),
           "part": ("part", "body part", "bodypart"),
           "wrvu": ("wrvu", "workunit", "work unit", "work units", "wu", "rvu"),
           "ppc": ("ppc",),
           "modality": ("modality", "mod"), "log": ("log",)}
NUMERIC = ("wrvu", "ppc")
FIELDS = ("part", "wrvu", "ppc", "modality", "log")


def norm(s) -> str:
    return "" if s is None or pd.isna(s) else re.sub(r"\s+", " ", str(s)).strip().lower()


def read_reference(src, suffix: str, blank: str | None = None) -> pd.DataFrame:
    """Read the reference sheet (.xlsx or delimited text). Headers are matched by name or alias
    (wrvu/workunit/wu, part/body part); with no 'exam' header the first column is used. Extra columns are kept.
    Text blanks stay empty (NaN) unless `blank` gives a label; '***' placeholders in number columns become NaN."""
    if suffix == ".csv":  # delimiter guessed from the header line (pipe, comma, tab or semicolon)
        data = src.read() if hasattr(src, "read") else Path(src).read_bytes()
        text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
        raw = pd.read_csv(io.StringIO(text), dtype=str, sep=max("|,\t;", key=text.split("\n")[0].count))
    else:
        raw = pd.read_excel(src, dtype=str)
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    ren = {}
    for key, names in ALIASES.items():
        hit = next((c for c in raw.columns if c in names and c not in ren), None)
        if hit:
            ren[hit] = key
    df = raw.rename(columns=ren)
    if "exam" not in df:
        df = df.rename(columns={df.columns[0]: "exam"})
    if "wrvu" not in df:
        raise ValueError("no work-unit column found (looked for wrvu, workunit, wu, rvu)")
    first = [c for c in ("exam", *FIELDS) if c in df]
    df = df[first + [c for c in df.columns if c not in first]].dropna(subset=["exam"]).copy()
    df["exam"] = df["exam"].str.strip()
    for c in NUMERIC:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in df.columns.difference(["exam", *NUMERIC]):
        s = df[c].astype("string").str.strip().replace("", pd.NA)
        df[c] = s if blank is None else s.fillna(blank)
    return df.reset_index(drop=True)


class Reference:
    def __init__(self, ref: pd.DataFrame):
        self.ref = ref.reset_index(drop=True)
        self.rec = self.ref.to_dict("records")
        self.exact: dict = {}
        self.wild: list = []
        for i, name in enumerate(self.ref["exam"]):
            k = norm(name)
            self.exact.setdefault(k, i)
            if k.endswith("*"):
                self.wild.append((k[:-1], i))

    def _find(self, k: str):
        if k in self.exact:
            return "exact", self.exact[k]
        for prefix, i in self.wild:
            if k.startswith(prefix):
                return "wildcard", i
        return "unmatched", None

    def match(self, descs) -> pd.DataFrame:
        """One output row per input description: exam, match (exact|wildcard|unmatched), matched_to, part,
        wrvu, ppc, modality, log. Each distinct description is looked up once."""
        descs = pd.Series(list(descs), dtype="object")
        keys = descs.map(norm)
        rows = []
        for k in keys.unique():
            kind, i = self._find(k)
            r = self.rec[i] if i is not None else {}
            rows.append({"match": kind, "matched_to": r.get("exam"), **{f: r.get(f) for f in FIELDS}})
        res = pd.DataFrame(rows, index=keys.unique()).loc[keys.values].reset_index(drop=True)
        for c in NUMERIC:
            res[c] = pd.to_numeric(res[c], errors="coerce")
        res.insert(0, "exam", descs.values)
        return res

    def duplicates(self) -> pd.DataFrame:
        return self.ref[self.ref["exam"].map(norm).duplicated(keep=False)]

    def suggest(self, names, cutoff: float = 0.75) -> list:
        """Closest non-wildcard reference exam for each name (for review only; never applied)."""
        pool = [k for k in self.exact if not k.endswith("*")]
        out = []
        for n in names:
            best = difflib.get_close_matches(norm(n), pool, 1, cutoff)
            out.append(self.ref["exam"][self.exact[best[0]]] if best else None)
        return out


def summarize(res: pd.DataFrame) -> dict:
    m = res["match"]
    logs = res["log"].map(norm)
    lb = logs[logs != ""].value_counts().rename_axis("Log").reset_index(name="Count")
    lb["Log"] = lb["Log"].str.title()
    return {"exams": len(res), "exact": int((m == "exact").sum()), "wildcard": int((m == "wildcard").sum()),
            "unmatched": int((m == "unmatched").sum()),
            "no_wrvu": int(((m != "unmatched") & res["wrvu"].isna()).sum()),
            "wrvu": round(float(res["wrvu"].sum()), 2), "ppc": round(float(res["ppc"].sum()), 2), "logs": lb}
