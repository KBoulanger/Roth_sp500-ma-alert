import os
import pandas as pd
import requests

# ============================
# Data sources
# ============================

STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
FRED_UNRATE_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"

ACCOUNT_LABEL = "ROTH"


# ============================
# Helpers
# ============================

def fetch_spy_history() -> pd.DataFrame:
    df = pd.read_csv(STOOQ_CSV_URL)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df[["Close"]].dropna()


def fetch_unrate() -> pd.DataFrame:
    df = pd.read_csv(FRED_UNRATE_CSV_URL)
    df["DATE"] = pd.to_datetime(df["DATE"])
    df["UNRATE"] = pd.to_numeric(df["UNRATE"], errors="coerce")
    return df.dropna()


def count_streak(series: pd.Series) -> int:
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
    prev = df.iloc[:-1]

    latest_date = latest.name.strftime("%Y-%m-%d")
    latest_close = latest["Close"]

    # ============================
    # MA50_E20_R5  (TopK overlay)
    # ============================

    df["SMA50"] = df["Close"].rolling(50).mean()

    above_50 = latest_close > latest["SMA50"]
    below_50 = latest_close < latest["SMA50"]

    above_streak_50 = count_streak(prev["Close"] > prev["SMA50"])
    below_streak_50 = count_streak(prev["Close"] < prev["SMA50"])

    exit_50 = below_streak_50 >= 20
    reentry_50 = above_streak_50 >= 5

    # UNRATE logic
    un_now = un.iloc[-1]
    un_prior = un.iloc[-4] if len(un) >= 4 else None

    un_now_date = un_now["DATE"].strftime("%Y-%m-%d")
    un_prior_date = un_prior["DATE"].strftime("%Y-%m-%d") if un_prior is not None else "N/A"

    un_chg = un_now["UNRATE"] - un_prior["UNRATE"] if un_prior is not None else 0.0
    un_flag = un_chg > 0.3

    # ============================
    # MA250_E80_R5 (SP500 holdings)
    # ============================

    df["SMA250"] = df["Close"].rolling(250).mean()

    above_250 = latest_close > latest["SMA250"]
    below_250 = latest_close < latest["SMA250"]

    above_streak_250 = count_streak(prev["Close"] > prev["SMA250"])
    below_streak_250 = count_streak(prev["Close"] < prev["SMA250"])

    exit_250 = below_streak_250 >= 80
    reentry_250 = above_streak_250 >= 5

    # ============================
    # Telegram message
    # ============================

    lines = []

    lines.append(f"<b>{ACCOUNT_LABEL} ACCOUNT</b>")
    lines.append("")
    lines.append(f"SPY Price as-of (trading day): {latest_date}")
    lines.append(f"Close: {latest_close:.2f}")
    lines.append("")

    # --- MA50 block ---
    lines.append("<b>MA50_E20_R5:</b>")
    lines.append(f"SMA50: {latest['SMA50']:.2f}")
    lines.append(f"SMA50 Status: <b>{'ABOVE' if above_50 else 'BELOW'}</b>")
    lines.append(f"SMA50 Above streak: {above_streak_50}")
    lines.append(f"SMA50 Below streak: {below_streak_50}")
    lines.append("")
    lines.append(f"Exit trigger (≥20 below): {'YES' if exit_50 else 'NO'}")
    lines.append(f"Reentry trigger (≥5 above): {'YES' if reentry_50 else 'NO'}")
    lines.append("")
    lines.append("UNRATE (FRED, monthly; no lag)")
    lines.append(f"UNRATE as-of date: {un_now_date} → {un_now['UNRATE']:.1f}%")
    lines.append(f"UNRATE 3-mo prior: {un_prior_date} → {un_prior['UNRATE']:.1f}%")
    lines.append(f"UNRATE 3-mo change: {un_chg:.2f} pp")
    lines.append(f"UNRATE rising flag (>0.3): <b>{'ON' if un_flag else 'OFF'}</b>")
    lines.append("")

    if exit_50:
        instr1 = "Exit Top-K equities → Treasuries" if un_flag else "Rotate Top-K → SP500"
    else:
        instr1 = "Remain invested in Top-K equities"

    lines.append("<b>What to do today (Top-K):</b>")
    lines.append(instr1)
    lines.append("")
    lines.append("")

    # --- MA250 block ---
    lines.append("<b>MA250_E80_R5:</b>")
    lines.append(f"SMA250: {latest['SMA250']:.2f}")
    lines.append(f"SMA250 Status: <b>{'ABOVE' if above_250 else 'BELOW'}</b>")
    lines.append(f"SMA250 Above streak: {above_streak_250}")
    lines.append(f"SMA250 Below streak: {below_streak_250}")
    lines.append("")
    lines.append(f"Exit trigger (≥80 below): {'YES' if exit_250 else 'NO'}")
    lines.append(f"Reentry trigger (≥5 above): {'YES' if reentry_250 else 'NO'}")
    lines.append("")

    if exit_250:
        instr2 = "Sell SP500 → move to Treasuries"
    elif reentry_250:
        instr2 = "Hold / reenter SP500"
    else:
        instr2 = "No action on SP500 holdings"

    lines.append("<b>What to do today (SP500):</b>")
    lines.append(instr2)

    send_telegram(bot_token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
