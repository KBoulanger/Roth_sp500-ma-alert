"""Production policy tests: independent historic ledgers plus targeted counterexamples."""
import json,gzip,unittest,tempfile
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch,Mock
from html import unescape
import numpy as np,pandas as pd
import joint_strategy as js
import joint_runtime as rt
import send_spy_ma_telegram as bot

FIX=Path(__file__).parent/'tests/fixtures'

def toy(sleeve='TQQQ',rp=None,bp=None,returns=None,out=None,dates=None):
    n=len(rp);dates=pd.to_datetime(dates) if dates is not None else pd.date_range('2020-06-01',periods=n+1)
    cols={'date':dates,'adj_price_index':100.,'r_tqqq_3x':0.,'r_qld_2x':0.,'r_upro_3x':0.,'r_sso_2x':0.,'r_ief':0.,'r_tbill':0.,'unemp_3m_change':0.}
    d=js.StrategyData(pd.DataFrame(cols),sleeve)
    for kind in ('base','dip20'):
        d.pos[kind,1]=np.r_[False,rp];d.pos[kind,4]=np.r_[False,bp]
    if returns is not None:d.returns[1:,0]=returns
    if out is not None:d.out[1:]=out
    return d,str(d.dates.iloc[1].date()),str(d.dates.iloc[-1].date())

class GoldenTests(unittest.TestCase):
    def test_entire_reference_ledgers_and_clashes(self):
        for spec in json.loads((FIX/'golden_manifest.json').read_text()):
            sleeve=spec['sleeve'];d=js.StrategyData(pd.read_csv(FIX/f'{sleeve}_input.csv.gz'),sleeve)
            z,l,_=js.replay_joint(d,js.POLICIES[sleeve],spec['start'],spec['end'],ledger=True)
            expected=pd.read_csv(FIX/f'{sleeve}_expected.csv.gz')
            pd.testing.assert_frame_equal(l[['date','account','fund','side']],expected[['date','account','fund','side']])
            np.testing.assert_allclose(l[['dollars','gain','basis','interest_taxed']],expected[['dollars','gain','basis','interest_taxed']],rtol=1e-10,atol=1e-10)
            self.assertEqual(z['clashes'],0 if sleeve=='TQQQ' else 2)
            buys=l[(l.account=='R')&(l.side=='BUY')&(l.fund=='T')]
            sales=l[(l.account=='B')&(l.side=='SELL')&(l.fund=='T')&(l.gain < -1e-12)]
            pairs=[]
            for row in sales.itertuples():
                for b in buys.itertuples():
                    if abs((pd.Timestamp(b.date)-pd.Timestamp(row.date)).days)<=30:pairs.append((row.date,b.date))
            self.assertEqual(pairs,[] if sleeve=='TQQQ' else [('1997-10-28','1997-10-03'),('2020-10-29','2020-10-09')])

