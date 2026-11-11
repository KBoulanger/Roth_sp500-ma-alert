"""Post-close portfolio monitor (all 6 sleeves). Rules + params come from the live Telegram bot
code on GitHub (main), so this report can never drift from the bot. Exit codes: 0 ok, 2 stale."""
import re, sys, json, importlib.util, datetime as dt, urllib.request
import numpy as np, pandas as pd, pytz, yfinance as yf, requests
import exchange_calendars as xc

RAW = "https://raw.githubusercontent.com/KBoulanger/Roth_sp500-ma-alert/main/"
et = pytz.timezone("America/New_York"); now = dt.datetime.now(et)
cal = xc.get_calendar("XNYS"); today = pd.Timestamp(now.date())
if not cal.is_session(today):
    print(f"MARKET CLOSED: {now.date()} is not an NYSE trading day"); sys.exit(0)
close_t = cal.session_close(today).tz_convert(et)
if now < close_t + pd.Timedelta(2, unit="m"):
    print(f"TOO EARLY: today's close is {close_t:%H:%M} ET"); sys.exit(0)
prev_sess = cal.previous_session(today)

# ---- bot code (single source of truth for rules/params) ----
src = requests.get(RAW + "send_spy_ma_telegram.py", timeout=30).text
open("bot_live.py", "w").write(src)
spec = importlib.util.spec_from_file_location("bot_live", "bot_live.py"); bot = importlib.util.module_from_spec(spec); spec.loader.exec_module(bot)
open("top7_basket.json", "w").write(requests.get(RAW + "top7_basket.json", timeout=30).text)
TIMING = getattr(bot, "EXECUTION_TIMING", {
    "spy_lev_roth": {"exit": "next_close", "entry": "same_day"}, "spy_lev_brok": {"exit": "next_close", "entry": "same_day"},
    "qqq_lev_roth": {"exit": "same_day", "entry": "same_day"}, "qqq_lev_brok": {"exit": "same_day", "entry": "same_day"}})
WHEN = {"same_day": "TODAY, minutes after the close (after-hours LIMIT order)",
        "next_close": "TOMORROW's close (MOC order, submit before 3:50 PM ET)"}

RH, LOSSES = {}, []
for a in sys.argv[1:]:
    k, v = a.split("=", 1)
    if k.upper() == "LOSS":  # taxable-account loss sale: LOSS=TQQQ@<ISO timestamp>@<realized gain>@<account>
        s, ts, g, acc = v.split("@", 3)
        sd = pd.Timestamp(ts).tz_convert(et).date() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).date()
        LOSSES.append((s.upper(), sd, float(g), acc.replace("_", " ")))
        continue
    px, d = v.split("@"); RH[k.upper()] = (float(px), dt.date.fromisoformat(d))
def wash(sym, on=None):
    """Open wash-sale window for sym (a taxable loss sold within the last 30 days), else None."""
    on = on or now.date(); hits = [x for x in LOSSES if x[0] == sym and 0 <= (on - x[1]).days <= 30]
    return max(hits, key=lambda x: x[1]) if hits else None
BASKET = []
def wash_any(name):
    syms = BASKET if name.startswith("Top-7") else [name]
    return [w for w in (wash(x) for x in syms) if w]
def wash_line(x):
    return (f"**{x[0]}** sold at a **${-x[2]:,.0f} loss** in {x[3]} on {x[1]:%b %-d} → don't buy {x[0]} in ANY account "
            f"through **{x[1] + dt.timedelta(days=30):%b %-d}** (Roth/IRA buy = loss lost forever; taxable buy = loss deferred). Safe from {x[1] + dt.timedelta(days=31):%b %-d}.")

problems, notes = [], []
APPROX = set()

