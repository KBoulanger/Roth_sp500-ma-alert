# Changes

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

### Scheduled monitor branch

- Replaced duplicated/floating downloaded rules with a commit- and SHA256-pinned
  copy of the shared main engine. Current confirmed facts/preferences are data.
- Post-close reports wait two minutes after the XNYS close; midday reads the prior
  completed close and respects the session-specific MOC cutoff. Both support
  read-only seed/export/LOSS inputs and explicit offline review fixtures.
- No delivery or state persistence from either wrapper. Structured next-close
  plans alone emit reminder text; no automatic scheduling or order actions.
- Synchronized the main branch's pre-existing basket-fetch retry and scheduler
  tests before adding shared policy tests and monitor contract checks.

### Claude review 1 — unemployment availability

- Prefer a verified snapshot, then FRED initial releases, then the original
  revised latest API / fredgraph CSV route. A key/snapshot is optional; only
  failure of all sources blocks the report.
- Print the exact first-release or revised-figures provenance line in every
  routed report and midday reminder, including each Telegram message chunk.
- No policy, signal parameters, Top-7 basket or golden accounting changes.
