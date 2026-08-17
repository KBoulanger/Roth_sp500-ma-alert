"""Daily Telegram regime monitor for ROTH and BROKERAGE accounts.

Built on 2026-04-26 final (sha256 fdad9c62ebd9...) with the following changes:

  ChatGPT review:
    + Flip-day display: HOLD NOW (current actual position until tomorrow's MOC)
      vs TOMORROW MOC (target after execution). Days-counter line shows the
      pending-execution clarifier. Avoids implying the new asset is held today.

  Bug fixes (audit, 2026-04-27):
    + NASDAQCOM ffill bug — vol30 now computed on raw native NASDAQCOM series, then
      ffilled onto SPY trading days. Previously ffilled prices first → fake
      0% returns understated vol when FRED published 1 day late.
    + QQQ ffill bug — same pattern, same fix for vol20 and SMA175.
    + SPY data length sanity check (aborts if <350 days; SMA300 needs it).
    + QQQ fetch failure now explicitly reloads prior state from state.json.
    + Holiday-aware staleness — USFederalHolidayCalendar trading-day count
      replaces calendar-day count (won't false-alarm after holidays).

  UX upgrades:
    + 🟢 ALL CLEAR / 🟡 WATCHING / 🟠 APPROACHING / 🔴 ACTION status block
    + 'Markets:' conditions line — SPY trend across all 3 SMAs collapsed,
      SPY vol20, QQQ trend, NASDAQ vol30, UNRATE.
    + Top-7 summary surfaces BOTH vol AND MA paths separately.
    + Action wording: 'MOC tomorrow' / 'MOC MONDAY (before 3:50pm ET)'
      with explicit 'SELL X, BUY Y'.
    + UNRATE-fetch-failure made visible in Top-7 routing line + Markets line.
    + Severe staleness banner if NASDAQCOM/QQQ >5 days stale.
    + Health footer upgraded from ✓/✗ to 'most recent trading day' /
      'N trading days STALE' detail.
    + NASDAQCOM source: yfinance ^IXIC primary (real-time post-close), FRED
      fallback. Verified equivalent to FRED NASDAQCOM (max diff 0.000044%).

  Cleanup:
    + Removed legacy SP500 MA250/E80/R5 dead code (find_exit_and_recovery,
      count_streak, commented renders).
    + Removed LTCG / entry_date tracking. Replaced with walker's
      last_transition_date → 'Currently <X> for N days (since signal flip on
      YYYY-MM-DD)'.

  Strategy specs:
    ROTH (ALT-A): D-asym 300/50/100/5/0.40/10/tiered_0.1  + runup ratchet G60/S25 (Roth only,
                  added 2026-08-14; third exit path on the basket's own NAV, locked to Jan rebalance)
    BROK (BROK_A): D-asym 300/50/100/5/0.45/25/tiered_0.1
    SPY Leveraged R+B:  MA300 v<21% e=1 r=1 → UPRO ↔ USFR
    QQQ Leveraged R+B:  MA150 v<31% e=4 r=1 → TQQQ ↔ USFR

    Config change 2026-08-12. Previously: SPY MA275 v<22% e=1 r=10 (Roth) /
    e=2 r=10 (Brok); QQQ MA175 v<30% e=2 r=2. Both accounts now share one config
    per sleeve. Rationale and supporting analysis are in the change log at the end
    of "Investment strategy implementation". Short version: the old SPY settings sat
    one to two steps from an exit-lag cliff where drawdown jumps to -78%/-84%; vol<21%
    with e=1 sits three steps clear at unchanged CAGR. The new QQQ settings raise CAGR
    and rank top-0.3%% in both halves of the sample, where the old ones ranked top-10%%
    in the first half only.
"""

import os
import json
import pandas as pd
import numpy as np
import requests
from datetime import datetime, date, timedelta
from pandas.tseries.holiday import USFederalHolidayCalendar


def today_et():
    """Current calendar date in America/New_York.

    GitHub Actions runners use UTC. Manual runs after 8pm ET during daylight
    time are already the next UTC date, which can make the bot falsely expect
    tomorrow's market close. All market freshness/execution-day messaging should
    use ET, not runner-local/UTC date.
    """
    import pytz
    return datetime.now(pytz.timezone("America/New_York")).date()


def trading_days_missed(latest_d, today_d):
    """Count NYSE trading days between latest_d (exclusive) and today_d (inclusive)
    that should have produced data. Accounts for weekends + US federal holidays.
    Returns 0 if data is fresh (latest_d is the most recent expected trading day)."""
    if today_d <= latest_d:
        return 0
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
# Runup ratchet — ROTH ONLY (added 2026-08-14). Third exit path on the Top-7 Roth sleeve.
# Arms when the Top-7 basket is >= +ARM_GAIN vs its January baseline; fires when it closes
# >= STOP_DROP below its post-arm high; then locked out until the next January rebalance.
TOP7_RATCHET_PARAMS = {
    "arm_gain": 0.60,
    "stop_drop": 0.25,
    "basket_file": "top7_basket.json",
    "roth_only": True,
}
SPY_LEV_ROTH_PARAMS = {
    "ma": 300, "vol_thr": 0.21, "vol_window": 20, "exit_lag": 1, "entry_lag": 1,
    "leveraged": "UPRO", "defensive": "USFR", "signal_asset": "SPY",
}
SPY_LEV_BROK_PARAMS = {
    # identical to Roth since 2026-08-12; the old brokerage-only exit_lag=2 was dropped
    "ma": 300, "vol_thr": 0.21, "vol_window": 20, "exit_lag": 1, "entry_lag": 1,
    "leveraged": "UPRO", "defensive": "USFR", "signal_asset": "SPY",
}
QQQ_LEV_PARAMS = {
    # e=4 / r=1 is deliberate: slow to exit, fast to re-enter
    "ma": 150, "vol_thr": 0.31, "vol_window": 20, "exit_lag": 4, "entry_lag": 1,
    "leveraged": "TQQQ", "defensive": "USFR", "signal_asset": "QQQ",
}

