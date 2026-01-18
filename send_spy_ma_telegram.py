import os
import pandas as pd
import requests
from datetime import datetime

# ============================
# Data sources
# ============================

STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
# FRED API endpoint for UNRATE (more reliable than CSV export)
FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

ACCOUNT_LABEL = "ROTH ACCOUNT"


# ============================
# Helpers
# ============================

def fetch_spy_history() -> pd.DataFrame:
    df = pd.read_csv(STOOQ_CSV_URL)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df[["Close"]].dropna()


def fetch_unrate_fred_api(api_key: str) -> pd.DataFrame:
    """
    Fetch UNRATE from FRED API (more reliable than CSV scraping).
    Requires FRED_API_KEY environment variable.
    https://fred.stlouisfed.org/series/UNRATE
    """
    params = {
        "series_id": "UNRATE",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 12,  # Get last 12 months of data
    }
    resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    observations = data.get("observations", [])
    records = []
    for obs in observations:
        date_str = obs.get("date")
        value_str = obs.get("value")
        if date_str and value_str and value_str != ".":
            records.append({
                "DATE": pd.to_datetime(date_str),
                "UNRATE": float(value_str)
            })

    df = pd.DataFrame(records)
    df = df.sort_values("DATE").reset_index(drop=True)
    return df


def fetch_unrate_fallback() -> pd.DataFrame:
    """
    Fallback: fetch UNRATE from FRED CSV if no API key available.
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"
    df = pd.read_csv(url)

    # Handle both DATE and observation_date column names
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "DATE"})

    df["DATE"] = pd.to_datetime(df["DATE"])
    df["UNRATE"] = pd.to_numeric(df["UNRATE"], errors="coerce")
    df = df.dropna().sort_values("DATE").reset_index(drop=True)

    # Return only last 12 months
    return df.tail(12).reset_index(drop=True)


def fetch_unrate() -> pd.DataFrame:
    """
    Try FRED API first, fall back to CSV scraping.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if api_key:
        try:
            return fetch_unrate_fred_api(api_key)
        except Exception as e:
            print(f"FRED API failed: {e}, falling back to CSV")

    return fetch_unrate_fallback()


def count_streak(series: pd.Series) -> int:
    """Count consecutive True values from the end of the series."""
    c = 0
    for v in reversed(series.tolist()):
        if bool(v):
            c += 1
        else:
            break
    return c


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    requests.post(url, json=payload, timeout=30)


# ============================
# Main
# ============================

