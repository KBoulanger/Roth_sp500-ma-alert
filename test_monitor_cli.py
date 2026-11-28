"""Wrapper contract tests; audited local code and explicit offline inputs only."""
from pathlib import Path
import contextlib,hashlib,importlib.util,io,os,subprocess,sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('monitor_under_test',ROOT/'monitor/monitor.py');mon=importlib.util.module_from_spec(spec);spec.loader.exec_module(mon)
class MonitorTests(unittest.TestCase):
 def cli(self,script,case='entry',extra=()):
  return subprocess.run([sys.executable,str(ROOT/'monitor'/script),'--repo',str(ROOT),'--cache-dir',str(ROOT/'.test_cache'),'--fixture',str(ROOT/'tests/fixtures'/f'{case}.json.gz')]+list(extra),capture_output=True,text=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),cwd=ROOT)
 def test_entry_accounts_reminder(self):
  r=self.cli('monitor.py');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  for text in ['Roth (TQQQ plan): sell USFR, buy QLD','Brokerage (TQQQ plan): sell USFR, buy TQQQ','SYNTHETIC','TRADE TODAY','Reminder: keep dividend reinvestment OFF in both Roth IRAs']:self.assertIn(text,r.stdout)
  self.assertNotIn('MODEL target',r.stdout)
 def test_final_check_reports_changed_or_unchanged(self):
  r=self.cli('monitor.py');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  key=[x for x in r.stdout.split('\n') if x.startswith('TOP7_KEY: ')][0][len('TOP7_KEY: '):]
  self.assertIn('NASDAQ_PRELIMINARY: no',r.stdout);self.assertNotIn('FINAL_CHECK',r.stdout)
  same=self.cli('monitor.py',extra=['--expect-top7-key',key]);self.assertIn('FINAL_CHECK: UNCHANGED',same.stdout)
  diff=self.cli('monitor.py',extra=['--expect-top7-key','top7_roth=SP500|->-|ok;top7_brok=SP500|->-|ok']);self.assertIn('FINAL_CHECK: CHANGED',diff.stdout)
 def test_cushion_and_guard(self):
  r=self.cli('monitor.py','cushion');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  for text in ['Roth (TQQQ plan): sell QLD, buy TQQQ','Roth (UPRO plan): sell SSO, buy UPRO']:self.assertIn(text,r.stdout)
  r=self.cli('monitor.py','guard');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  self.assertIn('Brokerage (TQQQ plan): sell TQQQ, buy IEF',r.stdout);self.assertNotIn('UPRO plan',r.stdout)
 def test_midday_preserves_same_day_timing(self):
  r=self.cli('moc_reminder.py',extra=['--now','2026-11-30T12:28:00-05:00']);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  self.assertIn('No model market-on-close instruction is due',r.stdout);self.assertNotIn('Roth: buy QLD',r.stdout)
 def test_early_close_cutoff_and_post_gate(self):
  for script,clock in [('moc_reminder.py','2026-11-27T12:50:00-05:00'),('monitor.py','2026-11-27T13:01:00-05:00')]:
   r=self.cli(script,extra=['--now',clock]);self.assertEqual(r.returncode,2)
  r=self.cli('monitor.py',extra=['--now','2026-11-27T13:03:00-05:00']);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
 def test_tampered_bundle_rejected(self):
  with tempfile.TemporaryDirectory(dir=ROOT) as folder:
   with self.assertRaisesRegex(ValueError,'hash mismatch'):mon.verified_bundle(folder)
 def test_midday_wrapper_integrity(self):
  self.assertIn(hashlib.sha256((ROOT/'monitor/monitor.py').read_bytes()).hexdigest(),(ROOT/'monitor/moc_reminder.py').read_text())
 def test_structured_early_close_moc_and_top7_warning(self):
  import joint_runtime as rt
  root,bot=mon.verified_bundle(ROOT);facts=rt.load_facts();facts['taxable_loss_sales']=[dict(symbol='MU',date='2026-11-09',taxable=True,gain=-1)]
  payload=dict(text='MODEL report',facts=facts,signal_date='2026-11-25',moc_orders=[dict(account='Roth',sell='SP500',buy='TOP-7 STOCKS',sleeve='Top-7',tickers=['MU'])])
  with patch.object(mon,'verified_bundle',return_value=(root,bot)),patch.object(bot,'main',return_value=payload) as run,contextlib.redirect_stdout(io.StringIO()) as out:
   code=mon.main('post',['--repo',str(ROOT),'--cache-dir',str(ROOT/'.test_cache'),'--fixture',str(ROOT/'tests/fixtures/entry.json.gz'),'--now','2026-11-25T17:30:00-05:00'])
  self.assertEqual(code,0)
  for text in ['MOC_REMINDER_AT: 2026-11-27T11:20:00-05:00','12:50 ET','WASH-SALE WARNING: MU']:self.assertIn(text,out.getvalue())
  self.assertTrue(run.call_args.kwargs['dry_run']);self.assertFalse(run.call_args.kwargs['persist'])
 def test_midday_shared_reminder(self):
  import joint_runtime as rt
  root,bot=mon.verified_bundle(ROOT);facts=rt.load_facts();payload=dict(text='MODEL',facts=facts,signal_date='2026-11-27',moc_orders=[dict(account='Katelyn Roth IRA',sell='USFR',buy='QLD',sleeve='test')])
  with patch.object(mon,'verified_bundle',return_value=(root,bot)),patch.object(bot,'main',return_value=payload),contextlib.redirect_stdout(io.StringIO()) as out:
   code=mon.main('moc',['--repo',str(ROOT),'--cache-dir',str(ROOT/'.test_cache'),'--fixture',str(ROOT/'tests/fixtures/entry.json.gz'),'--now','2026-11-30T12:28:00-05:00'])
  self.assertEqual(code,0);self.assertIn(rt.ROTH_BUY_REMINDER,out.getvalue())

 def test_owner_state_substitutes_reach_shared_engine(self):
  import joint_runtime as rt,json
  root,bot=mon.verified_bundle(ROOT)
  with tempfile.TemporaryDirectory(dir=ROOT) as folder:
   state=Path(folder)/'state.json';state.write_text(json.dumps({'substitutes':{'TEST':'PEER'}}))
   with patch.object(mon,'verified_bundle',return_value=(root,bot)),patch.object(bot,'main',return_value=None) as run,contextlib.redirect_stdout(io.StringIO()):
    code=mon.main('post',['--repo',str(ROOT),'--cache-dir',str(ROOT/'.test_cache'),'--fixture',str(ROOT/'tests/fixtures/entry.json.gz'),'--state-file',str(state)])
   self.assertEqual(run.call_args.kwargs['facts_override']['substitutes'],{'TEST':'PEER'})
 def test_revised_provenance_in_both_wrapper_messages(self):
  import gzip,json
  from joint_runtime import UNEMPLOYMENT_REVISED_LINE
  raw=json.loads(gzip.open(ROOT/'tests/fixtures/guard.json.gz','rt').read());raw['unemployment_first_release']=False
  raw['unemployment'].pop('release_date',None)
  with tempfile.TemporaryDirectory(dir=ROOT) as folder:
   fixture=Path(folder)/'revised.json';fixture.write_text(json.dumps(raw))
   for script,extra in [('monitor.py',[]),('moc_reminder.py',['--now','2026-11-30T12:28:00-05:00'])]:
    r=self.cli(script,extra=['--fixture',str(fixture)]+extra)
    self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertIn(UNEMPLOYMENT_REVISED_LINE if script=='moc_reminder.py' else 'used revised unemployment figures',r.stdout)
    self.assertNotIn('DATA NOT READY',r.stdout)
