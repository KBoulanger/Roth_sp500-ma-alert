# ============================
# ADD: MA250_E80_R5 (SP500 holdings) COMPUTATION
# Place this right after your existing SMA50/streak/exit/reentry computation
# (i.e., after you have `df`, `latest`, `latest_date`, `latest_close`, and the MA50 vars)
# ============================

df["SMA250"] = df["Close"].rolling(250).mean()

latest = df.iloc[-1]
prev = df.iloc[:-1]

above_250 = bool(latest["Close"] > latest["SMA250"])
below_250 = bool(latest["Close"] < latest["SMA250"])

def count_streak(series: pd.Series) -> int:
    c = 0
    for v in reversed(series.to_list()):
        if bool(v):
            c += 1
        else:
            break
    return c

above_streak_250 = count_streak(prev["Close"] > prev["SMA250"])
below_streak_250 = count_streak(prev["Close"] < prev["SMA250"])

exit_250 = below_streak_250 >= 80
reentry_250 = above_streak_250 >= 5


# ============================
# REPLACE: your existing message-building block (where you do `lines = []` etc.)
# with the block below. This keeps your existing Top-K / MA50 / UNRATE fields,
# but formats the message exactly as requested and adds the MA250_E80_R5 section.
# ============================

lines = []

lines.append(f"<b>{ACCOUNT_LABEL} ACCOUNT</b>")
lines.append("")
lines.append(f"SPY Price as-of (trading day): {latest_date}")
lines.append(f"Close: {latest_close:.2f}")
lines.append("")

# ----------------------------
# MA50_E20_R5 (TopK overlay)
# Assumes you already have:
#   above_50, below_50, above_streak_50, below_streak_50, exit_50, reentry_50
# and UNRATE values:
#   un_now_date, un_now, un_prior_date, un_prior, un_chg, un_flag
# ----------------------------
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
lines.append(f"UNRATE as-of date: {un_now_date} → {un_now:.1f}%")
lines.append(f"UNRATE 3-mo prior: {un_prior_date} → {un_prior:.1f}%")
lines.append(f"UNRATE 3-mo change: {un_chg:.2f} pp")
lines.append(f"UNRATE rising flag (>0.3): <b>{'ON' if un_flag else 'OFF'}</b>")
lines.append("")

# [ADD INSTRUCTION HERE 1] — TopK instructions
# Example requested: if below MA50 for >=20 days evaluate UNRATE; if UNRATE triggered -> treasuries else SP500.
if exit_50:
    if un_flag:
        instr1 = "TopK: Exit equities → move to Treasuries"
    else:
        instr1 = "TopK: Exit equities → move to SP500"
else:
    instr1 = "TopK: Stay invested in TopK equities"

lines.append("<b>What to do today (TopK):</b>")
lines.append(instr1)
lines.append("")
lines.append("")

# ----------------------------
# MA250_E80_R5 (SP500 holdings)
# ----------------------------
lines.append("<b>MA250_E80_R5:</b>")
lines.append(f"SMA250: {latest['SMA250']:.2f}")
lines.append(f"SMA250 Status: <b>{'ABOVE' if above_250 else 'BELOW'}</b>")
lines.append(f"SMA250 Above streak: {above_streak_250}")
lines.append(f"SMA250 Below streak: {below_streak_250}")
lines.append("")
lines.append(f"Exit trigger (≥80 below): {'YES' if exit_250 else 'NO'}")
lines.append(f"Reentry trigger (≥5 above): {'YES' if reentry_250 else 'NO'}")
lines.append("")

# [ADD INSTRUCTION HERE 2] — SP500 instructions
# Example requested: if below MA250 for >=80 days sell SP500 and go to treasuries; reenter on >=5 above.
if exit_250:
    instr2 = "SP500: Sell SP500 → move to Treasuries"
elif reentry_250:
    instr2 = "SP500: Buy/hold SP500 (reentry condition met)"
else:
    instr2 = "SP500: No action"

lines.append("<b>What to do today (SP500):</b>")
lines.append(instr2)

# Then keep your existing send_telegram call:
# send_telegram(bot_token, chat_id, "\n".join(lines))