def series(sym, rh_key=None, adjust=True):
    """Adjusted daily closes with TODAY's official close verified (Yahoo stamp and/or Robinhood)."""
    meta = {}
    try:
        tk = yf.Ticker(sym)
        h = tk.history(period="3y", auto_adjust=adjust)
        c = h["Close"].dropna(); c.index = pd.to_datetime(c.index.tz_localize(None)).normalize()
        assert len(c) > 400
        meta = tk.history_metadata or {}
    except Exception as ex:  # fallback: Nasdaq.com unadjusted daily history
        try:
            nsym = {"^IXIC": "COMP"}.get(sym, sym); acl = "index" if sym.startswith("^") else "etf"
            js = requests.get(f"https://api.nasdaq.com/api/quote/{nsym}/historical", params=dict(assetclass=acl,
                 fromdate=str(today.date() - dt.timedelta(days=1100)), todate=str(today.date()), limit=9999),
                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=20).json()
            c = pd.Series({pd.Timestamp(x["date"]): float(x["close"].replace("$", "").replace(",", ""))
                           for x in js["data"]["tradesTable"]["rows"]}).sort_index()
            assert len(c) > 400
            APPROX.add(sym); notes.append(f"{sym}: Yahoo history down ({ex}); using Nasdaq.com UNADJUSTED history")
        except Exception as ex2:
            problems.append(f"{sym}: no price history (Yahoo: {ex}; Nasdaq: {ex2})"); return None, None
    mp, mt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if mt is not None and not isinstance(mt, (int, float)): mt = int(pd.Timestamp(mt).timestamp())
    md = dt.datetime.fromtimestamp(int(mt), et) if mt else None
    y_ok = bool(mp and md and md.date() == today.date() and md >= close_t.to_pydatetime())
    rh = RH.get(rh_key or sym); r_ok = bool(rh and rh[1] == today.date())
    if y_ok and r_ok:
        if abs(float(mp) / rh[0] - 1) > 0.0002:
            problems.append(f"{sym}: Yahoo {mp:.2f} vs Robinhood {rh[0]:.2f} disagree"); return None, None
        px, how = rh[0], "Yahoo+Robinhood agree"
    elif r_ok: px, how = rh[0], "Robinhood only"
    elif y_ok: px, how = float(mp), "Yahoo only"
    else:
        problems.append(f"{sym}: no official close for {today.date()} (Yahoo stamped {md})"); return None, None
    hist_today = c.get(today)
    if "only" in how and hist_today is not None and abs(hist_today / px - 1) > 0.0005:
        problems.append(f"{sym}: daily bar {hist_today:.2f} vs official {px:.2f} disagree"); return None, None
    c.loc[today] = px; c = c.sort_index()
    miss = [d.date() for d in cal.sessions_window(today, -40) if pd.Timestamp(d.date()) not in c.index]
    if miss: problems.append(f"{sym}: missing sessions {miss[:3]}"); return None, None
    return c, how

spy, spy_src = series("SPY")
qqq, qqq_src = series("QQQ")
if problems:
    print("DATA NOT READY:"); [print(" -", p) for p in problems]
    print("RESULT: STALE DATA - NO SIGNAL COMPUTED"); sys.exit(2)
ixic, ixic_src = series("^IXIC")
if ixic is None:  # NASDAQ only feeds Top-7's vol path (far from its 40%/45% triggers) - don't block the leveraged sleeves
    notes.append("NASDAQ close for today not verified: " + problems.pop() + " — Top-7 vol path uses yesterday's vol30")
    h = yf.Ticker("^IXIC").history(period="3y")["Close"].dropna(); h.index = pd.to_datetime(h.index.tz_localize(None)).normalize()
    ixic, ixic_src = h[h.index < today], "UNVERIFIED (through prior session)"
if problems:
    print("DATA NOT READY:"); [print(" -", p) for p in problems]
    print("RESULT: STALE DATA - NO SIGNAL COMPUTED"); sys.exit(2)

# ---- unemployment (BLS = same series as FRED UNRATE) ----
try:
    js = requests.get("https://api.bls.gov/publicAPI/v2/timeseries/data/LNS14000000", timeout=30).json()
    pts = sorted(((int(x["year"]), int(x["period"][1:]), float(x["value"])) for x in js["Results"]["series"][0]["data"]
                  if x["period"].startswith("M") and x["period"] != "M13" and x["value"].replace(".", "", 1).isdigit()))
    d_un = pts[-1][2] - pts[-4][2]; un_txt = f"{d_un:+.2f}pp ({pts[-4][1]:02d}→{pts[-1][1]:02d}/{pts[-1][0]})"
except Exception as ex:
    d_un = None; un_txt = "UNAVAILABLE"; notes.append(f"Unemployment data unavailable ({ex}); defensive routing assumed SP500 — verify")
ur_thr = bot.ROTH_TOP7_PARAMS["ur_thr"]
top7_def = "TREASURIES" if (d_un is not None and d_un > ur_thr) else "SP500"

