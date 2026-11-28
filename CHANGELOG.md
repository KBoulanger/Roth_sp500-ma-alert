# Changes

## 2026-11-27 — Report survives a basket data gap; SNDK split baseline (no rule change)

- Cause of the nightly failures since the 20 Nov close: SNDK split 8-for-1 on 2026-11-20 and
  Yahoo's SNDK daily history has had blank rows since. The bot refused to report without
  every basket close.
- `top7_basket.json`: SNDK `baseline_close` 275.24 -> 34.405 (same price, split-adjusted).
- New `split_adjusted_basket_config`: if a baseline differs from split-adjusted history by a
  whole-number split factor it is rescaled with a note; any other large mismatch is an error.
- Basket gap handling: on the final (or a manual) attempt, if the ratchet has ALREADY fired
  on the complete history, the report is sent with a note. The ratchet never un-fires within
  a year, so later missing prices cannot change the Roth signal. If the ratchet has not
  fired, the bot still refuses, as before. Earlier attempts still wait for the data.
- Walkers, ratchet, NAV formula and all parameters are untouched (hash test unchanged).

## 2026-11-27 — Short daily message with early warnings (display only)

- Telegram now sends a short message: trades first (one line per account, with the
  dividend-reinvestment reminder under any leveraged Roth buy), then `WATCH` lines when a
  level is close, then nothing else. A quiet day is one line.
- New `short_report.py` holds the wording and the "how close is close" levels (3% from a
  moving average, 3 volatility points, 5% from the dip trigger, 5 points from the Roth
  cushion, 5 days from a Top-7 day-count, 3 of 5 re-entry days, 3 days to a wash-sale
  window end). It reads values the strategy code already computed.
- No signal, rule, parameter or holding changed. The full report is still built, returned
  as `text`, and printed to the run log; the short one is returned as `short_text`.

## 2026-11-27 — two-account leveraged rules (local review candidate)

- Top-7's rising-unemployment defensive fund is IEF; non-rising remains S&P 500.
- Leveraged defensive routing is IEF/USFR using first-release UNRATE and a rounded
  tenths comparison. Unadjusted price fallbacks fail closed.
- TQQQ Roth exits after one failing close; brokerage after four. Independent -20%
  dip entries re-arm only near a new all-time adjusted QQQ high.
- Brokerage uses 3x only. Roth uses QLD/SSO until the 30%/10% adjusted-tax-cost
  cushion and loss window clear. TQQQ has the 5% floor and entry lock; UPRO only
  the entry lock. Calendar windows are inclusive through day 30.
- Replaced the November 11 blanket 2x substitution/day-31 swap with this joint
  policy. Household wash monitoring remains; a 3x retirement buy is suppressed
  during a supplied household taxable-loss window.
- Model assumptions and dated observed facts are separate. No model fill becomes
  a confirmed holding. The checked-in seed can be overridden by a read-only
  spouse export, explicit tax cost, or updated observed-state file.
- Dividend reinvestment is OFF in both Roth IRAs per the owner. Future Roth cash
  distributions are not modelled as purchases; historical and unexpectedly
  observed real DRIPs still count. Every leveraged Roth buy/swap has the owner's
  exact reminder; holds, exits and brokerage buys do not.
- SPY/QQQ calculations use Adj Close; regular-price checks compare RawClose and
  preserve adjusted rows. QQQ history is full-history for its all-time high.
- Added complete W/X golden ledgers, rule/adapter/rendering tests and a bounded
  multi-part Telegram renderer. The existing scheduler safety gates are retained.

This code only computes and reports. It has no broker order API. Research has
historical overfitting and simplified tax accounting; TQQQ's deep drawdowns and
UPRO's remaining wash clashes are not removed by this change. No push/deployment
is part of this local review candidate.

### Owner addendum 2 — Top-7 message-only wash warnings

- Check each constituent on a basket BUY instruction (and an explicitly supplied
  January plan). Roth/IRA loss-window warnings are distinct from an identified
  taxable account's own deferral note. January sales also check prior Roth buys.
- Optional `state.json` `substitutes` contains only owner choices; default is empty.
  No automatic stock substitution, signal, weight or holdings-logic change occurs.

### Claude review 1 — unemployment availability

- Prefer a verified snapshot, then FRED initial releases, then the original
  revised latest API / fredgraph CSV route. A key/snapshot is optional; only
  failure of all sources blocks the report.
- Print the exact first-release or revised-figures provenance line in every
  routed report and midday reminder, including each Telegram message chunk.
- No policy, signal parameters, Top-7 basket or golden accounting changes.
