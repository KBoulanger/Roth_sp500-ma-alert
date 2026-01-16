import io
import os
import pandas as pd
import requests

# Prices (daily)
STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"

# Unemployment (monthly) from FRED as CSV (no API key)
FRED_UNRATE_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"

# Labeling (so alerts are unambiguous)
ACCOUNT_LABEL = "ROTH"  # change to "BROKERAGE" later if you clone this bot


def fetch_spy_history() -> pd.DataFrame:
    r = requests.get(STOOQ_CSV_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)


def fetch_unrate_monthly() -> pd.DataFrame:
    """
    Fetch monthly UNRATE from FRED and return a DataFrame with columns:
      DATE (datetime64), UNRATE (float)

    Robust to FRED column naming differences and whitespace/BOM issues.
    """
    r = requests.get(FRED_UNRATE_CSV_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))

    # Normalize column names
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    lower_map = {c.lower(): c for c in df.columns}

    # Identify date column
    date_col = None
    for candidate in ("date", "observation_date"):
        if candidate in lower_map:
            date_col = lower_map[candidate]
            break
    if date_col is None:
        raise RuntimeError(f"Could not find a date column in FRED CSV. Columns: {list(df.columns)}")

    # Identify value column
    if "unrate" in lower_map:
        val_col = lower_map["unrate"]
    else:
        non_date_cols = [c for c in df.columns if c != date_col]
        if len(non_date_cols) != 1:
            raise RuntimeError(f"Could not uniquely identify UNRATE column. Columns: {list(df.columns)}")
        val_col = non_date_cols[0]

    out = df[[date_col, val_col]].copy()
    out = out.rename(columns={date_col: "DATE", val_col: "UNRATE"})
    out["DATE"] = pd.to_datetime(out["DATE"])
    out["UNRATE"] = pd.to_numeric(out["UNRATE"], errors="coerce")
    out = out.dropna(subset=["UNRATE"]).sort_values("DATE").reset_index(drop=True)
    return out


def compute_streaks(close: pd.Series, sma: pd.Series) -> tuple[int, int]:
    """
    Returns (above_streak, below_streak) counted from the most recent day backward.
    Equality breaks both streaks (strict > and <).
    Assumes close and sma are aligned and contain no NaNs.
    """
    above_streak = 0
    below_streak = 0

    last_close = float(close.iloc[-1])
    last_sma = float(sma.iloc[-1])

    if last_close > last_sma:
        for c, m in zip(reversed(close.tolist()), reversed(sma.tolist())):
            if c > m:
                above_streak += 1
            else:
                break
    elif last_close < last_sma:
        for c, m in zip(reversed(close.tolist()), reversed(sma.tolist())):
            if c < m:
                below_streak += 1
            else:
                break
    # else: equal -> both 0

    return above_streak, below_streak


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
    spy = spy.dropna(subset=["SMA100"]).copy().reset_index(drop=True)
    if spy.empty:
        raise RuntimeError("Not enough data to compute SMA100 (need >=100 trading days).")

    last = spy.iloc[-1]
    asof_trading_dt = pd.to_datetime(last["Date"])
    asof_trading_date = asof_trading_dt.date().isoformat()

    close_last = float(last["Close"])
    sma_last = float(last["SMA100"])

    above_streak, below_streak = compute_streaks(spy["Close"], spy["SMA100"])
    status = "ABOVE" if close_last > sma_last else ("BELOW" if close_last < sma_last else "AT")

    # Rule mapping (MA100 / E20 / R5)
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
        current = un_m.iloc[-1]   # most recent UNRATE obs date <= trading date
        prior = un_m.iloc[-4]     # exactly 3 monthly observations earlier

        un_now = float(current["UNRATE"])
        un_prior = float(prior["UNRATE"])
        un_chg = un_now - un_prior
        un_flag = (un_chg > 0.3)

        un_now_date = current["DATE"].date().isoformat()
        un_prior_date = prior["DATE"].date().isoformat()

    # Safe-asset suggestion ONLY if an exit is triggered (tiered severity concept)
    safe_suggestion = "N/A (no exit signal)"
    if exit_signal:
        if un_flag is None:
            safe_suggestion = "Exit signal YES, but UNRATE 3-month change unavailable"
        elif un_flag:
            safe_suggestion = "Treasuries (UNRATE rising flag ON)"
        else:
            safe_suggestion = "SPY / S&P 500 exposure (UNRATE rising flag OFF)"

    # --- Telegram message (explicit UNRATE dates + account label) ---
    lines = []
    lines.append(f"<b>{ACCOUNT_LABEL} ACCOUNT — SPY vs SMA100</b>")
    lines.append(f"Account: <b>{ACCOUNT_LABEL}</b>")
    lines.append("")
    lines.append(f"Price as-of (trading day): {asof_trading_date}")
    lines.append(f"Close: {close_last:.2f}")
    lines.append(f"SMA100: {sma_last:.2f}")
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
        lines.append("UNRATE as-of date: NA (need >=4 monthly observations up to today)")
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
