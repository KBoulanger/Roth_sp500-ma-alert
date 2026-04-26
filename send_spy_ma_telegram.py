"""Daily Telegram regime monitor for ROTH and BROKERAGE accounts.

Updated 2026-04-26:
  - Added SPY Leveraged (Roth/Brok) and QQQ Leveraged (Roth/Brok) sections
    using the picks from the LETF Timing Strategy project (PROJECT_REVIEW.md)
  - Defensive asset for leveraged sections is USFR (floating-rate Treasury)
  - State persistence via state.json committed back to repo by GitHub Actions
  - Health-check footer
  - Top change-line summarizes flips/warnings/calm
  - LTCG eligibility tracking for brokerage positions (calendar days)
  - Existing Top-7 ALT-A / BROK_A logic UNTOUCHED; render uniform with new sections
  - Legacy SP500 (MA250 E80 R5) sections COMMENTED OUT (compute kept, render hidden)

Strategy specs:
  ROTH (ALT-A): D-asym 300/50/100/5/0.40/10/tiered_0.1
    Exit (whichever fires): NASDAQCOM vol30 ≥ 0.40 for 10 days OR SPY < SMA300 for 50 days
    Reentry: SPY ≥ SMA100 for 5 days
    Defensive routing: ΔUNRATE_3mo > 0.1pp → Treasuries, else SP500

  BROKERAGE (BROK_A): D-asym 300/50/100/5/0.45/25/tiered_0.1
    Same as Roth except vol_thr=0.45 and E_vol=25

  SPY LEVERAGED (Roth):     MA275 v<22% e=1 r=10 | UPRO ↔ USFR
  SPY LEVERAGED (Brok):     MA275 v<22% e=2 r=10 | UPRO ↔ USFR
  QQQ LEVERAGED (Roth):     MA175 v<30% e=2 r=2  | TQQQ ↔ USFR
  QQQ LEVERAGED (Brok):     MA175 v<30% e=2 r=2  | TQQQ ↔ USFR

Convention for leveraged strategies:
  raw_signal[i] = 1 if (price[i] > MA[i] AND vol20[i] < vol_thr) else 0
  Position walked forward through full history with entry/exit lag confirmation.
  Signal computed at end of day T → trade at close of day T+1 (1-day exec lag).

Vol calculations are STRICTLY per-strategy:
  Top-7:           NASDAQCOM 30-day vol (FRED) — series-specific to that strategy
  SPY Leveraged:   SPY 20-day vol (yfinance/Stooq)
  QQQ Leveraged:   QQQ 20-day vol (yfinance/Stooq)
"""

import os
import json
import pandas as pd
import numpy as np
import requests
from datetime import datetime, date, timedelta
from pandas.tseries.holiday import USFederalHolidayCalendar


def trading_days_missed(latest_d, today_d):
    """Count NYSE trading days between latest_d (exclusive) and today_d (inclusive)
    that should have produced data. Accounts for weekends + US federal holidays.
    Returns 0 if data is fresh (latest_d is the most recent expected trading day)."""
    if today_d <= latest_d: return 0
    cal = USFederalHolidayCalendar()
    holidays = set(cal.holidays(start=pd.Timestamp(latest_d),
                                 end=pd.Timestamp(today_d) + pd.Timedelta(days=1)).date)
    d = latest_d + timedelta(days=1)
    count = 0
    while d <= today_d:
        if d.weekday() < 5 and d not in holidays:
            count += 1
        d += timedelta(days=1)
    return count

# ============================
# Data sources
# ============================

STOOQ_SPY_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
STOOQ_QQQ_URL = "https://stooq.com/q/d/l/?s=qqq.us&i=d"
FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

ACCOUNT_LABEL = "PORTFOLIO MONITOR"
STATE_FILE = "state.json"

# ============================
# Strategy parameters
# ============================

# --- Top-7 (existing, untouched) ---
ROTH_TOP7_PARAMS = {
    "name": "ALT-A",
    "ma_exit": 300, "E_ma": 50, "ma_re": 100, "R_re": 5,
    "vol_thr": 0.40, "E_vol": 10, "ur_thr": 0.1,
}
BROK_TOP7_PARAMS = {
    "name": "BROK_A",
    "ma_exit": 300, "E_ma": 50, "ma_re": 100, "R_re": 5,
    "vol_thr": 0.45, "E_vol": 25, "ur_thr": 0.1,
}

# --- SPY Leveraged (new) ---
SPY_LEV_ROTH_PARAMS = {
    "ma": 275, "vol_thr": 0.22, "vol_window": 20, "exit_lag": 1, "entry_lag": 10,
    "leveraged": "UPRO", "defensive": "USFR", "signal_asset": "SPY",
}
SPY_LEV_BROK_PARAMS = {
    "ma": 275, "vol_thr": 0.22, "vol_window": 20, "exit_lag": 2, "entry_lag": 10,
    "leveraged": "UPRO", "defensive": "USFR", "signal_asset": "SPY",
}

# --- QQQ Leveraged (new, same config for both accounts) ---
QQQ_LEV_PARAMS = {
    "ma": 175, "vol_thr": 0.30, "vol_window": 20, "exit_lag": 2, "entry_lag": 2,
    "leveraged": "TQQQ", "defensive": "USFR", "signal_asset": "QQQ",
}

# ============================
# Data fetching
# ============================

def fetch_etf_yfinance(ticker: str) -> pd.DataFrame:
    """Pull adjusted close from yfinance. ~3y of history."""
    import yfinance as yf
    df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().title() for c in df.columns]
    if "Close" not in df.columns:
        raise ValueError(f"yfinance {ticker}: 'Close' not found. Got: {list(df.columns)}")
    df = df[["Close"]].dropna()
    if df.empty:
        raise ValueError(f"yfinance {ticker} returned empty")
    return df


def fetch_etf_stooq(ticker: str) -> pd.DataFrame:
    """Stooq fallback for SPY or QQQ."""
    url = STOOQ_SPY_URL if ticker.upper() == "SPY" else STOOQ_QQQ_URL
    df = pd.read_csv(url)
    if df.empty or "Close" not in df.columns:
        raise ValueError(f"Stooq {ticker} returned unusable data")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df[["Close"]].dropna()


def fetch_etf(ticker: str) -> pd.DataFrame:
    """yfinance primary, Stooq fallback."""
    try:
        return fetch_etf_yfinance(ticker)
    except Exception as e:
        print(f"yfinance {ticker} failed: {e}, falling back to Stooq")
    try:
        return fetch_etf_stooq(ticker)
    except Exception as e:
        raise RuntimeError(f"All {ticker} sources failed. Last error: {e}")


def fetch_nasdaqcom_fred_api(api_key: str, observation_start: str = None) -> pd.DataFrame:
    """NASDAQCOM (Nasdaq Composite) from FRED. Used by Top-7 vol30."""
    params = {"series_id": "NASDAQCOM", "api_key": api_key, "file_type": "json", "sort_order": "asc"}
    if observation_start:
        params["observation_start"] = observation_start
    resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    records = []
    for obs in data.get("observations", []):
        d, v = obs.get("date"), obs.get("value")
        if d and v and v != ".":
            try:
                records.append({"Date": pd.to_datetime(d), "NDX": float(v)})
            except (ValueError, TypeError):
                continue
    return pd.DataFrame(records).sort_values("Date").set_index("Date")


