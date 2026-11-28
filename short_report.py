"""Short daily message: trades first, then early warnings, nothing else.

Text only. This module reads values the strategy code already computed and
never changes a signal, a holding or a rule. Thresholds below decide only when
a "WATCH" line is shown.
"""
from datetime import date as _date, timedelta
from html import escape

ROTH_BUY_REMINDER = ("Reminder: keep dividend reinvestment OFF in both Roth IRAs "
                     "(yours and Katelyn's). Check it is still off before you buy.")

# How close counts as "close" (owner-set 27 Nov 2026; display only).
NEAR_MA = 0.03          # price within 3% of its moving average
NEAR_VOL_PTS = 3.0      # volatility within 3 points of the limit
NEAR_DIP = 0.05         # index within 5% above the dip-buy trigger
NEAR_CUSHION = 0.05     # brokerage within 5 points of the Roth cushion
NEAR_TOP7_DAYS = 5      # Top-7 day-count within 5 days of firing
NEAR_REENTRY_DAYS = 3   # 3 of the 5 re-entry days done
NEAR_RATCHET = 0.05     # basket within 5 points of arming / 5% of the stop
NEAR_WASH_DAYS = 3      # wash-sale window ends within 3 days

LEVERAGED = {'TQQQ': ('TQQQ', 'QLD'), 'UPRO': ('UPRO', 'SSO')}
CUSHION = {'TQQQ': 0.30, 'UPRO': 0.10}
DIP_DEPTH = 0.20
FUND_NAMES = {'BILL': 'USFR', 'T': None, 'Q': None}


def _d(value):
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(str(value)[:10])


def _day(value):
    d = _d(value)
    return f"{d.day} {d.strftime('%b')}"


def _pct(x, digits=1):
    return f"{abs(x) * 100:.{digits}f}%"


def _lev_trades(sleeve, result, moc_accounts):
    """Same-day instructions and standing mismatches for one leveraged plan."""
    trades, checks = [], []
    traded_today = {e.get('account') for e in result.get('events', [])}
    observed = result.get('observed') or {}
    for code, key, label in (('B', 'brokerage', 'Brokerage'), ('R', 'roth', 'Roth')):
        target = result['model_accounts'][code]['holding']
        actual = (observed.get(key) or {}).get('holding')
        if (sleeve, label) in moc_accounts:
            continue  # shown once, as a next-close order
        if actual is None:
            if code in traded_today:
                checks.append(f"{label} ({sleeve} plan): plan moved to {target} today. "
                              f"Holding on record is unknown; check the account.")
            continue
        if actual == target:
            continue
        line = f"{label} ({sleeve} plan): sell {actual}, buy {target}"
        if code in traded_today:
            trades.append(line + " — right after the close")
            if code == 'R' and target in LEVERAGED[sleeve]:
                trades.append(ROTH_BUY_REMINDER)
            if code == 'R' and target == LEVERAGED[sleeve][1] and result['model_accounts'][code].get('household_purchase_blocked'):
                trades.append(f"({target} instead of {sleeve}: a taxable {sleeve} loss sale is inside its 30-day window.)")
        else:
            checks.append(f"{label} ({sleeve} plan): record shows {actual}, plan says {target}. "
                          f"Ignore if you already traded.")
    return trades, checks


