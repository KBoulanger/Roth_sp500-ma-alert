import io
import os
import pandas as pd
import requests

STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"

def fetch_spy_history():
    r = requests.get(STOOQ_CSV_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)

def consecutive_streak(series):
    n = 0
    for v in reversed(series.tolist()):
        if v:
            n += 1
        else:
            break
    return n

def send_telegram(bot_token, chat_id, text):
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

def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    df = fetch_spy_history()
    df["SMA100"] = df["Close"].rolling(100, min_periods=100).mean()
    df = df.dropna(subset=["SMA100"])

    last = df.iloc[-1]
    close = float(last["Close"])
    sma = float(last["SMA100"])
    date = last["Date"].date().isoformat()

    above = df["Close"] > df["SMA100"]
    below = df["Close"] < df["SMA100"]

    above_streak = consecutive_streak(above)
    below_streak = consecutive_streak(below)

    status = "ABOVE" if close > sma else "BELOW"

    msg = (
        f"<b>SPY vs SMA100</b>\n"
        f"Date: {date}\n"
        f"Close: {close:.2f}\n"
        f"SMA100: {sma:.2f}\n"
        f"Status: <b>{status}</b>\n\n"
        f"Above streak: {above_streak}\n"
        f"Below streak: {below_streak}\n\n"
        f"Exit trigger (≥20 below): {'YES' if below_streak >= 20 else 'no'}\n"
        f"Reentry trigger (≥5 above): {'YES' if above_streak >= 5 else 'no'}"
    )

    send_telegram(bot_token, chat_id, msg)

if __name__ == "__main__":
    main()
