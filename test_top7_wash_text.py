"""Addendum 2 changes warnings only; no stock substitution enters signals or NAV."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import unittest,ast,hashlib,json
import pandas as pd
import joint_runtime as rt
import send_spy_ma_telegram as bot

class Top7WashTextTests(unittest.TestCase):
    def test_original_top7_signal_and_nav_code_unchanged(self):
        source=Path(bot.__file__).read_text();tree=ast.parse(source);expected=json.loads((Path(__file__).parent/'tests/fixtures/top7_signal_hashes.json').read_text());found={}
        for node in tree.body:
            name=node.name if isinstance(node,ast.FunctionDef) else node.targets[0].id if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) else None
            if name in expected:found[name]=hashlib.sha256(ast.get_source_segment(source,node).encode()).hexdigest()
        self.assertEqual(found,expected)
    def facts(self):
        return dict(taxable_loss_sales=[dict(symbol='MU',date='2026-11-09',account_last4='1514',taxable=True,gain=-1.)],substitutes={})
    def test_roth_warning_prior_and_same_day_fallback_choice(self):
        for on in ['2026-11-09','2026-11-27','2026-12-09']:
            lines=rt.top7_buy_warnings('Kyle Roth IRA',['MU'],on,self.facts(),taxable=False)
            text='\n'.join(lines);self.assertIn('WASH-SALE WARNING: MU was sold at a loss',text)
            self.assertIn('before 2026-12-10 permanently loses that loss',text)
            self.assertIn('a comparable holding of your choice',text);self.assertNotIn('DRAM',text)
        self.assertEqual(rt.top7_buy_warnings('Kyle Roth IRA',['MU'],'2026-12-10',self.facts(),taxable=False),[])
    def test_optional_mapping_used_only_in_text_and_inputs_unchanged(self):
        facts=self.facts();facts['substitutes']={'MU':'DRAM'};saved=deepcopy(facts)
        out=rt.top7_buy_warnings('Katelyn Roth IRA',['MU'],'2026-11-27',facts,taxable=False)
        self.assertIn('Buy DRAM (your configured comparable holding) instead','\n'.join(out))
        self.assertEqual(facts,saved)
        self.assertEqual(rt.top7_buy_warnings('Roth',['SNDK'],'2026-11-27',facts,taxable=False),[])
    def test_taxable_own_window_is_deferral_note_no_action(self):
        text='\n'.join(rt.top7_buy_warnings('Brokerage',['MU'],'2026-11-27',self.facts(),account_id='1514',taxable=True))
        self.assertEqual(text,"Note: rebuying MU in the taxable account within 30 days only postpones the loss (it is added to the new shares' cost). No action needed.")
        self.assertNotIn('permanently',text)
    def test_unknown_other_taxable_account_not_called_own(self):
        text='\n'.join(rt.top7_buy_warnings('Brokerage',['MU'],'2026-11-27',self.facts(),account_id='9999',taxable=True))
        self.assertIn('target taxable account is not identified',text);self.assertNotIn('No action needed',text)
    def test_january_before_window_same_day_day30_and_day31(self):
        for sale_date,expected in [('2027-01-04',True),('2027-02-03',True),('2027-02-04',False)]:
            facts=dict(taxable_loss_sales=[],retirement_purchases=[],substitutes={},top7_rebalance_plan=[dict(side='buy',symbol='MU',date='2027-01-04',taxable=False,account='Roth'),dict(side='sell',symbol='MU',date=sale_date,taxable=True,gain=-10.,account='Brokerage')])
            out='\n'.join(rt.top7_rebalance_warnings(facts));self.assertEqual('BEFORE window' in out,expected)
    def test_real_prior_retirement_buy_is_included(self):
        facts=dict(taxable_loss_sales=[],retirement_purchases=[dict(symbol='MU',date='2026-12-20',account_last4='6134')],top7_rebalance_plan=[dict(side='sell',symbol='MU',date='2027-01-04',taxable=True,expected_gain=-1)],substitutes={})
        self.assertIn('BEFORE window','\n'.join(rt.top7_rebalance_warnings(facts)))
    def test_positive_gain_and_outside_before_window_not_warned(self):
        facts=dict(taxable_loss_sales=[],retirement_purchases=[dict(symbol='MU',date='2026-12-01')],top7_rebalance_plan=[dict(side='sell',symbol='MU',date='2027-01-04',taxable=True,gain=10.)],substitutes={})
        self.assertEqual(rt.top7_rebalance_warnings(facts),[])
    def test_moc_buy_checks_each_constituent(self):
        facts=self.facts();facts['substitutes']={'MU':'DRAM'}
        out='\n'.join(rt.render_moc_orders([dict(sleeve='Top-7',account='Roth',sell='SP500',buy='TOP-7 STOCKS',tickers=['MU','SNDK'])],facts,'2026-11-30','3:50 PM'))
        self.assertIn('WASH-SALE WARNING: MU',out);self.assertNotIn('WASH-SALE WARNING: SNDK',out)
        self.assertNotIn(rt.ROTH_BUY_REMINDER,out)
    def test_main_buy_messages_have_warnings_and_targets_unchanged(self):
        f=Path(__file__).parent/'tests/fixtures/entry.json.gz';b=rt.load_data_bundle(f)
        b['facts']['taxable_loss_sales']=[dict(symbol='TEST',date='2026-11-27',taxable=True,account_last4='1514',gain=-1)]
        b['facts']['substitutes']={'TEST':'PEER'}
        prev={key:dict(position='SP500',actual_position='SP500',signal_position='SP500',state='defensive') for key in ['top7_roth','top7_brok']}
        with patch.object(bot,'send_telegram') as send,patch.object(bot,'save_state') as save:
            out=bot.main(pd.Timestamp(b['clock']).to_pydatetime(),dry_run=True,persist=False,data_bundle=b,facts_override=b['facts'],state_override=prev)
        self.assertIn('WASH-SALE WARNING: TEST',out['text']);self.assertIn('PEER',out['text'])
        self.assertEqual(out['state']['top7_roth']['signal_position'],'TOP-7 STOCKS');self.assertEqual(b['basket_config']['holdings'][0]['ticker'],'TEST')
        self.assertEqual(out['state']['substitutes'],{'TEST':'PEER'});send.assert_not_called();save.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
