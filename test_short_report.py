"""Short-message tests: wording and thresholds only; no signal logic lives there."""
import io, contextlib, unittest
from datetime import date
from html import unescape
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import joint_runtime as rt
import send_spy_ma_telegram as bot
import short_report as sr

FIX = Path(__file__).parent / 'tests/fixtures'


def run_case(case, state=None):
    b = rt.load_data_bundle(FIX / f'{case}.json.gz')
    with patch.object(bot, 'send_telegram') as send, patch.object(bot, 'save_state'), contextlib.redirect_stdout(io.StringIO()):
        out = bot.main(pd.Timestamp(b['clock']).to_pydatetime(), dry_run=True, persist=False,
                       data_bundle=b, facts_override=b['facts'], state_override=state or {})
        send.assert_not_called()
    return out, unescape(out['short_text'])


def result(r='USFR', b='USFR', obs_r='USFR', obs_b='USFR', events=(), meta=None, wealth=1.0, basis=1.0):
    meta = meta or {'armed': True, 'active': False, 'drawdown': 0.0, 'failing_closes': 0, 'pending_exit': False}
    return {'model_accounts': {'R': {'holding': r, 'wealth': 1.0, 'tax_basis': 1.0},
                               'B': {'holding': b, 'wealth': wealth, 'tax_basis': basis}},
            'observed': {'roth': {'holding': obs_r}, 'brokerage': {'holding': obs_b}},
            'events': [{'account': a} for a in events], 'signal_meta': {'roth': dict(meta), 'brokerage': dict(meta)}}


def ctx(**kw):
    base = {'date': date(2026, 11, 27), 'joint': {'TQQQ': result(), 'UPRO': result()},
            'lev_metrics': {'TQQQ': {'index': 'QQQ', 'price': 650.99, 'ma': 708.75, 'ma_days': 150, 'vol': .49, 'cap': .31},
                            'UPRO': {'index': 'SPY', 'price': 728.68, 'ma': 711.44, 'ma_days': 300, 'vol': .324, 'cap': .21}},
            'top7': [], 'losses': []}
    base.update(kw)
    return base