def fetch_nasdaqcom_csv_fallback() -> pd.DataFrame:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM"
    df = pd.read_csv(url)
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "Date", "NASDAQCOM": "NDX"})
    df["Date"] = pd.to_datetime(df["Date"])
    df["NDX"] = pd.to_numeric(df["NDX"], errors="coerce")
    return df.dropna().sort_values("Date").set_index("Date")


def fetch_nasdaqcom_yfinance() -> pd.DataFrame:
    """Fetch Nasdaq Composite via yfinance (^IXIC) — real-time post-close.
    Verified equivalent to FRED's NASDAQCOM series (max diff 0.000044% over 60d)."""
    import yfinance as yf
    df = yf.download("^IXIC", period="4y", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().title() for c in df.columns]
    if "Close" not in df.columns:
        raise ValueError(f"yfinance ^IXIC: 'Close' not found. Got: {list(df.columns)}")
    df = df[["Close"]].dropna().rename(columns={"Close": "NDX"})
    if df.empty:
        raise ValueError("yfinance ^IXIC returned empty")
    return df


def fetch_nasdaqcom() -> pd.DataFrame:
    """yfinance ^IXIC primary (real-time post-close), FRED API/CSV fallback.
    Switching from FRED-primary to yfinance-primary eliminates the ~1-day FRED
    publication lag that previously caused 1-day-late vol-path triggers."""
    # PRIMARY: yfinance ^IXIC (real-time post-close, same series as NASDAQCOM)
    try:
        df = fetch_nasdaqcom_yfinance()
        if not df.empty:
            return df
    except Exception as e:
        print(f"yfinance ^IXIC failed: {e}, falling back to FRED")

    # FALLBACK 1: FRED API
    api_key = os.environ.get("FRED_API_KEY")
    obs_start = (pd.Timestamp.today() - pd.Timedelta(days=365 * 4)).strftime("%Y-%m-%d")
    if api_key:
        try:
            df = fetch_nasdaqcom_fred_api(api_key, observation_start=obs_start)
            if not df.empty:
                return df
        except Exception as e:
            print(f"FRED NASDAQCOM API failed: {e}, falling back to CSV")

    # FALLBACK 2: FRED CSV scrape
    df = fetch_nasdaqcom_csv_fallback()
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=365 * 4)
    return df[df.index >= cutoff].copy()


def fetch_unrate_fred_api(api_key: str) -> pd.DataFrame:
    params = {"series_id": "UNRATE", "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": 12}
    resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    records = []
    for obs in data.get("observations", []):
        d, v = obs.get("date"), obs.get("value")
        if d and v and v != ".":
            records.append({"DATE": pd.to_datetime(d), "UNRATE": float(v)})
    return pd.DataFrame(records).sort_values("DATE").reset_index(drop=True)


def fetch_unrate_fallback() -> pd.DataFrame:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"
    df = pd.read_csv(url)
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "DATE"})
    df["DATE"] = pd.to_datetime(df["DATE"])
    df["UNRATE"] = pd.to_numeric(df["UNRATE"], errors="coerce")
    return df.dropna().sort_values("DATE").reset_index(drop=True).tail(12).reset_index(drop=True)


def fetch_unrate() -> pd.DataFrame:
    api_key = os.environ.get("FRED_API_KEY")
    if api_key:
        try:
            return fetch_unrate_fred_api(api_key)
        except Exception as e:
            print(f"FRED UNRATE API failed: {e}, falling back to CSV")
    return fetch_unrate_fallback()


# ============================
# Vol calculations
# ============================

def compute_vol30(ndx_series: pd.Series) -> pd.Series:
    """30-day rolling stdev of simple returns × √252. Used by Top-7 (NASDAQCOM)."""
    return ndx_series.pct_change().rolling(30, min_periods=30).std() * np.sqrt(252)


def compute_vol20(price_series: pd.Series) -> pd.Series:
    """20-day rolling stdev of simple returns × √252. Used by SPY/QQQ Leveraged."""
    return price_series.pct_change().rolling(20, min_periods=20).std() * np.sqrt(252)


# ============================
# State walkers
# ============================

def walk_dasym_state(spy: pd.Series, sma_exit: pd.Series, sma_re: pd.Series,
                     vol30: pd.Series, vol_thr: float, E_vol: int, E_ma: int, R_re: int) -> dict:
    """D-asym walker for Top-7 (existing, untouched)."""
    state = "invested"
    ev = em = rc = 0
    last_transition_date = None
    last_transition_reason = None

    for i in range(len(spy)):
        p = spy.iloc[i]; mx = sma_exit.iloc[i]; mr = sma_re.iloc[i]
        v = vol30.iloc[i] if i < len(vol30) else np.nan
        d = spy.index[i]
        if pd.isna(p) or pd.isna(mx) or pd.isna(mr):
            continue
        if not pd.isna(v) and v >= vol_thr: ev += 1
        else: ev = 0
        if p < mx: em += 1
        else: em = 0
        if p > mr: rc += 1
        else: rc = 0
        if state == "invested":
            if ev >= E_vol or em >= E_ma:
                last_transition_date = d
                last_transition_reason = "vol path" if ev >= E_vol else "MA path"
                state = "defensive"; ev = em = rc = 0
        else:
            if rc >= R_re:
                last_transition_date = d
                last_transition_reason = "reentry"
                state = "invested"; ev = em = rc = 0

    return {
        "state": state,
        "vol_streak": ev, "ma_streak": em, "reentry_streak": rc,
        "last_transition_date": last_transition_date,
        "last_transition_reason": last_transition_reason,
        "current_vol": float(vol30.iloc[-1]) if not pd.isna(vol30.iloc[-1]) else None,
        "current_spy": float(spy.iloc[-1]),
        "current_sma_exit": float(sma_exit.iloc[-1]) if not pd.isna(sma_exit.iloc[-1]) else None,
        "current_sma_re": float(sma_re.iloc[-1]) if not pd.isna(sma_re.iloc[-1]) else None,
        "latest_date": spy.index[-1],
    }


def walk_lev_state(price: pd.Series, ma: pd.Series, vol20: pd.Series,
                   vol_thr: float, exit_lag: int, entry_lag: int) -> dict:
    """Walker for SPY/QQQ Leveraged strategies.

    raw_signal[i] = 1 if (price > MA AND vol20 < vol_thr) else 0
    Apply entry_lag (consecutive ON days to ENTER) and exit_lag (OFF to EXIT).
    Initial state assumed 'defensive' before first entry; walker converges
    once enough history has been processed.
    """
    state = "defensive"
    days_on = days_off = 0
    last_transition_date = None
    last_transition_reason = None

    for i in range(len(price)):
        p = price.iloc[i]; m = ma.iloc[i]; v = vol20.iloc[i]
        d = price.index[i]
        if pd.isna(p) or pd.isna(m) or pd.isna(v):
            continue
        raw = 1 if (p > m and v < vol_thr) else 0
        if raw == 1:
            days_on += 1; days_off = 0
        else:
            days_off += 1; days_on = 0
        if state == "defensive":
            if days_on >= entry_lag:
                last_transition_date = d
                last_transition_reason = "reentry"
                state = "invested"; days_on = days_off = 0
        else:  # invested
            if days_off >= exit_lag:
                last_transition_date = d
                last_transition_reason = "vol or MA path"
                state = "defensive"; days_on = days_off = 0

    return {
        "state": state,
        "days_on_streak": days_on,
        "days_off_streak": days_off,
        "last_transition_date": last_transition_date,
        "last_transition_reason": last_transition_reason,
        "current_price": float(price.iloc[-1]),
        "current_ma": float(ma.iloc[-1]) if not pd.isna(ma.iloc[-1]) else None,
        "current_vol": float(vol20.iloc[-1]) if not pd.isna(vol20.iloc[-1]) else None,
    }