def _lev_watch(sleeve, result, m):
    """Early warnings for one leveraged plan. m: price, ma, ma_days, vol, cap, index."""
    out = []
    if not m or m.get('price') is None or m.get('ma') is None or m.get('vol') is None:
        return out
    three, two = LEVERAGED[sleeve]
    models = result['model_accounts']
    holding = {c: models[c]['holding'] for c in ('R', 'B')}
    invested = any(h in (three, two) for h in holding.values())
    idx = m['index']; gap = m['price'] / m['ma'] - 1.0
    vol_pts = (m['vol'] - m['cap']) * 100.0
    avg = f"{m['ma_days']}-day average"
    if invested:
        bits = []
        if 0 <= gap <= NEAR_MA:
            bits.append(f"{idx} is only {_pct(gap)} above its {avg} (exit on a close below {m['ma']:.2f})")
        elif gap < 0:
            bits.append(f"{idx} closed {_pct(gap)} below its {avg}")
        if vol_pts >= -NEAR_VOL_PTS:
            bits.append(f"volatility {m['vol'] * 100:.1f}% vs the {m['cap'] * 100:.0f}% limit")
        if bits:
            out.append(f"{sleeve} could exit soon: " + "; ".join(bits) + ".")
        meta_b = (result.get('signal_meta') or {}).get('brokerage') or {}
        fails = int(meta_b.get('failing_closes') or 0)
        if sleeve == 'TQQQ' and holding['B'] == three and 0 < fails < 4:
            out.append(f"Brokerage TQQQ: failing close {fails} of 4. It sells on the 4th.")
    else:
        if gap >= -NEAR_MA and vol_pts <= NEAR_VOL_PTS:
            price_bit = (f"{idx} is above its {avg}" if gap > 0 else
                         f"{idx} is {_pct(gap)} below its {avg} (needs a close above {m['ma']:.2f})")
            vol_bit = ("volatility is under the limit" if vol_pts < 0 else
                       f"volatility {m['vol'] * 100:.1f}% must fall below {m['cap'] * 100:.0f}%")
            out.append(f"{sleeve} could re-enter soon: {price_bit}; {vol_bit}.")
        if sleeve == 'TQQQ':
            meta = (result.get('signal_meta') or {}).get('brokerage') or {}
            dd = meta.get('drawdown')
            if meta.get('armed') and not meta.get('active') and dd is not None:
                depth = abs(float(dd))
                if DIP_DEPTH - NEAR_DIP <= depth < DIP_DEPTH and depth < 1:
                    trigger = m['price'] / (1.0 - depth) * (1.0 - DIP_DEPTH)
                    out.append(f"Dip buy is close: {idx} is {_pct(depth)} below its high; "
                               f"it buys at 20% below (a close at or under {trigger:.2f}).")
    if holding['R'] == two and holding['B'] == three and not result.get('basis_unknown'):
        b = models['B']
        if b.get('tax_basis') and b['tax_basis'] > 0:
            gain = b['wealth'] / b['tax_basis'] - 1.0
            need = CUSHION[sleeve]
            if need - NEAR_CUSHION <= gain < need:
                out.append(f"Roth could move from {two} to {three} soon: brokerage is up "
                           f"{gain * 100:.0f}% (needs {need * 100:.0f}%).")
    return out


def _top7_watch(entry, ratchet, arm_gain):
    out = []
    label = entry['label']
    if entry.get('guarded'):
        return out
    if entry.get('invested'):
        words = {'MA': 'days below the 300-day average', 'vol': 'high-volatility days'}
        for name, streak, threshold in entry.get('paths', []):
            if streak and threshold - streak <= NEAR_TOP7_DAYS and streak < threshold:
                out.append(f"{label} could exit soon: {streak} of {threshold} {words.get(name, name + ' days')}.")
        if ratchet and entry.get('has_ratchet') and not ratchet.get('fired'):
            if ratchet.get('armed') and ratchet.get('dist_to_stop') is not None and ratchet['dist_to_stop'] <= NEAR_RATCHET:
                out.append(f"{label} ratchet is close: basket is {_pct(ratchet['dist_to_stop'])} above its sell level.")
            elif not ratchet.get('armed') and ratchet.get('ytd') is not None and 0 <= arm_gain - ratchet['ytd'] <= NEAR_RATCHET:
                out.append(f"{label} ratchet is about to switch on: basket is +{ratchet['ytd'] * 100:.0f}% "
                           f"this year (switches on at +{arm_gain * 100:.0f}%).")
    elif not entry.get('locked'):
        streak, threshold = entry.get('reentry_streak') or 0, entry.get('reentry_threshold') or 5
        if NEAR_REENTRY_DAYS <= streak < threshold:
            out.append(f"{label} could re-enter soon: {streak} of {threshold} days above the 100-day average.")
    return out