# ============================
# Data fetching
# ============================

def fetch_etf_yfinance(ticker: str) -> pd.DataFrame:
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
    url = STOOQ_SPY_URL if ticker.upper() == "SPY" else STOOQ_QQQ_URL
    df = pd.read_csv(url)
    if df.empty or "Close" not in df.columns:
        raise ValueError(f"Stooq {ticker} returned unusable data")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df[["Close"]].dropna()


def fetch_etf(ticker: str) -> pd.DataFrame:
    try:
        return fetch_etf_yfinance(ticker)
    except Exception as e:
        print(f"yfinance {ticker} failed: {e}, falling back to Stooq")
    try:
        return fetch_etf_stooq(ticker)
    except Exception as e:
        raise RuntimeError(f"All {ticker} sources failed. Last error: {e}")


def fetch_nasdaqcom_yfinance() -> pd.DataFrame:
    """yfinance ^IXIC (Nasdaq Composite) — real-time post-close.
    Verified equivalent to FRED NASDAQCOM (max diff 0.000044% over 60d)."""
    import yfinance as yf
    df = yf.download("^IXIC", period="4y", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().title() for c in df.columns]
    if "Close" not in df.columns:
        raise ValueError(f"yfinance ^IXIC: 'Close' not found")
    df = df[["Close"]].dropna().rename(columns={"Close": "NASDAQCOM"})
    if df.empty:
        raise ValueError("yfinance ^IXIC returned empty")
    return df


def fetch_nasdaqcom_fred_api(api_key, observation_start=None) -> pd.DataFrame:
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
                records.append({"Date": pd.to_datetime(d), "NASDAQCOM": float(v)})
            except (ValueError, TypeError):
                continue
    return pd.DataFrame(records).sort_values("Date").set_index("Date")


def fetch_nasdaqcom_csv_fallback() -> pd.DataFrame:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM"
    df = pd.read_csv(url)
    date_col = "DATE" if "DATE" in df.columns else "observation_date"
    df = df.rename(columns={date_col: "Date", "NASDAQCOM": "NASDAQCOM"})
    df["Date"] = pd.to_datetime(df["Date"])
    df["NASDAQCOM"] = pd.to_numeric(df["NASDAQCOM"], errors="coerce")
    return df.dropna().sort_values("Date").set_index("Date")


def fetch_nasdaqcom() -> pd.DataFrame:
    """yfinance ^IXIC primary (real-time), FRED fallbacks."""
    try:
        df = fetch_nasdaqcom_yfinance()
        if not df.empty:
            return df
    except Exception as e:
        print(f"yfinance ^IXIC failed: {e}, falling back to FRED")
    api_key = os.environ.get("FRED_API_KEY")
    obs_start = (pd.Timestamp.today() - pd.Timedelta(days=365 * 4)).strftime("%Y-%m-%d")
    if api_key:
        try:
            df = fetch_nasdaqcom_fred_api(api_key, observation_start=obs_start)
            if not df.empty:
                return df
        except Exception as e:
            print(f"FRED NASDAQCOM API failed: {e}, falling back to CSV")
    df = fetch_nasdaqcom_csv_fallback()
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=365 * 4)
    return df[df.index >= cutoff].copy()


def fetch_unrate_fred_api(api_key) -> pd.DataFrame:
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

def compute_vol30(nasdaqcom_series: pd.Series) -> pd.Series:
    return nasdaqcom_series.pct_change().rolling(30, min_periods=30).std() * np.sqrt(252)


def compute_vol20(price_series: pd.Series) -> pd.Series:
    return price_series.pct_change().rolling(20, min_periods=20).std() * np.sqrt(252)


# ============================
# State walkers (matches fwk_core.asym_state to 1e-15)
# ============================

def walk_dasym_state(spy, sma_exit, sma_re, vol30, vol_thr, E_vol, E_ma, R_re):
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
        # Reentry uses >= to match Apr 26 backtest convention.
        # Verify your simulator's exact inequality before changing this line.
        if p >= mr: rc += 1
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


def walk_lev_state(price, ma, vol20, vol_thr, exit_lag, entry_lag):
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
        else:
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

def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def days_in_strategy(walker_state, fallback_first_date):
    """Calendar days since walker's last_transition_date. (signal_days, exact)."""
    asof_d = today_et()
    ltd = walker_state.get("last_transition_date")
    if ltd is not None:
        if isinstance(ltd, pd.Timestamp):
            ltd = ltd.date()
        return max(0, (asof_d - ltd).days), True
    if fallback_first_date is not None:
        if isinstance(fallback_first_date, pd.Timestamp):
            fallback_first_date = fallback_first_date.date()
        return max(0, (asof_d - fallback_first_date).days), False
    return 0, False


def hourglass_if_progressing(streak, threshold):
    if streak == 0:
        return ""
    if streak >= threshold - 1:
        return "⚠️"
    return "⏳"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to read {STATE_FILE}: {e}")
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def state_actual_position(entry):
    """Backward-compatible actual holding.

    Older state files only had `position`; new files separate the currently held
    position from the latest signal target.
    """
    return entry.get("actual_position") or entry.get("position")


def state_signal_position(entry):
    return entry.get("signal_position") or entry.get("position")


def apply_actual_vs_signal_state(prev_state, new_state, signal_date):
    """Persist actual holdings separately from signal targets.

    Signals are calculated from the latest close, but trades are assumed to be
    executed at the next MOC. If a prior pending order exists and we are now on a
    later signal date, mark it executed before comparing the new signal.
    """
    signal_date_s = signal_date.isoformat() if hasattr(signal_date, "isoformat") else str(signal_date)
    for key, ns in new_state.items():
        if key.startswith("_"):
            continue
        prev = prev_state.get(key, {})
        target = state_signal_position(ns)
        actual = state_actual_position(prev) or target

        pending = prev.get("pending_order") or {}
        pending_signal_date = pending.get("signal_date")
        pending_target = pending.get("buy")
        if pending_target and pending_signal_date and str(pending_signal_date) < signal_date_s:
            # Previous next-MOC order should now be reflected in actual holdings.
            actual = pending_target

        ns["signal_position"] = target
        ns["actual_position"] = actual
        ns["position"] = actual  # legacy field now means current holding
        ns.pop("pending_order", None)
        if target and actual and target != actual:
            ns["pending_order"] = {
                "sell": actual,
                "buy": target,
                "signal_date": signal_date_s,
                "execution": "next MOC",
            }
    return new_state


