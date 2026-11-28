"""Daily Telegram regime monitor for ROTH and BROKERAGE accounts.

Built on 2026-04-26 final (sha256 fdad9c62ebd9...) with the following changes:

  Two-account rules (2026-11-27):
    + Top-7 defensive IEF when first-release unemployment rises >0.1pp; SP500 otherwise.
    + TQQQ: Roth exit 1 / brokerage exit 4; independent once-per-decline -20% dip buys.
      Brokerage TQQQ only; Roth QLD until the 30% brokerage tax-cost cushion clears.
      Thirty-day brokerage entry lock and 5% floor after a Roth TQQQ purchase.
    + UPRO: brokerage UPRO only; Roth SSO until the 10% tax-cost cushion clears.
      Thirty-day brokerage entry lock, no floor and no dip. Exits next close.
    + Real fixed/exported facts override replay assumptions. Model fills are not real fills.
    + Both Roth IRAs have dividend reinvestment OFF from 2026-11-27; cash dividends stay
      cash until a discretionary strategy purchase. Historic DRIPs still count as buys.

  Scheduler hardening (2026-09-04):
    + Full-monitor attempts moved to 5:20 PM ET, with 5:40 PM and 6:10 PM
      retries. Nasdaq index corrections can run through 5:15 PM; GitHub's IANA
      timezone schedule keeps these times fixed through DST.
    + Every automatic attempt requires the same most-recent completed XNYS session
      from SPY, QQQ, Nasdaq Composite, and (when enabled) the Top-7 basket. Earlier
      missing bars defer; the final attempt fails closed and triggers an alert.
    + A persisted market-session key prevents duplicate Telegram reports/state
      advancement across retries. Manual runs are safe/deduped by default and expose
      explicit force-repeat / unsafe-partial switches for diagnostics.
    + XNYS sessions replace the US-federal-holiday approximation, and delayed
      GitHub jobs can process the latest completed session without a narrow clock gate.

  ChatGPT review:
    + Flip-day display: HOLD NOW (current actual position until the next-session MOC)
      vs NEXT-SESSION MOC (target after execution). Days-counter line shows the
      pending-execution clarifier. Avoids implying the new asset is held today.

  Bug fixes (audit, 2026-04-27):
    + NASDAQCOM ffill bug — vol30 now computed on raw native NASDAQCOM series, then
      ffilled onto SPY trading days. Previously ffilled prices first → fake
      0% returns understated vol when FRED published 1 day late.
    + QQQ ffill bug — same pattern, same fix for vol20 and SMA175.
    + SPY data length sanity check (aborts if <350 days; SMA300 needs it).
    + QQQ fetch failure now explicitly reloads prior state from state.json.
    + Holiday-aware staleness — later replaced by the XNYS exchange calendar.

  UX upgrades:
    + 🟢 ALL CLEAR / 🟡 WATCHING / 🟠 APPROACHING / 🔴 ACTION status block
    + 'Markets:' conditions line — SPY trend across all 3 SMAs collapsed,
      SPY vol20, QQQ trend, NASDAQ vol30, UNRATE.
    + Top-7 summary surfaces BOTH vol AND MA paths separately.
    + Action wording includes explicit 'SELL X, BUY Y'.
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
                  + big-year WATCH (alert only, added 2026-11-11): shown once the basket hits
                  +100% YTD; flags a -25% drawdown from the high for a manual review. No rule.
    SPY Leveraged R+B: MA300 v<21%, e=1 next close, r=1; paired UPRO/SSO policy
    QQQ Leveraged: MA150 v<31%, brokerage e=4 / Roth e=1, r=1; paired TQQQ/QLD policy

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
from datetime import datetime, date
from functools import lru_cache



from pathlib import Path
from joint_strategy import unemployment_rising, defensive_holding, SYMBOLS
from joint_runtime import (load_facts, evaluate_sleeve, render_joint, first_release_delta_history,
                           snapshot_unemployment, unemployment_data_line, render_order, ROTH_BUY_REMINDER, wash_text, cached_observed_facts, merge_observed_facts, top7_buy_warnings, top7_rebalance_warnings, normalize_substitutes)

EXECUTION_TIMING = {
    'spy_lev_roth': {'exit': 'next_close', 'entry': 'same_day'},
    'spy_lev_brok': {'exit': 'next_close', 'entry': 'same_day'},
    'qqq_lev_roth': {'exit': 'same_day', 'entry': 'same_day'},
    'qqq_lev_brok': {'exit': 'same_day', 'entry': 'same_day'},
}

def today_et():
    """Current calendar date in America/New_York.

    GitHub Actions runners use UTC. Manual runs after 8pm ET during daylight
    time are already the next UTC date, which can make the bot falsely expect
    tomorrow's market close. All market freshness/execution-day messaging should
    use ET, not runner-local/UTC date.
    """
    import pytz
    return datetime.now(pytz.timezone("America/New_York")).date()


def state_reported_market_date(state):
    """Return the last market date successfully reported, if one is recorded."""
    raw = (state.get("_meta") or {}).get("last_reported_market_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def required_data_missing(expected_date, source_dates):
    """List sources whose most recent row is not the required completed session."""
    return [name for name, source_date in source_dates.items()
            if source_date is None or source_date != expected_date]


@lru_cache(maxsize=1)
def xnyse_calendar():
    """Canonical NYSE session calendar (holidays and early closes included)."""
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS")


def latest_completed_market_session(current_et, grace_minutes=15):
    """Most recent XNYS session whose official close is at least `grace` old.

    The grace period is a publication buffer, not a claim that Yahoo certifies a
    final bar. It prevents a pre-close manual run from consuming today's partial
    row and gives post-close providers time to publish the session.
    """
    cal = xnyse_calendar()
    today_ts = pd.Timestamp(current_et.date())
    if cal.is_session(today_ts):
        close_et = cal.session_close(today_ts).tz_convert("America/New_York")
        if pd.Timestamp(current_et) >= close_et + pd.Timedelta(grace_minutes, unit="min"):
            return current_et.date()
        return cal.previous_session(today_ts).date()
    return cal.date_to_session(today_ts, direction="previous").date()


def frame_through_session(df, session_date):
    """Drop any intraday/current-session row newer than the completed session."""
    if df is None or df.empty:
        return df
    keep = [pd.Timestamp(v).date() <= session_date for v in df.index]
    return df.loc[keep]


def _normalized_session_index(index):
    """Return timezone-naive dates suitable for XNYS comparisons."""
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    return idx.normalize()


def assert_recent_session_continuity(df, label, end_session, session_count,
                                     columns=None, start_session=None):
    """Fail closed when a required XNYS session is absent from a daily series.

    A current-date metadata quote is not enough: if Yahoo silently omits an
    intermediate row, daily returns and rolling volatility compress a multi-day
    move into one observation. For a multi-column basket, every requested ticker
    must have a finite value on every required session.
    """
    if df is None or df.empty:
        raise ValueError(f"{label} has no data for continuity validation")
    cal = xnyse_calendar()
    end_ts = pd.Timestamp(end_session)
    if not cal.is_session(end_ts):
        raise ValueError(f"{end_session} is not an XNYS session")
    if start_session is None:
        # exchange-calendars interprets a negative count as the total window
        # length including `end_ts` (for example, -350 returns 350 sessions).
        expected = cal.sessions_window(end_ts, -int(session_count))
    else:
        start_ts = cal.date_to_session(pd.Timestamp(start_session), direction="next")
        expected = cal.sessions_in_range(start_ts, end_ts)
    expected = _normalized_session_index(expected).unique().sort_values()

    work = df.copy()
    work.index = _normalized_session_index(work.index)
    required_columns = list(columns) if columns is not None else list(work.columns)
    missing_columns = [c for c in required_columns if c not in work.columns]
    if missing_columns:
        raise ValueError(f"{label} is missing columns: {', '.join(missing_columns)}")
    valid = work[required_columns].apply(pd.to_numeric, errors="coerce").notna().all(axis=1)
    observed = (_normalized_session_index(work.index[valid.to_numpy()])
                .unique().sort_values())
    missing = expected.difference(observed)
    if len(missing):
        sample = ", ".join(ts.date().isoformat() for ts in missing[:5])
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        raise ValueError(
            f"{label} is missing {len(missing)} required XNYS session(s): {sample}{more}")
    return True


NASDAQ_FINAL_MINUTES_AFTER_CLOSE = 75


def regular_close_from_metadata(meta, ticker, session_date, tolerance_minutes=5,
                                allow_late_index_timestamp=False, allow_preliminary_index=False):
    """Validate Yahoo's regular-session close metadata for one XNYS session.

    `regularMarketPrice` is deliberately used instead of `lastPrice`, which can
    be an after-hours trade. The timestamp must line up with the exchange's
    official session close. Yahoo remains an unofficial data source, but this is
    materially stronger than accepting any daily row carrying today's date.
    """
    raw_price = meta.get("regularMarketPrice")
    raw_time = meta.get("regularMarketTime")
    try:
        price = float(raw_price)
        if isinstance(raw_time, (pd.Timestamp, datetime)):
            quote_time = pd.Timestamp(raw_time)
            if quote_time.tzinfo is None:
                quote_time = quote_time.tz_localize("America/New_York")
            quote_time = quote_time.tz_convert("UTC")
        else:
            quote_time = pd.to_datetime(int(raw_time), unit="s", utc=True)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{ticker} regular-close metadata is incomplete") from e
    if not np.isfinite(price) or price <= 0:
        raise ValueError(f"{ticker} regularMarketPrice is invalid: {raw_price}")
    expected_close = xnyse_calendar().session_close(pd.Timestamp(session_date))
    if quote_time.date() != expected_close.date():
        raise ValueError(
            f"{ticker} regularMarketTime {quote_time} is not session {session_date}")
    signed_delta = (quote_time - expected_close).total_seconds()
    if allow_preliminary_index:
        # Early read-only monitor only: accept the index value published at or after
        # the close, before the 5:15 PM correction deadline. Callers must label it
        # preliminary; the delivering bot never uses this path.
        invalid_time = signed_delta < -60
    elif allow_late_index_timestamp:
        # Nasdaq index closing values can be corrected through 5:15 PM ET.
        # Yahoo's Nasdaq Composite metadata is therefore accepted only once its timestamp
        # reaches that final-correction boundary, never from a preliminary 4 PM
        # value carrying the correct calendar date.
        # The deadline is 75 minutes after THAT session's close: 5:15 PM on a
        # normal day, 2:15 PM on a 1:00 PM early close (fixed 2026-11-28; a fixed
        # 5:15 PM rejected the already-final value on half-days).
        final_boundary = expected_close + pd.Timedelta(minutes=NASDAQ_FINAL_MINUTES_AFTER_CLOSE)
        invalid_time = quote_time < final_boundary
    else:
        invalid_time = abs(signed_delta) > tolerance_minutes * 60
    if invalid_time:
        reason = ("is earlier than Nasdaq's final-correction boundary (75 minutes after the close)"
                  if allow_late_index_timestamp and not allow_preliminary_index
                  else f"is {signed_delta:.0f}s from session close")
        raise ValueError(
            f"{ticker} regularMarketTime {quote_time} {reason}")
    return price


def fetch_yahoo_regular_close(ticker, session_date, allow_late_index_timestamp=False,
                              allow_preliminary_index=False):
    import yfinance as yf
    meta = yf.Ticker(ticker).get_history_metadata()
    return regular_close_from_metadata(
        meta, ticker, session_date,
        allow_late_index_timestamp=allow_late_index_timestamp,
        allow_preliminary_index=allow_preliminary_index)


def ensure_session_close(df, ticker, session_date, column="Close",
                         allow_late_index_timestamp=False, allow_preliminary_index=False):
    """Cross-check or fill a Yahoo daily close using regular-close metadata."""
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    out.index = idx.normalize()
    session_ts = pd.Timestamp(session_date)
    quote_close = fetch_yahoo_regular_close(
        ticker, session_date,
        allow_late_index_timestamp=allow_late_index_timestamp,
        allow_preliminary_index=allow_preliminary_index)
    existing = None
    if session_ts in out.index and column in out.columns:
        raw = out.loc[session_ts, column]
        if isinstance(raw, pd.Series): raw = raw.dropna().iloc[-1] if not raw.dropna().empty else np.nan
        if pd.notna(raw): existing = float(raw)
    if "RawClose" in out and column == "Close":
        if existing is None: raise ValueError(f"{ticker}: an adjusted daily close is required; raw metadata cannot replace it")
        raw_close = float(out.loc[session_ts, "RawClose"])
        if abs(raw_close / quote_close - 1) > .0005:
            raise ValueError(f"{ticker}: raw daily close disagrees with regular-close metadata")
        return out.sort_index()  # preserve Adj Close, including on a dividend-adjusted historical row
    if existing is not None and abs(existing / quote_close - 1) > .0005:
        raise ValueError(f"{ticker}: daily close disagrees with regular-close metadata")
    out.loc[session_ts, column] = quote_close
    return out.sort_index()


def split_adjusted_basket_config(cfg, closes):
    """Keep baseline closes consistent with split-adjusted price history.

    Yahoo back-adjusts every earlier close after a stock split, so a baseline
    close typed in before the split is then N times too large (or small). When a
    holding's baseline differs from the adjusted close on the baseline date by a
    whole-number split factor, rescale that baseline and say so. Any other large
    mismatch is an error. Display/data hygiene only: no rule or weight changes.
    """
    out = dict(cfg); out["holdings"] = [dict(h) for h in cfg["holdings"]]; notes = []
    base_dt = pd.Timestamp(cfg["baseline_date"])
    for h in out["holdings"]:
        t = h["ticker"]
        if t not in closes.columns or base_dt not in closes.index or pd.isna(closes.loc[base_dt, t]):
            continue
        ratio = float(h["baseline_close"]) / float(closes.loc[base_dt, t])
        if abs(ratio - 1.0) <= 0.10:
            continue
        factor = round(ratio) if ratio > 1 else 1.0 / round(1.0 / ratio)
        if factor not in (0, 1) and abs(ratio / factor - 1.0) <= 0.03:
            h["baseline_close"] = float(h["baseline_close"]) / factor
            kind = f"{factor:.0f}-for-1 split" if factor > 1 else f"1-for-{1 / factor:.0f} reverse split"
            notes.append(f"{t} baseline rescaled for a {kind}; update top7_basket.json")
        else:
            raise ValueError(f"{t} baseline close {h['baseline_close']} does not match adjusted "
                             f"history on {base_dt.date()} ({float(closes.loc[base_dt, t]):.2f})")
    return out, notes


def ensure_basket_session_closes(df, tickers, session_date):
    """Validate/fill every Top-7 close from its regular-session metadata."""
    out = df.copy()
    for ticker in tickers:
        out = ensure_session_close(out, ticker, session_date, column=ticker)
    return out


def trading_days_missed(latest_d, today_d):
    """Count XNYS sessions from after latest_d through today_d, inclusive."""
    if today_d <= latest_d:
        return 0
    cal = xnyse_calendar()
    start = pd.Timestamp(latest_d) + pd.Timedelta(1, unit="D")
    return len(cal.sessions_in_range(start, pd.Timestamp(today_d)))


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
# Big-year WATCH — BROKERAGE ONLY (added 2026-11-11). Alert only, never a trade signal.
# Shown only once the Top-7 basket has been >= +WATCH_GAIN YTD at some point this year.
# Backtest (1964-2024 + 2026): an automatic +100%/-25% stop in the brokerage was roughly
# break-even after tax and only paid off in 2026, so selling stays a manual, high-conviction call.
BROK_BIG_YEAR_WATCH = {
    "watch_gain": 1.00,     # basket peak YTD gain that turns the watch on
    "review_drop": 0.25,    # drawdown from the high worth reviewing (cyclical baskets, e.g. memory)
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

QQQ_LEV_BROK_PARAMS = dict(QQQ_LEV_PARAMS)
QQQ_LEV_ROTH_PARAMS = dict(QQQ_LEV_PARAMS, exit_lag=1)

# ============================
# Data fetching
# ============================

def fetch_etf_yfinance(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    # QQQ needs its full adjusted history for an ALL-TIME high, not a rolling 3y high.
    df = yf.download(ticker, period="max" if ticker == "QQQ" else "3y",
                     auto_adjust=False, actions=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Adj Close" not in df or "Close" not in df:
        raise ValueError(f"{ticker}: dividend-adjusted and raw closes are both required")
    out = pd.DataFrame({"Close": df["Adj Close"], "RawClose": df["Close"],
                        "Dividends": df.get("Dividends", pd.Series(0., index=df.index))}).dropna(subset=["Close", "RawClose"])
    if out.empty: raise ValueError(f"{ticker}: adjusted history is empty")
    out.attrs["dividend_adjusted"] = True
    return out


def fetch_etf_stooq(ticker: str) -> pd.DataFrame:
    raise RuntimeError("Unverified adjustment fallback disabled; adjusted Yahoo history is required")


def fetch_etf(ticker: str) -> pd.DataFrame:
    return fetch_etf_yfinance(ticker)


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
    params = {"series_id": "UNRATE", "api_key": api_key, "file_type": "json",
              "sort_order": "desc", "limit": 120, "output_type": 4,
              "realtime_start": "1776-07-04", "realtime_end": today_et().isoformat()}
    try: resp = requests.get(FRED_API_BASE, params=params, timeout=30)
    except requests.RequestException: raise RuntimeError("First-release FRED network unavailable") from None
    if resp.status_code != 200: raise RuntimeError(f"First-release FRED request failed (HTTP {resp.status_code})")
    records = [{"DATE": pd.to_datetime(o["date"]), "UNRATE": float(o["value"]),
                "release_date": pd.to_datetime(o["realtime_start"])}
               for o in resp.json().get("observations", []) if o.get("value") not in (None, ".")]
    if len(records) < 4: raise RuntimeError("Fewer than four initial UNRATE releases")
    df = pd.DataFrame(records).sort_values("DATE").reset_index(drop=True)
    df.attrs['first_release'] = True
    return df


def fetch_unrate_latest_api(api_key) -> pd.DataFrame:
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


def _valid_unemployment(df,first_release):
    """Reject incomplete/invalid sources before advancing to the next fallback."""
    if df is None or not {'DATE','UNRATE'}.issubset(df.columns):raise ValueError('Missing unemployment columns')
    out=df.copy();out['DATE']=pd.to_datetime(out['DATE']);out['UNRATE']=pd.to_numeric(out['UNRATE'],errors='coerce')
    out=out.dropna(subset=['DATE','UNRATE']).sort_values('DATE').drop_duplicates('DATE').reset_index(drop=True)
    if len(out)<4 or not np.isfinite(out['UNRATE']).all():raise ValueError('Four finite unemployment observations required')
    if not np.all(np.diff(pd.DatetimeIndex(out.DATE)[-4:].to_period('M').asi8)==1):raise ValueError('Four consecutive unemployment months required')
    out.attrs['first_release']=bool(first_release)
    return out


def fetch_unrate() -> pd.DataFrame:
    # Review 1: first-release is preferred, never the sole availability path.
    snapshot=os.environ.get('UNEMPLOYMENT_SNAPSHOT')
    if snapshot:
        try:return _valid_unemployment(snapshot_unemployment(snapshot,today_et()),True)
        except Exception:print('UNRATE verified snapshot unavailable; trying next source')
    key=os.environ.get('FRED_API_KEY')
    if key:
        try:return _valid_unemployment(fetch_unrate_fred_api(key),True)
        except Exception:print('UNRATE first-release API unavailable; trying original revised-data path')
        try:return _valid_unemployment(fetch_unrate_latest_api(key),False)
        except Exception:print('UNRATE latest API unavailable; trying original fredgraph CSV')
    try:return _valid_unemployment(fetch_unrate_fallback(),False)
    except Exception:raise RuntimeError('All unemployment sources unavailable: verified snapshot, first-release API, original revised API/CSV') from None


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

def telegram_chunks(text,limit=3900):
    # Keep the owner's Roth reminder in the SAME message immediately after its instruction.
    units=lambda value:len(value.encode('utf-16-le'))//2
    lines=text.splitlines()
    provenance=next((line for line in lines if line.startswith('Unemployment data: ')),None)
    if provenance:
        body='\n'.join(line for line in lines if line!=provenance)
        if not body:return [provenance]
        return [provenance+'\n'+chunk for chunk in telegram_chunks(body,limit-units(provenance)-1)]
    blocks=[];i=0
    while i<len(lines):
        block=lines[i]
        if i+1<len(lines) and lines[i+1]==ROTH_BUY_REMINDER:
            block+='\n'+lines[i+1];i+=1
        blocks.append(block);i+=1
    chunks=[];current=[];size=0
    for block in blocks:
        if units(block)>limit:raise ValueError('A Telegram instruction block exceeds the safe chunk size')
        if current and size+units(block)+1>limit:
            chunks.append('\n'.join(current));current=[];size=0
        current.append(block);size+=units(block)+1
    if current:chunks.append('\n'.join(current))
    return chunks


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    responses=[]
    for chunk in telegram_chunks(text):
        payload={"chat_id":chat_id,"text":chunk,"parse_mode":"HTML","disable_web_page_preview":True}
        resp=requests.post(url,json=payload,timeout=30);resp.raise_for_status();responses.append(resp.json())
    return responses


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
        if key.startswith("_") or key=="substitutes":
            continue
        if ns.get('joint_policy'):
            # An assumed model purchase is never promoted to an actual broker fill.
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
                f"You still HOLD <b>{prev_position}</b> until the next-session MOC executes.</i>")
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
    close = close.reindex(columns=list(tickers))
    # yf.download silently returns an all-NaN/gappy column when one ticker's
    # request fails (rate limit, transient error). Re-fetch those individually.
    import time
    for t in tickers:
        if close[t].notna().sum() >= 0.95 * len(close) and close[t].tail(5).notna().all():
            continue
        for attempt in range(3):
            time.sleep(2 * (attempt + 1))
            try:
                h = yf.Ticker(t).history(period="1y", auto_adjust=True)
            except Exception as e:
                print(f"NOTE: basket re-fetch {t} attempt {attempt + 1} failed ({e})")
                continue
            if h is not None and not h.empty and "Close" in h:
                s = h["Close"]
                s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
                close = close.reindex(close.index.union(s.index))
                close[t] = close[t].combine_first(s)
                print(f"NOTE: basket {t} re-fetched individually ({s.notna().sum()} rows)")
                break
    return close.sort_index().dropna(how="all")


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


def compute_big_year_watch(nav, watch_gain):
    """Peak-YTD watch on the basket NAV (indexed to 1.0 at the January baseline).
    Returns None unless the basket has been >= +watch_gain YTD at some point this year."""
    peak = float(nav.max())
    if peak - 1.0 < watch_gain:
        return None
    last = float(nav.iloc[-1])
    return {"ytd": last - 1.0, "peak_ytd": peak - 1.0,
            "peak_date": pd.Timestamp(nav.idxmax()).date(),
            "off_high": last / peak - 1.0}


def render_big_year_watch(w):
    rd = BROK_BIG_YEAR_WATCH["review_drop"]
    hit = w["off_high"] <= -rd
    lines = [f"<u>Big-year watch</u> (alert only, no rule): {'⚠️' if hit else '👀'} basket "
             f"{w['ytd']:+.0%} YTD | peak {w['peak_ytd']:+.0%} on {w['peak_date']} | "
             f"{w['off_high']:+.0%} off high"]
    if hit:
        lines.append(f"  • <b>Down {int(rd*100)}%+ from the high.</b> If the basket is cyclical "
                     f"(e.g. memory), review a manual sale — only with extreme confidence. "
                     f"Gains held &lt;1yr are taxed short-term.")
    else:
        lines.append(f"  • Basket passed +{int(BROK_BIG_YEAR_WATCH['watch_gain']*100)}% this year. "
                     f"If cyclical (e.g. memory), revisit at a -{int(rd*100)}% drawdown from the high. "
                     f"No automatic sell.")
    return lines


def render_top7_section(label_prefix, params, st, fallback_first_date, unrate_failed=False,
                         flipped_today=False, prev_position=None, ratchet=None, ratchet_warn=None, wash_warnings=None):
    lines = []
    lines.append(f"<b>Top-7 ({label_prefix}) — {params['name']} — MODEL</b>")
    lines.append(f"<i>D-asym {params['ma_exit']}/{params['E_ma']}/{params['ma_re']}/{params['R_re']}/"
                 f"{params['vol_thr']:.2f}/{params['E_vol']}/tiered_{params['ur_thr']} | "
                 f"basket: TOP-7 stocks | defensive: tiered_{params['ur_thr']}</i>")
    if st["state"] == "invested":
        target = "TOP-7 STOCKS"
    else:
        target = "IEF" if st["_unrate_high"] else "SP500"
        if unrate_failed:
            target = f"{target} ⚠️"
    if flipped_today and prev_position and prev_position != target:
        lines.append(f"➤ <b>HOLD NOW: {prev_position}</b>  (until next-session MOC)")
        lines.append(f"➤ <b>NEXT-SESSION MOC: SELL {prev_position} → BUY {target}</b>")
        if target=='TOP-7 STOCKS' and wash_warnings:
            from html import escape
            lines.extend(escape(x,quote=False) for x in wash_warnings)
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
        lines.append("<u>Defensive routing</u>: ⚠️ <b>UNRATE FETCH FAILED</b> — defensive routing not confirmed; no new instruction.")
    else:
        lines.append(f"<u>Defensive routing</u>: ΔUNRATE_3mo &gt; {params['ur_thr']}pp → IEF, else → SP500")
    lines.append(_format_days_in_strategy(target, st, fallback_first_date, flipped_today, prev_position))
    return lines


def render_lev_section(label, params, st, fallback_first_date,
                       flipped_today=False, prev_position=None):
    if st.get('_joint_blocked'):
        return [f'<b>{label}</b>', 'DATA NOT READY — no leveraged trade instruction: '+str(st['_joint_blocked'])]
    if st.get('_joint_result'):
        return render_joint(st['_joint_result'], st['_joint_account'], st['_joint_facts'], html=True)
    lines = []
    sig = params["signal_asset"]; lev = params["leveraged"]; defv = params["defensive"]
    ma_n = params["ma"]; vt = int(params["vol_thr"] * 100)
    el = params["exit_lag"]; rl = params["entry_lag"]
    lines.append(f"<b>{label}</b>")
    lines.append(f"<i>MA{ma_n} v&lt;{vt}% e={el} r={rl} | leveraged: {lev} | defensive: {defv}</i>")
    target = lev if st["state"] == "invested" else defv
    if flipped_today and prev_position and prev_position != target:
        lines.append(f"➤ <b>HOLD NOW: {prev_position}</b>  (until next-session MOC)")
        lines.append(f"➤ <b>NEXT-SESSION MOC: SELL {prev_position} → BUY {target}</b>")
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


def build_summary_block(prev_state, new_state, conditions_line, top7_warnings=None):
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
        if key.startswith("_") or key=="substitutes" or ns.get("joint_policy"): continue
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
        lines.append(
            f"<b>🔴 TOP-7 MODEL ACTION — {n} flip{'s' if n>1 else ''} at the "
            "NEXT MARKET-SESSION MOC (submit before your broker's cutoff)</b>")
        lines.append(conditions_line)
        for lbl, frm, to in flips:
            lines.append(f"  • <b>SELL {frm}, BUY {to}</b>  ({lbl})")
            if to=='TOP-7 STOCKS':
                from html import escape
                lines.extend(escape(x,quote=False) for x in (top7_warnings or {}).get(lbl,[]))
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
    lines.append(f"<b>🟢 TOP-7 — {n_total} of {n_total} strategies risk-on, no signals progressing</b>")
    lines.append(conditions_line)
    return lines


# ============================
# Main
# ============================

def main(current_et=None, *, dry_run=False, persist=True, monitor_mode=False, data_bundle=None, facts_override=None, state_override=None, publication_grace_minutes=15, historical_only=False):
    manual_run = os.environ.get("MANUAL_RUN", "").lower() == "true"
    final_attempt = os.environ.get("FINAL_ATTEMPT", "").lower() == "true"
    force_repeat = os.environ.get("FORCE_REPEAT", "").lower() == "true"
    force_partial = os.environ.get("FORCE_PARTIAL_DATA", "").lower() == "true"
    import pytz
    et_tz = pytz.timezone("America/New_York")
    current_et = current_et or datetime.now(et_tz)
    expected_session = latest_completed_market_session(current_et,grace_minutes=publication_grace_minutes)
    if historical_only and data_bundle is None and expected_session>=today_et(): raise ValueError("Historical mode is only for an already-completed prior date")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not dry_run and (not bot_token or not chat_id): raise RuntimeError('Telegram credentials required for delivery; use --dry-run for local preview')

    prev = load_state() if state_override is None else state_override
    joint_results = {}; joint_error = None
    if not force_repeat and state_reported_market_date(prev) == expected_session:
        print(f"Skipping - market session {expected_session} was already reported")
        return

    health = {"SPY": False, "QQQ": False, "NASDAQCOM": False, "UNRATE": False}

    try:
        # The audited strategy uses Yahoo adjusted closes. Automated runs fail
        # closed rather than silently switching to Stooq's different series.
        spy_df = data_bundle["prices"]["SPY"].copy() if data_bundle is not None else (fetch_etf("SPY") if force_partial else fetch_etf_yfinance("SPY"))
        spy_df = frame_through_session(spy_df, expected_session)
        if not force_partial and data_bundle is None and not historical_only:
            spy_df = ensure_session_close(spy_df, "SPY", expected_session)
        if spy_df is None or spy_df.empty:
            raise ValueError(f"SPY has no row through required session {expected_session}")
        if not force_partial:
            assert_recent_session_continuity(
                spy_df, "SPY", expected_session, 350, columns=["Close"])
        health["SPY"] = True
    except Exception as e:
        if not force_partial and not (final_attempt or manual_run):
            print(f"Deferring scheduled report - SPY input is not ready: {e}")
            return
        raise
    try:
        qqq_df = data_bundle["prices"]["QQQ"].copy() if data_bundle is not None else (fetch_etf("QQQ") if force_partial else fetch_etf_yfinance("QQQ"))
        qqq_df = frame_through_session(qqq_df, expected_session)
        if not force_partial and data_bundle is None and not historical_only:
            qqq_df = ensure_session_close(qqq_df, "QQQ", expected_session)
        if qqq_df is None or qqq_df.empty:
            raise ValueError(f"QQQ has no row through required session {expected_session}")
        if not force_partial:
            assert_recent_session_continuity(
                qqq_df, "QQQ", expected_session, 350, columns=["Close"])
        health["QQQ"] = True
    except Exception as e:
        print(f"WARN: QQQ fetch failed: {e}"); qqq_df = None
    # Read-only early monitor: before 5:15 PM it may use the index value published
    # at the close, labelled preliminary. The delivering bot always waits for final.
    nasdaq_preliminary = bool(data_bundle.get("nasdaq_preliminary")) if data_bundle is not None else False
    try:
        nasdaqcom_df = data_bundle["nasdaq"].copy() if data_bundle is not None else (fetch_nasdaqcom() if force_partial else fetch_nasdaqcom_yfinance())
        nasdaqcom_df = frame_through_session(nasdaqcom_df, expected_session)
        if not force_partial and data_bundle is None and not historical_only:
            try:
                nasdaqcom_df = ensure_session_close(
                    nasdaqcom_df, "^IXIC", expected_session, column="NASDAQCOM",
                    allow_late_index_timestamp=True)
            except ValueError as _early:
                if not (monitor_mode and dry_run and "final-correction boundary" in str(_early)):
                    raise
                nasdaqcom_df = ensure_session_close(
                    nasdaqcom_df, "^IXIC", expected_session, column="NASDAQCOM",
                    allow_late_index_timestamp=True, allow_preliminary_index=True)
                nasdaq_preliminary = True
        if nasdaqcom_df is None or nasdaqcom_df.empty:
            raise ValueError(f"NASDAQCOM has no row through required session {expected_session}")
        if not force_partial:
            assert_recent_session_continuity(
                nasdaqcom_df, "NASDAQCOM", expected_session, 350,
                columns=["NASDAQCOM"])
        health["NASDAQCOM"] = True
    except Exception as e:
        print(f"WARN: NASDAQCOM fetch failed: {e}"); nasdaqcom_df = None
    try:
        un = data_bundle['unemployment'].copy() if data_bundle is not None else fetch_unrate()
        if 'release_date' in un: un = un[un.release_date <= pd.Timestamp(expected_session)]
        health['UNRATE'] = len(un) >= 4
    except Exception as e:
        print(f"WARN: UNRATE fetch failed: {e}"); un = None

    if (un is None or len(un) < 4) and not force_partial:
        msg = "UNRATE input is unavailable or has fewer than four observations"
        if final_attempt or manual_run:
            raise RuntimeError(msg)
        print(f"Deferring scheduled report - {msg}")
        return

    spy_close = spy_df["Close"].copy()
    spy_close.index = pd.to_datetime(spy_close.index).normalize()
    latest_date = spy_close.index[-1]
    latest_close = float(spy_close.iloc[-1])
    today_d = current_et.date()
    latest_d = latest_date.date() if isinstance(latest_date, pd.Timestamp) else latest_date

    # Sanity check: SPY history must be long enough for SMA300
    if len(spy_close) < 350:
        if dry_run: raise RuntimeError(f"SPY DATA TOO SHORT: {len(spy_close)} rows; need 350+")
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

    missing_required = required_data_missing(
        expected_session,
        {"SPY": latest_d, "QQQ": qqq_last_d, "NASDAQCOM": nasdaqcom_last_d},
    )
    if missing_required and not force_partial and not (monitor_mode and set(missing_required) <= {"NASDAQCOM"}):
        msg = (f"required session {expected_session} is not available for: "
               f"{', '.join(missing_required)}")
        if final_attempt or manual_run:
            raise RuntimeError(msg)
        print(f"Deferring scheduled report - {msg}")
        return

    if un is not None and len(un)>=4:
        months=pd.DatetimeIndex(un['DATE'] if 'DATE' in un else un.index)[-4:].to_period('M').asi8
        if not np.all(np.diff(months)==1):raise RuntimeError('Four consecutive monthly first releases are required')
    # UNRATE delta
    if un is not None and len(un) >= 4:
        un_now_rate = float(un.iloc[-1]["UNRATE"])
        un_prior_rate = float(un.iloc[-4]["UNRATE"])
        un_chg = un_now_rate - un_prior_rate
        un_flag_01 = unemployment_rising(un_chg)
    else:
        un_chg = 0.0; un_flag_01 = False

    # Walkers
    roth_top7_st = walk_dasym_state(spy_close, sma300, sma100, vol30,
                                     ROTH_TOP7_PARAMS["vol_thr"], ROTH_TOP7_PARAMS["E_vol"],
                                     ROTH_TOP7_PARAMS["E_ma"], ROTH_TOP7_PARAMS["R_re"])
    roth_top7_st["_unrate_high"] = un_flag_01

    # --- Runup ratchet (ROTH ONLY). Third exit path; overrides the D-asym state when fired. ---
    _cfg = None
    _nav_latest = None
    ratchet = None
    ratchet_warn = None
    brok_watch = None
    basket_note = None
    try:
        _cfg, ratchet_warn = (data_bundle.get("basket_config"), None) if data_bundle is not None else load_basket_config(TOP7_RATCHET_PARAMS["basket_file"], pd.Timestamp(expected_session))
        if _cfg is None:
            raise RuntimeError(ratchet_warn or "Top-7 basket config is unavailable")
        _tickers = [h["ticker"] for h in _cfg["holdings"]]
        _closes = data_bundle["basket"].copy() if data_bundle is not None else fetch_basket_closes(_tickers)
        _closes = frame_through_session(_closes, expected_session)
        _cfg, _split_notes = split_adjusted_basket_config(_cfg, _closes)
        # A data-source gap in one stock (e.g. after a split) is recorded, not fatal
        # by itself: see below. It only matters if the ratchet could still change.
        basket_gap = None
        if not force_partial:
            try:
                if data_bundle is None and not historical_only:
                    _closes = ensure_basket_session_closes(_closes, _tickers, expected_session)
                assert_recent_session_continuity(
                    _closes, "Top-7 basket", expected_session, 1,
                    columns=_tickers, start_session=_cfg["baseline_date"])
            except Exception as _gap:
                basket_gap = _gap
        _nav = compute_basket_nav(_cfg, _closes)
        _nav_latest = pd.Timestamp(_nav.index[-1]).date()
        if basket_gap is None and not force_partial and required_data_missing(
                expected_session, {"Top-7 basket": _nav_latest}):
            basket_gap = RuntimeError(
                f"required session {expected_session} is not available for the Top-7 basket")
        ratchet = walk_ratchet_state(_nav, TOP7_RATCHET_PARAMS["arm_gain"],
                                     TOP7_RATCHET_PARAMS["stop_drop"])
        if basket_gap is not None:
            # The ratchet never un-fires within a year, so once it has fired on the
            # complete history, later missing prices cannot change the Roth signal.
            # Earlier scheduled attempts still wait for the data; only the final
            # (or a manual) attempt reports without it.
            if not ratchet["fired"] or not (final_attempt or manual_run):
                raise basket_gap
            ratchet_warn = (f"basket prices incomplete after {_nav_latest} ({basket_gap}); "
                            f"ratchet already fired {pd.Timestamp(ratchet['fire_date']).date()} "
                            "and stays locked, so the signal is unaffected. Basket figures below are as of that date.")
            print("NOTE: " + ratchet_warn)
            basket_note = ("Top-7 basket prices have gaps (data source). "
                           "No effect: the Roth ratchet already fired and stays locked.")
        else:
            try:
                brok_watch = compute_big_year_watch(_nav, BROK_BIG_YEAR_WATCH["watch_gain"])
            except Exception as _e:
                print(f"NOTE: big-year watch skipped ({_e})")
        if _split_notes:
            ratchet_warn = "; ".join(_split_notes + ([ratchet_warn] if ratchet_warn else []))
    except Exception as e:
        if monitor_mode:
            nasdaqcom_signal_fresh = False
            ratchet_warn = 'Top-7 data incomplete; retain prior state; no Top-7 instruction'
        elif not force_partial:
            if final_attempt or manual_run: raise
            print(f"Deferring scheduled report - ratchet input is not ready: {e}")
            return
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

    qqq_lev_roth_st = dict(qqq_lev_st) if qqq_lev_st is not None else None

    # Build new_state (with paths for Top-7)
    def top7_position(st):
        if st["state"] == "invested": return "TOP-7 STOCKS"
        return "IEF" if un_flag_01 else "SP500"

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

    facts = facts_override if facts_override is not None else load_facts(
        export_file=os.environ.get('SPOUSE_RH_EXPORT'),
        extra_losses=json.loads(os.environ.get('HOUSEHOLD_LOSSES_JSON', '[]')))
    if facts_override is None and 'substitutes' in prev:facts['substitutes']=dict(prev['substitutes'])
    facts['substitutes']=normalize_substitutes(facts.get('substitutes',{}))
    new_state['substitutes']=dict(facts['substitutes'])
    if facts_override is None:
        facts=merge_observed_facts(facts,(prev.get('_joint') or {}).get('observed_facts'))
    try:
        fund_prices = {}
        for ticker in ('TQQQ','QLD','UPRO','SSO','IEF','USFR'):
            f = data_bundle['prices'][ticker].copy() if data_bundle is not None else fetch_etf_yfinance(ticker)
            f = frame_through_session(f, expected_session)
            if f.empty or pd.Timestamp(f.index[-1]).date() != expected_session:
                raise ValueError(f'{ticker} adjusted return history is stale')
            if data_bundle is None and not force_partial and not historical_only: f = ensure_session_close(f,ticker,expected_session)
            fund_prices[ticker] = f
        next_session = xnyse_calendar().next_session(pd.Timestamp(expected_session)).date()
        for sleeve,ix in [('UPRO',spy_close),('TQQQ',qqq_close_raw if qqq_signal_fresh else None)]:
            if ix is None: continue
            joint_results[sleeve] = evaluate_sleeve(sleeve,fund_prices,ix,un_chg,next_session,facts,
                unemployment_history=first_release_delta_history(un,ix.index))
        for sleeve,prefix,walkers in [('UPRO','spy_lev',{'R':spy_lev_roth_st,'B':spy_lev_brok_st}),
                                      ('TQQQ','qqq_lev',{'R':qqq_lev_roth_st,'B':qqq_lev_st})]:
            if sleeve not in joint_results: continue
            result = joint_results[sleeve]
            for account,suffix in [('R','roth'),('B','brok')]:
                key = prefix+'_'+suffix; model = result['model_accounts'][account]
                observed = result['observed'].get('roth' if account=='R' else 'brokerage',{})
                target = model['holding']; actual = observed.get('holding')
                ns = new_state[key]
                ns.update(joint_policy=True,model_position=target,signal_position=target,
                    actual_position=actual,position=actual or 'UNKNOWN',facts_as_of=result['observed'].get('as_of'),
                    state='invested' if target in ('TQQQ','QLD','UPRO','SSO') else 'defensive',
                    exit_threshold=1 if account=='R' or sleeve=='UPRO' else 4)
                ns.pop('pending_order',None)
                walkers[account].update(_joint_result=result,_joint_account=account,_joint_facts=facts)
        new_state['_joint'] = {'rules_version':'2026-11-27','models':joint_results,'facts_source':facts.get('source'),'observed_facts':cached_observed_facts(facts)}
        # Persist model quantities only; observed account sizes stay in the private export.
        for result in new_state['_joint']['models'].values():
            for item in result['observed'].values():
                if isinstance(item,dict):item.pop('quantity',None)
    except Exception as e:
        joint_error = str(e)
        if not force_partial:
            if manual_run or final_attempt or dry_run: raise
            print('Deferring scheduled report - joint model inputs unavailable: '+joint_error)
            return
        for st in [spy_lev_roth_st,spy_lev_brok_st,qqq_lev_roth_st,qqq_lev_st]:
            if st is not None: st['_joint_blocked'] = joint_error

    if not nasdaqcom_signal_fresh:
        new_state = carry_forward_guarded_state(
            prev, new_state, ("top7_roth", "top7_brok"),
            "NASDAQCOM data stale; Top-7 state retained until native data refreshes")
    if not qqq_signal_fresh:
        new_state = carry_forward_guarded_state(
            prev, new_state, ("qqq_lev_roth", "qqq_lev_brok"),
            "QQQ data stale; QQQ leveraged state retained until native data refreshes")

    if not nasdaqcom_signal_fresh:
        for key in ('top7_roth','top7_brok'):
            new_state[key] = dict(prev.get(key) or {'label':key,'state':'unknown','position':'UNKNOWN'}, data_guarded='Top-7 input not final; no new instruction')
            new_state[key].pop('pending_order',None)
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
    top7_warnings={}
    stock_tickers=[h['ticker'] for h in (_cfg or {}).get('holdings',[])]
    stock_buy_date=xnyse_calendar().next_session(pd.Timestamp(expected_session)).date()
    for key,role in [('top7_roth','roth'),('top7_brok','brokerage')]:
        identity=(facts.get('top7_accounts') or {}).get(role) or {}
        if isinstance(identity,str):identity={'label':identity}
        account=identity.get('label','Roth' if role=='roth' else 'Brokerage')
        top7_warnings[new_state[key]['label']]=top7_buy_warnings(account,stock_tickers,stock_buy_date,facts,identity.get('account_last4'),role=='brokerage')
    summary_block = build_summary_block(prev, new_state, conditions_line,top7_warnings)

    from html import escape
    unemployment_line=unemployment_data_line(un)
    lines = [f"<b>📊 {ACCOUNT_LABEL}</b>", f"{latest_date.strftime('%Y-%m-%d')}",escape(unemployment_line,quote=False)]
    lines.append('Observed leveraged facts: '+str(facts.get('source')))
    lines.append('MODEL signals and assumed fills are not broker confirmations. Top-7 holdings are MODEL only.')
    lines.append('Household watch coverage: '+str(facts.get('household_coverage','only explicitly supplied observations')))
    from html import escape
    for note in facts.get('notes',[]):
        if 'OBSERVED DRIP' in note or 'export not present' in note:lines.append(escape(note,quote=False))
    if data_bundle is not None: lines.append('Data provenance: '+str(data_bundle.get('source','explicit offline bundle')))
    if force_partial:
        lines.append(
            "⚠️ <b>FORCED INCOMPLETE-DATA DIAGNOSTIC</b> — do not treat this "
            "as a close-confirmed action alert. State will not be saved.")
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
                                          ratchet=ratchet, ratchet_warn=ratchet_warn,wash_warnings=top7_warnings.get(new_state['top7_roth']['label'])))
    else:
        lines.extend(render_guarded_state_section("Top-7 (Roth) — ALT-A", new_state.get("top7_roth", {}), "TOP-7 STOCKS"))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Roth)", SPY_LEV_ROTH_PARAMS, spy_lev_roth_st,
                                     spy_first_date,
                                     flipped_today=("spy_lev_roth" in flipped),
                                     prev_position=flipped.get("spy_lev_roth")))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Roth)", QQQ_LEV_ROTH_PARAMS, qqq_lev_roth_st,
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
                                          prev_position=flipped.get("top7_brok"),wash_warnings=top7_warnings.get(new_state["top7_brok"]["label"])))
        if brok_watch is not None:
            lines.extend(render_big_year_watch(brok_watch))
    else:
        lines.extend(render_guarded_state_section("Top-7 (Brok) — BROK_A", new_state.get("top7_brok", {}), "TOP-7 STOCKS"))
    lines.append("")
    lines.extend(render_lev_section("SPY Leveraged (Brok)", SPY_LEV_BROK_PARAMS, spy_lev_brok_st,
                                     spy_first_date,
                                     flipped_today=("spy_lev_brok" in flipped),
                                     prev_position=flipped.get("spy_lev_brok")))
    lines.append("")
    if qqq_lev_st is not None:
        lines.extend(render_lev_section("QQQ Leveraged (Brok)", QQQ_LEV_BROK_PARAMS, qqq_lev_st,
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

    moc_orders = []
    for sleeve,result in joint_results.items():
        if sleeve != 'UPRO': continue
        for account,key in [('R','roth'),('B','brokerage')]:
            actual=result['observed'].get(key,{}).get('holding')
            if result['signal_meta'][key].get('pending_exit') and actual in ('UPRO','SSO'):
                moc_orders.append({'account':"Katelyn Roth IRA" if account=='R' else 'Brokerage',
                    'sell':actual,'buy':defensive_holding(un_chg),'sleeve':'UPRO','signal_date':expected_session.isoformat()})
    if nasdaqcom_signal_fresh:
        for key,walker in [('top7_roth',roth_top7_st),('top7_brok',brok_top7_st)]:
            transition=walker.get('last_transition_date')
            ratchet_flip=key=='top7_roth' and ratchet is not None and ratchet.get('fired') and pd.Timestamp(ratchet['fire_date']).date()==expected_session
            if (transition is not None and pd.Timestamp(transition).date()==expected_session) or ratchet_flip:
                target=new_state[key]['signal_position']
                previous=state_actual_position(prev.get(key,{})) or ('TOP-7 STOCKS' if target!='TOP-7 STOCKS' else 'previous defensive holding (confirm)')
                moc_orders.append({'account':'Roth' if key=='top7_roth' else 'Brokerage','sell':previous,
                    'buy':target,'sleeve':'Top-7','signal_date':expected_session.isoformat()})
    for order in moc_orders:
        lines.append('MODEL next-close plan: '+order['account']+': sell '+order['sell']+' / buy '+order['buy']+' at the next session close (MOC).')
        if order['sleeve']=='Top-7' and order['buy']=='TOP-7 STOCKS':
            order['tickers']=stock_tickers
            role='roth' if 'roth' in order['account'].lower() else 'brokerage'
            identity=(facts.get('top7_accounts') or {}).get(role) or {}
            if isinstance(identity,str):identity={'label':identity}
            lines.extend(escape(x,quote=False) for x in top7_buy_warnings(identity.get('label',order['account']),stock_tickers,stock_buy_date,facts,identity.get('account_last4'),role=='brokerage'))
    rebalance_notes=top7_rebalance_warnings(facts)
    if rebalance_notes:
        lines.append('Top-7 user-supplied January plan / before-window watch (text only; no signal or holding change):')
        lines.extend(escape(x,quote=False) for x in rebalance_notes)
    open_symbols=sorted({x['symbol'] for x in facts.get('taxable_loss_sales',[]) if 0<=(current_et.date()-pd.Timestamp(x['date']).date()).days<=30})
    if open_symbols:
        lines.append('Household wash-sale WATCH — every supplied taxable-loss ticker:')
        lines.extend(escape(wash_text(t,current_et.date(),facts['taxable_loss_sales']),quote=False) for t in open_symbols)
    text = "\n".join(lines)
    # Short message (display only): trades first, then early warnings. The full
    # report above is kept in the run log and in the returned 'text'.
    from short_report import build_short_report
    from joint_runtime import UNEMPLOYMENT_REVISED_LINE
    def _top7_entry(key, has_ratchet):
        e = new_state.get(key, {})
        return {'label': e.get('label', key), 'invested': e.get('state') == 'invested',
                'guarded': bool(e.get('data_guarded')), 'paths': e.get('paths', []),
                'reentry_streak': e.get('reentry_streak'), 'reentry_threshold': e.get('reentry_threshold'),
                'locked': bool(e.get('ratchet_locked')), 'has_ratchet': has_ratchet,
                'pending': e.get('pending_order')}
    # Preliminary Nasdaq (early monitor only): say so only when it could matter, i.e.
    # 30-day volatility sits within half a point of a Top-7 limit.
    from short_report import preliminary_top7_notes
    top7_preliminary_notes = preliminary_top7_notes(
        vol30_v if (nasdaq_preliminary and nasdaqcom_signal_fresh) else None,
        {"Roth": ROTH_TOP7_PARAMS["vol_thr"], "brokerage": BROK_TOP7_PARAMS["vol_thr"]})
    def _top7_key_part(key):
        e = new_state.get(key, {}); pend = e.get('pending_order') or {}
        return (f"{key}={e.get('signal_position') or e.get('position')}"
                f"|{pend.get('sell', '-')}>{pend.get('buy', '-')}"
                f"|{'guarded' if e.get('data_guarded') else 'ok'}")
    top7_key = ";".join(_top7_key_part(k) for k in ('top7_roth', 'top7_brok'))
    short_text = build_short_report({
        'date': expected_session, 'joint': joint_results, 'joint_error': joint_error,
        'lev_metrics': {
            'UPRO': {'index': 'SPY', 'price': latest_close, 'ma': sma300_v, 'ma_days': 300,
                     'vol': spy_vol20_v, 'cap': SPY_LEV_ROTH_PARAMS['vol_thr']},
            'TQQQ': {'index': 'QQQ', 'price': qqq_close_v, 'ma': qqq_sma150_v, 'ma_days': 150,
                     'vol': qqq_vol20_v, 'cap': QQQ_LEV_PARAMS['vol_thr']}},
        'top7': [_top7_entry('top7_roth', True), _top7_entry('top7_brok', False)],
        'ratchet': ratchet, 'arm_gain': TOP7_RATCHET_PARAMS['arm_gain'],
        'moc_orders': moc_orders, 'top7_warnings': top7_warnings,
        'losses': facts.get('taxable_loss_sales', []),
        'unemployment_revised': unemployment_line == UNEMPLOYMENT_REVISED_LINE,
        'missing': [k for k in ('SPY', 'QQQ') if not health.get(k)],
        'extra_notes': ([basket_note] if basket_note else []) + top7_preliminary_notes})
    if force_partial:
        short_text = "⚠️ FORCED INCOMPLETE-DATA DIAGNOSTIC — do not act on this.\n" + short_text
    if not dry_run: print(text)
    if not dry_run: send_telegram(bot_token, chat_id, short_text)
    if force_partial:
        print("Forced incomplete-data diagnostic; state intentionally not saved")
        return {"text":text,"state":new_state,"joint":joint_results,"facts":facts,"signal_date":expected_session.isoformat(),"incomplete":True,"unemployment_data_line":unemployment_line,"short_text":short_text,"top7_key":top7_key,"nasdaq_preliminary":nasdaq_preliminary}
    meta = dict(prev.get("_meta") or {})
    meta["last_reported_market_date"] = expected_session.isoformat()
    meta["last_reported_at_et"] = current_et.isoformat()
    meta["source_dates"] = {
        "SPY": latest_d.isoformat(),
        "QQQ": qqq_last_d.isoformat(),
        "NASDAQCOM": nasdaqcom_last_d.isoformat() if nasdaqcom_last_d else None,
        "TOP7_BASKET": _nav_latest.isoformat() if _nav_latest else None,
    }
    meta["price_gate"] = (
        "Yahoo regularMarketPrice metadata checked at the exchange close; "
        "Nasdaq Composite accepted only after its 5:15 PM correction window")
    new_state["_meta"] = meta
    if persist and not dry_run: save_state(new_state)
    if persist and not dry_run: print(f"State saved to {STATE_FILE}")
    return {'text':text,'state':new_state,'joint':joint_results,'facts':facts,'signal_date':expected_session.isoformat(),'incomplete':False,'moc_orders':moc_orders,'unemployment_data_line':unemployment_line,'short_text':short_text,'top7_key':top7_key,'nasdaq_preliminary':nasdaq_preliminary}


if __name__ == "__main__":
    import argparse
    from joint_runtime import load_data_bundle, blocked_preview
    parser=argparse.ArgumentParser()
    parser.add_argument('--dry-run',action='store_true',help='Print only; no Telegram delivery or state write')
    parser.add_argument('--facts',help='Read-only spouse export JSON')
    parser.add_argument('--seed',help='Explicit observed strategy-state JSON')
    parser.add_argument('--fixture',help='Explicit offline data bundle; requires --dry-run')
    parser.add_argument('--unemployment-json',help='Verified first-release observations')
    parser.add_argument('--loss',action='append',default=[],help='SYM@timestamp@gain@account; taxable losses only')
    args=parser.parse_args()
    if args.fixture and not args.dry_run:parser.error('--fixture requires --dry-run')
    if args.facts:os.environ['SPOUSE_RH_EXPORT']=args.facts
    if args.unemployment_json:os.environ['UNEMPLOYMENT_SNAPSHOT']=args.unemployment_json
    if args.loss:os.environ['HOUSEHOLD_LOSSES_JSON']=json.dumps(args.loss)
    bundle=load_data_bundle(args.fixture) if args.fixture else None
    facts=bundle.get('facts') if bundle else None
    if facts is None:facts=load_facts(seed_file=args.seed or Path(__file__).with_name('joint_state_seed.json'),export_file=args.facts,extra_losses=args.loss)
    if bundle and not bundle.get('data_ready',True):
        print(blocked_preview(facts,bundle.get('blocked_reason','input verification incomplete'),bundle.get('clock','unspecified')))
    else:
        try:
            output=main(current_et=pd.Timestamp(bundle['clock']).to_pydatetime() if bundle else None,dry_run=args.dry_run,persist=not args.dry_run,data_bundle=bundle,facts_override=facts if bundle is not None or args.seed else None)
            if args.dry_run:
                print(output['text'] if output else blocked_preview(facts,'required completed-session data unavailable',datetime.now().isoformat()))
        except Exception as exc:
            if not args.dry_run:raise
            print(blocked_preview(facts,str(exc),bundle.get('clock') if bundle else datetime.now().isoformat()))
