#!/usr/bin/env python3
"""Best-effort free NSE Nifty 500 quality + momentum screener.

Fundamentals are intentionally sourced from a user-maintained Google Sheet.
Prices are obtained through yfinance/Yahoo Finance, which is unofficial and can
throttle or change. This is a research tool, not investment advice.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

TZ = "Asia/Kolkata"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
BENCHMARK = os.getenv("BENCHMARK_TICKER", "^CRSLDX")
MIN_COVERAGE = float(os.getenv("MINIMUM_DATA_COVERAGE", "0.80"))
TOP_N = 20
RETAIN_BUFFER = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nse_screener")

SHEETS = {
    "Universe": ["Symbol", "Company", "Yahoo Symbol", "Active", "Source", "Last Updated"],
    "Fundamentals": [
        "Symbol", "Company", "Market Cap (₹ Cr)", "Sales CAGR 5Y (%)",
        "Profit CAGR 5Y (%)", "ROE Latest (%)", "ROE Avg 5Y (%)",
        "ROCE Latest (%)", "ROCE Avg 5Y (%)", "Debt/Equity",
        "Promoter Pledged (%)", "Positive OCF Years (last 5)",
        "Fundamental As Of", "Source / Notes", "Quality Score",
        "Quality Pass", "Missing Required Fields"
    ],
    "Ranking": [
        "Rank", "Symbol", "Company", "Price Date", "Close",
        "Return 1M (%)", "Return 6M (%)", "Return 12M (%)",
        "12-1M Momentum (%)", "Relative Strength vs Nifty 500 (%)",
        "Close > 200 DMA", "50 DMA > 200 DMA", "Quality Score",
        "Momentum Score", "Risk Score", "Combined Score",
        "Quality Pass", "Entry Candidate", "Portfolio Status", "Decision"
    ],
    "Portfolio": [
        "Symbol", "Company", "Status", "Rank", "Target Weight (%)",
        "Shares", "Average Cost", "Current Price", "Market Value",
        "Unrealized P/L", "P/L (%)", "Added On", "Last Reviewed", "Notes"
    ],
    "Run History": [
        "Run Timestamp", "Universe Count", "Price Success",
        "Fundamentals Passed", "Ranked Count", "Selected Top 20",
        "Retained Holdings", "Status", "Notes"
    ],
    "Error Log": ["Timestamp", "Level", "Symbol", "Message", "Details"],
}
FUNDAMENTAL_KEYS = {
    "market_cap": "Market Cap (₹ Cr)",
    "sales_cagr": "Sales CAGR 5Y (%)",
    "profit_cagr": "Profit CAGR 5Y (%)",
    "roe": "ROE Latest (%)",
    "roe5y": "ROE Avg 5Y (%)",
    "roce": "ROCE Latest (%)",
    "roce5y": "ROCE Avg 5Y (%)",
    "debt_equity": "Debt/Equity",
    "pledged": "Promoter Pledged (%)",
    "ocf_years": "Positive OCF Years (last 5)",
}
THRESHOLDS = {
    "market_cap": ("min", 1000),
    "sales_cagr": ("min", 10),
    "profit_cagr": ("min", 12),
    "roe": ("min", 15),
    "roe5y": ("min", 15),
    "roce": ("min", 18),
    "roce5y": ("min", 15),
    "debt_equity": ("max", 0.5),
    "pledged": ("max", 5),
    "ocf_years": ("min", 3),
}


def now_ist() -> str:
    return pd.Timestamp.now(tz=TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def get_sheet():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not raw or not sheet_id:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SHEET_ID GitHub secret.")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def ensure_worksheets(book):
    for title, headers in SHEETS.items():
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=title, rows=2000, cols=max(20, len(headers)))
        existing = ws.row_values(1)
        if existing != headers:
            ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
    try:
        cfg = book.worksheet("Read Me")
    except gspread.WorksheetNotFound:
        cfg = book.add_worksheet(title="Read Me", rows=30, cols=2)
        cfg.update(range_name="A1:B1", values=[["Topic", "Instructions"]])
        cfg.update(range_name="A2:B10", values=[
            ["Purpose", "Daily Nifty 500 quality + momentum screener. Research only; no orders are placed."],
            ["Fundamentals", "Maintain/import verified data in Fundamentals. Missing required data fails the quality gate."],
            ["Prices", "Yahoo Finance via yfinance is unofficial, best-effort and may throttle/change."],
            ["Quality gates", "Market cap >= ₹1,000 Cr; sales CAGR >=10%; profit CAGR >=12%; ROE >=15%; 5Y avg ROE >=15%; ROCE >=18%; 5Y avg ROCE >=15%; D/E <=0.5; pledged <=5%; positive OCF >=3/5 years."],
            ["Scoring", "Combined = 40% quality + 40% momentum percentile + 20% trend/risk score."],
            ["Portfolio", "Top 20 entry candidates; existing holdings can be retained to rank 30 unless quality fails."],
            ["No-cost note", "Public GitHub repository code is visible to everyone. Store secrets only in GitHub Actions Secrets."],
            ["Backtest caveat", "Not a point-in-time backtest; survivorship bias, delistings, corporate actions, taxes and slippage are not fully modeled."],
            ["Trigger", "Scheduled runs use UTC and may be delayed by GitHub. Use Actions > Run workflow for a manual run."]
        ])


def read_tab(book, title):
    ws = book.worksheet(title)
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


def write_tab(book, title, headers, rows):
    ws = book.worksheet(title)
    # Batch a whole output to reduce API calls; clear then write.
    ws.clear()
    data = [headers] + [[cell_value(v) for v in row] for row in rows]
    if len(data) > 1:
        ws.update(range_name="A1", values=data, value_input_option="USER_ENTERED")
    else:
        ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")


def cell_value(v):
    if v is None:
        return ""
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return float(v) if np.isfinite(v) else ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def num(v):
    try:
        if v is None or str(v).strip() == "":
            return np.nan
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return np.nan


def log_error(book, level, symbol, message, details=""):
    try:
        ws = book.worksheet("Error Log")
        ws.append_row([now_ist(), level, symbol, message, str(details)[:1500]], value_input_option="USER_ENTERED")
    except Exception:
        log.exception("Could not write Error Log row: %s / %s", message, details)


def get_universe(book):
    # Try current public Nifty Indices CSV. Preserve sheet fallback if endpoint is unavailable.
    try:
        response = requests.get(
            NIFTY500_URL, timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}
        )
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.content.decode("utf-8-sig")))
        cols = {str(c).strip().lower(): c for c in df.columns}
        symbol_col = next((cols[k] for k in ("symbol", "ticker") if k in cols), None)
        company_col = next((cols[k] for k in ("company name", "company", "name") if k in cols), None)
        if symbol_col is None or len(df) < 100:
            raise ValueError("Unexpected Nifty 500 CSV schema or too few rows.")
        out = pd.DataFrame({
            "Symbol": df[symbol_col].astype(str).str.strip().str.upper(),
            "Company": df[company_col].astype(str).str.strip() if company_col else "",
        })
        out = out[out["Symbol"].ne("") & out["Symbol"].ne("NAN")].drop_duplicates("Symbol")
        out["Yahoo Symbol"] = out["Symbol"] + ".NS"
        out["Active"] = True
        out["Source"] = "Nifty Indices public CSV"
        out["Last Updated"] = now_ist()
        write_tab(book, "Universe", SHEETS["Universe"], out[SHEETS["Universe"]].values.tolist())
        log.info("Loaded %d symbols from Nifty Indices CSV", len(out))
        return out[["Symbol", "Company", "Yahoo Symbol"]].to_dict("records")
    except Exception as exc:
        log.warning("Universe CSV unavailable; trying existing Universe sheet: %s", exc)
        log_error(book, "WARNING", "", "Universe CSV import failed; using sheet fallback", exc)
        old = read_tab(book, "Universe")
        if old.empty or "Symbol" not in old:
            raise RuntimeError("Universe CSV failed and Universe sheet has no fallback symbols.") from exc
        active = old.get("Active", pd.Series(["TRUE"] * len(old))).astype(str).str.lower().isin(["true", "1", "yes", "y"])
        old = old[active]
        return [{
            "Symbol": str(r["Symbol"]).strip().upper(),
            "Company": str(r.get("Company", "")),
            "Yahoo Symbol": str(r.get("Yahoo Symbol", "")).strip() or str(r["Symbol"]).strip().upper() + ".NS",
        } for _, r in old.iterrows() if str(r.get("Symbol", "")).strip()]


def parse_fundamentals(book):
    df = read_tab(book, "Fundamentals")
    if df.empty or "Symbol" not in df:
        return {}
    result = {}
    for _, row in df.iterrows():
        symbol = str(row.get("Symbol", "")).strip().upper()
        if not symbol:
            continue
        f = {key: num(row.get(col, "")) for key, col in FUNDAMENTAL_KEYS.items()}
        f["company"] = str(row.get("Company", ""))
        missing = [k for k, v in f.items() if k in FUNDAMENTAL_KEYS and not np.isfinite(v)]
        passed = not missing
        if passed:
            for key, (op, threshold) in THRESHOLDS.items():
                val = f[key]
                if (op == "min" and val < threshold) or (op == "max" and val > threshold):
                    passed = False
        # Weighted transparent quality score; component scores are capped at 0..100.
        if not missing:
            components = [
                min(100, max(0, f["market_cap"] / 10000 * 100)),
                min(100, max(0, f["sales_cagr"] / 25 * 100)),
                min(100, max(0, f["profit_cagr"] / 30 * 100)),
                min(100, max(0, f["roe"] / 25 * 100)),
                min(100, max(0, f["roe5y"] / 25 * 100)),
                min(100, max(0, f["roce"] / 30 * 100)),
                min(100, max(0, f["roce5y"] / 30 * 100)),
                min(100, max(0, (1 - f["debt_equity"]) * 100)),
                min(100, max(0, (1 - f["pledged"] / 10) * 100)),
                min(100, max(0, f["ocf_years"] / 5 * 100)),
            ]
            weights = [0.05, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.15]
            score = round(float(np.dot(components, weights)), 2)
        else:
            score = 0.0
        f.update({"pass": passed, "score": score, "missing": ", ".join(missing)})
        result[symbol] = f
    return result


def download_history(tickers):
    """Download adjusted daily history in batches; returns per-symbol DataFrames."""
    histories = {}
    all_tickers = list(dict.fromkeys(tickers + [BENCHMARK]))
    for start in range(0, len(all_tickers), 60):
        batch = all_tickers[start:start + 60]
        try:
            data = yf.download(
                tickers=batch, period="3y", interval="1d",
                auto_adjust=True, group_by="ticker", threads=True,
                progress=False, timeout=25
            )
            if data is None or data.empty:
                raise RuntimeError("Empty result from Yahoo Finance.")
            for ticker in batch:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        if ticker not in data.columns.get_level_values(0):
                            # yfinance versions sometimes invert the column levels.
                            if ticker in data.columns.get_level_values(-1):
                                sub = data.xs(ticker, axis=1, level=-1)
                            else:
                                continue
                        else:
                            sub = data[ticker]
                    else:
                        if len(batch) != 1:
                            continue
                        sub = data
                    sub = sub.dropna(subset=["Close"])
                    if len(sub) >= 205:
                        histories[ticker] = sub
                except Exception as exc:
                    log.warning("Could not parse price data for %s: %s", ticker, exc)
        except Exception as exc:
            log.warning("Yahoo batch failed (%s): %s", batch[0], exc)
        time.sleep(0.5)
    return histories


def return_pct(series, bars):
    if len(series) <= bars:
        return np.nan
    base = float(series.iloc[-1 - bars])
    return (float(series.iloc[-1]) / base - 1) * 100 if base > 0 else np.nan


def percentile_map(df, source_col, target_col):
    if df.empty:
        df[target_col] = []
        return
    ranks = df[source_col].rank(method="average", pct=True)
    df[target_col] = (ranks * 100).round(2)


def make_rankings(universe, fundamentals, histories):
    benchmark_hist = histories.get(BENCHMARK)
    benchmark_12_1 = np.nan
    if benchmark_hist is not None:
        benchmark_12_1 = return_pct(benchmark_hist["Close"], 252)  # 12-month benchmark proxy
        # 12-1 benchmark return excludes the most recent 21 sessions.
        close = benchmark_hist["Close"]
        benchmark_12_1 = (float(close.iloc[-22]) / float(close.iloc[-253]) - 1) * 100 if len(close) > 253 else np.nan

    rows = []
    errors = []
    for item in universe:
        sym = item["Symbol"]
        ticker = item["Yahoo Symbol"]
        hist = histories.get(ticker)
        if hist is None:
            hist = histories.get(sym + ".NS")
        f = fundamentals.get(sym)
        if hist is None:
            errors.append((sym, "No usable price history (Yahoo endpoint may throttle or symbol mapping may differ)."))
            continue
        close = hist["Close"].dropna()
        if len(close) < 253:
            errors.append((sym, "Insufficient history for 12-1 momentum."))
            continue
        last = float(close.iloc[-1])
        sma50 = float(close.tail(50).mean())
        sma200 = float(close.tail(200).mean())
        ret1m = return_pct(close, 21)
        ret6m = return_pct(close, 126)
        ret12m = return_pct(close, 252)
        # 12-1 momentum = return from ~12 months ago to ~1 month ago.
        ret12_1m = (float(close.iloc[-22]) / float(close.iloc[-253]) - 1) * 100
        relative = ret12_1m - benchmark_12_1 if np.isfinite(benchmark_12_1) else np.nan
        f = f or {"pass": False, "score": 0, "missing": "No fundamentals row", "company": ""}
        momentum_raw = (0.45 * ret12_1m + 0.25 * ret6m + 0.30 * relative
                        if np.isfinite(relative) else 0.70 * ret12_1m + 0.30 * ret6m)
        risk_raw = (40 if last > sma200 else 0) + (40 if sma50 > sma200 else 0) + (20 if np.isfinite(ret1m) and ret1m > -15 else 0)
        rows.append({
            "Symbol": sym, "Company": item.get("Company") or f.get("company", ""),
            "Price Date": pd.Timestamp(hist.index[-1]).strftime("%Y-%m-%d"),
            "Close": round(last, 2), "Return 1M (%)": round(ret1m, 2) if np.isfinite(ret1m) else "",
            "Return 6M (%)": round(ret6m, 2) if np.isfinite(ret6m) else "",
            "Return 12M (%)": round(ret12m, 2) if np.isfinite(ret12m) else "",
            "12-1M Momentum (%)": round(ret12_1m, 2),
            "Relative Strength vs Nifty 500 (%)": round(relative, 2) if np.isfinite(relative) else "",
            "Close > 200 DMA": bool(last > sma200), "50 DMA > 200 DMA": bool(sma50 > sma200),
            "Quality Score": f["score"], "Quality Pass": bool(f["pass"]),
            "Missing Required Fields": f.get("missing", ""),
            "_momentum_raw": momentum_raw, "_risk_raw": risk_raw,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df, errors
    eligible = df[df["Quality Pass"]].copy()
    percentile_map(eligible, "_momentum_raw", "Momentum Score")
    percentile_map(eligible, "_risk_raw", "Risk Score")
    score_lookup = eligible.set_index("Symbol")[["Momentum Score", "Risk Score"]].to_dict("index")
    df["Momentum Score"] = df["Symbol"].map(lambda s: score_lookup.get(s, {}).get("Momentum Score", 0))
    df["Risk Score"] = df["Symbol"].map(lambda s: score_lookup.get(s, {}).get("Risk Score", 0))
    df["Combined Score"] = np.where(
        df["Quality Pass"],
        0.40 * df["Quality Score"] + 0.40 * df["Momentum Score"] + 0.20 * df["Risk Score"],
        0.0,
    ).round(2)
    df = df.sort_values(["Quality Pass", "Combined Score"], ascending=[False, False]).reset_index(drop=True)
    df["Rank"] = ""
    eligible_idx = df.index[df["Quality Pass"]]
    for rank, idx in enumerate(eligible_idx, start=1):
        df.loc[idx, "Rank"] = rank
    df["Entry Candidate"] = [
        bool(row["Quality Pass"] and row["Rank"] != "" and int(row["Rank"]) <= TOP_N and float(row["Close"]) >= 20)
        for _, row in df.iterrows()
    ]
    return df, errors


def update_outputs(book, universe, fundamentals, rankings, errors, price_success):
    old_port = read_tab(book, "Portfolio")
    existing = {}
    if not old_port.empty and "Symbol" in old_port:
        for _, row in old_port.iterrows():
            sym = str(row.get("Symbol", "")).strip().upper()
            if sym:
                existing[sym] = row.to_dict()

    rank_by_symbol = {}
    if not rankings.empty:
        rank_by_symbol = {r["Symbol"]: r for _, r in rankings.iterrows()}

    ranking_rows = []
    for _, r in rankings.iterrows():
        sym = r["Symbol"]
        old = existing.get(sym, {})
        rank = int(r["Rank"]) if str(r["Rank"]).strip() else None
        held = sym in existing
        quality_pass = bool(r["Quality Pass"])
        if held and not quality_pass:
            status, decision = "EXIT REVIEW", "EXIT REVIEW (quality fail)"
        elif held and rank is not None and rank <= RETAIN_BUFFER:
            status, decision = "HOLD", "RETAIN (rank buffer)"
        elif held:
            status, decision = "EXIT REVIEW", "EXIT REVIEW (below rank buffer)"
        elif bool(r["Entry Candidate"]):
            status, decision = "CANDIDATE", "ENTRY CANDIDATE"
        else:
            status, decision = "", "WATCH" if quality_pass else "FAIL QUALITY / MISSING DATA"
        ranking_rows.append([
            rank if rank is not None else "", sym, r["Company"], r["Price Date"], r["Close"],
            r["Return 1M (%)"], r["Return 6M (%)"], r["Return 12M (%)"],
            r["12-1M Momentum (%)"], r["Relative Strength vs Nifty 500 (%)"],
            r["Close > 200 DMA"], r["50 DMA > 200 DMA"], r["Quality Score"],
            r["Momentum Score"], r["Risk Score"], r["Combined Score"], quality_pass,
            bool(r["Entry Candidate"]), status, decision
        ])

    # Update derived columns in Fundamentals without changing source inputs.
    fund_df = read_tab(book, "Fundamentals")
    fund_rows = []
    if not fund_df.empty:
        for _, row in fund_df.iterrows():
            sym = str(row.get("Symbol", "")).strip().upper()
            f = fundamentals.get(sym, {})
            fund_rows.append([
                row.get("Symbol", ""), row.get("Company", ""), row.get("Market Cap (₹ Cr)", ""),
                row.get("Sales CAGR 5Y (%)", ""), row.get("Profit CAGR 5Y (%)", ""),
                row.get("ROE Latest (%)", ""), row.get("ROE Avg 5Y (%)", ""),
                row.get("ROCE Latest (%)", ""), row.get("ROCE Avg 5Y (%)", ""),
                row.get("Debt/Equity", ""), row.get("Promoter Pledged (%)", ""),
                row.get("Positive OCF Years (last 5)", ""), row.get("Fundamental As Of", ""),
                row.get("Source / Notes", ""), f.get("score", 0), bool(f.get("pass", False)), f.get("missing", "No fundamentals row")
            ])
    write_tab(book, "Fundamentals", SHEETS["Fundamentals"], fund_rows)

    # Preserve user-entered share count/cost/notes and maintain held names plus current top-20 candidates.
    chosen = set(existing.keys())
    if not rankings.empty:
        candidates = rankings[rankings["Entry Candidate"]].head(TOP_N)["Symbol"].tolist()
        chosen.update(candidates)
    portfolio_rows = []
    for sym in sorted(chosen):
        r = rank_by_symbol.get(sym, {})
        old = existing.get(sym, {})
        rank = int(r["Rank"]) if r and str(r.get("Rank", "")).strip() else ""
        price = num(r.get("Close")) if r else np.nan
        shares = num(old.get("Shares", ""))
        cost = num(old.get("Average Cost", ""))
        mv = shares * price if np.isfinite(shares) and np.isfinite(price) else np.nan
        pl = mv - shares * cost if np.isfinite(mv) and np.isfinite(cost) else np.nan
        pl_pct = (pl / (shares * cost) * 100) if np.isfinite(pl) and np.isfinite(shares) and np.isfinite(cost) and shares > 0 and cost > 0 else np.nan
        if r and sym in existing:
            if not bool(r["Quality Pass"]):
                status = "EXIT REVIEW"
            elif rank != "" and rank <= RETAIN_BUFFER:
                status = "HOLD"
            else:
                status = "EXIT REVIEW"
        elif r:
            status = "TOP 20 CANDIDATE" if bool(r["Entry Candidate"]) else "WATCH"
        else:
            status = "NO CURRENT PRICE"
        portfolio_rows.append([
            sym, r.get("Company", old.get("Company", "")) if r else old.get("Company", ""),
            status, rank, 5, old.get("Shares", ""), old.get("Average Cost", ""),
            price if np.isfinite(price) else "", mv if np.isfinite(mv) else "",
            pl if np.isfinite(pl) else "", pl_pct if np.isfinite(pl_pct) else "",
            old.get("Added On", ""), now_ist(), old.get("Notes", "")
        ])
    write_tab(book, "Ranking", SHEETS["Ranking"], ranking_rows)
    write_tab(book, "Portfolio", SHEETS["Portfolio"], portfolio_rows)

    for sym, message in errors:
        log_error(book, "WARNING", sym, message)
    passed = int(rankings["Quality Pass"].sum()) if not rankings.empty else 0
    candidates = int(rankings["Entry Candidate"].sum()) if not rankings.empty else 0
    retained = sum(1 for sym, old in existing.items()
                   if sym in rank_by_symbol and bool(rank_by_symbol[sym]["Quality Pass"])
                   and str(rank_by_symbol[sym]["Rank"]).strip()
                   and int(rank_by_symbol[sym]["Rank"]) <= RETAIN_BUFFER)
    hist_ws = book.worksheet("Run History")
    hist_ws.append_row([
        now_ist(), len(universe), price_success, passed, len(rankings),
        candidates, retained, "OK" if len(rankings) else "NO DATA",
        f"benchmark={BENCHMARK}; price source=Yahoo/yfinance; fundamentals=manual/imported; warnings={len(errors)}"
    ], value_input_option="USER_ENTERED")


def main():
    book = get_sheet()
    ensure_worksheets(book)
    log.info("Starting daily screener")
    universe = get_universe(book)
    if not universe:
        raise RuntimeError("No active universe symbols.")
    fundamentals = parse_fundamentals(book)
    tickers = [u["Yahoo Symbol"] for u in universe]
    histories = download_history(tickers)
    price_success = sum(1 for u in universe if u["Yahoo Symbol"] in histories)
    coverage = price_success / max(1, len(universe))
    if coverage < MIN_COVERAGE:
        msg = f"Price coverage {coverage:.1%} below required {MIN_COVERAGE:.1%}; refusing to publish possibly misleading rankings."
        log_error(book, "ERROR", "", msg, f"{price_success}/{len(universe)} symbols fetched")
        raise RuntimeError(msg)
    rankings, errors = make_rankings(universe, fundamentals, histories)
    update_outputs(book, universe, fundamentals, rankings, errors, price_success)
    log.info("Finished: %d/%d price histories; %d ranked; %d quality-pass",
             price_success, len(universe), len(rankings),
             int(rankings["Quality Pass"].sum()) if not rankings.empty else 0)


if __name__ == "__main__":
    main()