def carry_forward_guarded_state(prev_state, new_state, keys, reason):
    """Do not advance affected strategies when required native data are stale."""
    for key in keys:
        if key in prev_state:
            carried = dict(prev_state[key])
            carried["data_guarded"] = reason
            new_state[key] = carried
    return new_state


# ============================
# Render helpers (with ChatGPT flip-day fix)
# ============================

def _format_days_in_strategy(currently, walker_state, fallback_first_date,
                              flipped_today=False, prev_position=None):
    """Builds the 'Currently X for N days' line. On flip day, makes clear that the
    position changes are pending (signal vs executed)."""
    if flipped_today and prev_position and prev_position != currently:
        return (f"<i>⚠️ FLIP TODAY: signal switched to <b>{currently}</b>. "
                f"You still HOLD <b>{prev_position}</b> until tomorrow's MOC executes.</i>")
    n_days, exact = days_in_strategy(walker_state, fallback_first_date)
    if exact:
        ltd = walker_state["last_transition_date"]
        ltd_str = ltd.date() if isinstance(ltd, pd.Timestamp) else ltd
        return f"<i>Currently {currently} for {n_days} days (since signal flip on {ltd_str})</i>"
    return f"<i>Currently {currently} for {n_days}+ days (no flip in current data window)</i>"


def load_basket_config(path, today):
    """Load the Top-7 basket config. Returns (cfg, warning). cfg is None if unusable.
    Refuses to run the ratchet on a stale file — a wrong-year basket means wrong weights."""
    import json, os
    if not os.path.exists(path):
        return None, f"basket config {path} missing — ratchet DISABLED"
    try:
        cfg = json.load(open(path))
    except Exception as e:
        return None, f"basket config unreadable ({e}) — ratchet DISABLED"
    if int(cfg.get("year", 0)) != today.year:
        return None, (f"basket config is for {cfg.get('year')} but it is {today.year} — "
                      f"ratchet DISABLED until re-seeded (see reseed_note in the file)")
    hs = cfg.get("holdings") or []
    if not hs:
        return None, "basket config has no holdings — ratchet DISABLED"
    tot = sum(float(h["weight"]) for h in hs)
    if abs(tot - 1.0) > 0.01:
        return None, f"basket weights sum to {tot:.4f}, not 1.0 — ratchet DISABLED"
    return cfg, None


def fetch_basket_closes(tickers):
    """Daily closes for the basket names. One call, auto-adjusted."""
    import yfinance as yf
    df = yf.download(tickers, period="1y", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]
    else:
        close = df[["Close"]].rename(columns={"Close": tickers[0]})
    return close.dropna(how="all")


def compute_basket_nav(cfg, closes):
    """Basket NAV indexed to 1.0 at the config's baseline closes (buy-and-hold, weights drift)."""
    hs = cfg["holdings"]
    ticks = [h["ticker"] for h in hs]
    missing = [t for t in ticks if t not in closes.columns]
    if missing:
        raise ValueError(f"missing price data for {missing}")
    sub = closes[ticks].dropna()
    # Only the performance year counts: pre-baseline prices must never arm the ratchet.
    base_dt = pd.Timestamp(cfg["baseline_date"])
    sub = sub[sub.index >= base_dt]
    if sub.empty:
        raise ValueError(f"no basket price data on/after baseline {base_dt.date()}")
    base = pd.Series({h["ticker"]: float(h["baseline_close"]) for h in hs})
    w = pd.Series({h["ticker"]: float(h["weight"]) for h in hs})
    nav = (sub[ticks] / base[ticks] * w[ticks]).sum(axis=1)
    return nav


def walk_ratchet_state(nav, arm_gain, stop_drop):
    """Walk the ratchet over the basket NAV (already indexed to 1.0 at January baseline).
    Arms at >= +arm_gain; fires when close <= high_water*(1-stop_drop). No re-entry."""
    armed = False
    hwm = 0.0
    fired = False
    arm_date = None
    fire_date = None
    for dt, v in nav.items():
        if not armed and (v - 1.0) >= arm_gain:
            armed = True
            hwm = v
            arm_date = dt
        if armed and not fired:
            if v > hwm:
                hwm = v
            if v <= hwm * (1.0 - stop_drop):
                fired = True
                fire_date = dt
    last = float(nav.iloc[-1])
    stop_level = hwm * (1.0 - stop_drop) if armed else None
    return {
        "armed": armed, "fired": fired,
        "arm_date": arm_date, "fire_date": fire_date,
        "hwm": hwm if armed else None,
        "nav": last,
        "ytd": last - 1.0,
        "off_high": (last / hwm - 1.0) if armed and hwm else None,
        "stop_level": stop_level,
        "dist_to_stop": ((last / stop_level) - 1.0) if stop_level else None,
    }


