# NSE Nifty 500 Quality + Momentum Screener (zero-cost route)

A Python screener run by GitHub Actions and published to Google Sheets. This starter kit does not place trades.

## What is automated

- Attempts to refresh Nifty 500 constituents from the Nifty Indices public CSV; uses the `Universe` tab as a fallback.
- Fetches daily adjusted price history in batches using `yfinance` (unofficial, best-effort).
- Calculates 1M, 6M, 12M and 12–1M momentum; relative strength versus a Nifty 500 index proxy; 50/200-day trend.
- Applies a fundamental quality gate from user-maintained/imported values.
- Scores eligible names: 40% quality + 40% momentum percentile + 20% trend/risk percentile.
- Marks top-20 eligible names as entry candidates and existing holdings as retainable through rank 30 unless quality fails.
- Writes `Universe`, `Fundamentals`, `Ranking`, `Portfolio`, `Run History`, `Error Log`, and `Read Me` tabs.

## Cost assumptions

- Public GitHub repository + standard GitHub-hosted runner: GitHub documents standard runner use on public repositories as free/unlimited.
- Google Sheets API: standard API usage is currently available at no additional charge within quotas.
- No paid server, paid database, or paid market-data API is configured.
- Free/public data is not a service-level guarantee. Yahoo Finance may throttle or change; the constituent CSV may change; fundamentals are not supplied automatically by this project.

## Important public-repository security warning

A public repository exposes source code and all committed files to everyone. Never commit a service-account JSON, API key, personal portfolio export, or private data. Add credentials only as GitHub Actions Secrets. This workflow runs only on schedule or manual dispatch; do not add untrusted pull-request execution that can access secrets. Prefer a dedicated Google service account with access only to the one screener spreadsheet.

## Setup

### A. Create a Google Sheet

1. Create a new spreadsheet in Google Sheets.
2. Copy the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`.
3. The workflow will create the needed tabs and headers on its first run.
4. The script attempts to populate `Universe`. If public CSV import fails, paste symbols into the `Universe` tab with columns `Symbol`, `Company`, `Yahoo Symbol`, `Active`, `Source`, `Last Updated`. Example ticker format: `RELIANCE.NS`.

### B. Configure Google Sheets API access

1. Open Google Cloud Console and create a project.
2. Enable **Google Sheets API**.
3. Create a service account and create a JSON key for it.
4. Copy the service account's `client_email` from the JSON.
5. Share your spreadsheet with that email as **Editor**.
6. In GitHub, open repository **Settings → Secrets and variables → Actions → New repository secret**.
7. Add:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: the complete JSON key content.
   - `GOOGLE_SHEET_ID`: the spreadsheet ID only.
8. Do not commit the JSON key to the repository.

### C. Create the GitHub repository

1. Create a **public** repository if you want standard GitHub-hosted runner usage to be free under GitHub's published policy. Remember: all code and committed files are public.
2. Add the files in this starter kit, preserving `.github/workflows/daily-screener.yml`.
3. Commit to the default branch.
4. Go to **Actions**, enable workflows if prompted, select `Daily NSE Quality Momentum Screener`, and click **Run workflow**.
5. Check the workflow logs and the `Run History` / `Error Log` tabs in Sheets.

### D. Enter fundamentals

In the `Fundamentals` tab, maintain these columns in this exact order:

`Symbol, Company, Market Cap (₹ Cr), Sales CAGR 5Y (%), Profit CAGR 5Y (%), ROE Latest (%), ROE Avg 5Y (%), ROCE Latest (%), ROCE Avg 5Y (%), Debt/Equity, Promoter Pledged (%), Positive OCF Years (last 5), Fundamental As Of, Source / Notes, Quality Score, Quality Pass, Missing Required Fields`

Enter data into the first 14 columns; the last 3 are calculated by the workflow. Use percent values as percentage points (e.g. 15 means 15%, not 0.15). Positive OCF years must be an integer from 0 to 5. Missing required data fails the quality gate.

Suggested public sources for manual verification: company annual reports, NSE/BSE filings, and company investor-relations pages. You may import your own licensed/exported data if you have it. Do not assume a blank fundamental means zero.

## Strategy defaults

- Market cap >= ₹1,000 crore
- 5-year sales CAGR >= 10%
- 5-year profit CAGR >= 12%
- Latest ROE >= 15%
- 5-year average ROE >= 15%
- Latest ROCE >= 18%
- 5-year average ROCE >= 15%
- Debt/equity <= 0.5
- Promoter pledged <= 5%
- Positive operating cash flow in >= 3 of last 5 years
- Ranking: 40% quality + 40% momentum + 20% risk/trend
- Top 20 entry candidates; retain existing holdings while rank <= 30 unless quality fails.

## Schedule

The included workflow uses `13:17 UTC` (18:47 India time) on weekdays. GitHub scheduled jobs use UTC unless a timezone is configured and may be delayed during high load. This is not an exchange-calendar-aware schedule: it can run on Indian market holidays. For exact timing, adjust the cron schedule or trigger manually.

## Limitations

- The public Yahoo Finance interface used through `yfinance` is unofficial, may block requests, may have missing symbols, and is not guaranteed accurate or available.
- Nifty 500 constituents can change; CSV headers/endpoints can change. The code has a sheet fallback.
- The `^CRSLDX` Yahoo symbol is a benchmark proxy and may not be available in all runs. If unavailable, relative strength will be blank and momentum scoring falls back to 12–1M and 6M returns.
- This is not a survivorship-bias-free or point-in-time backtest. It does not fully model delisted securities, historical index membership, dividends, taxes, slippage, or transaction costs.
- The top-20 rule labels candidates; it does not automatically buy, sell, or guarantee that a 20-stock portfolio is fully invested.
- A daily workflow is best-effort and should not be relied on as a sole source for investment decisions.

## Run locally

Python 3.11+ is recommended.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_SHEET_ID` in your shell before running `python screener.py`. Never store credentials in source control.
