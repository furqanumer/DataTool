"""
Data engine: load any tabular file, clean it, profile it and classify columns.
Pure pandas, no UI code, so it can be tested and reused (API, scheduled jobs, etc.).
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

NULL_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", "--", "?", "missing", "#n/a", "nil"}
TRUE_TOKENS = {"true", "yes", "y", "t"}
FALSE_TOKENS = {"false", "no", "n", "f"}
HTML_TAG = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"^(https?://|www\.|/[\w.-]+/)", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2})[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$", re.I)
DATE_HINT = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|^[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}")
NUM_STRIP = re.compile(r"[£$€¥,%\s]")


# --------------------------------------------------------------------------- loading
def load_file(data: bytes, filename: str, sheet: str | int | None = 0) -> pd.DataFrame:
    """Load CSV/TSV/TXT, Excel, JSON or Parquet from raw bytes."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(data), sheet_name=sheet if sheet is not None else 0)
    if name.endswith(".parquet"):
        return pd.read_parquet(io.BytesIO(data))
    if name.endswith((".json", ".jsonl", ".ndjson")):
        text = _decode(data)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:  # JSON lines
            obj = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(obj, dict):
            # use the first list value if the file is {"data": [...]}, else treat as one record
            lists = [v for v in obj.values() if isinstance(v, list)]
            obj = lists[0] if lists else [obj]
        df = pd.json_normalize(obj, sep="/")
        for c in df.columns:  # nested lists/dicts -> text so the frame is hashable
            if df[c].map(lambda v: isinstance(v, (list, dict))).any():
                df[c] = df[c].map(_flatten_cell)
        return df
    # default: delimited text
    text = _decode(data)
    sample = text[:20000]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    return pd.read_csv(io.StringIO(text), sep=delim, dtype=str, keep_default_na=False,
                       on_bad_lines="warn", engine="python")


def excel_sheets(data: bytes) -> list[str]:
    return pd.ExcelFile(io.BytesIO(data)).sheet_names


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- cleaning
@dataclass
class CleanOptions:
    drop_empty_threshold: float = 0.95   # drop columns with >= this share missing
    drop_constant: bool = True           # drop columns with a single value
    drop_duplicates: bool = True
    strip_html: bool = True
    infer_types: bool = True
    derive_postcode_area: bool = True
    collapse_lists: bool = True          # merge items/0/x, items/1/x into one column
    drop_identical: bool = True          # drop columns that repeat another column
    standardise_case: bool = True        # unify "john smith" / "John Smith"
    dedupe_key: str | None = None        # optional column to de-duplicate on


@dataclass
class CleanResult:
    df: pd.DataFrame
    log: list[dict] = field(default_factory=list)

    def add(self, step: str, detail: str, affected: int | str = ""):
        self.log.append({"step": step, "detail": detail, "affected": affected})


