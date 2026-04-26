"""Daily Telegram regime monitor for ROTH and BROKERAGE accounts.

Updated 2026-04-25 to use D-asym circuit breakers (ALT-A for Roth, BROK_A for
Brokerage) selected in the Apr 2026 v2 iteration. Defensive routing uses
tiered_0.1 (UNRATE 3-mo change > 0.1pp -> Treasuries, else SP500).

Strategy specs (from /mnt/claude_momentum/6 Iterating further with Claude v2 Apr 2026/
SESSION_REVIEW_2026-04-25.md):

  ROTH (ALT-A): D-asym 300/50/100/5/0.40/10/tiered_0.1
    Exit (whichever fires first):
      Vol path:  NDX-composite 30d realized vol >= 0.40 for 10 days
      MA path:   SPY < SMA300 for 50 days
    Reentry:     SPY > SMA100 for 5 days
    Defensive routing: tiered_0.1

  BROKERAGE (BROK_A): D-asym 300/50/100/5/0.45/25/tiered_0.1
    Same as ROTH except vol_thr=0.45, E_vol=25 (slower vol confirmation
    reduces tax-costly false trips)

Backtest convention (matched here):
  - vol30 = 30-day rolling stdev of NASDAQCOM (FRED) simple daily returns,
            annualized by sqrt(252), min_periods=30
  - asym_state: strict comparisons (price < SMA, price > SMA, vol >= thr).
                Counters reset on every state transition.
  - Exec lag = 1 day (signal at end of day T -> trade at open of day T+1).
  - Tiered routing uses ur_3m from the prior trading day (no look-ahead).

The S&P500 Strategy sections (MA250 E80 R5) below are LEGACY and unchanged
from the prior iteration — they are NOT the recommended strategy. Use the
basket Top-7 sections.
"""

import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime

# ============================
# Data sources
# ============================

STOOQ_SPY_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

ACCOUNT_LABEL = "PORTFOLIO MONITOR"

# ============================
# Strategy parameters (from Apr 2026 v2 picks)
# ============================

ROTH_PARAMS = {
    "name": "ALT-A",
    "ma_exit": 300,    # SPY SMA window for MA-exit path
    "E_ma":    50,     # consecutive days SPY < SMA300 to trigger MA exit
    "ma_re":   100,    # SPY SMA window for reentry
    "R_re":    5,      # consecutive days SPY > SMA100 to reenter
    "vol_thr": 0.40,   # vol threshold (annualized stdev of simple returns)
    "E_vol":   10,     # consecutive days vol >= vol_thr to trigger vol exit
    "ur_thr":  0.1,    # tiered_0.1 routing threshold (pp)
}

BROK_PARAMS = {
    "name": "BROK_A",
    "ma_exit": 300,
    "E_ma":    50,
    "ma_re":   100,
    "R_re":    5,
    "vol_thr": 0.45,
    "E_vol":   25,
    "ur_thr":  0.1,
}

# ============================
# Data fetching
# ============================

def fetch_spy_yfinance() -> pd.DataFrame:
    """Primary source: yfinance (Yahoo Finance). Pull 3y to cover SMA300 + buffer."""
    import yfinance as yf
    df = yf.download("SPY", period="3y", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().title() for c in df.columns]
    if "Close" not in df.columns:
        raise ValueError(f"yfinance: 'Close' column not found. Got: {list(df.columns)}")
    df = df[["Close"]].dropna()
    if df.empty:
        raise ValueError("yfinance returned empty data")
    return df


def fetch_spy_stooq() -> pd.DataFrame:
    """Fallback source: Stooq CSV."""
    try:
        df = pd.read_csv(STOOQ_SPY_URL)
    except Exception as e:
        raise ValueError(f"Stooq CSV read failed: {e}")
    if df.empty or "Close" not in df.columns:
        raise ValueError(f"Stooq returned unusable data. Columns: {list(df.columns) if not df.empty else 'empty'}")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df[["Close"]].dropna()


def fetch_spy_history() -> pd.DataFrame:
    """Try yfinance first, fall back to Stooq."""
    try:
        return fetch_spy_yfinance()
    except Exception as e:
        print(f"yfinance failed: {e}, falling back to Stooq")
    try:
        return fetch_spy_stooq()
    except Exception as e:
        raise RuntimeError(f"All SPY data sources failed. Last error: {e}")


