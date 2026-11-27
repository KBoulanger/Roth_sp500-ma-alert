# Joint policy and observed facts

`joint_strategy.py` is the deterministic W/X accounting replay and decision logic.
`joint_runtime.py` adapts read-only facts and renders the same instructions for
Telegram and both monitor entry points. The family account identifiers in the
seed are last-four only. Normalized model balances are not actual account values.

The seed is a dated observation, not a promise that subsequent modeled trades
filled. Edit `joint_state_seed.json` with a new `as_of` when supplying corrections,
including per-sleeve `dip_armed` (boolean or `roth`/`brokerage` mapping). Model state
is stored under `_joint`; confirmed observations are cached separately from model
fills. A missing export retains the dated fixed facts. Never infer a fill from a
calendar date or turn a model target into an observed holding.

Optional `SPOUSE_RH_EXPORT` / `--facts` reads the spouse export described by the
handoff. `positions`, `open_lots`, `transactions` and `loss_sales` are read-only.
Explicit `strategy_facts` may supply per-sleeve observations directly. A tax lot's
`tax_cost_basis` is total adjusted USD, divided by matched quantity; alternatively
use explicit `tax_cost_per_share`. No average fill price is substituted for tax
cost. Ambiguous quantities/basis remain unknown. Core model anchors use one
modeled share, not the actual account size. If pre-seed deferred loss amounts are
unknown, the retirement cushion is not confirmed until adjusted tax-lot cost is
provided. Brokerage rebuys are still permitted under the entry-lock rule.

`LOSS=SYM@timestamp@gain@account` remains a facts input for compatibility; it no
longer activates blanket substitutions. Only negative taxable gains enter the
watch. UTC executions are converted to New York trade dates. Roth/IRA losses do
not enter the taxable-loss watch. All retirement buys, including real DRIPs,
remain purchases. After 2026-11-27 the model holds Roth distributions as cash;
only an explicit discretionary model allocation can spend that cash. Unexpected
real post-confirmation DRIPs are retained and flagged, never erased.

Unemployment comes from FRED/ALFRED UNRATE initial releases (`output_type=4`) using
release dates, not observation-month dates. The preferred route accepts `FRED_API_KEY`
or `UNEMPLOYMENT_SNAPSHOT`: a JSON object with `first_release: true`, `as_of`, and
at least four consecutive monthly `observations` with `date`, `value` and
`release_date`. `as_of` must cover the signal date. Review 1 restores the original revised FRED latest API / fredgraph CSV path
when the verified snapshot or first-release API is unavailable; no key is required
for CSV. Snapshot and first-release failures, including insufficient observations,
advance to the next source. Only failure of every source blocks the report.
Every report, midday reminder and Telegram chunk states first-release provenance
or the exact revised-figures warning. Revised observations have no asserted
historic release dates: the current three-month delta drives current routing,
while unknown historical release timing remains unknown (not invented PIT data). Do not put API keys in this repository or in messages.

The UPRO next-close exit matches X's net-position convention: a passing close at
the scheduled exit close restores IN. This does not prove whether an unseen
same-close sell/rebuy occurred. Timing remains TQQQ same-day after-hours,
UPRO exits next-close MOC and entries same-day after-hours; Top-7 next-close MOC.
The full Telegram scheduler still waits for final Nasdaq data. Early post-close
monitors can report leveraged sleeves while retaining Top-7 if Nasdaq is not final.

Run tests with `python -m unittest discover -v`. Historic gzip fixtures include
independent W/X expected ledgers and input hashes. `--dry-run` never sends Telegram
or saves state. `--fixture` is accepted only with `--dry-run`. The normal code has
no broker connection or order mutation method.

Top-7 warnings (Addendum 2) do not change signals, holdings, NAV or weights.
`state.json` has an optional `substitutes` mapping (default `{}`); for example,
`{"MU":"DRAM"}` is an owner-selected example, never a default recommendation.
Monitor callers can supply the same mapping through their state/facts JSON.
The optional `top7_accounts` mapping identifies the target taxable account for
its own-window note; if identity is unknown, the household watch is still shown
without pretending the loss was in that same account. An explicit
`top7_rebalance_plan` is a list of user-supplied buy/sell records with symbol,
account, taxable flag, date and a negative gain/expected_gain for a proposed loss
sale. It adds forward- and before-window warnings, not a new January signal.