def clean(raw: pd.DataFrame, opt: CleanOptions | None = None) -> CleanResult:
    opt = opt or CleanOptions()
    df = raw.copy()
    res = CleanResult(df)

    # 1. column names
    new_cols = _clean_column_names(df.columns)
    renamed = sum(a != b for a, b in zip(df.columns, new_cols))
    df.columns = new_cols
    if renamed:
        res.add("Column names", "Trimmed, de-duplicated and tidied column names", renamed)

    # 1b. nested values left by JSON (lists / dicts in cells) -> readable text
    for c in df.columns:
        if _is_text(df[c]) and df[c].map(lambda v: isinstance(v, (list, dict))).any():
            df[c] = df[c].map(_flatten_cell)

    # 2. normalise text cells: trim whitespace, unify null tokens
    blanks = 0
    for c in df.columns:
        s = df[c]
        if _is_text(s):
            st_ = s.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
            mask = st_.str.lower().isin(NULL_TOKENS)
            blanks += int(mask.sum())
            df[c] = st_.mask(mask).astype(object).where(lambda x: x.notna(), np.nan)
    if blanks:
        res.add("Missing values", "Converted blanks and tokens like 'N/A', 'null', '-' to missing", blanks)

    # 2b. collapse flattened lists: items/0/name, items/1/name -> items/name = "a; b"
    if opt.collapse_lists:
        df, groups = _collapse_lists(df)
        if groups:
            res.add("Flattened lists", "Merged numbered list columns into single fields: "
                    + _preview(groups), len(groups))

    # 3. drop empty rows / mostly-empty columns
    before = len(df)
    df = df.dropna(how="all")
    if len(df) < before:
        res.add("Empty rows", "Removed rows with no data", before - len(df))
    miss = df.isna().mean()
    drop = miss[miss >= opt.drop_empty_threshold].index.tolist()
    if drop:
        df = df.drop(columns=drop)
        res.add("Sparse columns", f"Dropped columns ≥{opt.drop_empty_threshold:.0%} empty: "
                + _preview(drop), len(drop))

    # 4. strip HTML
    if opt.strip_html:
        html_cols = []
        for c in df.columns:
            s = df[c].dropna()
            if _is_text(df[c]) and len(s) and s.astype(str).str.contains(HTML_TAG).mean() > 0.2:
                df[c] = df[c].map(_strip_html)
                html_cols.append(c)
        if html_cols:
            res.add("HTML", "Stripped HTML tags from: " + _preview(html_cols), len(html_cols))

    # 5. type inference
    if opt.infer_types:
        conv = {"number": [], "date": [], "boolean": []}
        for c in df.columns:
            kind, new = _infer(df[c], c)
            if kind:
                df[c] = new
                conv[kind].append(c)
        for k, cols in conv.items():
            if cols:
                res.add("Data types", f"Converted to {k}: " + _preview(cols), len(cols))

    # 5b. inconsistent casing in categories ("john smith" vs "John Smith") -> most common form
    if opt.standardise_case:
        fixed_cols, fixed = [], 0
        for c in df.columns:
            s = df[c]
            if not _is_text(s) or s.nunique() > 2000:
                continue
            nn = s.dropna().astype(str)
            if not len(nn) or nn.str.len().mean() > 60:
                continue
            counts = nn.value_counts()
            canon = {}
            for val, _ in counts.items():  # most frequent first
                canon.setdefault(val.lower(), val)
            mapping = {v: canon[v.lower()] for v in counts.index if canon[v.lower()] != v}
            if mapping:
                fixed += int(s.isin(mapping.keys()).sum())
                df[c] = s.replace(mapping)
                fixed_cols.append(c)
        if fixed_cols:
            res.add("Inconsistent text", "Unified values differing only by case in: " + _preview(fixed_cols), fixed)

    # 6. duplicates
    if opt.drop_duplicates:
        before = len(df)
        df = df.drop_duplicates()
        if len(df) < before:
            res.add("Duplicates", "Removed exact duplicate rows", before - len(df))
    if opt.dedupe_key and opt.dedupe_key in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=[opt.dedupe_key], keep="last")
        if len(df) < before:
            res.add("Duplicates", f"Removed rows with a repeated '{opt.dedupe_key}' (kept latest)",
                    before - len(df))

    # 6b. identical columns (same values under different names)
    if opt.drop_identical:
        hashes, dup_cols = {}, []
        for c in df.columns:
            h = pd.util.hash_pandas_object(df[c].astype(str), index=False).sum()
            key = (h, df[c].notna().sum())
            if key in hashes and df[c].astype(str).equals(df[hashes[key]].astype(str)):
                dup_cols.append(c)
            else:
                hashes[key] = c
        if dup_cols:
            df = df.drop(columns=dup_cols)
            res.add("Repeated columns", "Dropped columns identical to an earlier column: "
                    + _preview(dup_cols), len(dup_cols))

    # 7. constant columns
    if opt.drop_constant:
        const = [c for c in df.columns if df[c].nunique(dropna=True) <= 1]
        if const and len(const) < len(df.columns):
            df = df.drop(columns=const)
            res.add("Constant columns", "Dropped columns with only one value: " + _preview(const), len(const))

    # 8. derived fields
    df = df.copy()
    if opt.derive_postcode_area:
        for c in list(df.columns):
            s = df[c].dropna()
            if _is_text(df[c]) and len(s) and s.astype(str).str.match(UK_POSTCODE_RE).mean() > 0.8:
                pc = df[c].astype("string").str.upper().str.replace(r"\s+", "", regex=True)
                pc = pc.str[:-3] + " " + pc.str[-3:]
                df[c] = pc.astype(object).where(pc.notna(), np.nan)
                area = pc.str.extract(UK_POSTCODE_RE)[0]
                df[f"{c}_area"] = area.astype(object).where(area.notna(), np.nan)
                res.add("Derived", f"Added '{c}_area' (UK postcode area) from '{c}'", int(area.notna().sum()))

    df = df.reset_index(drop=True)
    res.df = df
    return res