class ShortReportTests(unittest.TestCase):
    def test_quiet_day_is_one_line(self):
        self.assertEqual(sr.build_short_report(ctx()), '27 Nov: NO TRADES. Nothing close.')

    def test_entry_trades_and_roth_reminder(self):
        out, text = run_case('entry')
        self.assertTrue(text.startswith('27 Nov: TRADE TODAY'))
        self.assertIn('Brokerage (TQQQ plan): sell USFR, buy TQQQ', text)
        self.assertIn('Brokerage (UPRO plan): sell USFR, buy UPRO', text)
        lines = text.split('\n')
        for needle in ('Roth (TQQQ plan): sell USFR, buy QLD', 'Roth (UPRO plan): sell USFR, buy SSO'):
            i = next(k for k, x in enumerate(lines) if x.startswith(needle))
            self.assertEqual(lines[i + 1], sr.ROTH_BUY_REMINDER)
        self.assertEqual(sr.ROTH_BUY_REMINDER, rt.ROTH_BUY_REMINDER)
        self.assertIn('OBSERVED', out['text'])  # full report is still produced for the log

    def test_cushion_swap_and_guard_sale(self):
        _, text = run_case('cushion')
        self.assertIn('Roth (TQQQ plan): sell QLD, buy TQQQ', text)
        self.assertIn('Roth (UPRO plan): sell SSO, buy UPRO', text)
        self.assertNotIn('Brokerage', text)
        _, text = run_case('guard')
        self.assertIn('Brokerage (TQQQ plan): sell TQQQ, buy IEF', text)
        self.assertNotIn('Reminder', text)  # no Roth leveraged buy

    def test_top7_flip_from_pending_order(self):
        state = {'top7_roth': {'label': 'Top-7 (Roth)', 'state': 'defensive', 'position': 'SP500',
                               'actual_position': 'SP500', 'signal_position': 'SP500'}}
        _, text = run_case('entry', state)
        self.assertIn('Top-7 (Roth): sell SP500, buy TOP-7 STOCKS — at the NEXT close (market-on-close order)', text)

    def test_standing_mismatch_is_a_check_not_a_trade(self):
        c = ctx(joint={'TQQQ': result(r='QLD', b='TQQQ'), 'UPRO': result()})
        text = sr.build_short_report(c)
        self.assertTrue(text.startswith('27 Nov: NO TRADES.'))
        self.assertIn('CHECK: Brokerage (TQQQ plan): record shows USFR, plan says TQQQ', text)
        self.assertNotIn('TRADE TODAY', text)

    def test_reentry_and_exit_watch(self):
        c = ctx(); c['lev_metrics']['TQQQ'].update(price=695.0, vol=.33)
        text = sr.build_short_report(c)
        self.assertIn('WATCH: TQQQ could re-enter soon: QQQ is 1.9% below its 150-day average (needs a close above 708.75)', text)
        self.assertIn('volatility 33.0% must fall below 31%', text)
        c = ctx(); c['lev_metrics']['TQQQ'].update(price=650.0, vol=.33)   # 8% below: not close
        self.assertNotIn('re-enter', sr.build_short_report(c))
        c = ctx(joint={'TQQQ': result('TQQQ', 'TQQQ', 'TQQQ', 'TQQQ'), 'UPRO': result()})
        c['lev_metrics']['TQQQ'].update(price=720.0, vol=.20)
        self.assertIn('TQQQ could exit soon: QQQ is only 1.6% above its 150-day average (exit on a close below 708.75)', sr.build_short_report(c))
        c['lev_metrics']['TQQQ'].update(price=800.0, vol=.29)
        self.assertIn('volatility 29.0% vs the 31% limit', sr.build_short_report(c))
        c['lev_metrics']['TQQQ'].update(price=800.0, vol=.20)
        self.assertEqual(sr.build_short_report(c), '27 Nov: NO TRADES. Nothing close.')

    def test_brokerage_failing_close_count(self):
        meta = {'armed': True, 'active': False, 'drawdown': -0.05, 'failing_closes': 2, 'pending_exit': False}
        c = ctx(joint={'TQQQ': result('USFR', 'TQQQ', 'USFR', 'TQQQ', meta=meta), 'UPRO': result()})
        self.assertIn('Brokerage TQQQ: failing close 2 of 4', sr.build_short_report(c))

    def test_dip_watch_and_trigger_price(self):
        meta = {'armed': True, 'active': False, 'drawdown': -0.17, 'failing_closes': 9, 'pending_exit': False}
        c = ctx(joint={'TQQQ': result(meta=meta), 'UPRO': result()})
        c['lev_metrics']['TQQQ'].update(price=630.52)   # high 759.66 -> trigger 607.73
        text = sr.build_short_report(c)
        self.assertIn('Dip buy is close: QQQ is 17.0% below its high', text)
        self.assertIn('607.7', text)
        meta['drawdown'] = -0.143
        c = ctx(joint={'TQQQ': result(meta=meta), 'UPRO': result()})
        self.assertNotIn('Dip buy', sr.build_short_report(c))

    def test_cushion_watch(self):
        c = ctx(joint={'TQQQ': result('QLD', 'TQQQ', 'QLD', 'TQQQ', wealth=1.27, basis=1.0), 'UPRO': result()})
        c['lev_metrics']['TQQQ'].update(price=800.0, vol=.2)
        self.assertIn('Roth could move from QLD to TQQQ soon: brokerage is up 27% (needs 30%)', sr.build_short_report(c))

    def test_top7_watch_lines(self):
        roth = {'label': 'Top-7 (Roth)', 'invested': True, 'paths': [('vol', 6, 10), ('MA', 46, 50)], 'has_ratchet': True}
        brok = {'label': 'Top-7 (Brok)', 'invested': False, 'reentry_streak': 3, 'reentry_threshold': 5}
        text = sr.build_short_report(ctx(top7=[roth, brok], ratchet={'armed': False, 'fired': False, 'ytd': .57}, arm_gain=.60))
        self.assertIn('Top-7 (Roth) could exit soon: 6 of 10 high-volatility days', text)
        self.assertIn('Top-7 (Roth) could exit soon: 46 of 50 days below the 300-day average', text)
        self.assertIn('Top-7 (Brok) could re-enter soon: 3 of 5 days above the 100-day average', text)
        self.assertIn('ratchet is about to switch on: basket is +57% this year', text)
        quiet = {'label': 'Top-7 (Roth)', 'invested': True, 'paths': [('vol', 1, 10), ('MA', 10, 50)], 'has_ratchet': True}
        self.assertEqual(sr.build_short_report(ctx(top7=[quiet], ratchet={'armed': False, 'fired': False, 'ytd': .2}, arm_gain=.60)),
                         '27 Nov: NO TRADES. Nothing close.')

    def test_wash_window_ending_and_guarded_top7_note(self):
        text = sr.build_short_report(ctx(losses=[{'symbol': 'TQQQ', 'date': '2026-10-29'}, {'symbol': 'SNDK', 'date': '2026-11-18'}]))
        self.assertIn('Wash-sale window on TQQQ ends soon: a Roth/IRA can buy it again from 29 Nov', text)
        self.assertNotIn('SNDK', text)
        text = sr.build_short_report(ctx(top7=[{'label': 'Top-7 (Roth)', 'guarded': True}]))
        self.assertEqual(text, '27 Nov: NO TRADES. Nothing close.\nTop-7 not checked yet (final Nasdaq data arrives after 5:15 PM).')

    def test_preliminary_nasdaq_note_only_when_borderline(self):
        limits = {'Roth': 0.40, 'brokerage': 0.45}
        self.assertEqual(sr.preliminary_top7_notes(0.365, limits), [])
        self.assertEqual(sr.preliminary_top7_notes(None, limits), [])
        notes = sr.preliminary_top7_notes(0.403, limits)
        self.assertEqual(len(notes), 1)
        self.assertIn('Top-7 Roth volatility is borderline (40.3% vs the 40% limit)', notes[0])
        self.assertIn('Top-7 brokerage', sr.preliminary_top7_notes(0.446, limits)[0])

    def test_preliminary_index_time_gate(self):
        from datetime import date
        day = date(2026, 11, 25)
        def meta(hhmm):
            return {'regularMarketPrice': 100.0, 'regularMarketTime': pd.Timestamp(f'2026-11-25 {hhmm}', tz='America/New_York')}
        with self.assertRaises(ValueError):   # the delivering bot still refuses a pre-5:15 value
            bot.regular_close_from_metadata(meta('16:16'), '^IXIC', day, allow_late_index_timestamp=True)
        self.assertEqual(bot.regular_close_from_metadata(meta('16:16'), '^IXIC', day, allow_late_index_timestamp=True, allow_preliminary_index=True), 100.0)
        with self.assertRaises(ValueError):   # never a value from before the close
            bot.regular_close_from_metadata(meta('15:30'), '^IXIC', day, allow_late_index_timestamp=True, allow_preliminary_index=True)
        self.assertEqual(bot.regular_close_from_metadata(meta('17:16'), '^IXIC', day, allow_late_index_timestamp=True), 100.0)

    def test_top7_key_and_preliminary_flag_are_returned(self):
        out, _ = run_case('entry')
        self.assertFalse(out['nasdaq_preliminary'])
        self.assertIn('top7_roth=', out['top7_key']); self.assertIn(';top7_brok=', out['top7_key'])
        state = {'top7_roth': {'label': 'Top-7 (Roth)', 'state': 'defensive', 'position': 'SP500',
                               'actual_position': 'SP500', 'signal_position': 'SP500'}}
        flipped, _ = run_case('entry', state)
        self.assertNotEqual(out['top7_key'], flipped['top7_key'])
        self.assertIn('SP500>TOP-7 STOCKS', flipped['top7_key'])

    def test_delivery_sends_the_short_message(self):
        b = rt.load_data_bundle(FIX / 'entry.json.gz')
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': 'c', 'FORCE_REPEAT': 'true'}), \
                patch.object(bot, 'send_telegram') as send, patch.object(bot, 'save_state'), contextlib.redirect_stdout(io.StringIO()):
            out = bot.main(pd.Timestamp(b['clock']).to_pydatetime(), dry_run=False, persist=False,
                           data_bundle=b, facts_override=b['facts'], state_override={})
        sent = send.call_args.args[2]
        self.assertEqual(sent, out['short_text'])
        self.assertLess(len(sent), 900)
        self.assertNotIn('MODEL target', sent)


if __name__ == '__main__':
    unittest.main()