def fetch_nasdaqcom_fred_api(api_key: str, observation_start: str = None) -> pd.DataFrame:
    """Fetch NASDAQCOM (Nasdaq Composite) daily series from FRED API.

    Matches the backtest's vol30 input series exactly (fwk_core.py reads
    nasdaqcom.csv from FRED's NASDAQCOM series).
    """
    params = {
        "series_id": "NASDAQCOM",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if observation_start:
        params["observation_start"] = observation_start
    resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    records = []
    for obs in data.get("observations", []):
        date_str = obs.get("date")
        value_str = obs.get("value")
        if date_str and value_str and value_str != ".":
            try:
                records.append({"Date": pd.to_datetime(date_str), "NDX": float(value_str)})
            except (ValueError, TypeError):
                continue
    df = pd.DataFrame(records).sort_values("Date").set_index("Date")
    return df


def fetch_nasdaqcom_csv_fallback() -> pd.DataFrame:
    """Fallback: scrape NASDAQCOM from FRED CSV graph endpoint."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM"
    df = pd.read_csv(url)
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "Date", "NASDAQCOM": "NDX"})
    df["Date"] = pd.to_datetime(df["Date"])
    df["NDX"] = pd.to_numeric(df["NDX"], errors="coerce")
    df = df.dropna().sort_values("Date").set_index("Date")
    return df


def fetch_nasdaqcom() -> pd.DataFrame:
    """Try FRED API first, fall back to CSV. Returns last ~3 years."""
    api_key = os.environ.get("FRED_API_KEY")
    # Pull 3 years back to ensure we have enough for SMA300 + state walker convergence
    obs_start = (pd.Timestamp.today() - pd.Timedelta(days=365 * 4)).strftime("%Y-%m-%d")
    if api_key:
        try:
            df = fetch_nasdaqcom_fred_api(api_key, observation_start=obs_start)
            if not df.empty:
                return df
        except Exception as e:
            print(f"FRED NASDAQCOM API failed: {e}, falling back to CSV")
    df = fetch_nasdaqcom_csv_fallback()
    # Trim to last 4 years
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=365 * 4)
    return df[df.index >= cutoff].copy()


def fetch_unrate_fred_api(api_key: str) -> pd.DataFrame:
    """Fetch UNRATE from FRED API."""
    params = {
        "series_id": "UNRATE",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 12,
    }
    resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    records = []
    for obs in data.get("observations", []):
        date_str = obs.get("date")
        value_str = obs.get("value")
        if date_str and value_str and value_str != ".":
            records.append({"DATE": pd.to_datetime(date_str), "UNRATE": float(value_str)})
    return pd.DataFrame(records).sort_values("DATE").reset_index(drop=True)


def fetch_unrate_fallback() -> pd.DataFrame:
    """Fallback: fetch UNRATE from FRED CSV."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"
    df = pd.read_csv(url)
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "DATE"})
    df["DATE"] = pd.to_datetime(df["DATE"])
    df["UNRATE"] = pd.to_numeric(df["UNRATE"], errors="coerce")
    df = df.dropna().sort_values("DATE").reset_index(drop=True)
    return df.tail(12).reset_index(drop=True)


def fetch_unrate() -> pd.DataFrame:
    """Try FRED API first, fall back to CSV scraping."""
    api_key = os.environ.get("FRED_API_KEY")
    if api_key:
        try:
            return fetch_unrate_fred_api(api_key)
        except Exception as e:
            print(f"FRED API failed: {e}, falling back to CSV")
    return fetch_unrate_fallback()


# ============================
# Vol calculation (matches fwk_core.py exactly)
# ============================

def compute_vol30(ndx_series: pd.Series) -> pd.Series:
    """30-day rolling stdev of simple returns, annualized by sqrt(252).

    Matches fwk_core.py line 63:
      ndx_ret = ndx[1:] / ndx[:-1] - 1.0
      vol30 = pd.Series(ndx_ret).rolling(30, min_periods=30).std().values * np.sqrt(252)
    """
    simple_ret = ndx_series.pct_change()
    vol = simple_ret.rolling(30, min_periods=30).std() * np.sqrt(252)
    return vol


# ============================
# D-asym state walker (matches fwk_core.asym_state exactly)
# ============================

