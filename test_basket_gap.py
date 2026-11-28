"""A data gap in one basket stock must not silence the report once the ratchet has fired."""
import io, contextlib, os, unittest
from html import unescape
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import joint_runtime as rt
import send_spy_ma_telegram as bot

FIX = Path(__file__).parent / 'tests/fixtures'


def bundle(fired, gap_days=3, split=None):
    b = rt.load_data_bundle(FIX / 'entry.json.gz')
    basket = b['basket'].copy(); cfg = b['basket_config']
    base = pd.Timestamp(cfg['baseline_date']); first = cfg['holdings'][0]['ticker']
    after = basket.index[basket.index >= base]
    if fired:   # +80% then a 40% fall: arms at +60%, fires at -25% from the high
        n = len(after); path = np.concatenate([np.linspace(1.0, 1.8, n // 2), np.linspace(1.8, 1.05, n - n // 2)])
        for h in cfg['holdings']:
            basket.loc[after, h['ticker']] = float(h['baseline_close']) * path
    if split:
        basket[first] = basket[first] / split
    if gap_days:
        basket.loc[basket.index[-gap_days:], first] = np.nan
    b['basket'] = basket
    return b


def run(b, env=None):
    env = dict({'FINAL_ATTEMPT': 'true'}, **(env or {}))
    with patch.dict(os.environ, env), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'save_state'), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        result = bot.main(pd.Timestamp(b['clock']).to_pydatetime(), dry_run=True, persist=False,
                          data_bundle=b, facts_override=b['facts'], state_override={})
        send.assert_not_called()
    return result, out.getvalue()


class BasketGapTests(unittest.TestCase):
    def test_gap_after_ratchet_fired_still_reports(self):
        result, _ = run(bundle(fired=True))
        self.assertIsNotNone(result)
        text = unescape(result['text']); short = unescape(result['short_text'])
        self.assertIn('basket prices incomplete after', text)
        self.assertIn('ratchet already fired', text)
        self.assertTrue(result['state']['top7_roth']['ratchet_fired'])
        self.assertEqual(result['state']['top7_roth']['state'], 'defensive')
        self.assertIn('Top-7 basket prices incomplete since', short)
        self.assertIn('No effect: the Roth ratchet already fired', short)

    def test_gap_before_ratchet_fired_still_refuses(self):
        with self.assertRaises(Exception) as caught:
            run(bundle(fired=False))
        self.assertIn('Top-7 basket', str(caught.exception))

    def test_gap_on_an_early_attempt_still_waits(self):
        result, log = run(bundle(fired=True), {'FINAL_ATTEMPT': 'false', 'MANUAL_RUN': 'false'})
        self.assertIsNone(result)
        self.assertIn('Deferring scheduled report', log)

    def test_no_gap_is_unchanged(self):
        result, _ = run(bundle(fired=True, gap_days=0))
        self.assertNotIn('incomplete', unescape(result['short_text']))
        self.assertTrue(result['state']['top7_roth']['ratchet_fired'])

    def test_split_baseline_is_rescaled_and_flagged(self):
        plain, _ = run(bundle(fired=True, gap_days=0))
        split, _ = run(bundle(fired=True, gap_days=0, split=8))
        self.assertAlmostEqual(plain['state']['top7_roth']['ratchet_nav'], split['state']['top7_roth']['ratchet_nav'], places=9)
        self.assertIn('baseline rescaled for a 8-for-1 split', unescape(split['text']))
        cfg = {'baseline_date': '2026-01-02', 'holdings': [{'ticker': 'X', 'weight': 1.0, 'baseline_close': 100.0}]}
        closes = pd.DataFrame({'X': [61.0]}, index=[pd.Timestamp('2026-01-02')])
        with self.assertRaises(ValueError):
            bot.split_adjusted_basket_config(cfg, closes)
        same, notes = bot.split_adjusted_basket_config(cfg, pd.DataFrame({'X': [99.2]}, index=[pd.Timestamp('2026-01-02')]))
        self.assertEqual((same['holdings'][0]['baseline_close'], notes), (100.0, []))

    def test_live_basket_file_matches_split_adjusted_sndk(self):
        import json
        cfg = json.loads((Path(__file__).parent / 'top7_basket.json').read_text())
        sndk = next(h for h in cfg['holdings'] if h['ticker'] == 'SNDK')
        self.assertAlmostEqual(sndk['baseline_close'], 275.24 / 8, places=3)
        self.assertAlmostEqual(sum(h['weight'] for h in cfg['holdings']), 1.0, places=5)


if __name__ == '__main__':
    unittest.main()