# ---- shared series (same construction as the bot) ----
sma = {n: spy.rolling(n).mean() for n in (50, 100, 300)}
vol30 = bot.compute_vol30(ixic).reindex(spy.index, method="ffill")
spy_v20 = bot.compute_vol20(spy)
q_sma150 = qqq.rolling(bot.QQQ_LEV_PARAMS["ma"]).mean(); q_v20 = bot.compute_vol20(qqq)
spy_sma_lev = spy.rolling(bot.SPY_LEV_ROTH_PARAMS["ma"]).mean()

trades = []
def tdate(x): return pd.Timestamp(x).date() if x is not None else None
def pct(a, b): return f"{a / b - 1:+.1%}"

# ---- Top-7 (D-asym) ----
ratchet = None; brok_watch = None
try:
    cfg, warn = bot.load_basket_config("top7_basket.json", today)
    if cfg:
        tick = [h["ticker"] for h in cfg["holdings"]]
        BASKET[:] = [t.upper() for t in tick]
        cols = {}
        for t in tick:  # same adjusted closes as bot.fetch_basket_closes, plus today's official close
            tk = yf.Ticker(t); hh = tk.history(period="1y", auto_adjust=True)["Close"].dropna()
            hh.index = pd.to_datetime(hh.index.tz_localize(None)).normalize()
            m = tk.history_metadata or {}; mt = m.get("regularMarketTime")
            if mt is not None and not isinstance(mt, (int, float)): mt = int(pd.Timestamp(mt).timestamp())
            if m.get("regularMarketPrice") and mt and dt.datetime.fromtimestamp(int(mt), et) >= close_t.to_pydatetime():
                hh.loc[today] = float(m["regularMarketPrice"])
            cols[t] = hh.sort_index()
        cl = pd.DataFrame(cols)
        stale_names = [t for t in tick if cl[t].dropna().index.max().date() != today.date()]
        nav = bot.compute_basket_nav(cfg, cl)
        ratchet = bot.walk_ratchet_state(nav, bot.TOP7_RATCHET_PARAMS["arm_gain"], bot.TOP7_RATCHET_PARAMS["stop_drop"])
        if hasattr(bot, "compute_big_year_watch"):  # brokerage big-year WATCH (alert only, added 2026-11-11)
            brok_watch = bot.compute_big_year_watch(nav, bot.BROK_BIG_YEAR_WATCH["watch_gain"])
        if stale_names: notes.append(f"Basket closes not yet updated for {stale_names} (ratchet NAV as of last bar)")
    else:
        notes.append(f"Ratchet: {warn}")
except Exception as ex:
    notes.append(f"Ratchet computation failed ({ex})")

