#!/usr/bin/env python3
"""
Daily SPY SMA Telegram reporter (updated formatting).

Sets:
  - Top-level logic all inside main()
  - Robust price-column detection
  - SMA50 and SMA250 computed inside main()
  - UNRATE optional via UNRATE_SOURCE env var
  - Message formatting adjusted per user request

Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID required.
  Optional: SPY_SOURCE, UNRATE_SOURCE, DEBUG=1
"""

from __future__ import annotations
import os
import sys
import io
from datetime import datetime
from dateutil import parser as dateparser

import pandas as pd
import requests

DEFAULT_SPY_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"


def _read_csv_flex(path_or_url: str) -> pd.DataFrame:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        r = requests.get(path_or_url, timeout=30)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))
    return pd.read_csv(path_or_url)


def _find_price_col(df: pd.DataFrame) -> str | None:
    candidates = [c for c in df.columns if c.lower() in ("close", "adj close", "adjusted close", "price", "spy")]
    if candidates:
        return candidates[0]
    numeric_cols = df.select_dtypes("number").columns.tolist()
    if len(numeric_cols) == 1:
        return numeric_cols[0]
    for alt in ("Close", "close", "PRICE"):
        if alt in df.columns:
            return alt
    return None


def _ensure_date_index(df: pd.DataFrame, date_cols_possible=None) -> pd.DataFrame:
    if date_cols_possible is None:
        date_cols_possible = ["Date", "date", "DATE", "DATE_TIME", "timestamp"]
    if isinstance(df.index, pd.DatetimeIndex):
        return df.sort_index()
    for c in date_cols_possible:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
            df = df.set_index(c).sort_index()
            return df
    # try first column
    first = df.columns[0]
    parsed = pd.to_datetime(df[first], errors="coerce")
    if parsed.notna().sum() > 0:
        df = df.set_index(parsed)
        df.index.name = None
        return df.sort_index()
    raise RuntimeError(f"Unable to find/parse a date column in CSV. Columns: {list(df.columns)}")