def _clean_column_names(cols) -> list[str]:
    out, seen = [], {}
    for i, c in enumerate(cols):
        c = str(c).strip()
        if not c or c.lower().startswith("unnamed:"):
            c = f"column_{i + 1}"
        c = re.sub(r"\s+", " ", c)
        base, n = c, seen.get(c, 0)
        while c in seen:
            n += 1
            c = f"{base}_{n}"
        seen[base] = n
        seen[c] = 0
        out.append(c)
    return out


LIST_COL = re.compile(r"^(.*?)[/._](\d+)[/._](.+)$")


def _collapse_lists(df: pd.DataFrame):
    groups: dict[tuple, list] = {}
    for c in df.columns:
        m = LIST_COL.match(str(c))
        if m:
            groups.setdefault((m.group(1), m.group(3)), []).append((int(m.group(2)), c))
    new_cols, drop, names = {}, [], []
    for (prefix, suffix), items in groups.items():
        if len(items) < 2:
            continue
        cols = [c for _, c in sorted(items)]
        name = f"{prefix}/{suffix}"
        joined = df[cols].apply(
            lambda r: "; ".join(str(v) for v in r if pd.notna(v) and str(v) != "") or np.nan, axis=1)
        new_cols[name] = joined
        drop += cols
        names.append(name)
    if not names:
        return df, []
    df = pd.concat([df.drop(columns=drop), pd.DataFrame(new_cols, index=df.index)], axis=1)
    return df, names


def _flatten_cell(v):
    if isinstance(v, list):
        parts = [(" ".join(str(x) for x in i.values()) if isinstance(i, dict) else str(i)) for i in v]
        return "; ".join(parts) if parts else np.nan
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v


def _strip_html(v):
    if not isinstance(v, str):
        return v
    v = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", " ", v, flags=re.I)
    v = html.unescape(HTML_TAG.sub("", v))
    v = re.sub(r"\s+", " ", v).strip()
    return v or np.nan


def _infer(s: pd.Series, name: str):
    """Return (kind, converted_series) or (None, None)."""
    if not _is_text(s):
        return None, None
    vals = s.dropna().astype(str)
    if len(vals) == 0:
        return None, None
    low = vals.str.lower()

    # boolean
    if low.isin(TRUE_TOKENS | FALSE_TOKENS).mean() >= 0.95 and low.nunique() <= 4:
        return "boolean", s.map(lambda x: np.nan if pd.isna(x) else str(x).strip().lower() in TRUE_TOKENS)

    # numbers (guard against phone numbers, codes with leading zeros, IDs-as-text)
    looks_coded = vals.str.match(r"^0\d{3,}").mean() > 0.2 or re.search(r"phone|tel|mobile|postcode|zip|sort.?code", name, re.I)
    if not looks_coded:
        cleaned = vals.str.replace(NUM_STRIP, "", regex=True).str.replace(r"^\((.*)\)$", r"-\1", regex=True)
        nums = pd.to_numeric(cleaned, errors="coerce")
        if nums.notna().mean() >= 0.95:
            out = pd.to_numeric(s.astype("string").str.replace(NUM_STRIP, "", regex=True)
                                .str.replace(r"^\((.*)\)$", r"-\1", regex=True), errors="coerce")
            if vals.str.contains("%").mean() > 0.5:
                out = out / 100
            if out.dropna().mod(1).eq(0).all():
                out = out.astype("Int64")
            return "number", out

    # dates
    if vals.str.contains(DATE_HINT).mean() >= 0.8:
        parsed = _parse_dates(vals)
        if parsed.notna().mean() >= 0.9:
            return "date", _parse_dates(s.astype("string"))
    return None, None