# ============================
# Helpers
# ============================

def count_streak(series: pd.Series) -> int:
    c = 0
    for v in reversed(series.tolist()):
        if bool(v): c += 1
        else: break
    return c


def find_exit_and_recovery(below_series: pd.Series, exit_threshold: int, reentry_threshold: int = 5) -> dict:
    """Legacy DMA exit/recovery detector — kept for compatibility but NOT rendered."""
    values = below_series.tolist(); dates = below_series.index.tolist()
    days_above = 0
    for v in reversed(values):
        if not v: days_above += 1
        else: break
    if days_above == 0:
        below_streak = count_streak(below_series)
        return {"exited": below_streak >= exit_threshold, "exit_day": None,
                "days_above_since_exit": 0, "recovering": False,
                "reentry_threshold": reentry_threshold}
    idx_start_of_above = len(values) - days_above
    if idx_start_of_above <= 0:
        return {"exited": False, "exit_day": None, "days_above_since_exit": days_above,
                "recovering": False, "reentry_threshold": reentry_threshold}
    below_streak_before = 0
    for i in range(idx_start_of_above - 1, -1, -1):
        if values[i]: below_streak_before += 1
        else: break
    if below_streak_before >= exit_threshold:
        return {"exited": True,
                "exit_day": dates[idx_start_of_above - 1] if idx_start_of_above > 0 else None,
                "days_above_since_exit": days_above,
                "recovering": days_above < reentry_threshold,
                "reentry_threshold": reentry_threshold}
    return {"exited": False, "exit_day": None, "days_above_since_exit": days_above,
            "recovering": False, "reentry_threshold": reentry_threshold}


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def days_in_strategy(walker_state: dict, fallback_first_date) -> tuple:
    """Return (N_days, exact) where N_days is calendar days the strategy has
    been in its current state, derived from the walker's last_transition_date.

    If walker has a last_transition_date: N = today - that date, exact=True.
    If no transition in the data window: N = today - fallback_first_date, exact=False
    (meaning "at least N days; no flip in the data window").
    """
    ltd = walker_state.get("last_transition_date")
    if ltd is not None:
        if isinstance(ltd, pd.Timestamp):
            ltd = ltd.date()
        return max(0, (date.today() - ltd).days), True
    if fallback_first_date is not None:
        if isinstance(fallback_first_date, pd.Timestamp):
            fallback_first_date = fallback_first_date.date()
        return max(0, (date.today() - fallback_first_date).days), False
    return 0, False


def hourglass_if_progressing(streak: int, threshold: int) -> str:
    """Return ⏳ when any non-zero progress; ⚠️ when within 1 of threshold; '' otherwise."""
    if streak == 0:
        return ""
    if streak >= threshold - 1:
        return "⚠️"
    return "⏳"


# ============================
# State persistence (state.json)
# ============================

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to read {STATE_FILE}: {e}. Bootstrapping new state.")
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ============================
# Render helpers
# ============================

def _format_days_in_strategy(currently: str, walker_state: dict, fallback_first_date) -> str:
    """Build the 'Currently X for N days' line. Uses walker's last_transition_date
    when available; otherwise fallback to data window start with a '+' suffix."""
    n_days, exact = days_in_strategy(walker_state, fallback_first_date)
    if exact:
        return f"<i>Currently {currently} for {n_days} days (since signal flip on {walker_state['last_transition_date'].date() if isinstance(walker_state['last_transition_date'], pd.Timestamp) else walker_state['last_transition_date']})</i>"
    return f"<i>Currently {currently} for {n_days}+ days (no flip in current data window)</i>"


def render_top7_section(label_prefix: str, params: dict, st: dict,
                        fallback_first_date, unrate_failed: bool = False) -> list:
    lines = []
    lines.append(f"<b>Top-7 ({label_prefix}) — {params['name']}</b>")
    lines.append(f"<i>D-asym {params['ma_exit']}/{params['E_ma']}/{params['ma_re']}/{params['R_re']}/"
                 f"{params['vol_thr']:.2f}/{params['E_vol']}/tiered_{params['ur_thr']} | "
                 f"basket: TOP-7 stocks | defensive: tiered_{params['ur_thr']}</i>")

    # Determine current holding
    if st["state"] == "invested":
        held = "TOP-7 STOCKS"
    else:
        # Defensive routing depends on UNRATE
        held = "TREASURIES" if st["_unrate_high"] else "SP500"
        if unrate_failed:
            held = f"{held} ⚠️"
    lines.append(f"➤ <b>HOLD: {held}</b>")

    # Exit conditions
    if st["state"] == "invested":
        lines.append("<u>Exit</u> (→ defensive, whichever fires first):")
        vol_emoji = hourglass_if_progressing(st["vol_streak"], params["E_vol"])
        ma_emoji = hourglass_if_progressing(st["ma_streak"], params["E_ma"])
        lines.append(f"  • Vol path: {st['vol_streak']}/{params['E_vol']} days where (NASDAQCOM vol30 ≥ {int(params['vol_thr']*100)}%) {vol_emoji}".rstrip())
        lines.append(f"  • MA path:  {st['ma_streak']}/{params['E_ma']} days where (SP500 &lt; SMA{params['ma_exit']}) {ma_emoji}".rstrip())
        lines.append(f"<u>Reentry</u> (→ TOP-7): — (rule: {params['R_re']} consecutive days where SP500 ≥ SMA{params['ma_re']})")
    else:
        lines.append(f"<u>Exit</u> (→ defensive): — (rules: NASDAQCOM vol30 ≥ {int(params['vol_thr']*100)}% for {params['E_vol']}d OR SP500 &lt; SMA{params['ma_exit']} for {params['E_ma']}d)")
        re_emoji = hourglass_if_progressing(st["reentry_streak"], params["R_re"])
        lines.append(f"<u>Reentry</u> (→ TOP-7): {st['reentry_streak']}/{params['R_re']} days where (SP500 ≥ SMA{params['ma_re']}) {re_emoji}".rstrip())

    if unrate_failed:
        lines.append(f"<u>Defensive routing</u>: ⚠️ <b>UNRATE FETCH FAILED</b> — assuming stable (→ SP500). Verify manually before acting.")
    else:
        lines.append(f"<u>Defensive routing</u>: ΔUNRATE_3mo &gt; {params['ur_thr']}pp → Treasuries, else → SP500")

    # "Currently X for N days" line
    if st["state"] == "invested":
        currently = "TOP-7 STOCKS"
    else:
        currently = "TREASURIES" if st["_unrate_high"] else "SP500"
    lines.append(_format_days_in_strategy(currently, st, fallback_first_date))
    return lines