class RuleTests(unittest.TestCase):
    def test_defensive_threshold_rounding_and_top7(self):
        for v in [0.,.1,.10000000000000009,-.2]:
            self.assertEqual(js.defensive_holding(v),'USFR');self.assertEqual(js.defensive_holding(v,True),'SP500')
        self.assertEqual(js.defensive_holding(.2),'IEF');self.assertEqual(js.defensive_holding(.2,True),'IEF')
        with self.assertRaises(ValueError):js.defensive_holding(None)
    def test_exit_speeds_are_independent(self):
        p=pd.Series([100+0.1*i for i in range(320)]+[50]*5)
        r,_,_=js.signal_positions(p,150,.31,1);b,_,_=js.signal_positions(p,150,.31,4)
        self.assertFalse(r[321]);self.assertTrue(b[321]);self.assertFalse(b[324])
    def test_dip_is_inclusive_once_per_decline_and_rearms(self):
        p=[100]*5+[80,79,100,80,79]
        pos,nxt,meta=js.signal_positions(p,2,0.,1,dip=True)
        self.assertTrue(pos[6]);self.assertTrue(pos[7]);self.assertFalse(pos[8]);self.assertTrue(pos[9]);self.assertFalse(meta['armed'])
    def test_manual_dip_state_override(self):
        p=[100]*5+[80,79,78]
        a,_,_=js.signal_positions(p,2,0.,1,dip=True)
        b,_,_=js.signal_positions(p,2,0.,1,dip=True,override_index=6,armed_override=False,active_override=False)
        self.assertTrue(a[6]);self.assertFalse(b[6])
    def test_day31_entry_not_day30_and_no_2x_in_brokerage(self):
        rp=[1,1,0]+[1]*42;d,s,e=toy('UPRO',rp,rp,[.2,-.3]+[0]*43)
        z,l,_=js.replay_joint(d,js.POLICIES['UPRO'],s,e,ledger=True)
        b=l[(l.account=='B')&(l.side=='BUY')&(l.fund=='T')];r=l[(l.account=='R')&(l.side=='BUY')&(l.fund=='T')].iloc[0]
        self.assertEqual((pd.Timestamp(b.iloc[1].date)-pd.Timestamp(r.date)).days,31)
        self.assertFalse(((l.account=='B')&(l.fund=='Q')).any());self.assertEqual(z['guard_sales'],0)
    def test_upro_has_no_floor_but_tqqq_has_5percent_floor(self):
        for sleeve,expected in [('UPRO',0),('TQQQ',1)]:
            d,s,e=toy(sleeve,[1]*5,[1]*5,[.4,-.3,0,0,0])
            z,l,_=js.replay_joint(d,js.POLICIES[sleeve],s,e,ledger=True)
            self.assertEqual(z['guard_sales'],expected)
    def test_busy_day_exits_roth_then_brokerage_buys(self):
        d,s,e=toy('TQQQ',[1,0,1],[1,0,1],[0,0,0])
        _,l,_=js.replay_joint(d,js.POLICIES['TQQQ'],s,e,ledger=True)
        for _,g in l.groupby('date',sort=False):
            sides=g.side.tolist()
            if 'BUY' in sides:self.assertNotIn('SELL',sides[sides.index('BUY'):])
            buys=g[g.side=='BUY'].account.tolist()
            if len(buys)>1:self.assertEqual(buys,['R','B'])
    def test_real_tax_cost_per_share_overrides_model_without_quantity(self):
        d,s,e=toy('TQQQ',[1],[1],[0])
        fact=dict(as_of='2020-06-01',roth=dict(holding='QLD'),brokerage=dict(holding='TQQQ',tax_cost_per_share=100.))
        z,l,_=js.replay_joint(d,js.POLICIES['TQQQ'],s,e,ledger=True,facts=fact,market_prices={'2020-06-01':{'TQQQ':130.}})
        self.assertEqual(l[(l.account=='R')&(l.side=='BUY')].iloc[0].fund,'T')
    def test_cushions_inclusive_and_floor_strict(self):
        for sleeve,price in [('TQQQ',130.),('UPRO',110.)]:
            for delta,target in [(0.,'T'),(-.00001,'Q')]:
                d,a,b=toy(sleeve,[1],[1],[0]);other='QLD' if sleeve=='TQQQ' else 'SSO'
                facts=dict(as_of='2020-06-01',roth=dict(holding=other),brokerage=dict(holding=sleeve,tax_cost_per_share=100.))
                z,l,_=js.replay_joint(d,js.POLICIES[sleeve],a,b,ledger=True,liquidate=False,facts=facts,market_prices={'2020-06-01':{sleeve:price+delta}})
                actual=z['model_accounts']['R']['holding'];self.assertEqual(actual,sleeve if target=='T' else other)
        d,a,b=toy('TQQQ',[1],[1],[0]);facts=dict(as_of='2020-06-01',roth=dict(holding='TQQQ',last_3x_buy='2020-05-31'),brokerage=dict(holding='TQQQ',tax_cost_per_share=100.))
        z,_,_=js.replay_joint(d,js.POLICIES['TQQQ'],a,b,ledger=True,liquidate=False,facts=facts,market_prices={'2020-06-01':{'TQQQ':105.}})
        self.assertEqual(z['guard_sales'],0)
    def test_unknown_cost_does_not_fabricate_cushion_or_floor(self):
        d,s,e=toy('TQQQ',[1],[1],[0]);fact=dict(as_of='2020-06-01',roth=dict(holding='QLD',last_3x_buy='2020-05-31'),brokerage=dict(holding='TQQQ'))
        z,l,_=js.replay_joint(d,js.POLICIES['TQQQ'],s,e,ledger=True,facts=fact)
        self.assertTrue(z['basis_unknown']);self.assertEqual(z['guard_sales'],0)
        self.assertFalse(((l.account=='R')&(l.side=='BUY')&(l.fund=='T')).any())
    def test_household_taxable_loss_blocks_only_new_roth_3x(self):
        d,s,e=toy('TQQQ',[1,1],[1,1],[.4,0])
        z,l,_=js.replay_joint(d,js.POLICIES['TQQQ'],s,e,ledger=True,household_losses=[dict(symbol='TQQQ',date='2020-06-01')])
        self.assertFalse(((l.account=='R')&(l.side=='BUY')&(l.fund=='T')).any())
        self.assertTrue(((l.account=='B')&(l.side=='BUY')&(l.fund=='T')).any())
    def test_forward_roth_dividends_are_cash_not_new_purchases(self):
        d,s,e=toy('UPRO',[1]*3,[1]*3,[.4,0,0],dates=pd.date_range('2026-11-26',periods=4))
        # First day opens SSO; next day swaps to UPRO. Last day pays a 10% cash dividend.
        d.roth_cash_returns={(int(d.days[3]),1):(0.,.1)}
        z,l,_=js.replay_joint(d,js.POLICIES['UPRO'],s,e,ledger=True,liquidate=False)
        self.assertGreater(z['model_accounts']['R']['cash'],0)
        self.assertEqual(z['last_roth_3x_buy'],'2026-11-27')
        self.assertFalse(((l.account=='R')&(l.side=='BUY')&(l.date=='2026-11-28')).any())