def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    # --- Load data ---
    df = fetch_spy_history()
    un = fetch_unrate()

    latest = df.iloc[-1]
    latest_date = latest.name.strftime("%Y-%m-%d")
    latest_close = latest["Close"]

    # ============================
    # MA50_E20_R5 (Top-5 Strategy)
    # ============================

    df["SMA50"] = df["Close"].rolling(50).mean()
    sma50_value = df["SMA50"].iloc[-1]

    # Current status
    above_50 = latest_close >= sma50_value
    below_50 = latest_close < sma50_value

    # Calculate streaks (using all data including today)
    df["above_sma50"] = df["Close"] >= df["SMA50"]
    df["below_sma50"] = df["Close"] < df["SMA50"]

    above_streak_50 = count_streak(df["above_sma50"])
    below_streak_50 = count_streak(df["below_sma50"])

    # Triggers
    exit_50 = below_streak_50 >= 20
    reentry_50 = above_streak_50 >= 5

    # UNRATE logic (3-month comparison)
    un_now = un.iloc[-1]
    # 3 months prior = 3 rows back in monthly data
    un_prior = un.iloc[-4] if len(un) >= 4 else un.iloc[0]

    un_now_date = un_now["DATE"].strftime("%Y-%m-%d")
    un_now_rate = un_now["UNRATE"]
    un_prior_date = un_prior["DATE"].strftime("%Y-%m-%d")
    un_prior_rate = un_prior["UNRATE"]
    un_chg = un_now_rate - un_prior_rate
    un_flag = un_chg > 0.3

    # ============================
    # MA250_E80_R5 (SP500 holdings)
    # ============================

    df["SMA250"] = df["Close"].rolling(250).mean()
    sma250_value = df["SMA250"].iloc[-1]

    above_250 = latest_close >= sma250_value
    below_250 = latest_close < sma250_value

    df["above_sma250"] = df["Close"] >= df["SMA250"]
    df["below_sma250"] = df["Close"] < df["SMA250"]

    above_streak_250 = count_streak(df["above_sma250"])
    below_streak_250 = count_streak(df["below_sma250"])

    exit_250 = below_streak_250 >= 80
    reentry_250 = above_streak_250 >= 5

    # ============================
    # Determine safe asset for Top-5 (only relevant if exit triggered)
    # ============================
    if exit_50:
        if un_flag:
            safe_asset = "Treasuries"
        else:
            safe_asset = "SP500"
    else:
        safe_asset = "N/A (no exit signal)"

    # ============================
    # Build Telegram message (matching Word format)
    # ============================

    lines = []

    # Header
    lines.append(f"<b>{ACCOUNT_LABEL}</b>")
    lines.append(f"Date:{latest_date}")
    lines.append(f"SPY CLOSE: {latest_close:.2f}")
    lines.append("")

    # --- Top-5 Strategy block ---
    lines.append("<b>Top-5 Strategy (cb: MA50_E20_R5)</b>")
    lines.append(f"  SMA50 = {sma50_value:.2f}")
    lines.append(f"  Status: {'ABOVE' if above_50 else 'BELOW'} SMA50")
    lines.append(f"  Above streak: {above_streak_50} days; Below streak: {below_streak_50} days")
    lines.append(f"  EXIT trigger (≥20 days below SMA50): <b>{'YES' if exit_50 else 'NO'}</b>")
    lines.append(f"  REENTRY trigger (≥5 days above SMA50): <b>{'YES' if reentry_50 else 'NO'}</b>")

    # UNRATE subsection
    lines.append("  <b>UNRATE (FRED, monthly; no lag)</b>")
    lines.append(f"  UNRATE as-of date: {un_now_date} → {un_now_rate:.1f}%")
    lines.append(f"  UNRATE 3-mo prior: {un_prior_date} → {un_prior_rate:.1f}%")
    lines.append(f"  UNRATE 3-mo change: {un_chg:.2f} pp")
    lines.append(f"  UNRATE rising flag (>0.3): <b>{'ON' if un_flag else 'OFF'}</b>")
    lines.append("")

    # Top-5 decision tree
    lines.append("  If exit signal is YES -> Check UNRATE:")
    lines.append("      If UNRATE 3-mo Δ > 0.3 pp -> Move to Treasuries")
    lines.append("      Else -> Move to SP500")
    lines.append(f"      Resolved safe asset (if EXIT): {safe_asset}")
    lines.append("  If exited -> Check SMA50:")
    lines.append("      If SPY ≥ SMA50 for 5+ days, re-enter Top-5.")
    lines.append("")

    # --- SP500 holdings block ---
    lines.append("<b>SP500 holdings (cb: MA250_E80_R5)</b>")
    lines.append(f"  SMA250 = {sma250_value:.2f}")
    lines.append(f"  Status: {'ABOVE' if above_250 else 'BELOW'} SMA250")
    lines.append(f"  Above streak: {above_streak_250} days; Below streak: {below_streak_250} days")
    lines.append(f"  EXIT trigger (≥80 days below SMA250): <b>{'YES' if exit_250 else 'NO'}</b>")
    lines.append(f"  REENTRY trigger (≥5 days above SMA250): <b>{'YES' if reentry_250 else 'NO'}</b>")
    lines.append("")

    # SP500 decision tree
    lines.append("  If exit signal is YES -> Move to Treasuries")
    lines.append("  If exited -> Check SMA250:")
    lines.append("      If SPY ≥ SMA250 for 5+ days, re-enter Top-5.")

    send_telegram(bot_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