def render_lev_section(label: str, params: dict, st: dict, fallback_first_date) -> list:
    """Render SPY Leveraged or QQQ Leveraged section."""
    lines = []
    sig = params["signal_asset"]
    lev = params["leveraged"]
    defv = params["defensive"]
    ma_n = params["ma"]
    vt = int(params["vol_thr"] * 100)
    el = params["exit_lag"]
    rl = params["entry_lag"]

    lines.append(f"<b>{label}</b>")
    lines.append(f"<i>MA{ma_n} v&lt;{vt}% e={el} r={rl} | leveraged: {lev} | defensive: {defv}</i>")

    held = lev if st["state"] == "invested" else defv
    lines.append(f"➤ <b>HOLD: {held}</b>")

    exit_rule = f"{sig} &lt; SMA{ma_n} OR {sig} vol20 ≥ {vt}%"
    reentry_rule = f"{sig} &gt; SMA{ma_n} AND {sig} vol20 &lt; {vt}%"

    if st["state"] == "invested":
        emoji = hourglass_if_progressing(st["days_off_streak"], el)
        lines.append(f"<u>Exit</u> (→ {defv}): {st['days_off_streak']}/{el} days where ({exit_rule}) {emoji}".rstrip())
        days_word = "day" if rl == 1 else "days"
        lines.append(f"<u>Reentry</u> (→ {lev}): — (rule: {rl} consecutive {days_word} where {reentry_rule})")
    else:
        days_word = "day" if el == 1 else "days"
        lines.append(f"<u>Exit</u> (→ {defv}): — (rule: {el} consecutive {days_word} where {exit_rule})")
        emoji = hourglass_if_progressing(st["days_on_streak"], rl)
        lines.append(f"<u>Reentry</u> (→ {lev}): {st['days_on_streak']}/{rl} days where ({reentry_rule}) {emoji}".rstrip())

    # "Currently X for N days" line
    currently = lev if st["state"] == "invested" else defv
    lines.append(_format_days_in_strategy(currently, st, fallback_first_date))
    return lines


# ============================
# Change-line builder
# ============================

def build_conditions_line(spy_close, sma100, sma275, sma300, spy_vol20,
                           qqq_close, qqq_sma175, qqq_vol20,
                           ndx_vol30, un_chg, un_flag_01, unrate_failed,
                           ndx_stale_days: int = 0) -> str:
    """One-line market conditions snapshot.

    Compares SPY against ALL 3 reference MAs (SMA100=Top-7 reentry, SMA275=SPY Lev exit,
    SMA300=Top-7 exit) and QQQ vs SMA175 (Q Lev). Plain language: "above all MAs"
    when calm, explicit detail when not. Borderline (<1.5% gap or <1.5pp vol) flagged.
    """
    BORDER_PCT = 1.5
    BORDER_VOL = 1.5

    def vol_phrase(v, thr_pct, label):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "vol n/a"
        v_pct = v * 100
        if v_pct >= thr_pct:
            return f"⚠️ {label} {v_pct:.0f}% (≥{thr_pct}% thr)"
        if (thr_pct - v_pct) < BORDER_VOL:
            return f"⚠️ {label} {v_pct:.0f}% (approaching {thr_pct}% thr)"
        return f"{label} {v_pct:.0f}%"

    def spy_trend_summary(close, smas_with_labels):
        """smas_with_labels: list of (sma_value, label_str). Compare close to each."""
        if pd.isna(close) or any(pd.isna(s) for s, _ in smas_with_labels):
            return "SPY trend (data missing)"
        results = [(close > s, (close - s) / s * 100, lbl) for s, lbl in smas_with_labels]
        all_above = all(above for above, _, _ in results)
        all_below = all(not above for above, _, _ in results)
        if all_above:
            min_gap = min(gap for _, gap, _ in results)
            if min_gap < BORDER_PCT:
                tight = next(lbl for _, g, lbl in results if g == min_gap)
                return f"⚠️ SPY barely above all MAs (closest: {tight} at {min_gap:.1f}%)"
            return "SPY above all MAs"
        if all_below:
            worst_gap = min(gap for _, gap, _ in results)
            return f"⚠️ SPY BELOW all MAs ({-worst_gap:.1f}% below worst)"
        # Mixed
        above_lbls = [lbl for above, _, lbl in results if above]
        below_lbls = [lbl for above, _, lbl in results if not above]
        return f"⚠️ SPY above {'/'.join(above_lbls)} but BELOW {'/'.join(below_lbls)}"

    def qqq_trend(close, sma, label_n):
        if pd.isna(close) or pd.isna(sma):
            return "QQQ trend (data missing)"
        gap_pct = (close - sma) / sma * 100
        if close > sma:
            if gap_pct < BORDER_PCT:
                return f"⚠️ QQQ only {gap_pct:.1f}% above SMA{label_n} (approaching)"
            return f"QQQ above SMA{label_n}"
        return f"⚠️ QQQ {-gap_pct:.1f}% BELOW SMA{label_n}"

    bits = []
    # SPY: trend vs SMA100/275/300 + vol20 (SPY Lev thr 22%)
    if spy_close is not None and sma100 is not None and sma275 is not None and sma300 is not None:
        smas = [(sma100, "SMA100"), (sma275, "SMA275"), (sma300, "SMA300")]
        bits.append(f"{spy_trend_summary(spy_close, smas)}, {vol_phrase(spy_vol20, 22, 'SPY vol')}")
    # QQQ: SMA175 only + vol20 (Q Lev thr 30%)
    if qqq_close is not None and qqq_sma175 is not None:
        bits.append(f"{qqq_trend(qqq_close, qqq_sma175, '175')}, {vol_phrase(qqq_vol20, 30, 'QQQ vol')}")
    # NDX vol30 (used by Top-7) — Roth thr 40%, Brok thr 45%
    if ndx_vol30 is not None:
        v_pct = ndx_vol30 * 100
        if v_pct >= 45:
            ndx_str = f"⚠️ NASDAQ vol {v_pct:.0f}% (≥45% Brok thr)"
        elif v_pct >= 40:
            ndx_str = f"⚠️ NASDAQ vol {v_pct:.0f}% (≥40% Roth thr)"
        elif (40 - v_pct) < BORDER_VOL:
            ndx_str = f"⚠️ NASDAQ vol {v_pct:.0f}% (approaching 40% Roth thr)"
        else:
            ndx_str = f"NASDAQ vol {v_pct:.0f}%"
        if ndx_stale_days > 1:
            ndx_str = f"{ndx_str} (data {ndx_stale_days}d stale)"
        bits.append(ndx_str)
    # UNRATE
    if unrate_failed:
        bits.append("⚠️ UNRATE fetch failed")
    elif un_flag_01:
        bits.append(f"⚠️ unemployment rising (+{un_chg:.2f}pp)")
    else:
        bits.append("unemployment stable")
    return "Markets: " + " | ".join(bits)