def render_top7_section(label_prefix, params, st, fallback_first_date, unrate_failed=False,
                         flipped_today=False, prev_position=None, ratchet=None, ratchet_warn=None):
    lines = []
    lines.append(f"<b>Top-7 ({label_prefix}) — {params['name']}</b>")
    lines.append(f"<i>D-asym {params['ma_exit']}/{params['E_ma']}/{params['ma_re']}/{params['R_re']}/"
                 f"{params['vol_thr']:.2f}/{params['E_vol']}/tiered_{params['ur_thr']} | "
                 f"basket: TOP-7 stocks | defensive: tiered_{params['ur_thr']}</i>")
    if st["state"] == "invested":
        target = "TOP-7 STOCKS"
    else:
        target = "TREASURIES" if st["_unrate_high"] else "SP500"
        if unrate_failed:
            target = f"{target} ⚠️"
    if flipped_today and prev_position and prev_position != target:
        lines.append(f"➤ <b>HOLD NOW: {prev_position}</b>  (until tomorrow's MOC)")
        lines.append(f"➤ <b>TOMORROW MOC: SELL {prev_position} → BUY {target}</b>")
    else:
        lines.append(f"➤ <b>HOLD: {target}</b>")
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
    if ratchet_warn:
        lines.append(f"<u>Runup ratchet</u>: ⚠️ <b>{ratchet_warn}</b>")
    elif ratchet is not None:
        g = int(TOP7_RATCHET_PARAMS["arm_gain"] * 100)
        d = int(TOP7_RATCHET_PARAMS["stop_drop"] * 100)
        if ratchet["fired"]:
            lines.append(f"<u>Runup ratchet</u>: 🔴 <b>FIRED {ratchet['fire_date'].date()}</b> — "
                         f"locked to defensive until the January rebalance (no reentry).")
            lines.append(f"  • basket {ratchet['ytd']:+.0%} YTD, {ratchet['off_high']:+.0%} off its post-arm high")
        elif ratchet["armed"]:
            lines.append(f"<u>Runup ratchet</u>: 🟡 ARMED {ratchet['arm_date'].date()} (basket hit +{g}% YTD)")
            lines.append(f"  • basket {ratchet['ytd']:+.0%} YTD | {ratchet['off_high']:+.0%} off high | "
                         f"stop at -{d}% from high = {ratchet['stop_level'] - 1.0:+.0%} YTD | "
                         f"{ratchet['dist_to_stop']:+.1%} to trigger")
        else:
            lines.append(f"<u>Runup ratchet</u>: not armed (basket {ratchet['ytd']:+.0%} YTD; arms at +{g}%)")
    if unrate_failed:
        lines.append("<u>Defensive routing</u>: ⚠️ <b>UNRATE FETCH FAILED</b> — assuming stable (→ SP500). Verify manually.")
    else:
        lines.append(f"<u>Defensive routing</u>: ΔUNRATE_3mo &gt; {params['ur_thr']}pp → Treasuries, else → SP500")
    lines.append(_format_days_in_strategy(target, st, fallback_first_date, flipped_today, prev_position))
    return lines


def render_lev_section(label, params, st, fallback_first_date,
                       flipped_today=False, prev_position=None):
    lines = []
    sig = params["signal_asset"]; lev = params["leveraged"]; defv = params["defensive"]
    ma_n = params["ma"]; vt = int(params["vol_thr"] * 100)
    el = params["exit_lag"]; rl = params["entry_lag"]
    lines.append(f"<b>{label}</b>")
    lines.append(f"<i>MA{ma_n} v&lt;{vt}% e={el} r={rl} | leveraged: {lev} | defensive: {defv}</i>")
    target = lev if st["state"] == "invested" else defv
    if flipped_today and prev_position and prev_position != target:
        lines.append(f"➤ <b>HOLD NOW: {prev_position}</b>  (until tomorrow's MOC)")
        lines.append(f"➤ <b>TOMORROW MOC: SELL {prev_position} → BUY {target}</b>")
    else:
        lines.append(f"➤ <b>HOLD: {target}</b>")
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
    lines.append(_format_days_in_strategy(target, st, fallback_first_date, flipped_today, prev_position))
    return lines


def render_guarded_state_section(title, state_entry, default_position="prior position"):
    lines = [f"<b>{title}</b>"]
    reason = state_entry.get("data_guarded", "Required data stale; state retained until native data refreshes")
    actual = state_actual_position(state_entry) or default_position
    signal = state_signal_position(state_entry) or actual
    lines.append(f"⚠️ <b>DATA STALE — state retained</b>: {reason}")
    lines.append(f"➤ <b>HOLD: {actual}</b>")
    if signal != actual:
        lines.append(f"Latest retained signal target: {signal}")
    pending = state_entry.get("pending_order")
    if pending:
        lines.append(f"Pending order retained: SELL {pending['sell']} → BUY {pending['buy']} ({pending.get('execution', 'next MOC')})")
    return lines


# ============================
# Conditions line + summary block
# ============================

