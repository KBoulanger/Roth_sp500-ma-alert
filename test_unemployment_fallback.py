"""Review 1: keep reporting with baseline revised data and explicit provenance."""
from pathlib import Path
from unittest.mock import patch,Mock
from html import unescape
import json,os,tempfile,unittest
import pandas as pd
import send_spy_ma_telegram as bot
import joint_runtime as rt

class UnemploymentFallbackTests(unittest.TestCase):
 def revised(self):return pd.DataFrame({'DATE':pd.to_datetime(['2026-07-01','2026-08-01','2026-09-01','2026-10-01']),'UNRATE':[4.,4.,4.,4.2]})
 def run_revised_report(self,un):
  b=rt.load_data_bundle(Path(__file__).parent/'tests/fixtures/guard.json.gz');b['unemployment']=un
  with patch.object(bot,'send_telegram') as send,patch.object(bot,'save_state') as save:
   out=bot.main(pd.Timestamp(b['clock']).to_pydatetime(),dry_run=True,persist=False,data_bundle=b,facts_override=b['facts'],state_override={})
  send.assert_not_called();save.assert_not_called()
  self.assertIn(rt.UNEMPLOYMENT_REVISED_LINE,unescape(out['text']))
  self.assertEqual(out['unemployment_data_line'],rt.UNEMPLOYMENT_REVISED_LINE)
  self.assertIn('Brokerage: buy IEF / sell TQQQ',out['text'])
  self.assertTrue(out['joint']);return out
 def test_no_key_original_csv_fallback_and_report_warning(self):
  with patch.dict(os.environ,{},clear=True),patch.object(bot,'fetch_unrate_fallback',return_value=self.revised()) as csv,patch.object(bot,'fetch_unrate_fred_api') as first,patch.object(bot,'fetch_unrate_latest_api') as latest:
   un=bot.fetch_unrate();csv.assert_called_once();first.assert_not_called();latest.assert_not_called()
  self.run_revised_report(un)
 def test_first_release_error_uses_original_latest_api(self):
  with patch.dict(os.environ,{'FRED_API_KEY':'test-key'},clear=True),patch.object(bot,'fetch_unrate_fred_api',side_effect=RuntimeError('API failed')),patch.object(bot,'fetch_unrate_latest_api',return_value=self.revised()) as latest,patch.object(bot,'fetch_unrate_fallback') as csv:
   un=bot.fetch_unrate();latest.assert_called_once_with('test-key');csv.assert_not_called()
  self.run_revised_report(un)
 def test_original_latest_error_uses_original_csv(self):
  with patch.dict(os.environ,{'FRED_API_KEY':'test-key'},clear=True),patch.object(bot,'fetch_unrate_fred_api',side_effect=RuntimeError()),patch.object(bot,'fetch_unrate_latest_api',side_effect=RuntimeError()),patch.object(bot,'fetch_unrate_fallback',return_value=self.revised()) as csv:
   un=bot.fetch_unrate();csv.assert_called_once();self.assertFalse(un.attrs['first_release'])
 def test_verified_snapshot_wins_without_network(self):
  raw={'first_release':True,'as_of':'2026-11-27','observations':[dict(date=str(r.DATE.date()),value=r.UNRATE,release_date=str((r.DATE+pd.offsets.MonthBegin(1)+pd.Timedelta(days=5)).date())) for r in self.revised().itertuples()]}
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   p=Path(folder)/'snapshot.json';p.write_text(json.dumps(raw))
   with patch.dict(os.environ,{'UNEMPLOYMENT_SNAPSHOT':str(p),'FRED_API_KEY':'test-key'},clear=True),patch.object(bot.requests,'get') as get,patch.object(pd,'read_csv') as csv:
    un=bot.fetch_unrate();get.assert_not_called();csv.assert_not_called()
  self.assertEqual(rt.unemployment_data_line(un),'Unemployment data: first-release.')
  b=rt.load_data_bundle(Path(__file__).parent/'tests/fixtures/entry.json.gz');b['unemployment']=un
  with patch.object(bot,'send_telegram'),patch.object(bot,'save_state'):
   out=bot.main(pd.Timestamp(b['clock']).to_pydatetime(),dry_run=True,persist=False,data_bundle=b,facts_override=b['facts'],state_override={})
  self.assertIn('Unemployment data: first-release.',out['text']);self.assertNotIn('revised figures',out['text'])
 def test_bad_snapshot_and_short_first_release_advance_fallback(self):
  with patch.dict(os.environ,{'UNEMPLOYMENT_SNAPSHOT':'missing','FRED_API_KEY':'test-key'},clear=True),patch.object(bot,'snapshot_unemployment',side_effect=ValueError()),patch.object(bot,'fetch_unrate_fred_api',return_value=self.revised().iloc[:3]),patch.object(bot,'fetch_unrate_latest_api',return_value=self.revised()) as latest:
   self.assertFalse(bot.fetch_unrate().attrs['first_release']);latest.assert_called_once()
 def test_first_release_api_success_skips_revised(self):
  with patch.dict(os.environ,{'FRED_API_KEY':'test-key'},clear=True),patch.object(bot,'fetch_unrate_fred_api',return_value=self.revised()),patch.object(bot,'fetch_unrate_latest_api') as latest,patch.object(bot,'fetch_unrate_fallback') as csv:
   self.assertTrue(bot.fetch_unrate().attrs['first_release']);latest.assert_not_called();csv.assert_not_called()
 def test_all_sources_fail_remains_not_ready(self):
  with patch.dict(os.environ,{'UNEMPLOYMENT_SNAPSHOT':'missing','FRED_API_KEY':'test-key'},clear=True),patch.object(bot,'snapshot_unemployment',side_effect=ValueError()),patch.object(bot,'fetch_unrate_fred_api',side_effect=RuntimeError()),patch.object(bot,'fetch_unrate_latest_api',return_value=self.revised().iloc[:2]),patch.object(bot,'fetch_unrate_fallback',side_effect=RuntimeError()):
   with self.assertRaisesRegex(RuntimeError,'All unemployment sources unavailable'):bot.fetch_unrate()
 def test_baseline_revised_request_and_csv_contract(self):
  response=Mock();response.json.return_value={'observations':[dict(date=str(r.DATE.date()),value=str(r.UNRATE)) for r in self.revised().itertuples()]}
  with patch.object(bot.requests,'get',return_value=response) as get:un=bot.fetch_unrate_latest_api('key')
  self.assertEqual(get.call_args.kwargs['params'],dict(series_id='UNRATE',api_key='key',file_type='json',sort_order='desc',limit=12))
  with patch.object(pd,'read_csv',return_value=self.revised().rename(columns={'DATE':'observation_date'})) as read:csv=bot.fetch_unrate_fallback()
  read.assert_called_once_with('https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE');self.assertEqual(len(csv),4)
 def test_warning_in_each_telegram_chunk_and_midday_lines(self):
  note=rt.UNEMPLOYMENT_REVISED_LINE
  text='Unemployment data: revised figures (first-release unavailable) — IEF/USFR and IEF/S&amp;P routing may differ slightly from the tested rule.\n'+'x'*3700+'\nRoth: buy QLD\n'+rt.ROTH_BUY_REMINDER
  for chunk in bot.telegram_chunks(text):
   self.assertIn(note,unescape(chunk));self.assertLessEqual(len(chunk.encode('utf-16-le'))//2,3900)
   if 'Roth: buy' in chunk:self.assertIn('Roth: buy QLD\n'+rt.ROTH_BUY_REMINDER,chunk)
  self.assertIn(note,rt.render_moc_orders([],{},'2026-11-30','15:50',note))
 def test_revised_history_does_not_invent_release_dates(self):
  self.assertTrue(rt.first_release_delta_history(self.revised(),pd.date_range('2026-11-01',periods=3)).isna().all())

 def test_revised_monitor_retains_report_if_top7_inputs_incomplete(self):
  b=rt.load_data_bundle(Path(__file__).parent/'tests/fixtures/guard.json.gz');b['unemployment']=self.revised();b['basket_config']=None;b['nasdaq']=b['nasdaq'].iloc[:0]
  with patch.object(bot,'send_telegram') as send,patch.object(bot,'save_state') as save:
   out=bot.main(pd.Timestamp(b['clock']).to_pydatetime(),dry_run=True,persist=False,monitor_mode=True,data_bundle=b,facts_override=b['facts'],state_override={})
  self.assertIn(rt.UNEMPLOYMENT_REVISED_LINE,unescape(out['text']));self.assertTrue(out['joint'])
  self.assertIsNone(out['state']['_meta']['source_dates']['TOP7_BASKET']);self.assertIsNone(out['state']['_meta']['source_dates']['NASDAQCOM'])
  send.assert_not_called();save.assert_not_called()
