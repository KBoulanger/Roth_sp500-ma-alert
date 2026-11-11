"""Midday reminder: are any market-on-close (MOC) orders due at TODAY's close?
They come from flips signalled at the PREVIOUS session's close whose execution is 'next close'.
Rules/params come from the live Telegram bot code on GitHub."""
import re, sys, importlib.util, datetime as dt
import pandas as pd, pytz, yfinance as yf, requests
import exchange_calendars as xc

RAW = "https://raw.githubusercontent.com/KBoulanger/Roth_sp500-ma-alert/main/"
et = pytz.timezone("America/New_York"); now = dt.datetime.now(et)
cal = xc.get_calendar("XNYS"); today = pd.Timestamp(now.date())
if not cal.is_session(today):
    print("MARKET CLOSED"); sys.exit(0)
prev = cal.previous_session(today); prev_d = prev.date()
close_t = cal.session_close(today).tz_convert(et)
cutoff = (close_t - pd.Timedelta(10, unit="m")).strftime("%-I:%M %p")

src = requests.get(RAW + "send_spy_ma_telegram.py", timeout=30).text
open("bot_live.py", "w").write(src)
spec = importlib.util.spec_from_file_location("bot_live", "bot_live.py"); bot = importlib.util.module_from_spec(spec); spec.loader.exec_module(bot)
open("top7_basket.json", "w").write(requests.get(RAW + "top7_basket.json", timeout=30).text)
TIMING = getattr(bot, "EXECUTION_TIMING", {
    "spy_lev_roth": {"exit": "next_close", "entry": "same_day"}, "spy_lev_brok": {"exit": "next_close", "entry": "same_day"},
    "qqq_lev_roth": {"exit": "same_day", "entry": "same_day"}, "qqq_lev_brok": {"exit": "same_day", "entry": "same_day"}})

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
notes = []
def hist(sym, required=True):
    c = yf.Ticker(sym).history(period="3y", auto_adjust=True)["Close"].dropna()
    c.index = pd.to_datetime(c.index.tz_localize(None)).normalize()
    c = c[c.index <= prev]  # only completed sessions through yesterday
    if c.index[-1].date() != prev_d:
        r = RH.get(sym)
        if r and r[1] == prev_d and c.index[-1] == cal.previous_session(prev):
            c.loc[prev] = r[0]; c = c.sort_index()   # Yahoo daily bar missing: use Robinhood's official close
        elif not required:
            notes.append(f"{sym} close for {prev_d} unavailable; Top-7 vol check uses the day before")
        else:
            print(f"DATA NOT READY: {sym} last close {c.index[-1].date()}, expected {prev_d}"); sys.exit(2)
    return c

spy, qqq, ixic = hist("SPY"), hist("QQQ"), hist("^IXIC", required=False)
td = lambda x: pd.Timestamp(x).date() if x is not None else None
due = []

for key, acct, p, px, n in (("spy_lev_roth", "Roth", bot.SPY_LEV_ROTH_PARAMS, spy, None),
                            ("spy_lev_brok", "Brokerage", bot.SPY_LEV_BROK_PARAMS, spy, None),
                            ("qqq_lev_roth", "Roth", bot.QQQ_LEV_PARAMS, qqq, None),
                            ("qqq_lev_brok", "Brokerage", bot.QQQ_LEV_PARAMS, qqq, None)):
    st = bot.walk_lev_state(px, px.rolling(p["ma"]).mean(), bot.compute_vol20(px), p["vol_thr"], p["exit_lag"], p["entry_lag"])
    if td(st["last_transition_date"]) == prev_d:
        direction = "entry" if st["state"] == "invested" else "exit"
        if TIMING.get(key, {}).get(direction, "next_close") == "next_close":
            frm, to = (p["defensive"], p["leveraged"]) if direction == "entry" else (p["leveraged"], p["defensive"])
            due.append((acct, f"Sell **{frm}** → buy **{to}**"))

# Top-7 flips (always next-session MOC)
sma = {n: spy.rolling(n).mean() for n in (100, 300)}
vol30 = bot.compute_vol30(ixic).reindex(spy.index, method="ffill")
for acct, p in (("Roth", bot.ROTH_TOP7_PARAMS), ("Brokerage", bot.BROK_TOP7_PARAMS)):
    st = bot.walk_dasym_state(spy, sma[p["ma_exit"]], sma[p["ma_re"]], vol30, p["vol_thr"], p["E_vol"], p["E_ma"], p["R_re"])
    if td(st["last_transition_date"]) == prev_d:
        due.append((acct, "Top-7 flip: " + ("sell defensive → buy **Top-7 basket**" if st["state"] == "invested" else "sell **Top-7 basket** → buy defensive")))
try:  # Roth runup ratchet firing yesterday
    cfg, _ = bot.load_basket_config("top7_basket.json", today)
    if cfg:
        BASKET[:] = [h["ticker"].upper() for h in cfg["holdings"]]
        cl = bot.fetch_basket_closes([h["ticker"] for h in cfg["holdings"]])
        cl.index = pd.to_datetime(cl.index).tz_localize(None).normalize() if getattr(cl.index, "tz", None) else pd.to_datetime(cl.index).normalize()
        r = bot.walk_ratchet_state(bot.compute_basket_nav(cfg, cl[cl.index <= prev]), bot.TOP7_RATCHET_PARAMS["arm_gain"], bot.TOP7_RATCHET_PARAMS["stop_drop"])
        if r["fired"] and td(r["fire_date"]) == prev_d:
            due.append(("Roth", "Top-7 ratchet fired: sell **Top-7 basket** → buy defensive"))
except Exception as ex:
    print(f"NOTE: ratchet check skipped ({ex})")

print("MESSAGE START")
if due:
    merged = {}
    for acct, txt in due: merged.setdefault(txt, []).append(acct)
    print(f"## ⏰ MOC orders due today\n\nSubmit **market-on-close** orders before **{cutoff} ET** (from yesterday's signal):\n")
    for txt, accts in merged.items():
        b = re.search(r"buy \*\*([^*]+)\*\*", txt); ws = wash_any(b.group(1).strip()) if b else []
        print(f"- {txt} ({' + '.join(accts)})" + ((" ⚠️ **WASH SALE**: " + "; ".join(f"{w[0]} loss sold {w[1]:%b %-d}, safe from {w[1] + dt.timedelta(days=31):%b %-d}" for w in ws)) if ws else ""))
else:
    print(f"✅ No market-on-close orders due today ({today.date():%a %b %-d}).")
open_w = [x for x in (wash(s) for s in sorted({x[0] for x in LOSSES})) if x]
if open_w: print("\n🧾 **Wash-sale watch**\n" + "\n".join(f"- {wash_line(x)}" for x in open_w))
for n in notes: print(f"\n⚠️ {n}")
print("MESSAGE END")