def build_conditions_line(spy_close, sma100, sma300, spy_vol20,
                           qqq_close, qqq_sma150, qqq_vol20,
                           nasdaqcom_vol30, un_chg, un_flag_01, unrate_failed,
                           nasdaqcom_stale_days=0):
    BORDER_PCT = 1.5
    BORDER_VOL = 1.5

    def fmt_vol_pct(v_pct, threshold_pct=None):
        """Show extra precision when rounded whole percentages could obscure a threshold.
        Example: 29.982% should not display as 30% next to a 30% trigger."""
        if threshold_pct is not None and abs(v_pct - threshold_pct) < 0.5:
            return f"{v_pct:.2f}%"
        return f"{v_pct:.1f}%"

    def vol_phrase(v, thr_pct, label):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "vol n/a"
        v_pct = v * 100
        v_str = fmt_vol_pct(v_pct, thr_pct)
        if v_pct >= thr_pct:
            return f"⚠️ {label} {v_str} (≥{thr_pct}% thr)"
        if (thr_pct - v_pct) < BORDER_VOL:
            return f"⚠️ {label} {v_str} (approaching {thr_pct}% thr)"
        return f"{label} {v_str}"

    def spy_trend_summary(close, smas_with_labels):
        if pd.isna(close) or any(pd.isna(s) for s, _ in smas_with_labels):
            return "SPY trend (data missing)"
        results = [(close > s, (close - s) / s * 100, lbl) for s, lbl in smas_with_labels]
        all_above = all(a for a, _, _ in results)
        all_below = all(not a for a, _, _ in results)
        if all_above:
            min_gap = min(g for _, g, _ in results)
            if min_gap < BORDER_PCT:
                tight = next(lbl for _, g, lbl in results if g == min_gap)
                return f"⚠️ SPY barely above all MAs (closest: {tight} at {min_gap:.1f}%)"
            return "SPY above all MAs"
        if all_below:
            worst_gap = min(g for _, g, _ in results)
            return f"⚠️ SPY BELOW all MAs ({-worst_gap:.1f}% below worst)"
        above_lbls = [lbl for a, _, lbl in results if a]
        below_lbls = [lbl for a, _, lbl in results if not a]
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
    if spy_close is not None and sma100 is not None and sma300 is not None:
        smas = [(sma100, "SMA100"), (sma300, "SMA300")]
        bits.append(f"{spy_trend_summary(spy_close, smas)}, {vol_phrase(spy_vol20, 21, 'SPY vol')}")
    if qqq_close is not None and qqq_sma150 is not None:
        bits.append(f"{qqq_trend(qqq_close, qqq_sma150, '150')}, {vol_phrase(qqq_vol20, 31, 'QQQ vol')}")
    if nasdaqcom_vol30 is not None:
        v_pct = nasdaqcom_vol30 * 100
        if v_pct >= 45: nasdaqcom_str = f"⚠️ NASDAQ vol {fmt_vol_pct(v_pct, 45)} (≥45% Brok thr)"
        elif v_pct >= 40: nasdaqcom_str = f"⚠️ NASDAQ vol {fmt_vol_pct(v_pct, 40)} (≥40% Roth thr)"
        elif (40 - v_pct) < BORDER_VOL: nasdaqcom_str = f"⚠️ NASDAQ vol {fmt_vol_pct(v_pct, 40)} (approaching 40% Roth thr)"
        else: nasdaqcom_str = f"NASDAQ vol {fmt_vol_pct(v_pct)}"
        if nasdaqcom_stale_days > 1: nasdaqcom_str = f"{nasdaqcom_str} (data {nasdaqcom_stale_days}d stale)"
        bits.append(nasdaqcom_str)
    if unrate_failed:
        bits.append("⚠️ UNRATE fetch failed")
    elif un_flag_01:
        bits.append(f"⚠️ unemployment rising (+{un_chg:.2f}pp)")
    else:
        bits.append("unemployment stable")
    return "Markets: " + " | ".join(bits)


