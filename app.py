"""
Data Tool: upload any CSV / Excel / JSON / Parquet file -> automatic cleaning,
data-quality report, dashboard, chart builder and cleaned-data export.

Run:  streamlit run app.py
"""
import io

import pandas as pd
import plotly.express as px
import streamlit as st

import engine as e

st.set_page_config(page_title="Data Tool", page_icon="📊", layout="wide")

MAX_CHARTS = 6
TOP_N = 15


def short(col: str) -> str:
    """Readable label for long flattened names like agentProfile/industryAffiliations/name."""
    parts = str(col).split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else str(col)


# --------------------------------------------------------------------------- cached work
@st.cache_data(show_spinner=False)
def load(data: bytes, name: str, sheet):
    return e.load_file(data, name, sheet)


@st.cache_data(show_spinner=False)
def run_clean(raw: pd.DataFrame, opts: e.CleanOptions):
    res = e.clean(raw, opts)
    roles = e.classify(res.df)
    return res.df, pd.DataFrame(res.log), roles


def to_excel(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for sheet, d in frames.items():
            d.to_excel(xw, sheet_name=sheet[:31], index=False)
    return buf.getvalue()


# --------------------------------------------------------------------------- sidebar
st.sidebar.title("📊 Data Tool")
up = st.sidebar.file_uploader(
    "Upload a data file",
    type=["csv", "tsv", "txt", "xlsx", "xlsm", "xls", "json", "jsonl", "ndjson", "parquet"],
)

if not up:
    st.title("Upload a file to get started")
    st.markdown(
        "Drop in any **CSV, Excel, JSON or Parquet** file. The tool will:\n"
        "1. **Clean** it: fix blanks, types, dates, currency, duplicates, HTML, casing and messy JSON exports\n"
        "2. **Report** on data quality, showing exactly what was changed and why\n"
        "3. **Build a dashboard** automatically from the kinds of columns it finds\n"
        "4. Let you **build your own charts** and **download** the cleaned data"
    )
    st.stop()

data = up.getvalue()
sheet = 0
if up.name.lower().endswith((".xlsx", ".xlsm", ".xls")):
    sheets = e.excel_sheets(data)
    if len(sheets) > 1:
        sheet = st.sidebar.selectbox("Sheet", sheets)

with st.spinner("Reading file…"):
    try:
        raw = load(data, up.name, sheet)
    except Exception as ex:  # surface parsing problems clearly
        st.error(f"Couldn't read this file: {ex}")
        st.stop()

with st.sidebar.expander("Cleaning options", expanded=False):
    opts = e.CleanOptions(
        drop_empty_threshold=st.slider("Drop columns that are at least this % empty", 50, 100, 95) / 100,
        collapse_lists=st.checkbox("Merge numbered list columns (JSON exports)", True),
        drop_identical=st.checkbox("Drop repeated columns", True),
        drop_constant=st.checkbox("Drop columns with only one value", True),
        drop_duplicates=st.checkbox("Remove duplicate rows", True),
        strip_html=st.checkbox("Strip HTML from text", True),
        infer_types=st.checkbox("Detect numbers, dates and yes/no", True),
        standardise_case=st.checkbox("Unify case differences ('john' / 'John')", True),
        derive_postcode_area=st.checkbox("Add UK postcode area", True),
    )
    key = st.selectbox("De-duplicate on a key column (optional)", ["(none)"] + list(raw.columns))
    opts.dedupe_key = None if key == "(none)" else e._clean_column_names([key])[0]

with st.spinner("Cleaning…"):
    df, log, roles = run_clean(raw, opts)

by_role = {r: [c for c, x in roles.items() if x == r] for r in e.ROLE_ORDER}

# filters
st.sidebar.markdown("### Filters")
filter_cols = st.sidebar.multiselect(
    "Filter by", by_role["category"] + by_role["boolean"], format_func=short, max_selections=4)
fdf = df
for c in filter_cols:
    opts_ = sorted(df[c].dropna().astype(str).unique())
    chosen = st.sidebar.multiselect(short(c), opts_, default=opts_)
    fdf = fdf[fdf[c].astype(str).isin(chosen)]
if by_role["datetime"]:
    dcol = by_role["datetime"][0]
    lo, hi = df[dcol].min(), df[dcol].max()
    if pd.notna(lo) and pd.notna(hi) and lo.date() < hi.date():
        rng = st.sidebar.date_input(f"{short(dcol)} range", (lo.date(), hi.date()), lo.date(), hi.date())
        if isinstance(rng, tuple) and len(rng) == 2:
            fdf = fdf[fdf[dcol].isna() | fdf[dcol].between(pd.Timestamp(rng[0]), pd.Timestamp(rng[1]) + pd.Timedelta(days=1))]

st.sidebar.caption(f"Showing {len(fdf):,} of {len(df):,} rows")

# --------------------------------------------------------------------------- header
st.title(up.name)
k = st.columns(5)
k[0].metric("Rows", f"{len(fdf):,}", f"{len(df) - len(raw):,} from cleaning" if len(df) != len(raw) else None)
k[1].metric("Columns", df.shape[1], f"{df.shape[1] - raw.shape[1]:,} from cleaning" if df.shape[1] != raw.shape[1] else None)
q_before, q_after = e.quality_score(raw), e.quality_score(df)
k[2].metric("Quality score", f"{q_after:.0f}/100", f"{q_after - q_before:+.0f} vs raw")
k[3].metric("Missing cells", f"{fdf.isna().mean().mean():.0%}")
k[4].metric("Fixes applied", f"{len(log)} steps")

tab_dash, tab_quality, tab_build, tab_data = st.tabs(["Dashboard", "Data quality", "Chart builder", "Data"])

# --------------------------------------------------------------------------- dashboard
with tab_dash:
    found = e.insights(fdf, roles)
    if found:
        with st.expander("Key findings", expanded=True):
            for line in found[:12]:
                st.markdown(f"- {line}")

    # numeric columns
    nums = by_role["numeric"]
    if nums:
        st.subheader("Numbers")
        stats = fdf[nums].describe().T[["count", "mean", "50%", "min", "max"]]
        stats.columns = ["count", "mean", "median", "min", "max"]
        stats.index = [short(c) for c in stats.index]
        st.dataframe(stats.round(2))
        cols = st.columns(2)
        for i, c in enumerate(nums[:MAX_CHARTS]):
            fig = px.histogram(fdf, x=c, nbins=30, title=short(c), labels={c: short(c)})
            fig.update_layout(bargap=0.05, height=320, margin=dict(t=50, b=10))
            cols[i % 2].plotly_chart(fig)

    # time series
    if by_role["datetime"]:
        st.subheader("Over time")
        dcol = st.selectbox("Date column", by_role["datetime"], format_func=short)
        span = (fdf[dcol].max() - fdf[dcol].min()).days if fdf[dcol].notna().any() else 0
        freq = "D" if span <= 62 else "W" if span <= 365 else "MS"
        measure = st.selectbox("Measure", ["Row count"] + nums, format_func=short)
        g = fdf.dropna(subset=[dcol]).set_index(dcol)
        series = g.resample(freq).size() if measure == "Row count" else g[measure].resample(freq).sum()
        fig = px.line(series.reset_index(name="value"), x=dcol, y="value", markers=True,
                      labels={dcol: short(dcol), "value": "rows" if measure == "Row count" else short(measure)})
        fig.update_layout(height=340)
        st.plotly_chart(fig)

    # yes/no fields
    if by_role["boolean"]:
        st.subheader("Yes / no fields")
        share = pd.DataFrame({
            "field": [short(c) for c in by_role["boolean"]],
            "% true": [fdf[c].dropna().astype(bool).mean() * 100 for c in by_role["boolean"]],
        }).sort_values("% true")
        fig = px.bar(share, x="% true", y="field", orientation="h", text_auto=".0f", range_x=[0, 100])
        fig.update_layout(height=max(250, 40 * len(share)), yaxis_title=None)
        st.plotly_chart(fig)

    # categories (+ multi-value lists)
    cats = by_role["category"] + by_role["multi"]
    if cats:
        st.subheader("Breakdowns")
        useful = sorted(cats, key=lambda c: (fdf[c].isna().mean(), -min(fdf[c].nunique(), 30)))
        pick = st.multiselect("Fields to break down", cats, default=useful[:4], format_func=short)
        cols = st.columns(2)
        for i, c in enumerate(pick):
            vals = e.explode_multi(fdf[c]) if roles[c] == "multi" else fdf[c].dropna().astype(str)
            vc = vals.value_counts().head(TOP_N).sort_values()
            fig = px.bar(x=vc.values, y=vc.index, orientation="h", title=short(c),
                         labels={"x": "rows", "y": ""}, text_auto=True)
            fig.update_layout(height=max(280, 28 * len(vc) + 80), margin=dict(t=50, b=10))
            cols[i % 2].plotly_chart(fig)

    if len(nums) >= 3:
        st.subheader("How the numbers relate")
        corr = fdf[nums].corr(numeric_only=True)
        corr.index = corr.columns = [short(c) for c in corr.columns]
        fig = px.imshow(corr, text_auto=".2f", color_continuous_scale="RdBu", zmin=-1, zmax=1, aspect="auto")
        st.plotly_chart(fig)

    if not (nums or cats or by_role["boolean"] or by_role["datetime"]):
        st.info("No chartable columns found. Check the Data quality tab to see how columns were classified.")

# --------------------------------------------------------------------------- quality
with tab_quality:
    a, b = st.columns(2)
    a.metric("Raw file", f"{raw.shape[0]:,} rows × {raw.shape[1]:,} cols", f"quality {q_before:.0f}/100", delta_color="off")
    b.metric("Cleaned", f"{df.shape[0]:,} rows × {df.shape[1]:,} cols", f"quality {q_after:.0f}/100", delta_color="off")

    st.subheader("What was changed")
    if log.empty:
        st.success("Nothing needed fixing.")
    else:
        st.dataframe(log.rename(columns={"step": "Step", "detail": "What happened", "affected": "Count"}),
                     hide_index=True)

    st.subheader("Column profile")
    prof = e.profile(df, roles)
    st.dataframe(prof, hide_index=True)

    miss = df.isna().mean().mul(100).round(1)
    miss = miss[miss > 0].sort_values(ascending=False).head(30)
    if len(miss):
        st.subheader("Columns with missing values")
        fig = px.bar(x=miss.values, y=[short(c) for c in miss.index], orientation="h",
                     labels={"x": "% missing", "y": ""})
        fig.update_layout(height=max(300, 22 * len(miss) + 80), yaxis={"autorange": "reversed"})
        st.plotly_chart(fig)

# --------------------------------------------------------------------------- chart builder
with tab_build:
    st.caption("Build any chart from the cleaned, filtered data.")
    all_cols = list(fdf.columns)
    c1, c2, c3, c4 = st.columns(4)
    kind = c1.selectbox("Chart", ["Bar", "Line", "Scatter", "Histogram", "Box", "Pie"])
    x = c2.selectbox("X / group by", all_cols, format_func=short)
    y_opts = ["(count rows)"] + by_role["numeric"]
    y = c3.selectbox("Y / value", y_opts, format_func=lambda c: c if c.startswith("(") else short(c))
    agg = c4.selectbox("Aggregate", ["sum", "mean", "median", "min", "max"], disabled=y == "(count rows)")
    color = st.selectbox("Colour by (optional)", ["(none)"] + by_role["category"] + by_role["boolean"], format_func=short)
    color = None if color == "(none)" else color

    try:
        if kind in ("Histogram", "Box", "Scatter"):
            yy = None if y == "(count rows)" else y
            if kind == "Histogram":
                fig = px.histogram(fdf, x=x, color=color)
            elif kind == "Box":
                fig = px.box(fdf, x=x if yy else None, y=yy or x, color=color)
            else:
                fig = px.scatter(fdf, x=x, y=yy or x, color=color, opacity=0.7)
        else:
            keys = [x] + ([color] if color and color != x else [])
            src = fdf.dropna(subset=[x])
            g = (src.groupby(keys, dropna=False).size().reset_index(name="rows") if y == "(count rows)"
                 else src.groupby(keys, dropna=False)[y].agg(agg).reset_index())
            val = "rows" if y == "(count rows)" else y
            if kind != "Line" and not pd.api.types.is_datetime64_any_dtype(g[x]):
                top = g.groupby(x)[val].sum().nlargest(30).index
                g = g[g[x].isin(top)]
            if kind == "Bar":
                fig = px.bar(g.sort_values(val, ascending=False), x=x, y=val, color=color)
            elif kind == "Line":
                fig = px.line(g.sort_values(x), x=x, y=val, color=color, markers=True)
            else:
                fig = px.pie(g, names=x, values=val)
        fig.update_layout(height=480)
        st.plotly_chart(fig)
    except Exception as ex:
        st.warning(f"That combination can't be charted: {ex}")

# --------------------------------------------------------------------------- data + export
with tab_data:
    st.dataframe(fdf, height=520)
    d1, d2, d3 = st.columns(3)
    base = up.name.rsplit(".", 1)[0]
    d1.download_button("⬇️ Cleaned data (CSV)", fdf.to_csv(index=False).encode("utf-8-sig"),
                       f"{base}_clean.csv", "text/csv")
    d2.download_button("⬇️ Cleaned data + report (Excel)",
                       to_excel({"clean_data": fdf, "cleaning_log": log, "column_profile": e.profile(df, roles)}),
                       f"{base}_clean.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d3.download_button("⬇️ Cleaning log (CSV)", log.to_csv(index=False).encode("utf-8-sig"),
                       f"{base}_cleaning_log.csv", "text/csv")
