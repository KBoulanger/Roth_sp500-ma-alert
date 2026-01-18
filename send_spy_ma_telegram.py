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


def find_exit_and_recovery(below_series: pd.Series, exit_threshold: int) -> dict:
    """
    Scan history to determine if we triggered an exit and whether we've recovered.

    Returns dict with:
    - exited: True if an exit was triggered and we haven't fully re-entered
    - exit_day: date when exit was triggered (if applicable)
    - days_above_since_exit: consecutive days above MA since last being below
    """
    values = below_series.tolist()
    dates = below_series.index.tolist()

    # Work backwards to find current state
    # First, count current streak above (i.e., NOT below)
    days_above = 0
    for v in reversed(values):
        if not v:  # not below = above
            days_above += 1
        else:
            break

    # If we're currently below, no recovery in progress
    if days_above == 0:
        # Check if we're at/past exit threshold
        below_streak = count_streak(below_series)
        return {
            "exited": below_streak >= exit_threshold,
            "exit_day": None,
            "days_above_since_exit": 0,
            "recovering": False
        }

    # We're above now. Look back before this "above" period to see if there was an exit.
    # Start from where the current above streak began
    idx_start_of_above = len(values) - days_above

    # Now scan backwards from there to find if there was a 20+ day below streak
    if idx_start_of_above <= 0:
        return {"exited": False, "exit_day": None, "days_above_since_exit": days_above, "recovering": False}

    # Count the below streak that ended right before current above streak
    below_streak_before = 0
    for i in range(idx_start_of_above - 1, -1, -1):
        if values[i]:  # below
            below_streak_before += 1
        else:
            break

    # Did that below streak trigger an exit?
    if below_streak_before >= exit_threshold:
        # Yes - we exited. Are we recovered (5+ days above)?
        return {
            "exited": True,
            "exit_day": dates[idx_start_of_above - 1] if idx_start_of_above > 0 else None,
            "days_above_since_exit": days_above,
            "recovering": days_above < 5  # Still recovering if < 5 days above
        }

    # No exit was triggered
    return {"exited": False, "exit_day": None, "days_above_since_exit": days_above, "recovering": False}


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

    # Triggers - use historical analysis
    state_50 = find_exit_and_recovery(df["below_sma50"], exit_threshold=20)
    exited_50 = state_50["exited"] or state_50["recovering"]
    reentry_50 = state_50["exited"] and state_50["days_above_since_exit"] >= 5

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

    # Triggers - use historical analysis
    state_250 = find_exit_and_recovery(df["below_sma250"], exit_threshold=80)
    exited_250 = state_250["exited"] or state_250["recovering"]
    reentry_250 = state_250["exited"] and state_250["days_above_since_exit"] >= 5

    # ============================
    # Determine current position status for each strategy
    # ============================

    # Top-5 position logic:
    # - If we exited (20+ days below) and haven't re-entered (5+ days above): defensive
    # - If we re-entered (5+ days above after exit): back in Top-5
    # - If never exited: Top-5
    if exited_50 and not reentry_50:
        # Still out - in defensive position
        if un_flag:
            top5_position = "TREASURIES"
        else:
            top5_position = "SP500"
        top5_status = "EXITED"
    else:
        top5_position = "TOP-5 STOCKS"
        top5_status = "INVESTED"

    # SP500 holdings position
    if exited_250 and not reentry_250:
        sp500_position = "TREASURIES"
        sp500_status = "EXITED"
    else:
        sp500_position = "SP500"
        sp500_status = "INVESTED"

    # ============================
    # Build Telegram message (simplified)
    # ============================

    lines = []

    # Header
    lines.append(f"<b>{ACCOUNT_LABEL}</b>")
    lines.append(f"{latest_date} | SPY: {latest_close:.2f}")
    lines.append("")

    # --- Top-5 Strategy ---
    lines.append(f"<b>Top-5 Strategy</b>")
    lines.append(f"➤ <b>HOLD: {top5_position}</b>")
    lines.append(f"SMA50: {sma50_value:.2f}")
    lines.append(f"SPY vs SMA50: {'ABOVE' if above_50 else 'BELOW'}")

    if top5_status == "INVESTED":
        # Show progress toward exit - add warning emoji if any days below
        if below_streak_50 > 0:
            lines.append(f"Exit watch: {below_streak_50}/20 days below ⚠️")
        else:
            lines.append(f"Exit watch: {below_streak_50}/20 days below")
    else:
        # Show progress toward re-entry
        days_above = state_50["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above ⏳")

    lines.append(f"UNRATE 3-mo Δ: {un_chg:+.2f}pp ({'rising ⚠️' if un_flag else 'stable'})")
    lines.append("")

    # --- SP500 Holdings ---
    lines.append(f"<b>SP500 Holdings</b>")
    lines.append(f"➤ <b>HOLD: {sp500_position}</b>")
    lines.append(f"SMA250: {sma250_value:.2f}")
    lines.append(f"SPY vs SMA250: {'ABOVE' if above_250 else 'BELOW'}")

    if sp500_status == "INVESTED":
        # Show progress toward exit - add warning emoji if any days below
        if below_streak_250 > 0:
            lines.append(f"Exit watch: {below_streak_250}/80 days below ⚠️")
        else:
            lines.append(f"Exit watch: {below_streak_250}/80 days below")
    else:
        # Show progress toward re-entry
        days_above = state_250["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above ⏳")

    send_telegram(bot_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
