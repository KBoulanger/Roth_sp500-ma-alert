"""Offline full-message tests; no Telegram delivery or broker mutation."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from html import unescape
import pandas as pd
import send_spy_ma_telegram as bot
import joint_runtime as rt
FIX=Path(__file__).parent/'tests/fixtures'

def run_case(case):
    b=rt.load_data_bundle(FIX/f'{case}.json.gz')
    with patch.object(bot,'send_telegram') as send,patch.object(bot,'save_state') as save,patch.object(bot.requests,'post') as post:
        out=bot.main(pd.Timestamp(b['clock']).to_pydatetime(),dry_run=True,persist=False,data_bundle=b,facts_override=b['facts'],state_override={})
    send.assert_not_called();save.assert_not_called();post.assert_not_called();return out

class DryRunIntegrationTests(unittest.TestCase):
    def test_entry_separates_accounts_and_does_not_promote_fills(self):
        out=run_case('entry');text=unescape(out['text'])
        for x in ['Roth: buy QLD','Roth: buy SSO','Brokerage: buy TQQQ','Brokerage: buy UPRO']:self.assertIn(x,text)
        self.assertEqual(text.count(rt.ROTH_BUY_REMINDER),2)
        self.assertEqual(out['state']['qqq_lev_roth']['actual_position'],'USFR')
        self.assertEqual(out['state']['qqq_lev_roth']['signal_position'],'QLD')
        self.assertEqual(out['state']['qqq_lev_roth']['exit_threshold'],1)
        self.assertEqual(out['state']['qqq_lev_brok']['exit_threshold'],4)
    def test_cushion_swaps_have_immediate_exact_reminders(self):
        text=unescape(run_case('cushion')['text'])
        for x in ['Roth: buy TQQQ / sell QLD','Roth: buy UPRO / sell SSO']:self.assertIn(x,text)
        lines=text.splitlines()
        for i,x in enumerate(lines):
            if x.startswith('Roth: buy '):self.assertEqual(lines[i+1],rt.ROTH_BUY_REMINDER)
        self.assertEqual(text.count(rt.ROTH_BUY_REMINDER),2)
    def test_guard_sells_only_tqqq_brokerage_and_no_reminder(self):
        out=run_case('guard');text=unescape(out['text'])
        self.assertIn('Brokerage: buy IEF / sell TQQQ',text)
        self.assertNotIn('Brokerage: buy SSO',text);self.assertNotIn('Brokerage: buy QLD',text)
        self.assertNotIn(rt.ROTH_BUY_REMINDER,text)
        self.assertEqual(out['state']['spy_lev_brok']['model_position'],'UPRO')
    def test_old_pending_model_fill_not_promoted_to_real(self):
        prev={'qqq_lev_roth':{'position':'USFR','pending_order':{'buy':'TQQQ','signal_date':'2026-11-26'}}}
        new={'qqq_lev_roth':{'joint_policy':True,'actual_position':'USFR','position':'USFR','signal_position':'QLD'}}
        bot.apply_actual_vs_signal_state(prev,new,pd.Timestamp('2026-11-27').date());self.assertEqual(new['qqq_lev_roth']['actual_position'],'USFR')
    def test_all_moc_roth_buy_targets_and_negative_cases(self):
        for who in ['Kyle Roth IRA','Katelyn Roth IRA','Brokerage']:
            for target in ['TQQQ','QLD','UPRO','SSO','IEF']:
                lines=rt.render_moc_orders([dict(account=who,sell='USFR',buy=target)],{'taxable_loss_sales':[]},'2026-11-27','12:50 PM')
                expect='Roth' in who and target!='IEF';self.assertEqual(rt.ROTH_BUY_REMINDER in lines,expect)
                if expect:
                    i=next(i for i,l in enumerate(lines) if ': sell ' in l);self.assertEqual(lines[i+1],rt.ROTH_BUY_REMINDER)
        self.assertNotIn(rt.ROTH_BUY_REMINDER,rt.render_moc_orders([],{'taxable_loss_sales':[]},'2026-11-27','12:50 PM'))
    def test_household_loss_routes_roth_only(self):
        loss=[dict(symbol='TQQQ',date='2026-11-09',taxable=True)]
        r=rt.render_order('Katelyn Roth IRA','USFR','TQQQ','same close',loss,'2026-11-27')
        b=rt.render_order('Brokerage','USFR','TQQQ','same close',loss,'2026-11-27')
        self.assertIn('buy QLD',r[0]);self.assertIn('buy TQQQ',b[0]);self.assertEqual(r[1],rt.ROTH_BUY_REMINDER)
    def test_top7_rising_defensive_is_ief(self):
        st=dict(state='defensive',_unrate_high=True,reentry_streak=0,ma_streak=0,vol_streak=0,last_transition_date=None)
        text='\n'.join(bot.render_top7_section('Roth',bot.ROTH_TOP7_PARAMS,st,pd.Timestamp('2026-01-02')))
        self.assertIn('HOLD: IEF',text);self.assertNotIn('TREASURIES',text)
    def test_actual_snapshot_newer_than_prices_blocks_old_instruction(self):
        f=rt.load_facts();r=dict(sleeve='TQQQ',signal_date='2026-11-25',observed=f['sleeves']['TQQQ'],model_accounts={'R':dict(holding='QLD')},signal_meta={'roth':dict(armed=True,active=False,failing_closes=0)})
        text='\n'.join(rt.render_joint(r,'R',f));self.assertIn('no current trade instruction',text);self.assertNotIn(rt.ROTH_BUY_REMINDER,text)
    def test_execution_timing_preserved(self):
        self.assertEqual(bot.EXECUTION_TIMING['spy_lev_roth'],{'exit':'next_close','entry':'same_day'})
        self.assertEqual(bot.EXECUTION_TIMING['qqq_lev_roth'],{'exit':'same_day','entry':'same_day'})

class ExportTests(unittest.TestCase):
    def file(self,obj):
        td=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(td.cleanup);p=Path(td.name)/'export.json';p.write_text(json.dumps(obj));return p
    def test_real_export_tax_basis_utc_and_unexpected_actual_drip(self):
        d=dict(fetched_at='2026-12-02T18:00:00-05:00',accounts=[dict(account_last4='1514',taxable=True),dict(account_last4='6134',taxable=False)],positions=[dict(account_last4='1514',symbol='TQQQ',quantity=2)],open_lots=[dict(account_last4='1514',symbol='TQQQ',quantity=2,tax_cost_basis=200)],transactions=[dict(account_last4='6134',taxable=False,symbol='TQQQ',side='buy',state='filled',placed_agent='drip',executions=[dict(timestamp_utc='2026-12-02T01:00:00Z',quantity='0.1')])])
        f=rt.load_facts(export_file=self.file(d));self.assertEqual(f['sleeves']['TQQQ']['brokerage']['tax_cost_per_share'],100.)
        self.assertEqual(f['sleeves']['TQQQ']['roth']['last_3x_buy'],'2026-12-01');self.assertTrue(any('OBSERVED DRIP' in x for x in f['notes']))
    def test_september_drips_convert_to_september29(self):
        tx=[dict(account_last4='6134',taxable=False,symbol=sym,side='buy',state='filled',placed_agent='drip',executions=[dict(timestamp_utc='2026-09-30T01:00:00Z',quantity='.01')]) for sym in ['TQQQ','UPRO']]
        f=rt.load_facts(seed_file=None,export_file=self.file(dict(fetched_at='2026-11-27T12:00:00-05:00',accounts=[dict(account_last4='6134',taxable=False)],positions=[],open_lots=[],transactions=tx)))
        for sym in ['TQQQ','UPRO']:self.assertEqual(f['sleeves'][sym]['roth']['last_3x_buy'],'2026-09-29')
    def test_missing_export_uses_dated_seed(self):
        f=rt.load_facts(export_file=Path(__file__).parent/'nonexistent-read-only-export.json')
        self.assertEqual(f['sleeves']['TQQQ']['brokerage']['last_3x_loss'],'2026-11-09');self.assertTrue(any('not present' in x for x in f['notes']))
    def test_trade_date_conflict_and_full_account_number_rejected(self):
        with self.assertRaises(ValueError):rt._trade_dates(dict(trade_date_et='2026-09-30',executions=[dict(timestamp_utc='2026-09-30T01:00:00Z',quantity=1)]))
        with self.assertRaises(ValueError):rt._account_last4({'account_last4':'12345678'})
    def test_unverified_macro_snapshot_rejected(self):
        with self.assertRaises(ValueError):rt.snapshot_unemployment(self.file({'first_release':False,'as_of':'2026-11-27'}),'2026-11-27')

if __name__=='__main__':unittest.main(verbosity=2)