def walk_dasym_state(spy: pd.Series, sma_exit: pd.Series, sma_re: pd.Series,
                      vol30: pd.Series, vol_thr: float, E_vol: int, E_ma: int, R_re: int) -> dict:
    """Walk through the joint history to determine current D-asym state and counters.

    All inputs must be aligned on the same index (SPY trading days). vol30 will
    have NaN for days where NASDAQCOM was missing or for the first 29 days; we
    handle NaN exactly as fwk_core does (NaN vol does NOT increment ev counter).

    State semantics:
      - Initial state assumed 'invested' at the start of the window. We need
        enough history (>= max(ma_exit, ma_re) days, plus a buffer for the
        state walker to converge through any past transitions) for the current
        state to be reliable.
      - On each day i:
          ev (vol counter): +=1 if vol30[i] >= vol_thr (and not NaN), else 0
          em (MA-exit counter): +=1 if SPY[i] < SMA[ma_exit][i], else 0
          rc (reentry counter): +=1 if SPY[i] > SMA[ma_re][i], else 0
          if invested: trigger defensive if ev >= E_vol OR em >= E_ma; reset all
          if defensive: trigger reentry if rc >= R_re; reset all

    Returns dict with: state, vol_streak, ma_streak, reentry_streak,
    last_transition_date, last_transition_reason, current_vol, current_spy,
    current_sma_exit, current_sma_re, latest_date.
    """
    state = "invested"
    ev = em = rc = 0
    last_transition_date = None
    last_transition_reason = None

    for i in range(len(spy)):
        p = spy.iloc[i]
        mx = sma_exit.iloc[i]
        mr = sma_re.iloc[i]
        v = vol30.iloc[i] if i < len(vol30) else np.nan
        date = spy.index[i]
        if pd.isna(p) or pd.isna(mx) or pd.isna(mr):
            continue

        # Update counters
        if not pd.isna(v) and v >= vol_thr:
            ev += 1
        else:
            ev = 0
        if p < mx:
            em += 1
        else:
            em = 0
        if p > mr:
            rc += 1
        else:
            rc = 0

        # State transitions
        if state == "invested":
            if ev >= E_vol or em >= E_ma:
                last_transition_date = date
                last_transition_reason = "vol path" if ev >= E_vol else "MA path"
                state = "defensive"
                ev = em = rc = 0
        else:  # defensive
            if rc >= R_re:
                last_transition_date = date
                last_transition_reason = "reentry"
                state = "invested"
                ev = em = rc = 0

    return {
        "state": state,
        "vol_streak": ev,
        "ma_streak": em,
        "reentry_streak": rc,
        "last_transition_date": last_transition_date,
        "last_transition_reason": last_transition_reason,
        "current_vol": float(vol30.iloc[-1]) if not pd.isna(vol30.iloc[-1]) else None,
        "current_spy": float(spy.iloc[-1]),
        "current_sma_exit": float(sma_exit.iloc[-1]) if not pd.isna(sma_exit.iloc[-1]) else None,
        "current_sma_re": float(sma_re.iloc[-1]) if not pd.isna(sma_re.iloc[-1]) else None,
        "latest_date": spy.index[-1],
    }


