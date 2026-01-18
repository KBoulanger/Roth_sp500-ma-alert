#!/usr/bin/env python3
"""
Daily SPY SMA Telegram reporter.

Requirements:
  pip install pandas requests python-dateutil

Usage:
  Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment (GitHub Actions secrets).
  Optionally set:
    SPY_SOURCE            - URL or local CSV path for SPY daily data (default: Stooq CSV)
    UNRATE_SOURCE         - URL or local CSV path for UNRATE (default: None -> skip UNRATE)
    DEBUG                 - if "1", prints debug info to stdout
"""

from __future__ import annotations
import os
import sys
import io
from datetime import datetime, timedelta
from dateutil import parser as dateparser

import pandas as pd
import requests

# ---------- Configuration defaults ----------
DEFAULT_SPY_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
# Optionally provide UNRATE CSV URL/path via env var UNRATE_SOURCE
# FRED CSVs often have a DATE or observation_date column and a numeric column.
# Example FRED file: https.../UNRATE.csv
# If UNRATE_SOURCE is not provided, UNRATE is skipped.
# --------------------------------------------

def _read_csv_flex(path_or_url: str) -> pd.DataFrame:
    """Read CSV from URL or local path robustly."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        r = requests.get(path_or_url, timeout=30)
        r.raise_for_status()
        s = io.StringIO(r.text)
        return pd.read_csv(s)
    return pd.read_csv(path_or_url)

def _find_price_col(df: pd.DataFrame) -> str | None:
    candidates = [c for c in df.columns if c.lower() in ("close", "adj close", "adjusted close", "price", "spy", "close*")]
    if candidates:
        return candidates[0]
    numeric_cols = df.select_dtypes("number").columns.tolist()
    if len(numeric_cols) == 1:
        return numeric_cols[0]
    # fallback: try common names
    for alt in ("Close", "close", "PRICE"):
        if alt in df.columns:
            return alt
    return None

def _ensure_date_index(df: pd.DataFrame, date_cols_possible=None) -> pd.DataFrame:
    """Ensure df indexed by datetime and sorted ascending."""
    if date_cols_possible is None:
        date_cols_possible = ["Date", "date", "DATE", "DATE_TIME", "timestamp"]
    # If already has DatetimeIndex, keep it
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.sort_index()
        return df
    # Try to find a date column
    for c in date_cols_possible:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
            df = df.set_index(c)
            df = df.sort_index()
            return df
    # try to infer first column as date
    first = df.columns[0]
    try:
        parsed = pd.to_datetime(df[first], errors="coerce")
        if parsed.notna().sum() > 0:
            df = df.set_index(parsed)
            df.index.name = None
            df = df.sort_index()
            return df
    except Exception:
        pass
    raise RuntimeError(f"Unable to find/parse a date column in CSV. Columns: {list(df.columns)}")

def _count_consecutive(series: pd.Series, predicate) -> int:
    """Count consecutive matching predicate values at end of series."""
    # predicate is function taking a value -> bool
    cnt = 0
    for val in reversed(series):
        try:
            ok = predicate(val)
        except Exception:
            ok = False
        if ok:
            cnt += 1
        else:
            break
    return cnt

def _telegram_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text})
    resp.raise_for_status()

def format_money(x: float) -> str:
    return f"{x:,.2f}"

def main():
    # ---- env / inputs ----
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    SPY_SOURCE = os.getenv("SPY_SOURCE", DEFAULT_SPY_URL)
    UNRATE_SOURCE = os.getenv("UNRATE_SOURCE", "")  # optional
    DEBUG = os.getenv("DEBUG", "0") == "1"

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment.")

    # ---- load SPY data ----
    df = _read_csv_flex(SPY_SOURCE)
    df = _ensure_date_index(df)

    # detect price column and standardize
    price_col = _find_price_col(df)
    if price_col is None:
        raise RuntimeError(f"No price/close column found in SPY data. Columns: {list(df.columns)}")
    if price_col != "Close":
        df = df.rename(columns={price_col: "Close"})

    # convert Close to numeric
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise RuntimeError("SPY data contains no valid Close values.")

    # compute SMAs (inside main)
    df["SMA50"] = df["Close"].rolling(window=50, min_periods=1).mean()
    df["SMA250"] = df["Close"].rolling(window=250, min_periods=1).mean()

    # ---- UNRATE (optional) ----
    unrate_df = None
    unrate_3mo_change = None
    unrate_latest = None
    if UNRATE_SOURCE:
        try:
            u = _read_csv_flex(UNRATE_SOURCE)
            # find date col and rate column
            date_candidates = [c for c in u.columns if "date" in c.lower() or c.lower() == "date"]
            if date_candidates:
                date_col = date_candidates[0]
            else:
                date_col = u.columns[0]
            u[date_col] = pd.to_datetime(u[date_col], errors="coerce")
            # find numeric column for rate
            num_cols = u.select_dtypes("number").columns.tolist()
            if len(num_cols) >= 1:
                rate_col = num_cols[0]
            else:
                # try common name
                rate_candidates = [c for c in u.columns if c.lower() in ("value", "unrate", "rate", "observed_value")]
                rate_col = rate_candidates[0] if rate_candidates else None
            if rate_col is None:
                raise RuntimeError("UNRATE file has no numeric column for rate.")
            u = u.set_index(date_col).sort_index()
            u[rate_col] = pd.to_numeric(u[rate_col], errors="coerce")
            unrate_df = u[[rate_col]].dropna()
            # compute 3-month change using last available month
            unrate_latest = float(unrate_df.iloc[-1, 0])
            # find value roughly 3 months prior: get index - 90 days
            cutoff = unrate_df.index[-1] - pd.DateOffset(months=3)
            prior = unrate_df[unrate_df.index <= cutoff]
            if not prior.empty:
                prior_val = float(prior.iloc[-1, 0])
                unrate_3mo_change = unrate_latest - prior_val
            else:
                # fallback: difference between last and first of file if short
                unrate_3mo_change = unrate_latest - float(unrate_df.iloc[0, 0])
        except Exception as e:
            # non-fatal: keep unrate info as None
            if DEBUG:
                print("UNRATE load error:", e, file=sys.stderr)
            unrate_df = None

    # ---- get latest trading row ----
    latest = df.iloc[-1]
    latest_date = latest.name if hasattr(latest, "name") else df.index[-1]
    latest_close = float(latest["Close"])

    # ---- Strategy computations ----
    # Strategy 1: MA50 / Exit-20 / Reentry-5, UNRATE decision
    series_sma50 = df["Close"] > df["SMA50"]
    above_50 = bool(series_sma50.iloc[-1])
    streak_above_50 = _count_consecutive(series_sma50.values, lambda v: bool(v))
    streak_below_50 = _count_consecutive(series_sma50.values, lambda v: not bool(v))
    exit_20 = streak_below_50 >= 20
    reentry_5 = streak_above_50 >= 5

    # Strategy 2: MA250 / Exit-80 / Reentry-5, treasuries only safe asset
    series_sma250 = df["Close"] > df["SMA250"]
    above_250 = bool(series_sma250.iloc[-1])
    streak_above_250 = _count_consecutive(series_sma250.values, lambda v: bool(v))
    streak_below_250 = _count_consecutive(series_sma250.values, lambda v: not bool(v))
    exit_80 = streak_below_250 >= 80
    reentry_5_250 = streak_above_250 >= 5

    # Decision for Strategy 1's safe asset when exit triggered:
    # If UNRATE rising > 0.3 percentage points over 3 months -> Treasuries else SP500
    strat1_safe = "SP500"
    if exit_20:
        if unrate_3mo_change is not None and unrate_3mo_change > 0.3:
            strat1_safe = "Treasuries"
        else:
            strat1_safe = "SP500"

    # Compose "what to do" texts
    def action_text_ma50():
        if exit_20:
            return f"EXIT triggered (≥20 days below SMA50). Move to {strat1_safe}."
        if reentry_5:
            return "REENTRY triggered (≥5 days above SMA50). Re-enter SP500 exposure."
        return "No action."

    def action_text_ma250():
        if exit_80:
            return "EXIT triggered (≥80 days below SMA250). Move to Treasuries."
        if reentry_5_250:
            return "REENTRY triggered (≥5 days above SMA250). Re-enter SP500 exposure."
        return "No action."

    # ---- Build message ----
    lines = []
    lines.append("ROTH ACCOUNT")
    lines.append(f"SPY {format_money(latest_close)}  {pd.to_datetime(latest_date).strftime('%Y-%m-%d')}")
    lines.append("")

    # MA50 block
    lines.append("MA50_E20_R5 (Top-K)")
    lines.append(f"  SMA50 = {format_money(float(latest['SMA50']))}")
    lines.append(f"  Status: {'ABOVE' if above_50 else 'BELOW'} SMA50")
    lines.append(f"  Above streak: {streak_above_50} days; Below streak: {streak_below_50} days")
    if unrate_latest is not None:
        lines.append(f"  UNRATE = {unrate_latest:.2f}%; 3-mo Δ = {unrate_3mo_change:+.2f} pp")
        lines.append(f"  UNRATE rising > 0.3 pp? {'YES' if (unrate_3mo_change is not None and unrate_3mo_change>0.3) else 'NO'}")
    lines.append(f"  What to do today: {action_text_ma50()}")
    lines.append("")

    # MA250 block
    lines.append("MA250_E80_R5 (SP500)")
    lines.append(f"  SMA250 = {format_money(float(latest['SMA250']))}")
    lines.append(f"  Status: {'ABOVE' if above_250 else 'BELOW'} SMA250")
    lines.append(f"  Above streak: {streak_above_250} days; Below streak: {streak_below_250} days")
    lines.append(f"  What to do today: {action_text_ma250()}")
    lines.append("")

    message = "\n".join(lines)

    if DEBUG:
        print("DEBUG: message:\n", message, file=sys.stderr)

    # ---- send Telegram ----
    _telegram_send(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)

if __name__ == "__main__":
    main()
