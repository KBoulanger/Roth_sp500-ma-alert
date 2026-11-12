# Scheduled-task scripts (Claude scheduled tasks)

- `monitor.py` — post-close portfolio monitor (4:03 PM ET, weekdays).
- `moc_reminder.py` — midday market-on-close reminder (12:28 PM ET, weekdays).

Both import the live rules from `send_spy_ma_telegram.py` on `main`, verify today's close
against Robinhood, and include a **wash-sale watch**: the scheduled task passes every
stock/ETF loss sale from Kyle's taxable Robinhood accounts as
`LOSS=<SYM>@<ISO timestamp>@<realized gain>@<account>`. For 30 days after a loss sale the
report warns against re-buying that ticker in ANY account (Roth/IRA buy = loss permanently
disallowed, Rev. Rul. 2008-5; taxable buy = loss deferred into the new basis), and flags any
signalled re-entry that would trigger it.

The scheduled-task prompts pin each file's SHA-256; update the prompts after editing these files.

Midday reminders: the recurring 12:28 PM task is disabled (Kyle only wants a midday push when a
trade is due). When the post-close monitor reports a next-close (MOC) trade, it prints
`MOC_REMINDER_AT` / `MOC_REMINDER_TEXT`, and the scheduled task books a one-shot reminder for the
next session. `moc_reminder.py` is kept for manual use.

Wash-sale stand-in rule (2026-11-11): if a leveraged re-entry would buy TQQQ or UPRO inside an open
wash-sale window (Kyle's or his wife's taxable loss sale in the last 30 days — joint filers), the
monitor tells you to buy the 2x fund on the same index instead (QLD for TQQQ, SSO for UPRO) with the
same dollars, rather than waiting in cash. On the first session after the window passes (day 31) it
prints a swap trade: sell the 2x stand-in, buy the 3x fund. Exits during the window say to sell the
stand-in if that is what you hold. The wife's loss sales are passed as fixed `LOSS=` arguments by the
scheduled task (her account is checked manually via Chrome).
