import io
import os
import pandas as pd
import requests

# Prices (daily)
STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"

# Unemployment (monthly) from FRED as CSV (no API key)
FRED_UNRATE_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"


def fetch_spy_history() -> pd.DataFrame:
    r = requests.get(STOOQ_CSV_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)


def fetch_unrate_monthly() -> pd.DataFrame:
    r = requests.get(FRED_UNRATE_CSV_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))  # columns: DATE, UNRATE
    df["DATE"] = pd.to_datetime(df["DATE"])
    df["UNRATE"] = pd.to_numeric(df["UNRATE"], errors="coerce")
    df = df.dropna(subset=["UNRATE"]).sort_values("DATE").reset_index(drop=True)
    return df


def consecutive_streak(series: pd.Series) -> int:
    n = 0
    for v in reversed(series.tolist()):
        if v:
            n += 1
        else:
            break
    return n


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    ).raise_for_status()


def main() -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()

    # --- SPY prices + SMA100 ---
    spy = fetch_spy_history()
    spy["SMA100"] = spy["Close"].rolling(100, min_periods=100).mean()
    spy = spy.dropna(subset=["SMA100"]).copy()
    if spy.empty:
        raise RuntimeError("Not enough data to compute SMA100.")

    last = spy.iloc[-1]
    asof_trading_dt = last["Date"]  # pandas Timestamp
    asof_trading_date = asof_trading_dt.date().isoformat()
    close = float(last["Close"])
    sma = float(last["SMA100"])

    above = spy["Close"] > spy["SMA100"]
    below = spy["Close"] < spy["SMA100"]
    above_streak = consecutive_streak(above)
    below_streak = consecutive_streak(below)

    status = "ABOVE" if close > sma else ("BELOW" if close < sma else "AT")
    exit_signal = (below_streak >= 20)
    reentry_signal = (above_streak >= 5)

    # --- UNRATE monthly, no lag, true 3-month change (3 monthly observations earlier) ---
    un_m = fetch_unrate_monthly()
    un_m = un_m[un_m["DATE"] <= asof_trading_dt].copy()

    un_flag = None
    un_now = None
    un_prior = None
    un_chg = None
    un_now_date = None
    un_prior_date = None

    if len(un_m) >= 4:
        current = un_m.iloc[-1]   # most recent UNRATE observation date <= trading date
        prior = un_m.iloc[-4]     # exactly 3 monthly observations earlier

        un_now = float(current["UNRATE"])
        un_prior = float(prior["UNRATE"])
        un_chg = un_now - un_prior
        un_flag = (un_chg > 0.3)

        un_now_date = current["DATE"].date().isoformat()
        un_prior_date = prior["DATE"].date().isoformat()

    # Safe-asset suggestion ONLY if an exit is triggered
    safe_suggestion = "N/A (no exit signal)"
    if exit_signal:
        if un_flag is None:
            safe_suggestion = "Exit signal YES, but UNRATE 3-month change unavailable"
        elif un_flag:
            safe_suggestion = "Treasuries (UNRATE rising flag ON)"
        else:
            safe_suggestion = "SPY / S&P 500 exposure (UNRATE rising flag OFF)"

    # --- Telegram message (explicit UNRATE dates) ---
    lines = []
    lines.append("<b>SPY vs SMA100</b>")
    lines.append(f"Price as-of (trading day): {asof_trading_date}")
    lines.append(f"Close: {close:.2f}")
    lines.append(f"SMA100: {sma:.2f}")
    lines.append(f"Status: <b>{status}</b>")
    lines.append("")
    lines.append(f"Above streak: {above_streak}")
    lines.append(f"Below streak: {below_streak}")
    lines.append("")
    lines.append(f"Exit trigger (≥20 below): <b>{'YES' if exit_signal else 'no'}</b>")
    lines.append(f"Reentry trigger (≥5 above): <b>{'YES' if reentry_signal else 'no'}</b>")
    lines.append("")

    lines.append("<b>UNRATE (FRED, monthly; no lag)</b>")
    if un_flag is None:
        lines.append("UNRATE as-of date: NA")
        lines.append("UNRATE 3-mo prior date: NA")
        lines.append("3-mo change: NA")
        lines.append("UNRATE rising flag (>0.3): NA")
    else:
        lines.append(f"UNRATE as-of date: {un_now_date} → {un_now:.1f}%")
        lines.append(f"UNRATE 3-mo prior: {un_prior_date} → {un_prior:.1f}%")
        lines.append(f"UNRATE 3-mo change: {un_chg:.2f} pp")
        lines.append(f"UNRATE rising flag (>0.3): <b>{'ON' if un_flag else 'OFF'}</b>")

    lines.append("")
    lines.append(f"<b>If exit signal is YES → safe asset:</b> {safe_suggestion}")

    send_telegram(bot_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