def _count_consecutive(values, predicate) -> int:
    cnt = 0
    for v in reversed(values):
        try:
            ok = predicate(v)
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
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    SPY_SOURCE = os.getenv("SPY_SOURCE", DEFAULT_SPY_URL)
    UNRATE_SOURCE = os.getenv("UNRATE_SOURCE", "")
    DEBUG = os.getenv("DEBUG", "0") == "1"

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment.")

    # Load SPY
    df = _read_csv_flex(SPY_SOURCE)
    df = _ensure_date_index(df)

    price_col = _find_price_col(df)
    if price_col is None:
        raise RuntimeError(f"No price/close column found in SPY data. Columns: {list(df.columns)}")
    if price_col != "Close":
        df = df.rename(columns={price_col: "Close"})

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise RuntimeError("SPY data contains no valid Close values.")

    # Compute SMAs inside main
    df["SMA50"] = df["Close"].rolling(window=50, min_periods=1).mean()
    df["SMA250"] = df["Close"].rolling(window=250, min_periods=1).mean()

    # Load UNRATE (optional)
    unrate_df = None
    unrate_latest = None
    unrate_prior_date = None
    unrate_prior_val = None
    unrate_3mo_change = None
    if UNRATE_SOURCE:
        try:
            u = _read_csv_flex(UNRATE_SOURCE)
            # find date col
            date_candidates = [c for c in u.columns if "date" in c.lower()]
            date_col = date_candidates[0] if date_candidates else u.columns[0]
            u[date_col] = pd.to_datetime(u[date_col], errors="coerce")
            u = u.set_index(date_col).sort_index()
            # find numeric col
            num_cols = u.select_dtypes("number").columns.tolist()
            if num_cols:
                rate_col = num_cols[0]
            else:
                rate_cands = [c for c in u.columns if c.lower() in ("value", "unrate", "rate", "observed_value")]
                rate_col = rate_cands[0] if rate_cands else None
            if rate_col is None:
                raise RuntimeError("UNRATE file has no numeric column for rate.")
            u[rate_col] = pd.to_numeric(u[rate_col], errors="coerce")
            unrate_df = u[[rate_col]].dropna()
            if not unrate_df.empty:
                unrate_latest = float(unrate_df.iloc[-1, 0])
                # find 3-month prior value by date
                cutoff = unrate_df.index[-1] - pd.DateOffset(months=3)
                prior = unrate_df[unrate_df.index <= cutoff]
                if not prior.empty:
                    unrate_prior_val = float(prior.iloc[-1, 0])
                    unrate_prior_date = prior.index[-1]
                    unrate_3mo_change = unrate_latest - unrate_prior_val
                else:
                    unrate_prior_val = float(unrate_df.iloc[0, 0])
                    unrate_prior_date = unrate_df.index[0]
                    unrate_3mo_change = unrate_latest - unrate_prior_val
        except Exception as e:
            if DEBUG:
                print("UNRATE load/parsing error:", e, file=sys.stderr)
            unrate_df = None

    # Latest trading row
    latest = df.iloc[-1]
    latest_date = latest.name if hasattr(latest, "name") else df.index[-1]
    latest_close = float(latest["Close"])

    # Strategy 1 (MA50)
    series_sma50 = df["Close"] > df["SMA50"]
    is_above_50 = bool(series_sma50.iloc[-1])
    streak_above_50 = _count_consecutive(series_sma50.values, lambda v: bool(v))
    streak_below_50 = _count_consecutive(series_sma50.values, lambda v: not bool(v))
    exit_20 = streak_below_50 >= 20
    reentry_5 = streak_above_50 >= 5

    # Strategy 2 (MA250)
    series_sma250 = df["Close"] > df["SMA250"]
    is_above_250 = bool(series_sma250.iloc[-1])
    streak_above_250 = _count_consecutive(series_sma250.values, lambda v: bool(v))
    streak_below_250 = _count_consecutive(series_sma250.values, lambda v: not bool(v))
    exit_80 = streak_below_250 >= 80
    reentry_5_250 = streak_above_250 >= 5

    # Decide safe asset for MA50 if exit
    strat1_safe_asset = "N/A (no exit signal)"
    if exit_20:
        if (unrate_3mo_change is not None) and (unrate_3mo_change > 0.3):
            strat1_safe_asset = "Treasuries"
        else:
            strat1_safe_asset = "SP500"

    # Compose message with requested formatting changes
    lines = []
    lines.append("ROTH ACCOUNT")
    lines.append(f"SPY CLOSE {format_money(latest_close)}  {pd.to_datetime(latest_date).strftime('%Y-%m-%d')}")
    lines.append("")

    # MA50 block - include exit/reentry trigger lines and UNRATE data as requested
    lines.append("MA50_E20_R5 (Top-K)")
    lines.append(f"  SMA50 = {format_money(float(latest['SMA50']))}")
    lines.append(f"  Status: {'ABOVE' if is_above_50 else 'BELOW'} SMA50")
    lines.append(f"  Above streak: {streak_above_50} days; Below streak: {streak_below_50} days")
    lines.append(f"  EXIT trigger (≥20 days below SMA50): {'YES' if exit_20 else 'NO'}")
    lines.append(f"  REENTRY trigger (≥5 days above SMA50): {'YES' if reentry_5 else 'NO'}")

    # UNRATE details (re-add like original)
    if unrate_latest is not None:
        # format prior date
        prior_date_str = pd.to_datetime(unrate_prior_date).strftime('%Y-%m-%d') if unrate_prior_date is not None else "N/A"
        change_str = f"{unrate_3mo_change:+.2f} pp" if unrate_3mo_change is not None else "N/A"
        rising_flag = "ON" if (unrate_3mo_change is not None and unrate_3mo_change > 0.3) else "OFF"
        lines.append("")
        lines.append("  UNRATE (FRED, monthly; no lag)")
        lines.append(f"    UNRATE as-of date: {pd.to_datetime(unrate_df.index[-1]).strftime('%Y-%m-%d')}  → {unrate_latest:.1f}%")
        lines.append(f"    UNRATE 3-mo prior: {prior_date_str}  → {unrate_prior_val:.1f}%")
        lines.append(f"    UNRATE 3-mo change: {unrate_3mo_change:+.2f} pp")
        lines.append(f"    UNRATE rising flag (>0.3 pp): {rising_flag}")
    else:
        lines.append("")
        lines.append("  UNRATE: not provided or failed to load")

    # Replace "what to do today" with compact instruction block comparable to original
    lines.append("")
    lines.append("  If exit signal is YES -> Check UNRATE:")
    lines.append("    \tIf UNRATE 3-mo Δ > 0.3 pp -> Move to Treasuries")
    lines.append("    \tElse -> Move to SP500")
    # also show resolved safe asset for convenience
    lines.append(f"    \tResolved safe asset (if EXIT): {strat1_safe_asset}")
    lines.append("")

    # MA250 block (keep similar formatting, no UNRATE logic here)
    lines.append("MA250_E80_R5 (SP500)")
    lines.append(f"  SMA250 = {format_money(float(latest['SMA250']))}")
    lines.append(f"  Status: {'ABOVE' if is_above_250 else 'BELOW'} SMA250")
    lines.append(f"  Above streak: {streak_above_250} days; Below streak: {streak_below_250} days")
    lines.append(f"  EXIT trigger (≥80 days below SMA250): {'YES' if exit_80 else 'NO'}")
    lines.append(f"  REENTRY trigger (≥5 days above SMA250): {'YES' if reentry_5_250 else 'NO'}")
    lines.append("")
    lines.append("  If exit signal is YES -> Move to Treasuries")
    lines.append("")

    message = "\n".join(lines)

    if DEBUG:
        print("DEBUG: message:\n", message, file=sys.stderr)

    _telegram_send(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)


if __name__ == "__main__":
    main()
