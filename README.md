# Data Tool

Upload any CSV, Excel, JSON or Parquet file and get:

1. **Automatic cleaning**, with every change logged
2. **A data-quality report**: before/after quality score, column profile and missing values
3. **An auto-built dashboard**: key findings, number distributions, trends over time, yes/no fields, breakdowns and correlations
4. **A chart builder**: bar, line, scatter, histogram, box or pie from any columns
5. **Exports**: cleaned CSV, or an Excel file with the cleaned data, cleaning log and column profile

## Run it on your Mac

```bash
cd ~/Documents/data-tool          # the folder with app.py, engine.py, requirements.txt
uv venv --python 3.12             # or: python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt   # or: pip install -r requirements.txt
streamlit run app.py
```

It opens in your browser at http://localhost:8501. Next time, you only need `source .venv/bin/activate` and `streamlit run app.py`.

## What the cleaning does

| Step | Example |
|---|---|
| Reads any delimiter / encoding | `;` or tab separated, Windows (cp1252) exports |
| Flattens nested JSON | `{"b": {"c": 1}}` → column `b/c` |
| Merges numbered list columns | `affiliations/0/name`, `affiliations/1/name` → `affiliations/name` = "NAEA; ARLA" |
| Unifies missing values | `""`, `N/A`, `null`, `-` → missing |
| Drops sparse, repeated and constant columns | 375 → 75 columns on the Rightmove agents sample |
| Strips HTML | `<p>Founded in 2012</p>` → `Founded in 2012` |
| Detects types | `£1,250` → 1250 · `15%` → 0.15 · `03/04/2025` → 3 April 2025 (UK day-first) · `Yes`/`no` → True/False |
| Protects codes | phone numbers and leading-zero codes stay as text |
| Unifies casing | `john smith` / `John Smith` → most common form |
| Removes duplicates | exact rows, or on a key column you choose |
| UK postcodes | standardised (`gu2 7xh` → `GU2 7XH`) plus a `_area` column (`GU`) |

All of these can be switched on or off in the sidebar under **Cleaning options**.

## Structure

- `engine.py`: all the loading, cleaning, profiling and column classification. It's pure pandas with no UI, so you can reuse it in a scheduled job, an API or n8n.
- `app.py`: the Streamlit interface.

## Ideas for next versions

- Saved cleaning "recipes" per client, so the same rules run on every monthly upload
- Connectors (Google Sheets, Xero, CRM APIs) instead of file upload
- An AI-written summary of the dashboard ("rent arrears up 12% vs last month…")
- Login and per-client workspaces once it's a paid product