class CashDividendTests(unittest.TestCase):
    def test_cash_does_not_compound_in_fund_after_distribution(self):
        d,a,b=toy('UPRO',[1]*4,[1]*4,[.4,0,0,.1],dates=pd.date_range('2026-11-26',periods=5))
        d.roth_cash_returns={(int(d.days[3]),1):(0.,.1),(int(d.days[4]),1):(.1,0.)}
        z,l,_=js.replay_joint(d,js.POLICIES['UPRO'],a,b,ledger=True,liquidate=False)
        cash=z['model_accounts']['R']['cash'];value=z['model_accounts']['R']['wealth']
        self.assertAlmostEqual(cash,js.COST*.1);self.assertAlmostEqual(value,js.COST*1.1+cash)
        self.assertEqual(len(l[(l.account=='R')&(l.side=='BUY')]),2)

class ReminderTests(unittest.TestCase):
    def test_every_leveraged_roth_buy_and_swap_and_no_other_instruction(self):
        for account in ['Roth','Kyle Roth IRA','Katelyn Roth IRA','Brokerage']:
            for target in ['TQQQ','QLD','UPRO','SSO','IEF','USFR','SP500']:
                lines=rt.render_order(account,'USFR',target,'same close',[],'2026-11-27')
                expected='Roth' in account and target in ['TQQQ','QLD','UPRO','SSO']
                self.assertEqual(rt.ROTH_BUY_REMINDER in lines,expected,(account,target))
                if expected:self.assertEqual(lines[1],rt.ROTH_BUY_REMINDER)
        self.assertNotIn(rt.ROTH_BUY_REMINDER,rt.render_order('Roth','TQQQ','IEF','sell at MOC',[],'2026-11-27'))
    def test_same_rule_in_html_account_render(self):
        for target in ['TQQQ','QLD','UPRO','SSO','USFR']:
            sleeve='TQQQ' if target in ['TQQQ','QLD'] else 'UPRO'
            result=dict(sleeve=sleeve,signal_date='2026-11-27',observed=dict(as_of='2026-11-27',roth=dict(holding='USFR')),model_accounts={'R':dict(holding=target)},signal_meta={'roth':dict(pending_exit=False,armed=True,active=False,failing_closes=0)})
            text='\n'.join(rt.render_joint(result,'R',dict(taxable_loss_sales=[]),html=True));text=unescape(text)
            self.assertEqual(rt.ROTH_BUY_REMINDER in text,target!='USFR')
            if target!='USFR':self.assertIn('MODEL plan; real fills are not confirmed\n'+rt.ROTH_BUY_REMINDER,text)