def build_summary_block(prev_state, new_state, conditions_line):
    flips = []; approaching = []; watching = []

    def categorize(label_text, streak, threshold, direction):
        if streak == 0 or threshold <= 0:
            return
        if threshold > 1 and streak == threshold - 1:
            approaching.append((label_text, direction, streak, threshold))
        elif streak >= 1 and streak < threshold:
            watching.append((label_text, direction, streak, threshold))

    n_total = 0
    n_risk_on = 0
    defensive = []
    for key, ns in new_state.items():
        if key.startswith("_"): continue
        n_total += 1
        if ns.get("state") == "invested":
            n_risk_on += 1
        else:
            defensive.append((ns.get("label", key), ns.get("position", "defensive")))
        pending = ns.get("pending_order")
        if pending:
            flips.append((ns["label"], pending["sell"], pending["buy"]))
            continue
        if ns.get("data_guarded"):
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
    if flips:
        n = len(flips)
        today_dow = today_et().weekday()
        nxt_label = "MONDAY" if today_dow >= 4 else "tomorrow"
        lines.append(f"<b>🔴 ACTION REQUIRED — {n} flip{'s' if n>1 else ''} at MOC {nxt_label} (before 3:50pm ET)</b>")
        lines.append(conditions_line)
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
    if defensive:
        n_def = len(defensive)
        lines.append(f"<b>🟡 NO NEW CHANGES — {n_risk_on} risk-on, {n_def} defensive; no signals progressing</b>")
        lines.append(conditions_line)
        for lbl, pos in defensive:
            lines.append(f"  • {lbl} remains in {pos}")
        return lines
    lines.append(f"<b>🟢 ALL CLEAR — {n_total} of {n_total} strategies risk-on, no signals progressing</b>")
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

    health = {"SPY": False, "QQQ": False, "NASDAQCOM": False, "UNRATE": False}

    try:
        spy_df = fetch_etf("SPY"); health["SPY"] = True
    except Exception as e:
        send_telegram(bot_token, chat_id, f"<b>⚠️ Bot error</b>\nSPY fetch failed: {e}\nRun aborted; positions retained.")
        return
    try:
        qqq_df = fetch_etf("QQQ"); health["QQQ"] = True
    except Exception as e:
        print(f"WARN: QQQ fetch failed: {e}"); qqq_df = None
    try:
        nasdaqcom_df = fetch_nasdaqcom(); health["NASDAQCOM"] = True
    except Exception as e:
        print(f"WARN: NASDAQCOM fetch failed: {e}"); nasdaqcom_df = None
    try:
        un = fetch_unrate(); health["UNRATE"] = True
    except Exception as e:
        print(f"WARN: UNRATE fetch failed: {e}"); un = None

    spy_close = spy_df["Close"].copy()
    spy_close.index = pd.to_datetime(spy_close.index).normalize()
    latest_date = spy_close.index[-1]
    latest_close = float(spy_close.iloc[-1])
    prev = load_state()
    today_d = current_et.date()
    latest_d = latest_date.date() if isinstance(latest_date, pd.Timestamp) else latest_date

    # Sanity check: SPY history must be long enough for SMA300
    if len(spy_close) < 350:
        send_telegram(bot_token, chat_id,
                      f"<b>⚠️ SPY DATA TOO SHORT</b>\nGot {len(spy_close)} days; need 350+ for SMA300. Bot aborted.")
        return

    # MAs
    sma50 = spy_close.rolling(50).mean()
    sma100 = spy_close.rolling(100).mean()
    sma250 = spy_close.rolling(250).mean()
    sma300 = spy_close.rolling(300).mean()
    sma50_v = float(sma50.iloc[-1]); sma100_v = float(sma100.iloc[-1])
    sma250_v = float(sma250.iloc[-1]); sma300_v = float(sma300.iloc[-1])

    # SPY 20d vol
    spy_vol20 = compute_vol20(spy_close)
    spy_vol20_v = float(spy_vol20.iloc[-1]) if not pd.isna(spy_vol20.iloc[-1]) else None

    # NASDAQCOM vol30: compute on NATIVE NASDAQCOM series, then ffill onto SPY trading days
    # (avoids the bug where ffilling the price series creates artificial 0% returns)
    nasdaqcom_stale_days = 0
    if nasdaqcom_df is not None:
        nasdaqcom_close = nasdaqcom_df["NASDAQCOM"].copy()
        nasdaqcom_close.index = pd.to_datetime(nasdaqcom_close.index).normalize()
        vol30_native = compute_vol30(nasdaqcom_close)
        vol30 = vol30_native.reindex(spy_close.index, method="ffill")
        vol30_v = float(vol30.iloc[-1]) if not pd.isna(vol30.iloc[-1]) else None
        nasdaqcom_stale_days = max(0, (spy_close.index[-1] - nasdaqcom_close.index[-1]).days)
        nasdaqcom_last_d = nasdaqcom_close.index[-1].date()
    else:
        vol30 = pd.Series(np.nan, index=spy_close.index); vol30_v = None
        nasdaqcom_last_d = None

    # QQQ vol20 + SMA150: same native-then-ffill pattern
    qqq_stale_days = 0
    if qqq_df is not None:
        qqq_close_raw = qqq_df["Close"].copy()
        qqq_close_raw.index = pd.to_datetime(qqq_close_raw.index).normalize()
        qqq_sma150_native = qqq_close_raw.rolling(150).mean()
        qqq_vol20_native = compute_vol20(qqq_close_raw)
        qqq_close = qqq_close_raw.reindex(spy_close.index, method="ffill")
        qqq_sma150 = qqq_sma150_native.reindex(spy_close.index, method="ffill")
        qqq_vol20 = qqq_vol20_native.reindex(spy_close.index, method="ffill")
        qqq_close_v = float(qqq_close.iloc[-1]) if not pd.isna(qqq_close.iloc[-1]) else None
        qqq_sma150_v = float(qqq_sma150.iloc[-1]) if not pd.isna(qqq_sma150.iloc[-1]) else None
        qqq_vol20_v = float(qqq_vol20.iloc[-1]) if not pd.isna(qqq_vol20.iloc[-1]) else None
        qqq_stale_days = max(0, (spy_close.index[-1] - qqq_close_raw.index[-1]).days)
        qqq_last_d = qqq_close_raw.index[-1].date()
    else:
        qqq_close = qqq_sma150 = qqq_vol20 = None
        qqq_close_v = qqq_sma150_v = qqq_vol20_v = None
        qqq_last_d = None

    # Per-source freshness verification. Required native data must be fresh for
    # the latest SPY signal date before affected strategy streaks/orders advance.
    days_stale = (today_d - latest_d).days
    spy_missed = trading_days_missed(latest_d, today_d)
    qqq_missed = trading_days_missed(qqq_last_d, today_d) if qqq_last_d else None
    nasdaqcom_missed = trading_days_missed(nasdaqcom_last_d, today_d) if nasdaqcom_last_d else None
    qqq_signal_fresh = qqq_last_d is not None and qqq_last_d >= latest_d
    nasdaqcom_signal_fresh = nasdaqcom_last_d is not None and nasdaqcom_last_d >= latest_d

    # UNRATE delta
    if un is not None and len(un) >= 4:
        un_now_rate = float(un.iloc[-1]["UNRATE"])
        un_prior_rate = float(un.iloc[-4]["UNRATE"])
        un_chg = un_now_rate - un_prior_rate
        un_flag_01 = un_chg > 0.1
    else:
        un_chg = 0.0; un_flag_01 = False

    # Walkers
    roth_top7_st = walk_dasym_state(spy_close, sma300, sma100, vol30,
                                     ROTH_TOP7_PARAMS["vol_thr"], ROTH_TOP7_PARAMS["E_vol"],
                                     ROTH_TOP7_PARAMS["E_ma"], ROTH_TOP7_PARAMS["R_re"])
    roth_top7_st["_unrate_high"] = un_flag_01

    # --- Runup ratchet (ROTH ONLY). Third exit path; overrides the D-asym state when fired. ---
    ratchet = None
    ratchet_warn = None
    try:
        _cfg, ratchet_warn = load_basket_config(TOP7_RATCHET_PARAMS["basket_file"], pd.Timestamp.today())
        if _cfg is not None:
            _closes = fetch_basket_closes([h["ticker"] for h in _cfg["holdings"]])
            _nav = compute_basket_nav(_cfg, _closes)
            ratchet = walk_ratchet_state(_nav, TOP7_RATCHET_PARAMS["arm_gain"],
                                         TOP7_RATCHET_PARAMS["stop_drop"])
    except Exception as e:
        ratchet = None
        ratchet_warn = f"ratchet computation failed ({e}) — Roth D-asym signal shown alone"
    if ratchet is not None and ratchet["fired"]:
        # Lockout: force defensive and block reentry until the January rebalance.
        roth_top7_st["state"] = "defensive"
        roth_top7_st["reentry_streak"] = 0
        roth_top7_st["_ratchet_locked"] = True
    brok_top7_st = walk_dasym_state(spy_close, sma300, sma100, vol30,
                                     BROK_TOP7_PARAMS["vol_thr"], BROK_TOP7_PARAMS["E_vol"],
                                     BROK_TOP7_PARAMS["E_ma"], BROK_TOP7_PARAMS["R_re"])
    brok_top7_st["_unrate_high"] = un_flag_01
    spy_lev_roth_st = walk_lev_state(spy_close, sma300, spy_vol20,
                                      SPY_LEV_ROTH_PARAMS["vol_thr"],
                                      SPY_LEV_ROTH_PARAMS["exit_lag"], SPY_LEV_ROTH_PARAMS["entry_lag"])
    spy_lev_brok_st = walk_lev_state(spy_close, sma300, spy_vol20,
                                      SPY_LEV_BROK_PARAMS["vol_thr"],
                                      SPY_LEV_BROK_PARAMS["exit_lag"], SPY_LEV_BROK_PARAMS["entry_lag"])
    if qqq_close is not None and qqq_signal_fresh:
        qqq_lev_st = walk_lev_state(qqq_close, qqq_sma150, qqq_vol20,
                                     QQQ_LEV_PARAMS["vol_thr"],
                                     QQQ_LEV_PARAMS["exit_lag"], QQQ_LEV_PARAMS["entry_lag"])
    else:
        qqq_lev_st = None

    # Build new_state (with paths for Top-7)
    def top7_position(st):
        if st["state"] == "invested": return "TOP-7 STOCKS"
        return "TREASURIES" if un_flag_01 else "SP500"

    new_state = {}
    new_state["top7_roth"] = {
        "label": "Top-7 (Roth)", "state": roth_top7_st["state"],
        "position": top7_position(roth_top7_st),
        "paths": [("vol", roth_top7_st["vol_streak"], ROTH_TOP7_PARAMS["E_vol"]),
                  ("MA",  roth_top7_st["ma_streak"],  ROTH_TOP7_PARAMS["E_ma"])],
        "reentry_streak": roth_top7_st["reentry_streak"], "reentry_threshold": ROTH_TOP7_PARAMS["R_re"],
        "ratchet_armed": bool(ratchet["armed"]) if ratchet else None,
        "ratchet_fired": bool(ratchet["fired"]) if ratchet else None,
        "ratchet_hwm": float(ratchet["hwm"]) if (ratchet and ratchet["hwm"]) else None,
        "ratchet_nav": float(ratchet["nav"]) if ratchet else None,
        "ratchet_locked": bool(roth_top7_st.get("_ratchet_locked", False)),
    }
    new_state["top7_brok"] = {
        "label": "Top-7 (Brok)", "state": brok_top7_st["state"],
        "position": top7_position(brok_top7_st),
        "paths": [("vol", brok_top7_st["vol_streak"], BROK_TOP7_PARAMS["E_vol"]),
                  ("MA",  brok_top7_st["ma_streak"],  BROK_TOP7_PARAMS["E_ma"])],
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
    else:
        prev_for_qqq = prev
        for k in ("qqq_lev_roth", "qqq_lev_brok"):
            if k in prev_for_qqq:
                new_state[k] = prev_for_qqq[k]

    if not nasdaqcom_signal_fresh:
        new_state = carry_forward_guarded_state(
            prev, new_state, ("top7_roth", "top7_brok"),
            "NASDAQCOM data stale; Top-7 state retained until native data refreshes")
    if not qqq_signal_fresh:
        new_state = carry_forward_guarded_state(
            prev, new_state, ("qqq_lev_roth", "qqq_lev_brok"),
            "QQQ data stale; QQQ leveraged state retained until native data refreshes")

    new_state = apply_actual_vs_signal_state(prev, new_state, latest_d)

    # Detect pending orders (per-strategy) — needed for flip-day display
    flipped = {}
    for key, ns in new_state.items():
        pending = ns.get("pending_order")
        if pending:
            flipped[key] = pending["sell"]  # current actual position

    fresh_warns = []
    if spy_missed >= 1:
        fresh_warns.append(f"SPY missing today's close ({spy_missed} trading day{'s' if spy_missed>1 else ''} stale)")
    if qqq_missed is not None and qqq_missed >= 1:
        fresh_warns.append(f"QQQ missing today's close ({qqq_missed} trading day{'s' if qqq_missed>1 else ''} stale)")
    if nasdaqcom_missed is not None and nasdaqcom_missed >= 2:
        fresh_warns.append(f"NASDAQCOM {nasdaqcom_missed} trading days stale")

    stale_warning = None
    if fresh_warns:
        stale_warning = "⚠️ <b>DATA FRESHNESS WARNING</b> — " + "; ".join(fresh_warns) + ". Affected strategy state/orders are retained until native data refreshes."
    elif days_stale > 5:
        stale_warning = f"⚠️ <b>STALE DATA</b> — SPY most recent close is {latest_d} ({days_stale} calendar days old)."

    spy_first_date = spy_close.index[0]
    qqq_first_date = qqq_close.index[0] if qqq_close is not None else spy_first_date
    unrate_failed = not health["UNRATE"]

    # Build top of message
    conditions_line = build_conditions_line(
        spy_close=latest_close, sma100=sma100_v, sma300=sma300_v,
        spy_vol20=spy_vol20_v,
        qqq_close=qqq_close_v, qqq_sma150=qqq_sma150_v, qqq_vol20=qqq_vol20_v,
        nasdaqcom_vol30=vol30_v, un_chg=un_chg, un_flag_01=un_flag_01,
        unrate_failed=unrate_failed, nasdaqcom_stale_days=nasdaqcom_stale_days)
    summary_block = build_summary_block(prev, new_state, conditions_line)

    lines = [f"<b>📊 {ACCOUNT_LABEL}</b>", f"{latest_date.strftime('%Y-%m-%d')}"]
    if stale_warning:
        lines.append(stale_warning)
    lines.extend(summary_block)
    lines.append("")

    # Inputs blocks
    def fmt_input_vol(v, threshold_pct=None):
        if v is None:
            return "n/a"
        v_pct = v * 100
        if threshold_pct is not None and abs(v_pct - threshold_pct) < 0.5:
            return f"{v_pct:.2f}%"
        return f"{v_pct:.1f}%"

    cur_vol_str = fmt_input_vol(vol30_v)
    spy_vol_str = fmt_input_vol(spy_vol20_v, 22)
    qqq_vol_str = fmt_input_vol(qqq_vol20_v, 30)
    unrate_str = (f"{un_chg:+.2f}pp ({'rising ⚠️' if un_flag_01 else 'stable'}; tiered_0.1 routing)"
                   if not unrate_failed else "⚠️ FETCH FAILED — assuming stable")
    lines.append("<b>Top-7 inputs:</b>")
    lines.append(f"SPY: {latest_close:.2f} | NASDAQCOM vol30: {cur_vol_str} | "
                 f"SP500 SMAs: 50:{sma50_v:.2f}, 100:{sma100_v:.2f}, 250:{sma250_v:.2f}, 300:{sma300_v:.2f} | "
                 f"UNRATE 3-mo Δ: {unrate_str}")
    lines.append("")
    lines.append("<b>SPY Leveraged inputs:</b>")
    lines.append(f"SPY: {latest_close:.2f} | SPY SMA300: {sma300_v:.2f} | SPY vol20: {spy_vol_str} (thr 21%)")
    lines.append("")
    if qqq_close is not None:
        lines.append("<b>QQQ Leveraged inputs:</b>")
        lines.append(f"QQQ: {qqq_close_v:.2f} | QQQ SMA150: {qqq_sma150_v:.2f} | QQQ vol20: {qqq_vol_str} (thr 31%)")
        lines.append("")

    # ROTH section (with flip-day awareness via flipped dict)
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>🏦 ROTH IRA</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    if nasdaqcom_signal_fresh:
        lines.extend(render_top7_section("Roth", ROTH_TOP7_PARAMS, roth_top7_st, spy_first_date,
                                          unrate_failed=unrate_failed,
                                          flipped_today=("top7_roth" in flipped),
                                          prev_position=flipped.get("top7_roth"),
                                          ratchet=ratchet, ratchet_warn=ratchet_warn))
    else:
        lines.extend(render_guarded_state_section("Top-7 (Roth) — ALT-A", new_state.get("top7_roth", {}), "TOP-7 STOCKS"))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Roth)", SPY_LEV_ROTH_PARAMS, spy_lev_roth_st,
                                     spy_first_date,
                                     flipped_today=("spy_lev_roth" in flipped),
                                     prev_position=flipped.get("spy_lev_roth")))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Roth)", QQQ_LEV_PARAMS, qqq_lev_st,
                                         qqq_first_date,
                                         flipped_today=("qqq_lev_roth" in flipped),
                                         prev_position=flipped.get("qqq_lev_roth")))
        lines.append("")
    else:
        lines.extend(render_guarded_state_section("QQQ Leveraged (Roth)", new_state.get("qqq_lev_roth", {}), "TQQQ")); lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>💼 BROKERAGE</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    if nasdaqcom_signal_fresh:
        lines.extend(render_top7_section("Brok", BROK_TOP7_PARAMS, brok_top7_st, spy_first_date,
                                          unrate_failed=unrate_failed,
                                          flipped_today=("top7_brok" in flipped),
                                          prev_position=flipped.get("top7_brok")))
    else:
        lines.extend(render_guarded_state_section("Top-7 (Brok) — BROK_A", new_state.get("top7_brok", {}), "TOP-7 STOCKS"))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Brok)", SPY_LEV_BROK_PARAMS, spy_lev_brok_st,
                                     spy_first_date,
                                     flipped_today=("spy_lev_brok" in flipped),
                                     prev_position=flipped.get("spy_lev_brok")))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Brok)", QQQ_LEV_PARAMS, qqq_lev_st,
                                         qqq_first_date,
                                         flipped_today=("qqq_lev_brok" in flipped),
                                         prev_position=flipped.get("qqq_lev_brok")))
        lines.append("")
    else:
        lines.extend(render_guarded_state_section("QQQ Leveraged (Brok)", new_state.get("qqq_lev_brok", {}), "TQQQ")); lines.append("")

    # Health footer with holiday-aware staleness
    lines.append("━━━━━━━━━━━━━━━━━━")
    age_map = {"SPY": days_stale, "QQQ": qqq_stale_days, "NASDAQCOM": nasdaqcom_stale_days, "UNRATE": None}
    def _to_date(ts):
        if ts is None: return None
        return ts.date() if hasattr(ts, "date") else ts
    src_latest_map = {
        "SPY": latest_d,
        "QQQ": _to_date(qqq_df["Close"].dropna().index[-1]) if qqq_df is not None else None,
        "NASDAQCOM": _to_date(nasdaqcom_df["NASDAQCOM"].dropna().index[-1]) if nasdaqcom_df is not None else None,
        "UNRATE": None,
    }
    def stale_label(age, src_latest_date):
        if age is None: return "✓"
        if src_latest_date is None: return f"✓ ({age}d cal)"
        missed = trading_days_missed(src_latest_date, today_d)
        if missed == 0: return f"✓ ({age}d cal — most recent trading day)"
        if missed == 1: return f"⚠️ 1 trading day missed ({age}d cal)"
        return f"⚠️ {missed} trading days STALE ({age}d cal)"
    health_bits = []
    for src, ok in health.items():
        if not ok:
            health_bits.append(f"{src} ✗"); continue
        health_bits.append(f"{src} {stale_label(age_map.get(src), src_latest_map.get(src))}")
    lines.append(f"📡 Data: {' | '.join(health_bits)}")

    severe = []
    if nasdaqcom_stale_days > 5: severe.append(f"NASDAQCOM is {nasdaqcom_stale_days}d stale — Top-7 vol path may be wrong")
    if qqq_stale_days > 5: severe.append(f"QQQ data is {qqq_stale_days}d stale — QQQ Lev signals may be wrong")
    if severe:
        lines.append("⚠️ <b>SEVERE STALENESS</b>: " + "; ".join(severe) + ". Verify before acting.")

    send_telegram(bot_token, chat_id, "\n".join(lines))
    save_state(new_state)
    print(f"State saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