CARDS = {"ROTH": [], "BROK": []}
def ok(b): return "✅" if b else "💥"
for key, acct, p in (("top7_roth", "ROTH", bot.ROTH_TOP7_PARAMS), ("top7_brok", "BROK", bot.BROK_TOP7_PARAMS)):
    label = f"Top-7 {acct}"
    st = bot.walk_dasym_state(spy, sma[p["ma_exit"]], sma[p["ma_re"]], vol30, p["vol_thr"], p["E_vol"], p["E_ma"], p["R_re"])
    locked = key == "top7_roth" and ratchet is not None and ratchet["fired"]
    state = "defensive" if locked else st["state"]
    pos = "Top-7 stocks" if state == "invested" else top7_def.title().replace("Sp500", "S&P 500")
    flip_today = (tdate(st["last_transition_date"]) == today.date() and not locked) or \
                 (locked and tdate(ratchet["fire_date"]) == today.date())
    if flip_today:
        frm = "S&P 500" if pos == "Top-7 stocks" else "Top-7 stocks"
        trades.append(("Top-7", acct, f"Sell {frm} → buy **{pos}** at the **next session's close** (MOC)"))
    if tdate(st["last_transition_date"]) == prev_sess.date() and not locked:
        notes.append(f"{label}: yesterday's flip should have executed at today's close — confirm the fill")
    s7, m7, r7 = spy.iloc[-1], sma[p["ma_exit"]].iloc[-1], sma[p["ma_re"]].iloc[-1]
    if locked:
        head = f"🔒 **{pos}** — ratchet lock until January rebalance"
        bl = [f"Ratchet fired {tdate(ratchet['fire_date']):%b %d}; no re-entry this year",
              f"Basket {ratchet['ytd']:+.0%} YTD, {ratchet['off_high']:+.0%} from its high"]
    elif state == "invested":
        head = f"✅ **{pos}** — invested"
        bl = [f"Vol exit: NASDAQ vol30 {vol30.iloc[-1]:.1%} vs {p['vol_thr']:.0%} limit · {st['vol_streak']}/{p['E_vol']} days",
              f"Trend exit: SPY {s7:.2f} vs SMA{p['ma_exit']} {m7:.2f} ({pct(s7, m7)}) · {st['ma_streak']}/{p['E_ma']} days below"]
        if key == "top7_roth" and ratchet is not None:
            bl.append(f"Ratchet {'ARMED' if ratchet['armed'] else 'not armed'}: basket {ratchet['ytd']:+.0%} YTD" +
                      (f", {ratchet['dist_to_stop']:+.1%} above stop" if ratchet["armed"] else f" (arms at +{bot.TOP7_RATCHET_PARAMS['arm_gain']:.0%})"))
    else:
        head = f"🛡️ **{pos}** — defensive"
        bl = [f"Re-entry: SPY {s7:.2f} vs SMA{p['ma_re']} {r7:.2f} ({pct(s7, r7)}) {ok(s7 >= r7)} · {st['reentry_streak']}/{p['R_re']} days"]
    if key == "top7_brok" and brok_watch is not None:
        rd = bot.BROK_BIG_YEAR_WATCH["review_drop"]; hit = brok_watch["off_high"] <= -rd
        bl.append(f"Big-year watch (alert only) {'⚠️' if hit else '👀'}: basket {brok_watch['ytd']:+.0%} YTD, peak {brok_watch['peak_ytd']:+.0%} "
                  f"on {brok_watch['peak_date']:%b %-d}, {brok_watch['off_high']:+.0%} off high" +
                  (f" — **down {rd:.0%}+ from the high: review a manual sale only with extreme confidence** (cyclical baskets)" if hit
                   else f" · review at -{rd:.0%} from high; no automatic sell"))
    CARDS[acct].append(("Top-7", head, bl))

# ---- Leveraged sleeves ----
def lev(key, acct, p, price, ma_s, v20):
    st = bot.walk_lev_state(price, ma_s, v20, p["vol_thr"], p["exit_lag"], p["entry_lag"])
    L_, D_ = p["leveraged"], p["defensive"]
    pos = L_ if st["state"] == "invested" else D_
    t = TIMING.get(key, {"exit": "next_close", "entry": "next_close"})
    ltd = tdate(st["last_transition_date"])
    direction = "entry" if st["state"] == "invested" else "exit"
    sig = p["signal_asset"]; ma = p["ma"]; vt = p["vol_thr"]
    if sig in APPROX and (abs(price.iloc[-1] / ma_s.iloc[-1] - 1) < 0.01 or abs(v20.iloc[-1] - vt) < 0.01):
        problems.append(f"{L_} {acct}: TOO CLOSE TO CALL on unadjusted fallback history — check manually")
    frm = D_ if direction == "entry" else L_
    if ltd == today.date():
        when = ("**tonight**, minutes after the close (after-hours limit order)" if t[direction] == "same_day"
                else "at **tomorrow's close** (MOC — submit before 3:50 PM ET)")
        trades.append((L_, acct, f"Sell {frm} → buy **{pos}** {when}"))
        head = (f"⏳ **{frm} → {pos}** tomorrow at the close" if t[direction] == "next_close"
                else f"🔄 **{frm} → {pos}** tonight")
    elif ltd == prev_sess.date() and t[direction] == "next_close":
        notes.append(f"{L_} {acct}: yesterday's switch to {pos} should have filled at today's close — confirm")
        head = f"{'✅' if pos == L_ else '🛡️'} **{pos}** (switched at today's close)"
    else:
        head = f"✅ **{L_}** — invested" if pos == L_ else f"🛡️ **{D_}** — out of {L_}"
    px, m, v = price.iloc[-1], ma_s.iloc[-1], v20.iloc[-1]
    bl = [f"Trend: {sig} {px:.2f} vs SMA{ma} {m:.2f} ({pct(px, m)}) {ok(px > m)}",
          f"Volatility: {v:.1%} vs {vt:.0%} limit {ok(v < vt)}"]
    if st["state"] == "invested":
        bl.append(f"Exits after {p['exit_lag']} failing day(s) · now {st['days_off_streak']}/{p['exit_lag']}")
    else:
        bl.append(f"Re-enters when both ✅ for {p['entry_lag']} day · now {st['days_on_streak']}/{p['entry_lag']}")
    tl = {"same_day": "same day", "next_close": "next-day close"}
    bl.append(f"Timing: exit {tl[t['exit']]}, re-entry {tl[t['entry']]}")
    CARDS[acct].append((L_, head, bl))