# ============================
# Helpers
# ============================

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
    """Legacy DMA exit/recovery detector (used by the SP500 holdings sections)."""
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
        return {"exited": below_streak >= exit_threshold, "exit_day": None,
                "days_above_since_exit": 0, "recovering": False,
                "reentry_threshold": reentry_threshold}

    idx_start_of_above = len(values) - days_above
    if idx_start_of_above <= 0:
        return {"exited": False, "exit_day": None, "days_above_since_exit": days_above,
                "recovering": False, "reentry_threshold": reentry_threshold}

    below_streak_before = 0
    for i in range(idx_start_of_above - 1, -1, -1):
        if values[i]:
            below_streak_before += 1
        else:
            break

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
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


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

    # --- Load data ---
    spy_df = fetch_spy_history()              # SPY daily close, ~3y
    ndx_df = fetch_nasdaqcom()                # NASDAQCOM daily close, ~4y
    un = fetch_unrate()                        # UNRATE monthly, last 12

    # Align NASDAQCOM to SPY trading days (forward-fill missing NDX days to match)
    spy_close = spy_df["Close"].copy()
    spy_close.index = pd.to_datetime(spy_close.index).normalize()
    ndx_close = ndx_df["NDX"].copy()
    ndx_close.index = pd.to_datetime(ndx_close.index).normalize()

    # Align: reindex NASDAQCOM to SPY's trading days
    ndx_aligned = ndx_close.reindex(spy_close.index)
    # If FRED has gaps within SPY's trading days, leave as NaN — vol calc will skip
    vol30 = compute_vol30(ndx_aligned)

    latest_date = spy_close.index[-1]
    latest_close = float(spy_close.iloc[-1])

    # ============================
    # Calculate Moving Averages
    # ============================

    sma50 = spy_close.rolling(50).mean()
    sma100 = spy_close.rolling(100).mean()
    sma250 = spy_close.rolling(250).mean()
    sma300 = spy_close.rolling(300).mean()

    sma50_value = float(sma50.iloc[-1])
    sma100_value = float(sma100.iloc[-1])
    sma250_value = float(sma250.iloc[-1])
    sma300_value = float(sma300.iloc[-1])

    below_sma50 = spy_close < sma50
    below_sma250 = spy_close < sma250

    below_streak_50 = count_streak(below_sma50)
    below_streak_250 = count_streak(below_sma250)

    # ============================
    # UNRATE logic (3-month change)
    # ============================

    un_now = un.iloc[-1]
    un_prior = un.iloc[-4] if len(un) >= 4 else un.iloc[0]
    un_now_rate = float(un_now["UNRATE"])
    un_prior_rate = float(un_prior["UNRATE"])
    un_chg = un_now_rate - un_prior_rate

    # Tiered_0.1 flag (used by both new basket strategies)
    un_flag_01 = un_chg > 0.1
    # Legacy 0.3 flag is no longer used — kept here for reference / removable
    un_flag_03 = un_chg > 0.3

    # ============================
    # ROTH BASKET: ALT-A (D-asym 300/50/100/5/0.40/10/tiered_0.1)
    # ============================

    roth_state = walk_dasym_state(
        spy=spy_close, sma_exit=sma300, sma_re=sma100, vol30=vol30,
        vol_thr=ROTH_PARAMS["vol_thr"], E_vol=ROTH_PARAMS["E_vol"],
        E_ma=ROTH_PARAMS["E_ma"], R_re=ROTH_PARAMS["R_re"],
    )

    if roth_state["state"] == "invested":
        roth_position = "TOP-7 STOCKS"
    else:
        roth_position = "TREASURIES" if un_flag_01 else "SP500"

    # ============================
    # ROTH SP500 HOLDINGS: legacy MA250_E80_R5 (UNCHANGED)
    # NOTE: this is the legacy SP500-side overlay, NOT the recommended Top-7 pick.
    # ============================

    state_roth_sp500 = find_exit_and_recovery(below_sma250, exit_threshold=80, reentry_threshold=5)
    exited_roth_sp500 = state_roth_sp500["exited"] or state_roth_sp500["recovering"]
    reentry_roth_sp500 = state_roth_sp500["exited"] and state_roth_sp500["days_above_since_exit"] >= 5

    if exited_roth_sp500 and not reentry_roth_sp500:
        roth_sp500_position = "TREASURIES"
        roth_sp500_status = "EXITED"
    else:
        roth_sp500_position = "SP500"
        roth_sp500_status = "INVESTED"

    # ============================
    # BROKERAGE BASKET: BROK_A (D-asym 300/50/100/5/0.45/25/tiered_0.1)
    # ============================

    brok_state = walk_dasym_state(
        spy=spy_close, sma_exit=sma300, sma_re=sma100, vol30=vol30,
        vol_thr=BROK_PARAMS["vol_thr"], E_vol=BROK_PARAMS["E_vol"],
        E_ma=BROK_PARAMS["E_ma"], R_re=BROK_PARAMS["R_re"],
    )

    if brok_state["state"] == "invested":
        brok_position = "TOP-7 STOCKS"
    else:
        brok_position = "TREASURIES" if un_flag_01 else "SP500"

    # ============================
    # BROKERAGE SP500 HOLDINGS: legacy MA250_E80_R5 (UNCHANGED)
    # ============================

    state_brokerage_sp500 = find_exit_and_recovery(below_sma250, exit_threshold=80, reentry_threshold=5)
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

    cur_vol = roth_state["current_vol"]   # same series, same value for both Roth/Brok
    vol_str = f"{cur_vol*100:.1f}%" if cur_vol is not None else "n/a"

    lines = []
    lines.append(f"<b>📊 {ACCOUNT_LABEL}</b>")
    lines.append(f"{latest_date.strftime('%Y-%m-%d')} | SPY: {latest_close:.2f} | NDXcomp vol30: {vol_str}")
    lines.append(f"SMA50: {sma50_value:.2f} | SMA100: {sma100_value:.2f} | SMA250: {sma250_value:.2f} | SMA300: {sma300_value:.2f}")
    lines.append(f"UNRATE 3-mo Δ: {un_chg:+.2f}pp ({'rising ⚠️' if un_flag_01 else 'stable'}; tiered_0.1 routing)")
    lines.append("")

    # --- ROTH ACCOUNT SECTION ---
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>🏦 ROTH IRA</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")

    lines.append(f"<b>Top-7 Strategy — ALT-A</b>")
    lines.append(f"<i>(D-asym MA{ROTH_PARAMS['ma_exit']}/E_ma{ROTH_PARAMS['E_ma']}/MA{ROTH_PARAMS['ma_re']}/R_re{ROTH_PARAMS['R_re']}/vol{int(ROTH_PARAMS['vol_thr']*100)}%/E_vol{ROTH_PARAMS['E_vol']} + tiered_0.1)</i>")
    lines.append(f"➤ <b>HOLD: {roth_position}</b>")

    if roth_state["state"] == "invested":
        vol_warn = "⚠️" if roth_state["vol_streak"] > 0 else ""
        ma_warn = "⚠️" if roth_state["ma_streak"] > 0 else ""
        lines.append(f"Vol exit watch: {roth_state['vol_streak']}/{ROTH_PARAMS['E_vol']} days NDX vol30 ≥ {int(ROTH_PARAMS['vol_thr']*100)}% {vol_warn}")
        lines.append(f"MA exit watch:  {roth_state['ma_streak']}/{ROTH_PARAMS['E_ma']} days SPY below SMA{ROTH_PARAMS['ma_exit']} {ma_warn}")
    else:
        lines.append(f"Reentry watch: {roth_state['reentry_streak']}/{ROTH_PARAMS['R_re']} days SPY above SMA{ROTH_PARAMS['ma_re']} ⏳")
        if roth_state["last_transition_date"] is not None:
            tdate = roth_state["last_transition_date"].strftime("%Y-%m-%d")
            lines.append(f"<i>Exited {tdate} via {roth_state['last_transition_reason']}</i>")
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

    lines.append(f"<b>Top-7 Strategy — BROK_A</b>")
    lines.append(f"<i>(D-asym MA{BROK_PARAMS['ma_exit']}/E_ma{BROK_PARAMS['E_ma']}/MA{BROK_PARAMS['ma_re']}/R_re{BROK_PARAMS['R_re']}/vol{int(BROK_PARAMS['vol_thr']*100)}%/E_vol{BROK_PARAMS['E_vol']} + tiered_0.1)</i>")
    lines.append(f"➤ <b>HOLD: {brok_position}</b>")

    if brok_state["state"] == "invested":
        vol_warn = "⚠️" if brok_state["vol_streak"] > 0 else ""
        ma_warn = "⚠️" if brok_state["ma_streak"] > 0 else ""
        lines.append(f"Vol exit watch: {brok_state['vol_streak']}/{BROK_PARAMS['E_vol']} days NDX vol30 ≥ {int(BROK_PARAMS['vol_thr']*100)}% {vol_warn}")
        lines.append(f"MA exit watch:  {brok_state['ma_streak']}/{BROK_PARAMS['E_ma']} days SPY below SMA{BROK_PARAMS['ma_exit']} {ma_warn}")
    else:
        lines.append(f"Reentry watch: {brok_state['reentry_streak']}/{BROK_PARAMS['R_re']} days SPY above SMA{BROK_PARAMS['ma_re']} ⏳")
        if brok_state["last_transition_date"] is not None:
            tdate = brok_state["last_transition_date"].strftime("%Y-%m-%d")
            lines.append(f"<i>Exited {tdate} via {brok_state['last_transition_reason']}</i>")
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