def _parse_dates(s: pd.Series) -> pd.Series:
    """Parse mixed date formats, UK day-first by default; drop timezones for charting."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        iso = s.str.match(r"^\d{4}-\d{2}-\d{2}").mean() > 0.5 if len(s.dropna()) else False
        out = pd.to_datetime(s, errors="coerce", format="mixed", dayfirst=not iso, utc=True)
    return out.dt.tz_convert(None)


def _is_text(s: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _preview(cols, n=6) -> str:
    cols = [str(c) for c in cols]
    return ", ".join(cols[:n]) + (f" … (+{len(cols) - n} more)" if len(cols) > n else "")


# --------------------------------------------------------------------------- profiling
ROLE_ORDER = ["numeric", "datetime", "boolean", "category", "multi", "id", "text", "url", "email"]


def classify(df: pd.DataFrame) -> dict[str, str]:
    """Assign each column a role used to pick charts."""
    roles = {}
    n = max(len(df), 1)
    for c in df.columns:
        s = df[c]
        nonnull = s.dropna()
        nun = nonnull.nunique()
        ratio = nun / max(len(nonnull), 1)
        idish = bool(re.search(r"(^|[_\s/])(?i:id|key|ref|code|uuid|number|no)$|Id$|ID$", str(c)))
        if pd.api.types.is_bool_dtype(s) or (nun == 2 and set(map(str, nonnull.unique())) <= {"True", "False"}):
            roles[c] = "boolean"
        elif pd.api.types.is_datetime64_any_dtype(s):
            roles[c] = "datetime"
        elif pd.api.types.is_numeric_dtype(s):
            roles[c] = "id" if idish else "numeric"
        else:
            sample = nonnull.astype(str).head(200)
            avg_len = sample.str.len().mean() if len(sample) else 0
            if len(sample) and sample.str.match(URL_RE).mean() > 0.8:
                roles[c] = "url"
            elif len(sample) and sample.str.match(EMAIL_RE).mean() > 0.8:
                roles[c] = "email"
            elif len(sample) and sample.str.match(UUID_RE).mean() > 0.8:
                roles[c] = "id"
            elif idish:
                roles[c] = "id"
            elif len(sample) and sample.str.contains("; ").mean() > 0.2 and avg_len < 200 \
                    and explode_multi(nonnull).nunique() <= 100:
                roles[c] = "multi"
            elif avg_len > 60:
                roles[c] = "text"
            elif nun <= 30 or (ratio < 0.5 and nun <= 200):
                roles[c] = "category"
            else:
                roles[c] = "id"
    return roles


def explode_multi(s: pd.Series) -> pd.Series:
    """'a; b; c' cells -> one value per row (for counting multi-value fields)."""
    return s.dropna().astype(str).str.split("; ").explode().str.strip()


def profile(df: pd.DataFrame, roles: dict[str, str] | None = None) -> pd.DataFrame:
    roles = roles or classify(df)
    rows = []
    for c in df.columns:
        s = df[c]
        nn = s.dropna()
        rows.append({
            "column": c,
            "role": roles.get(c, ""),
            "dtype": str(s.dtype),
            "missing_%": round(s.isna().mean() * 100, 1),
            "unique": int(nn.nunique()),
            "example": str(nn.iloc[0])[:60] if len(nn) else "",
        })
    return pd.DataFrame(rows)


def quality_score(df: pd.DataFrame) -> float:
    """Simple 0-100 score: completeness weighted with uniqueness of rows."""
    if df.empty:
        return 0.0
    completeness = 1 - df.isna().mean().mean()
    dup = df.duplicated().mean()
    return round(100 * completeness * (1 - dup), 1)


def outliers_iqr(s: pd.Series) -> int:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) < 10:
        return 0
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    return int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum())


def insights(df: pd.DataFrame, roles: dict[str, str]) -> list[str]:
    """Plain-English findings for the dashboard header."""
    out = []
    for c, r in roles.items():
        s = df[c].dropna()
        if not len(s):
            continue
        if r == "boolean":
            out.append(f"**{c}**: {s.astype(bool).mean():.0%} true")
        elif r == "multi":
            vc = explode_multi(s).value_counts()
            out.append(f"**{c}**: most common value is *{vc.index[0]}* (in {vc.iloc[0] / len(df):.0%} of rows)")
        elif r == "category":
            vc = s.value_counts(normalize=True)
            if len(vc) > 1:
                out.append(f"**{c}**: most common is *{vc.index[0]}* ({vc.iloc[0]:.0%} of rows)")
        elif r == "numeric":
            o = outliers_iqr(s)
            msg = f"**{c}**: median {s.median():,.2f}, range {s.min():,.2f} to {s.max():,.2f}"
            if o:
                msg += f", {o} outlier(s)"
            out.append(msg)
        elif r == "datetime":
            out.append(f"**{c}**: {s.min():%d %b %Y} to {s.max():%d %b %Y}")
    return out