class FactAndDataTests(unittest.TestCase):
    def test_seed_records_off_setting_and_historic_drips(self):
        f=rt.load_facts();self.assertEqual(f['dividend_reinvestment']['forward_dividends'],'cash, never simulated DRIP purchases')
        self.assertEqual([p['date'] for p in f['retirement_purchases'] if p.get('placed_agent')=='drip'],['2026-09-29','2026-09-29'])
    def test_utc_fill_conversion_and_positive_or_ira_loss_not_watched(self):
        self.assertEqual(rt.et_date('2026-09-30T01:00:00Z').isoformat(),'2026-09-29')
        self.assertIsNone(rt.loss_argument('TQQQ@2026-11-27@10@Kyle_Brokerage'))
        self.assertEqual(rt.open_wash_windows('TQQQ','2026-11-27',[dict(symbol='TQQQ',date='2026-11-09',taxable=False)]),[])
    def test_first_release_not_backdated_to_month(self):
        u=pd.DataFrame({'DATE':pd.to_datetime(['2026-05-01','2026-06-01','2026-07-01','2026-08-01']),'release_date':pd.to_datetime(['2026-06-05','2026-07-02','2026-08-07','2026-09-04']),'UNRATE':[4.,4.,4.,4.2]})
        x=rt.first_release_delta_history(u,pd.to_datetime(['2026-09-03','2026-09-04']))
        self.assertTrue(np.isnan(x.iloc[0]));self.assertAlmostEqual(x.iloc[1],.2)
    def test_metadata_keeps_adjusted_close(self):
        f=pd.DataFrame({'Close':[99.],'RawClose':[100.]},index=pd.to_datetime(['2026-09-04']))
        with patch.object(bot,'fetch_yahoo_regular_close',return_value=100.):out=bot.ensure_session_close(f,'SPY',pd.Timestamp('2026-09-04').date())
        self.assertEqual(out.Close.iloc[0],99.)
    def test_adjusted_download_and_full_qqq_ath_history(self):
        frame=pd.DataFrame({'Close':[100.,101.],'Adj Close':[90.,91.],'Dividends':[0.,0.]},index=pd.to_datetime(['2026-11-24','2026-11-25']))
        with patch('yfinance.download',return_value=frame) as download:
            out=bot.fetch_etf_yfinance('QQQ')
        self.assertEqual(out.Close.tolist(),[90.,91.]);self.assertEqual(out.RawClose.tolist(),[100.,101.])
        self.assertEqual(download.call_args.kwargs['period'],'max');self.assertFalse(download.call_args.kwargs['auto_adjust'])
    def test_no_unadjusted_or_revised_fallback(self):
        with self.assertRaises(RuntimeError):bot.fetch_etf_stooq('SPY')
        with patch.dict(bot.os.environ,{},clear=True),patch.object(bot.requests,'get') as get:
            with self.assertRaises(RuntimeError):bot.fetch_unrate()
            get.assert_not_called()
    def test_fred_initial_release_request(self):
        response=Mock(status_code=200);response.json.return_value={'observations':[{'date':f'2026-0{i}-01','value':'4.0','realtime_start':f'2026-0{i+1}-05'} for i in range(5,9)]}
        with patch.object(bot.requests,'get',return_value=response) as get:
            u=bot.fetch_unrate_fred_api('not-a-real-key')
        self.assertEqual(get.call_args.kwargs['params']['output_type'],4);self.assertIn('release_date',u)
    def test_chunker_keeps_reminder_with_its_instruction(self):
        text='x'*3800+'\nRoth: buy QLD\n'+rt.ROTH_BUY_REMINDER
        chunks=bot.telegram_chunks(text);self.assertEqual(len(chunks),2)
        for chunk in chunks:
            if 'Roth: buy' in chunk:self.assertIn('Roth: buy QLD\n'+rt.ROTH_BUY_REMINDER,chunk)
    def test_chunking_is_bounded(self):
        text='\n'.join(['<b>valid closed HTML line</b>']*300)
        chunks=bot.telegram_chunks(text);self.assertGreater(len(chunks),1);self.assertTrue(all(len(c)<=3900 for c in chunks))

if __name__=='__main__':unittest.main(verbosity=2)
