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

ACCOUNT_LABEL = "PORTFOLIO MONITOR"


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


def find_exit_and_recovery(below_series: pd.Series, exit_threshold: int, reentry_threshold: int = 5) -> dict:
    """
    Scan history to determine if we triggered an exit and whether we've recovered.
    """
    values = below_series.tolist()
    dates = below_series.index.tolist()

    days_above = 0
    for v in reversed(values):
        if not v:  
            days_above += 1
        else:
            break

    if days_above == 0:
        below_streak = count_streak(below_series)
        return {
            "exited": below_streak >= exit_threshold,
            "exit_day": None,
            "days_above_since_exit": 0,
            "recovering": False,
            "reentry_threshold": reentry_threshold
        }

    idx_start_of_above = len(values) - days_above

    if idx_start_of_above <= 0:
        return {"exited": False, "exit_day": None, "days_above_since_exit": days_above,
                "recovering": False, "reentry_threshold": reentry_threshold}

    below_streak_before = 0
    for i in range(idx_start_of_above - 1, -1, -1):
        if values[i]:  # below
            below_streak_before += 1
        else:
            break

    if below_streak_before >= exit_threshold:
        return {
            "exited": True,
            "exit_day": dates[idx_start_of_above - 1] if idx_start_of_above > 0 else None,
            "days_above_since_exit": days_above,
            "recovering": days_above < reentry_threshold,
            "reentry_threshold": reentry_threshold
        }

    return {"exited": False, "exit_day": None, "days_above_since_exit": days_above,
            "recovering": False, "reentry_threshold": reentry_threshold}


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
    # Calculate Moving Averages
    # ============================

    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA250"] = df["Close"].rolling(250).mean()

    sma50_value = df["SMA50"].iloc[-1]
    sma250_value = df["SMA250"].iloc[-1]

    df["below_sma50"] = df["Close"] < df["SMA50"]
    df["below_sma250"] = df["Close"] < df["SMA250"]

    below_streak_50 = count_streak(df["below_sma50"])
    below_streak_250 = count_streak(df["below_sma250"])

    # ============================
    # UNRATE logic (3-month comparison)
    # ============================

    un_now = un.iloc[-1]
    un_prior = un.iloc[-4] if len(un) >= 4 else un.iloc[0]

    un_now_rate = un_now["UNRATE"]
    un_prior_rate = un_prior["UNRATE"]
    un_chg = un_now_rate - un_prior_rate
    un_flag = un_chg > 0.3

    # ============================
    # ROTH: MA50_E20_R5 (Top-5 Strategy)
    # ============================

    state_roth = find_exit_and_recovery(df["below_sma50"], exit_threshold=20, reentry_threshold=5)
    exited_roth = state_roth["exited"] or state_roth["recovering"]
    reentry_roth = state_roth["exited"] and state_roth["days_above_since_exit"] >= 5

    if exited_roth and not reentry_roth:
        if un_flag:
            roth_position = "TREASURIES"
        else:
            roth_position = "SP500"
        roth_status = "EXITED"
    else:
        roth_position = "TOP-5 STOCKS"
        roth_status = "INVESTED"

    # ============================
    # ROTH: MA250_E80_R5 (SP500 holdings in Roth)
    # ============================

    state_roth_sp500 = find_exit_and_recovery(df["below_sma250"], exit_threshold=80, reentry_threshold=5)
    exited_roth_sp500 = state_roth_sp500["exited"] or state_roth_sp500["recovering"]
    reentry_roth_sp500 = state_roth_sp500["exited"] and state_roth_sp500["days_above_since_exit"] >= 5

    if exited_roth_sp500 and not reentry_roth_sp500:
        roth_sp500_position = "TREASURIES"
        roth_sp500_status = "EXITED"
    else:
        roth_sp500_position = "SP500"
        roth_sp500_status = "INVESTED"

    # ============================
    # BROKERAGE: MA250_E90_R5 (Top-5 Strategy)
    # ============================

    state_brokerage_top5 = find_exit_and_recovery(df["below_sma250"], exit_threshold=90, reentry_threshold=5)
    exited_brokerage_top5 = state_brokerage_top5["exited"] or state_brokerage_top5["recovering"]
    reentry_brokerage_top5 = state_brokerage_top5["exited"] and state_brokerage_top5["days_above_since_exit"] >= 5

    if exited_brokerage_top5 and not reentry_brokerage_top5:
        brokerage_top5_position = "TREASURIES"
        brokerage_top5_status = "EXITED"
    else:
        brokerage_top5_position = "TOP-5 STOCKS"
        brokerage_top5_status = "INVESTED"

    # ============================
    # BROKERAGE: MA250_E80_R5 (SP500 holdings)
    # ============================

    state_brokerage_sp500 = find_exit_and_recovery(df["below_sma250"], exit_threshold=80, reentry_threshold=5)
    exited_brokerage_sp500 = state_brokerage_sp500["exited"] or state_brokerage_sp500["recovering"]
    reentry_brokerage_sp500 = state_brokerage_sp500["exited"] and state_brokerage_sp500["days_above_since_exit"] >= 5

    if exited_brokerage_sp500 and not reentry_brokerage_sp500:
        brokerage_sp500_position = "TREASURIES"
        brokerage_sp500_status = "EXITED"
    else:
        brokerage_sp500_position = "SP500"
        brokerage_sp500_status = "INVESTED"

    # ============================
    # Build Telegram message
    # ============================

    lines = []

    lines.append(f"<b>📊 {ACCOUNT_LABEL}</b>")
    lines.append(f"{latest_date} | SPY: {latest_close:.2f}")
    lines.append(f"SMA50: {sma50_value:.2f} | SMA250: {sma250_value:.2f}")
    lines.append("")

    # --- ROTH ACCOUNT SECTION ---
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>🏦 ROTH IRA</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")

    lines.append(f"Top-5 Strategy</b> <i>(MA50 E20 R5)</i>")
    lines.append(f"➤ <b>HOLD: {roth_position}</b>")

    if roth_status == "INVESTED":
        status_char = "⚠️" if below_streak_50 > 0 else ""
        lines.append(f"Exit watch: {below_streak_50}/20 days below SMA50 {status_char}")
    else:
        days_above = state_roth["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above SMA50 ⏳")

    # UNRATE line stays here
    lines.append(f"UNRATE 3-mo Δ: {un_chg:+.2f}pp ({'rising ⚠️' if un_flag else 'stable'})")
    lines.append("")

    lines.append(f"<b>SP500 Holdings</b> <i>(MA250 E80 R5)</i>")
    lines.append(f"➤ <b>HOLD: {roth_sp500_position}</b>")

    if roth_sp500_status == "INVESTED":
        status_char = "⚠️" if below_streak_250 > 0 else ""
        lines.append(f"Exit watch: {below_streak_250}/80 days below SMA250 {status_char}")
    else:
        days_above = state_roth_sp500["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above SMA250 ⏳")

    lines.append("")

    # --- BROKERAGE ACCOUNT SECTION ---
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>💼 BROKERAGE</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")

    lines.append(f"Top-5 Strategy</b> <i>(MA250 E90 R5)</i>")
    lines.append(f"➤ <b>HOLD: {brokerage_top5_position}</b>")

    if brokerage_top5_status == "INVESTED":
        status_char = "⚠️" if below_streak_250 > 0 else ""
        lines.append(f"Exit watch: {below_streak_250}/90 days below SMA250 {status_char}")
    else:
        days_above = state_brokerage_top5["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above SMA250 ⏳")

    # UNRATE line removed from here
    lines.append("")

    lines.append(f"<b>SP500 Strategy</b> <i>(MA250 E80 R5)</i>")
    lines.append(f"➤ <b>HOLD: {brokerage_sp500_position}</b>")

    if brokerage_sp500_status == "INVESTED":
        status_char = "⚠️" if below_streak_250 > 0 else ""
        lines.append(f"Exit watch: {below_streak_250}/80 days below SMA250 {status_char}")
    else:
        days_above = state_brokerage_sp500["days_above_since_exit"]
        lines.append(f"Re-entry watch: {days_above}/5 days above SMA250 ⏳")

    send_telegram(bot_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