def _wash_watch(losses, today):
    last = {}
    for item in losses or []:
        try:
            d = _d(item['date'])
        except Exception:
            continue
        last[item['symbol']] = max(last.get(item['symbol'], d), d)
    out = []
    for symbol, d in sorted(last.items()):
        left = (d + timedelta(days=30) - today).days
        if 0 <= left <= NEAR_WASH_DAYS:
            out.append(f"Wash-sale window on {symbol} ends soon: a Roth/IRA can buy it again from {_day(d + timedelta(days=31))}.")
    return out


def build_short_report(ctx):
    """ctx keys: date, joint, joint_error, lev_metrics, top7, ratchet, arm_gain,
    moc_orders, top7_warnings, losses, unemployment_revised, missing, extra_notes."""
    today = _d(ctx['date'])
    trades, checks, watch, notes = [], [], [], []
    moc_orders = ctx.get('moc_orders') or []
    moc_accounts = set()
    for order in moc_orders:
        if order.get('sleeve') in LEVERAGED:
            moc_accounts.add((order['sleeve'], 'Roth' if 'roth' in order['account'].lower() else 'Brokerage'))
    for sleeve in ('TQQQ', 'UPRO'):
        result = (ctx.get('joint') or {}).get(sleeve)
        if result is None:
            notes.append(f"⚠️ {sleeve} plan not checked today (data missing).")
            continue
        t, c = _lev_trades(sleeve, result, moc_accounts)
        trades += t; checks += c
        watch += _lev_watch(sleeve, result, (ctx.get('lev_metrics') or {}).get(sleeve))
    for order in moc_orders:
        if order.get('sleeve') not in LEVERAGED:
            continue  # Top-7 orders come from the pending-order record below
        who = ('Roth' if 'roth' in order['account'].lower() else 'Brokerage') + f" ({order['sleeve']} plan)"
        trades.append(f"{who}: sell {order['sell']}, buy {order['buy']} — at the NEXT close (market-on-close order)")
    for entry in ctx.get('top7') or []:
        pending = entry.get('pending')
        if entry.get('guarded') or not pending:
            continue
        trades.append(f"{entry['label']}: sell {pending['sell']}, buy {pending['buy']} — at the NEXT close (market-on-close order)")
        if pending.get('buy') == 'TOP-7 STOCKS':
            trades += [x for x in (ctx.get('top7_warnings') or {}).get(entry['label'], []) if x]
    for entry in ctx.get('top7') or []:
        watch += _top7_watch(entry, ctx.get('ratchet'), ctx.get('arm_gain') or 0.60)
    if any(e.get('guarded') for e in ctx.get('top7') or []):
        notes.append("Top-7 not checked yet (final Nasdaq data arrives after 5:15 PM).")
    watch += _wash_watch(ctx.get('losses'), today)
    if ctx.get('joint_error'):
        notes.append("⚠️ Leveraged plans not checked: " + str(ctx['joint_error']))
    for name in ctx.get('missing') or []:
        notes.append(f"⚠️ {name} data missing today.")
    notes += list(ctx.get('extra_notes') or [])
    defensive_move = any((' buy IEF' in x or ' buy USFR' in x or ' buy SP500' in x) for x in trades)
    if ctx.get('unemployment_revised') and defensive_move:
        notes.append("Note: the IEF-or-not choice used revised unemployment figures (first-release unavailable).")

    head = _day(today) + ": "
    if trades:
        lines = [head + "TRADE TODAY"] + trades
    elif watch or checks:
        lines = [head + "NO TRADES."]
    else:
        lines = [head + "NO TRADES. Nothing close."]
    lines += ["⚠️ WATCH: " + x for x in watch]
    lines += ["⚠️ CHECK: " + x for x in checks]
    lines += notes
    return "\n".join(escape(x, quote=False) for x in lines)
