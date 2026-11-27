# Scheduled read-only reports

`monitor.py` is the post-close report; `moc_reminder.py` is the midday MOC preview.
Both use the identical main-bot accounting and message engine. No broker access,
Telegram delivery or state persistence is available. The main bundle is pinned by
commit and SHA256, and the midday wrapper additionally verifies monitor.py.
Download BOTH scripts into the same task folder and verify both published hashes.
The pinned main commit must be published after Claude review before remote runs work.

For local review pass `--repo /absolute/path/to/repo --cache-dir /task/cache`.
For deterministic review add `--fixture /path/to/entry.json` (explicit synthetic).
Post-close uses the fixture clock. Midday synthetic review uses `--now` at noon of
the next XNYS session so the fixture represents its prior completed signal close.
Live post-close waits until session close +2 minutes, including early closes;
Top-7 stays frozen until final Nasdaq data. Midday uses the previous completed
signal close and refuses submit-now language after close minus ten minutes.

Pass read-only facts via `--spouse-export`, `--seed`, `--state-file`,
`--unemployment-json`, or `LOSS=SYM@ISO@gain@account`. Optional raw close observations
`SPY=price@date` / `QQQ=price@date` are cross-checks, never adjusted-series replacements.
The export defaults to the fixed dated seed if missing. Remote execution reads
main/state.json as data for confirmed observations and optional `substitutes`.
Explicit --state-file overrides this. FRED_API_KEY or a verified first-release
snapshot is optional. Prefer the snapshot, then initial-release API, then the original
revised FRED API/CSV fallback with the exact revised-data warning. Only all-source
failure blocks. Every report/MOC reminder states its provenance. No raw-price fallback. Keep caches in task folder.

There is no blanket 2x substitution or automatic day-31 swap. Brokerage stays 3x
subject to its Roth-purchase lock; Roth upgrades need adjusted-cost cushion and
no recent supplied household loss. Both Roths keep reinvestment OFF and each
leveraged Roth buy carries the exact reminder. Top-7 constituent warnings and
optional owner-chosen substitutes affect text only. Household watch covers every
supplied ticker. Only structured next-close plans generate MOC_REMINDER_AT/TEXT;
same-day leveraged entries and upgrades are never moved to MOC. No schedule is
created by these scripts. See JOINT_POLICY.md for observed/model and plan schemas.