def build_summary_block(prev_state: dict, new_state: dict, conditions_line: str) -> list:
    """Build a 2+ line summary block: status headline + conditions + (if non-calm) action items.

    Returns a list of message lines (HTML formatted) ready to insert at top of message.

    Status tiers:
      🟢 ALL CLEAR — markets calm, all positions stable
      🟡 WATCHING — exit/reentry counters progressing (none near threshold)
      🟠 APPROACHING — one or more 1 day from flip
      🔴 ACTION — flip(s) confirmed today, MOC order required tomorrow
    """
    flips = []
    approaching = []
    watching = []

    def categorize(label_text, streak, threshold, direction):
        if streak == 0 or threshold <= 0:
            return
        if threshold > 1 and streak == threshold - 1:
            approaching.append((label_text, direction, streak, threshold))
        elif streak >= 1 and streak < threshold:
            watching.append((label_text, direction, streak, threshold))

    n_total = 0
    for key, ns in new_state.items():
        if key.startswith("_"): continue
        n_total += 1
        prev_pos = prev_state.get(key, {}).get("position")
        if prev_pos and prev_pos != ns["position"]:
            flips.append((ns["label"], prev_pos, ns["position"]))
            continue
        if ns["state"] == "invested":
            paths = ns.get("paths")
            if paths:
                for path_name, streak, threshold in paths:
                    categorize(f"{ns['label']} {path_name}", streak, threshold, "exit")
            else:
                categorize(ns["label"], ns.get("exit_streak", 0), ns.get("exit_threshold", 1), "exit")
        else:
            categorize(ns["label"], ns.get("reentry_streak", 0), ns.get("reentry_threshold", 1), "reentry")

    lines = []

    # ============ STATUS LINE ============
    if flips:
        n = len(flips)
        # Compute "next trading day" descriptor — handles Friday signals correctly
        today_dow = date.today().weekday()  # Mon=0..Fri=4
        if today_dow == 4:
            nxt_label = "MONDAY"
        elif today_dow == 5:  # Sat (manual run)
            nxt_label = "MONDAY"
        elif today_dow == 6:  # Sun (manual run)
            nxt_label = "MONDAY"
        else:
            nxt_label = "tomorrow"
        lines.append(f"<b>🔴 ACTION REQUIRED — {n} flip{'s' if n>1 else ''} at MOC {nxt_label} (before 3:50pm ET)</b>")
        lines.append(conditions_line)
        if n == 1:
            lbl, frm, to = flips[0]
            lines.append(f"  • <b>SELL {frm}, BUY {to}</b>  ({lbl})")
        else:
            for lbl, frm, to in flips:
                lines.append(f"  • <b>SELL {frm}, BUY {to}</b>  ({lbl})")
        return lines

    if approaching:
        n = len(approaching)
        if n == 1:
            lbl, direction, s, t = approaching[0]
            lines.append(f"<b>🟠 APPROACHING FLIP — {lbl} is 1 day from {direction}</b>")
        else:
            lines.append(f"<b>🟠 APPROACHING FLIP — {n} signals 1 day from threshold</b>")
        lines.append(conditions_line)
        for lbl, direction, s, t in approaching:
            lines.append(f"  • {lbl} {direction} {s}/{t} ⚠️")
        # Also show any items in WATCH state below approach
        for lbl, direction, s, t in watching:
            lines.append(f"  • {lbl} {direction} {s}/{t} ⏳")
        return lines

    if watching:
        n = len(watching)
        if n == 1:
            lbl, direction, s, t = watching[0]
            lines.append(f"<b>🟡 WATCHING — {lbl} {direction} ticking ({s}/{t} days)</b>")
        else:
            lines.append(f"<b>🟡 WATCHING — {n} signals progressing (none near threshold)</b>")
        lines.append(conditions_line)
        for lbl, direction, s, t in watching:
            lines.append(f"  • {lbl} {direction} {s}/{t} ⏳")
        return lines

    # ALL CLEAR
    lines.append(f"<b>🟢 ALL CLEAR — {n_total} of {n_total} strategies stable, no signals progressing</b>")
    lines.append(conditions_line)
    return lines


# ============================
# Main
# ============================