lev("spy_lev_roth", "ROTH", bot.SPY_LEV_ROTH_PARAMS, spy, spy_sma_lev, spy_v20)
lev("qqq_lev_roth", "ROTH", bot.QQQ_LEV_PARAMS, qqq, q_sma150, q_v20)
lev("spy_lev_brok", "BROK", bot.SPY_LEV_BROK_PARAMS, spy, spy_sma_lev, spy_v20)
lev("qqq_lev_brok", "BROK", bot.QQQ_LEV_PARAMS, qqq, q_sma150, q_v20)

if problems:
    print("DATA NOT READY:"); [print(" -", p) for p in problems]
    print("RESULT: STALE DATA - NO SIGNAL COMPUTED"); sys.exit(2)
msg = [f"## 📊 Portfolio Monitor — {today.date():%a %b %-d}", ""]
for i, (name, acct, txt) in enumerate(trades):
    bought = re.search(r"buy \*\*([^*]+)\*\*", txt)
    ws = wash_any(bought.group(1).strip()) if bought else []
    if ws: trades[i] = (name, acct, txt + " ⚠️ **WASH SALE**: " + "; ".join(f"{w[0]} loss sold {w[1]:%b %-d}, safe from {w[1] + dt.timedelta(days=31):%b %-d}" for w in ws))
if trades:
    msg += ["### 🔴 Trades needed", ""]
    merged = {}
    for name, acct, txt in trades: merged.setdefault((name, txt), []).append(acct)
    for (name, txt), accts in merged.items():
        who = "Roth + Brokerage" if len(accts) == 2 else ("Roth" if accts == ["ROTH"] else "Brokerage")
        msg.append(f"- **{name} ({who}):** {txt}")
else:
    msg += ["### ✅ No trades today"]
open_w = [wash(s) for s in sorted({x[0] for x in LOSSES})]
open_w = [x for x in open_w if x]
if open_w:
    msg += ["", "### 🧾 Wash-sale watch"] + [f"- {wash_line(x)}" for x in open_w]
if notes:
    msg += ["", "### ⚠️ Check"] + [f"- {n}" for n in notes]
for acct, title in (("ROTH", "🏦 Roth IRA"), ("BROK", "💼 Brokerage")):
    msg += ["", "---", "", f"### {title}"]
    for name, head, bl in CARDS[acct]:
        msg += ["", f"**{name}** · {head}"] + [f"- {x}" for x in bl]
msg += ["", "---", "",
        f"*Closes: SPY {spy.iloc[-1]:.2f}, QQQ {qqq.iloc[-1]:.2f} ({spy_src.lower()}) · NASDAQ {ixic.iloc[-1]:,.0f} ({ixic_src.lower()}) · "
        f"unemployment 3-mo change {un_txt} → Top-7 defensive = {top7_def.replace('SP500', 'S&P 500').title()}*"]
moc = [l for l in msg if "(MOC" in l]
if moc:  # next-close trades: the scheduled task books a one-shot midday reminder for the next session
    nxt = cal.next_session(today); cut = (cal.session_close(nxt).tz_convert(et) - pd.Timedelta(10, unit="m"))
    rem = min(cut - pd.Timedelta(90, unit="m"), pd.Timestamp(nxt.date()).tz_localize(et) + pd.Timedelta(hours=12, minutes=28))
    print(f"MOC_REMINDER_AT: {rem:%Y-%m-%dT%H:%M:00}[America/New_York]")
    print("MOC_REMINDER_TEXT START")
    print(f"## ⏰ MOC order due today ({nxt.date():%a %b %-d})\n\nSubmit **market-on-close** before **{cut:%-I:%M %p} ET** (from yesterday's signal):\n")
    print("\n".join(moc)); print("MOC_REMINDER_TEXT END")
print("MESSAGE START"); print("\n".join(msg)); print("MESSAGE END")