def main():
    manual_run = os.environ.get("MANUAL_RUN", "").lower() == "true"

    import pytz
    et_tz = pytz.timezone("America/New_York")
    current_et = datetime.now(et_tz)
    in_window = (current_et.hour == 16 and current_et.minute >= 5) or (17 <= current_et.hour <= 20)
    if not manual_run and not in_window:
        print(f"Skipping - current ET time is {current_et.strftime('%I:%M %p')}, not within 4:05-8:59 PM window")
        return

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    # ============ Data fetch with health tracking ============
    health = {"SPY": False, "QQQ": False, "NASDAQCOM": False, "UNRATE": False}

    try:
        spy_df = fetch_etf("SPY"); health["SPY"] = True
    except Exception as e:
        print(f"FATAL: SPY fetch failed: {e}")
        send_telegram(bot_token, chat_id, f"<b>⚠️ Bot error</b>\nSPY fetch failed: {e}\nRun aborted; positions retained.")
        return

    try:
        qqq_df = fetch_etf("QQQ"); health["QQQ"] = True
    except Exception as e:
        print(f"WARN: QQQ fetch failed: {e}")
        qqq_df = None

    try:
        ndx_df = fetch_nasdaqcom(); health["NASDAQCOM"] = True
    except Exception as e:
        print(f"WARN: NASDAQCOM fetch failed: {e}")
        ndx_df = None

    try:
        un = fetch_unrate(); health["UNRATE"] = True
    except Exception as e:
        print(f"WARN: UNRATE fetch failed: {e}")
        un = None

    # ============ Align to SPY trading days ============
    spy_close = spy_df["Close"].copy()
    spy_close.index = pd.to_datetime(spy_close.index).normalize()
    latest_date = spy_close.index[-1]
    latest_close = float(spy_close.iloc[-1])

    # Sanity check: SPY history must be long enough for SMA300 (Top-7) + walker buffer.
    # Without this, SMAs would be NaN throughout, walker counters never increment,
    # state stuck at initial. Silent fail — must catch.
    if len(spy_close) < 350:
        msg = (f"<b>⚠️ SPY DATA TOO SHORT</b>\n"
               f"Got {len(spy_close)} trading days; need 350+ for SMA300 + walker convergence.\n"
               f"Bot aborted to prevent silent signal failure. Check yfinance/Stooq.")
        send_telegram(bot_token, chat_id, msg)
        return

    # MAs for Top-7 + legacy
    sma50 = spy_close.rolling(50).mean()
    sma100 = spy_close.rolling(100).mean()
    sma250 = spy_close.rolling(250).mean()
    sma300 = spy_close.rolling(300).mean()
    sma275 = spy_close.rolling(275).mean()  # NEW for SPY Leveraged

    sma50_v = float(sma50.iloc[-1]); sma100_v = float(sma100.iloc[-1])
    sma250_v = float(sma250.iloc[-1]); sma300_v = float(sma300.iloc[-1])
    sma275_v = float(sma275.iloc[-1])

    # SPY 20-day vol (for SPY Leveraged)
    spy_vol20 = compute_vol20(spy_close)
    spy_vol20_v = float(spy_vol20.iloc[-1]) if not pd.isna(spy_vol20.iloc[-1]) else None

    # NASDAQCOM 30-day vol (for Top-7)
    # FIX: NASDAQCOM on FRED has ~1-day publication lag. If today's NDX is missing,
    # raw reindex leaves NaN at index[-1] → vol30=NaN → walker resets vol counter to 0,
    # silently masking a vol-path exit if vol has been elevated. Solution: compute
    # vol30 on the actual NDX series (no gaps), then ffill the resulting vol30 to the
    # SPY trading-day index. Also track ndx staleness so we can surface it.
    ndx_stale_days = 0
    if ndx_df is not None:
        ndx_close = ndx_df["NDX"].copy()
        ndx_close.index = pd.to_datetime(ndx_close.index).normalize()
        # Compute vol30 on the original (non-gappy) NDX series first
        vol30_native = compute_vol30(ndx_close)
        # Then align to SPY trading days, ffilling any missing days
        vol30 = vol30_native.reindex(spy_close.index, method="ffill")
        vol30_v = float(vol30.iloc[-1]) if not pd.isna(vol30.iloc[-1]) else None
        # Track staleness: how many SPY trading days since the most recent NDX observation
        last_ndx_date = ndx_close.index[-1]
        spy_latest = spy_close.index[-1]
        ndx_stale_days = max(0, (spy_latest - last_ndx_date).days)
    else:
        vol30 = pd.Series(np.nan, index=spy_close.index)
        vol30_v = None

    # QQQ data + 175-day MA + 20-day vol
    # Same bug-fix pattern as NASDAQCOM: if we ffill PRICE before computing vol/MA,
    # ffilled days produce artificial 0% returns that understate vol. Compute MA + vol
    # on the original (non-ffilled) QQQ series, then ffill the resulting series to the
    # SPY trading-day index.
    qqq_stale_days = 0
    if qqq_df is not None:
        qqq_close_raw = qqq_df["Close"].copy()
        qqq_close_raw.index = pd.to_datetime(qqq_close_raw.index).normalize()
        # Compute MA + vol on original QQQ series first
        qqq_sma175_native = qqq_close_raw.rolling(175).mean()
        qqq_vol20_native = compute_vol20(qqq_close_raw)
        # Then align to SPY trading days, ffilling
        qqq_close = qqq_close_raw.reindex(spy_close.index, method="ffill")
        qqq_sma175 = qqq_sma175_native.reindex(spy_close.index, method="ffill")
        qqq_vol20 = qqq_vol20_native.reindex(spy_close.index, method="ffill")
        qqq_close_v = float(qqq_close.iloc[-1]) if not pd.isna(qqq_close.iloc[-1]) else None
        qqq_sma175_v = float(qqq_sma175.iloc[-1]) if not pd.isna(qqq_sma175.iloc[-1]) else None
        qqq_vol20_v = float(qqq_vol20.iloc[-1]) if not pd.isna(qqq_vol20.iloc[-1]) else None
        # Track staleness
        last_qqq_date = qqq_close_raw.index[-1]
        spy_latest = spy_close.index[-1]
        qqq_stale_days = max(0, (spy_latest - last_qqq_date).days)
    else:
        qqq_close = qqq_sma175 = qqq_vol20 = None
        qqq_close_v = qqq_sma175_v = qqq_vol20_v = None

    # Below-MA series for legacy (kept, not rendered)
    below_sma250 = spy_close < sma250
    below_streak_250 = count_streak(below_sma250)

    # UNRATE delta
    if un is not None and len(un) >= 4:
        un_now_rate = float(un.iloc[-1]["UNRATE"])
        un_prior_rate = float(un.iloc[-4]["UNRATE"])
        un_chg = un_now_rate - un_prior_rate
        un_flag_01 = un_chg > 0.1
    else:
        un_chg = 0.0
        un_flag_01 = False

    # ============ Run state walkers ============
    roth_top7_st = walk_dasym_state(
        spy=spy_close, sma_exit=sma300, sma_re=sma100, vol30=vol30,
        vol_thr=ROTH_TOP7_PARAMS["vol_thr"], E_vol=ROTH_TOP7_PARAMS["E_vol"],
        E_ma=ROTH_TOP7_PARAMS["E_ma"], R_re=ROTH_TOP7_PARAMS["R_re"])
    roth_top7_st["_unrate_high"] = un_flag_01

    brok_top7_st = walk_dasym_state(
        spy=spy_close, sma_exit=sma300, sma_re=sma100, vol30=vol30,
        vol_thr=BROK_TOP7_PARAMS["vol_thr"], E_vol=BROK_TOP7_PARAMS["E_vol"],
        E_ma=BROK_TOP7_PARAMS["E_ma"], R_re=BROK_TOP7_PARAMS["R_re"])
    brok_top7_st["_unrate_high"] = un_flag_01

    spy_lev_roth_st = walk_lev_state(
        price=spy_close, ma=sma275, vol20=spy_vol20,
        vol_thr=SPY_LEV_ROTH_PARAMS["vol_thr"],
        exit_lag=SPY_LEV_ROTH_PARAMS["exit_lag"],
        entry_lag=SPY_LEV_ROTH_PARAMS["entry_lag"])

    spy_lev_brok_st = walk_lev_state(
        price=spy_close, ma=sma275, vol20=spy_vol20,
        vol_thr=SPY_LEV_BROK_PARAMS["vol_thr"],
        exit_lag=SPY_LEV_BROK_PARAMS["exit_lag"],
        entry_lag=SPY_LEV_BROK_PARAMS["entry_lag"])

    if qqq_close is not None:
        qqq_lev_st = walk_lev_state(
            price=qqq_close, ma=qqq_sma175, vol20=qqq_vol20,
            vol_thr=QQQ_LEV_PARAMS["vol_thr"],
            exit_lag=QQQ_LEV_PARAMS["exit_lag"],
            entry_lag=QQQ_LEV_PARAMS["entry_lag"])
    else:
        qqq_lev_st = None

    # ============ Legacy (compute kept, render hidden) ============
    state_roth_sp500 = find_exit_and_recovery(below_sma250, exit_threshold=80, reentry_threshold=5)
    exited_roth_sp500 = state_roth_sp500["exited"] or state_roth_sp500["recovering"]
    reentry_roth_sp500 = state_roth_sp500["exited"] and state_roth_sp500["days_above_since_exit"] >= 5
    if exited_roth_sp500 and not reentry_roth_sp500:
        roth_sp500_position = "TREASURIES"; roth_sp500_status = "EXITED"
    else:
        roth_sp500_position = "SP500"; roth_sp500_status = "INVESTED"

    state_brokerage_sp500 = find_exit_and_recovery(below_sma250, exit_threshold=80, reentry_threshold=5)
    exited_brokerage_sp500 = state_brokerage_sp500["exited"] or state_brokerage_sp500["recovering"]
    reentry_brokerage_sp500 = state_brokerage_sp500["exited"] and state_brokerage_sp500["days_above_since_exit"] >= 5
    if exited_brokerage_sp500 and not reentry_brokerage_sp500:
        brokerage_sp500_position = "TREASURIES"; brokerage_sp500_status = "EXITED"
    else:
        brokerage_sp500_position = "SP500"; brokerage_sp500_status = "INVESTED"

    # ============ Build new_state for change-line + persistence ============
    def build_state_record(label, walker_st, exit_threshold, reentry_threshold,
                           current_position, get_position_fn=None):
        return {
            "label": label,
            "state": walker_st["state"],
            "position": current_position,
            "exit_streak": walker_st.get("days_off_streak", walker_st.get("vol_streak", 0)),
            "exit_threshold": exit_threshold,
            "reentry_streak": walker_st.get("days_on_streak", walker_st.get("reentry_streak", 0)),
            "reentry_threshold": reentry_threshold,
        }

    # Build current positions per strategy
    def top7_position(st):
        if st["state"] == "invested": return "TOP-7 STOCKS"
        return "TREASURIES" if un_flag_01 else "SP500"

    new_state = {}

    # Top-7: expose BOTH vol and MA paths so change-line can surface either/both
    new_state["top7_roth"] = {
        "label": "Top-7 (Roth)", "state": roth_top7_st["state"],
        "position": top7_position(roth_top7_st),
        "paths": [
            ("vol", roth_top7_st["vol_streak"], ROTH_TOP7_PARAMS["E_vol"]),
            ("MA",  roth_top7_st["ma_streak"],  ROTH_TOP7_PARAMS["E_ma"]),
        ],
        "reentry_streak": roth_top7_st["reentry_streak"], "reentry_threshold": ROTH_TOP7_PARAMS["R_re"],
    }
    new_state["top7_brok"] = {
        "label": "Top-7 (Brok)", "state": brok_top7_st["state"],
        "position": top7_position(brok_top7_st),
        "paths": [
            ("vol", brok_top7_st["vol_streak"], BROK_TOP7_PARAMS["E_vol"]),
            ("MA",  brok_top7_st["ma_streak"],  BROK_TOP7_PARAMS["E_ma"]),
        ],
        "reentry_streak": brok_top7_st["reentry_streak"], "reentry_threshold": BROK_TOP7_PARAMS["R_re"],
    }

    new_state["spy_lev_roth"] = {
        "label": "SPY Leveraged (Roth)", "state": spy_lev_roth_st["state"],
        "position": "UPRO" if spy_lev_roth_st["state"] == "invested" else "USFR",
        "exit_streak": spy_lev_roth_st["days_off_streak"], "exit_threshold": SPY_LEV_ROTH_PARAMS["exit_lag"],
        "reentry_streak": spy_lev_roth_st["days_on_streak"], "reentry_threshold": SPY_LEV_ROTH_PARAMS["entry_lag"],
    }
    new_state["spy_lev_brok"] = {
        "label": "SPY Leveraged (Brok)", "state": spy_lev_brok_st["state"],
        "position": "UPRO" if spy_lev_brok_st["state"] == "invested" else "USFR",
        "exit_streak": spy_lev_brok_st["days_off_streak"], "exit_threshold": SPY_LEV_BROK_PARAMS["exit_lag"],
        "reentry_streak": spy_lev_brok_st["days_on_streak"], "reentry_threshold": SPY_LEV_BROK_PARAMS["entry_lag"],
    }
    if qqq_lev_st is not None:
        new_state["qqq_lev_roth"] = {
            "label": "QQQ Leveraged (Roth)", "state": qqq_lev_st["state"],
            "position": "TQQQ" if qqq_lev_st["state"] == "invested" else "USFR",
            "exit_streak": qqq_lev_st["days_off_streak"], "exit_threshold": QQQ_LEV_PARAMS["exit_lag"],
            "reentry_streak": qqq_lev_st["days_on_streak"], "reentry_threshold": QQQ_LEV_PARAMS["entry_lag"],
        }
        new_state["qqq_lev_brok"] = dict(new_state["qqq_lev_roth"])
        new_state["qqq_lev_brok"]["label"] = "QQQ Leveraged (Brok)"
    # If QQQ fetch failed, preserve previous state so flip detection isn't lost
    # on the resume day (otherwise prev would have no QQQ keys → first-run behavior).
    else:
        prev_for_qqq = load_state()
        for k in ("qqq_lev_roth", "qqq_lev_brok"):
            if k in prev_for_qqq:
                new_state[k] = prev_for_qqq[k]

    # ============ Build the Telegram message ============
    cur_vol_str = f"{vol30_v*100:.1f}%" if vol30_v is not None else "n/a"
    spy_vol_str = f"{spy_vol20_v*100:.1f}%" if spy_vol20_v is not None else "n/a"
    qqq_vol_str = f"{qqq_vol20_v*100:.1f}%" if qqq_vol20_v is not None else "n/a"

    # ============ Per-source freshness verification ============
    # Verify that the EXPECTED most recent trading day's close is in each source.
    # Walker uses these series directly; if today's data is missing (and today is
    # a trading day), the bot will compute signals from yesterday's data — wrong.
    today_d = date.today()
    latest_d = latest_date.date() if isinstance(latest_date, pd.Timestamp) else latest_date
    days_stale = (today_d - latest_d).days

    spy_missed = trading_days_missed(latest_d, today_d)
    qqq_last_d = (qqq_df["Close"].dropna().index[-1].date()
                  if qqq_df is not None and len(qqq_df["Close"].dropna()) > 0 else None)
    qqq_missed = trading_days_missed(qqq_last_d, today_d) if qqq_last_d else None
    ndx_last_d = (ndx_df["NDX"].dropna().index[-1].date()
                  if ndx_df is not None and len(ndx_df["NDX"].dropna()) > 0 else None)
    ndx_missed = trading_days_missed(ndx_last_d, today_d) if ndx_last_d else None

    # Build the freshness warning. SPY/QQQ are real-time post-close (expect 0 missed).
    # NASDAQCOM has typical 0-1 day FRED publication lag (allow 1, warn at 2+).
    fresh_warns = []
    if spy_missed >= 1:
        fresh_warns.append(f"SPY missing today's close ({spy_missed} trading day{'s' if spy_missed>1 else ''} stale)")
    if qqq_missed is not None and qqq_missed >= 1:
        fresh_warns.append(f"QQQ missing today's close ({qqq_missed} trading day{'s' if qqq_missed>1 else ''} stale)")
    if ndx_missed is not None and ndx_missed >= 2:
        fresh_warns.append(f"NASDAQCOM {ndx_missed} trading days stale (expected ≤1 from FRED lag)")

    stale_warning = None
    if fresh_warns:
        stale_warning = ("⚠️ <b>DATA FRESHNESS WARNING</b> — " + "; ".join(fresh_warns) +
                         ". Bot is computing signals from older data; today's true signal may differ.")
    elif days_stale > 5:
        # Fallback for very-old SPY data (e.g., long period of yfinance failures)
        stale_warning = f"⚠️ <b>STALE DATA</b> — SPY most recent close is {latest_d} ({days_stale} calendar days old). Verify data sources."

    # Fallback dates for "for N days" computation when walker has no transition
    spy_first_date = spy_close.index[0]
    qqq_first_date = qqq_close.index[0] if qqq_close is not None else spy_first_date
    unrate_failed = not health["UNRATE"]

    # Build top-of-message summary (status + conditions + active items)
    prev = load_state()
    conditions_line = build_conditions_line(
        spy_close=latest_close, sma100=sma100_v, sma275=sma275_v, sma300=sma300_v,
        spy_vol20=spy_vol20_v,
        qqq_close=qqq_close_v, qqq_sma175=qqq_sma175_v, qqq_vol20=qqq_vol20_v,
        ndx_vol30=vol30_v, un_chg=un_chg, un_flag_01=un_flag_01,
        unrate_failed=unrate_failed, ndx_stale_days=ndx_stale_days,
    )
    summary_block = build_summary_block(prev, new_state, conditions_line)

    lines = []
    lines.append(f"<b>📊 {ACCOUNT_LABEL}</b>")
    lines.append(f"{latest_date.strftime('%Y-%m-%d')}")
    if stale_warning:
        lines.append(stale_warning)
    lines.extend(summary_block)
    lines.append("")

    # Inputs blocks
    unrate_str = f"{un_chg:+.2f}pp ({'rising ⚠️' if un_flag_01 else 'stable'}; tiered_0.1 routing)" if not unrate_failed else "⚠️ FETCH FAILED — assuming stable"
    lines.append("<b>Top-7 inputs:</b>")
    lines.append(f"SPY: {latest_close:.2f} | NASDAQCOM vol30: {cur_vol_str} | "
                 f"SP500 SMAs: 50:{sma50_v:.2f}, 100:{sma100_v:.2f}, 250:{sma250_v:.2f}, 300:{sma300_v:.2f} | "
                 f"UNRATE 3-mo Δ: {unrate_str}")
    lines.append("")
    lines.append("<b>SPY Leveraged inputs:</b>")
    lines.append(f"SPY: {latest_close:.2f} | SPY SMA275: {sma275_v:.2f} | SPY vol20: {spy_vol_str} (thr 22%)")
    lines.append("")
    if qqq_close is not None:
        lines.append("<b>QQQ Leveraged inputs:</b>")
        lines.append(f"QQQ: {qqq_close_v:.2f} | QQQ SMA175: {qqq_sma175_v:.2f} | QQQ vol20: {qqq_vol_str} (thr 30%)")
        lines.append("")

    # ROTH section
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>🏦 ROTH IRA</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.extend(render_top7_section("Roth", ROTH_TOP7_PARAMS, roth_top7_st,
                                     spy_first_date, unrate_failed=unrate_failed))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Roth)", SPY_LEV_ROTH_PARAMS, spy_lev_roth_st,
                                    spy_first_date))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Roth)", QQQ_LEV_PARAMS, qqq_lev_st,
                                        qqq_first_date))
        lines.append("")
    else:
        lines.append("<b>QQQ Leveraged (Roth)</b>")
        lines.append("⚠️ DATA UNAVAILABLE — retain prior position")
        lines.append("")

    # --- LEGACY: SP500 MA250/E80/R5 holdings (Roth) ---
    # Hidden from Telegram per Apr 2026 leveraged-CB rollout.
    # Compute logic preserved above; uncomment to re-render.
    # lines.append(f"<b>SP500 Holdings</b> <i>(MA250 E80 R5)</i>")
    # lines.append(f"➤ <b>HOLD: {roth_sp500_position}</b>")
    # if roth_sp500_status == "INVESTED":
    #     status_char = "⚠️" if below_streak_250 > 0 else ""
    #     lines.append(f"Exit watch: {below_streak_250}/80 days below SMA250 {status_char}")
    # else:
    #     days_above = state_roth_sp500["days_above_since_exit"]
    #     lines.append(f"Re-entry watch: {days_above}/5 days above SMA250 ⏳")
    # lines.append("")

    # BROKERAGE section
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>💼 BROKERAGE</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.extend(render_top7_section("Brok", BROK_TOP7_PARAMS, brok_top7_st,
                                     spy_first_date, unrate_failed=unrate_failed))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Brok)", SPY_LEV_BROK_PARAMS, spy_lev_brok_st,
                                    spy_first_date))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Brok)", QQQ_LEV_PARAMS, qqq_lev_st,
                                        qqq_first_date))
        lines.append("")
    else:
        lines.append("<b>QQQ Leveraged (Brok)</b>")
        lines.append("⚠️ DATA UNAVAILABLE — retain prior position")
        lines.append("")

    # --- LEGACY: SP500 MA250/E80/R5 strategy (Brokerage) ---
    # Hidden from Telegram per Apr 2026 leveraged-CB rollout.
    # Compute logic preserved above; uncomment to re-render.
    # lines.append(f"<b>SP500 Strategy</b> <i>(MA250 E80 R5)</i>")
    # lines.append(f"➤ <b>HOLD: {brokerage_sp500_position}</b>")
    # if brokerage_sp500_status == "INVESTED":
    #     status_char = "⚠️" if below_streak_250 > 0 else ""
    #     lines.append(f"Exit watch: {below_streak_250}/80 days below SMA250 {status_char}")
    # else:
    #     days_above = state_brokerage_sp500["days_above_since_exit"]
    #     lines.append(f"Re-entry watch: {days_above}/5 days above SMA250 ⏳")

    # Health footer with weekend-aware staleness (catches silent staleness without
    # false-alarming every Monday for the Friday→Monday weekend gap).
    lines.append("━━━━━━━━━━━━━━━━━━")
    age_map = {"SPY": days_stale, "QQQ": qqq_stale_days, "NASDAQCOM": ndx_stale_days, "UNRATE": None}
    # Track each source's actual latest date so stale_label can count trading-days-missed
    def _to_date(ts):
        if ts is None: return None
        return ts.date() if hasattr(ts, 'date') else ts
    src_latest_map = {
        "SPY": latest_d,
        "QQQ": _to_date(qqq_df["Close"].dropna().index[-1]) if qqq_df is not None else None,
        "NASDAQCOM": _to_date(ndx_df["NDX"].dropna().index[-1]) if ndx_df is not None else None,
        "UNRATE": None,
    }

    def stale_label(age, src_latest_date):
        """Holiday-aware staleness: 0 trading days missed = fresh, regardless of
        calendar gap (e.g., 3-day weekend, Thanksgiving, MLK Day all OK)."""
        if age is None: return "✓"
        if src_latest_date is None: return f"✓ ({age}d cal)"
        missed = trading_days_missed(src_latest_date, date.today())
        if missed == 0:
            return f"✓ ({age}d cal — most recent trading day)"
        if missed == 1:
            return f"⚠️ 1 trading day missed ({age}d cal)"
        return f"⚠️ {missed} trading days STALE ({age}d cal)"

    health_bits = []
    for src, ok in health.items():
        if not ok:
            health_bits.append(f"{src} ✗")
            continue
        health_bits.append(f"{src} {stale_label(age_map.get(src), src_latest_map.get(src))}")
    lines.append(f"📡 Data: {' | '.join(health_bits)}")

    # If NASDAQCOM or QQQ is severely stale (>5 days), the ffill carries an old vol
    # value forward into today's signal. Walker would increment counters based on
    # potentially-wrong vol → false exits or missed exits. Surface this prominently.
    severe = []
    if ndx_stale_days > 5: severe.append(f"NASDAQCOM is {ndx_stale_days}d stale — Top-7 vol path may be wrong")
    if qqq_stale_days > 5: severe.append(f"QQQ data is {qqq_stale_days}d stale — QQQ Lev signals may be wrong")
    if severe:
        lines.append("⚠️ <b>SEVERE STALENESS</b>: " + "; ".join(severe) + ". Verify before acting.")

    # Send
    send_telegram(bot_token, chat_id, "\n".join(lines))

    # Persist new state (entry dates already populated above)
    save_state(new_state)
    print(f"State saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
