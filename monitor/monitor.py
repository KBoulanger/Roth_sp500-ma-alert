"""Read-only scheduled portfolio report. No broker access, delivery or state writes.
Code is immutable and hash verified; owner observations/state are separate data.
"""
from pathlib import Path
import argparse,hashlib,importlib.util,json,os,re,sys,urllib.request
import pandas as pd
import exchange_calendars as xc

REVISION = '142c87287f6d785d9ddec74e6381ef23954b2051'
PINS = {'send_spy_ma_telegram.py': '1d2ce57a25178a23662c98efb9ff3ed9e51d18c2aa6e1751448e671b22fe9c09', 'short_report.py': '1e78c15eaedfaf5afa15eef87c6ff6c0ee63dafa68feeea067d334cc37efe34f', 'joint_strategy.py': '883aa31a949d3877715688f1579b46aebd5253a197853182c8af14fae490fb3b', 'joint_runtime.py': '2be233dbe4599bbf30474183ba2030a24ce0fb131af8629f0cc47ab184c94388', 'joint_state_seed.json': '6b8d7c187434420fc89f8b7240c0c311cc56268f0a6fb196c93584fd9e2f784b', 'top7_basket.json': '622870e683162a30656356cfa493368b5498505c1f8eef435be2cc032ca8cb78'}
RAW = 'https://raw.githubusercontent.com/KBoulanger/Roth_sp500-ma-alert/'

def verified_bundle(repo=None,cache=None):
    root=Path(repo).resolve() if repo else Path(cache or Path(__file__).with_name('.verified_bundle')).resolve()
    if not repo:root.mkdir(parents=True,exist_ok=True)
    for name,digest in PINS.items():
        path=root/name
        if not repo and (not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest):
            with urllib.request.urlopen(RAW+REVISION+'/'+name,timeout=30) as r:content=r.read()
            if hashlib.sha256(content).hexdigest()!=digest:raise ValueError('Code/data hash mismatch: '+name)
            path.write_bytes(content)
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Code/data hash mismatch: '+name)
    sys.path.insert(0,str(root))
    spec=importlib.util.spec_from_file_location('scheduled_joint_bot',root/'send_spy_ma_telegram.py')
    bot=importlib.util.module_from_spec(spec);spec.loader.exec_module(bot)
    bot.TOP7_RATCHET_PARAMS = dict(bot.TOP7_RATCHET_PARAMS, basket_file=str(root/'top7_basket.json'))
    return root,bot

def readable(text):
    from html import unescape
    return unescape(re.sub(r'</?(?:b|i|code|pre)>','',text))

def main(mode='post',argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',help='Local audited bundle (all pinned hashes must match)')
    p.add_argument('--fixture',help='Explicit synthetic/offline bundle; never live data')
    p.add_argument('--now',help='Explicit ISO report clock')
    p.add_argument('--spouse-export');p.add_argument('--seed');p.add_argument('--state-file')
    p.add_argument('--unemployment-json');p.add_argument('--cache-dir')
    p.add_argument('--expect-top7-key',help='Final check: Top-7 key printed by the earlier run; reports whether it changed')
    p.add_argument('facts',nargs='*',help='LOSS=SYM@ISO@gain@account; optional SYMBOL=raw-close@date cross-check')
    args=p.parse_args(argv)
    # Cache destinations are explicit so scheduled execution never writes to an unrelated cwd.
    cache=Path(args.cache_dir or Path(__file__).with_name('.market_cache')).resolve()
    cache.mkdir(parents=True,exist_ok=True)
    import yfinance as yf
    yf.set_tz_cache_location(str(cache))
    root,bot=verified_bundle(args.repo,cache/'code')
    from joint_runtime import load_facts,load_data_bundle,blocked_preview,render_moc_orders,normalize_substitutes,merge_observed_facts
    if args.unemployment_json:os.environ['UNEMPLOYMENT_SNAPSHOT']=args.unemployment_json
    os.environ['MANUAL_RUN']='true';os.environ['FINAL_ATTEMPT']='true';os.environ['FORCE_PARTIAL_DATA']='false';os.environ['FORCE_REPEAT']='true'
    losses=[];quotes={}
    for value in args.facts:
        key,payload=value.split('=',1)
        if key.upper()=='LOSS':losses.append(payload)
        else:
            price,day=payload.split('@');quotes[key.upper()]=(float(price),day)
    bundle=load_data_bundle(args.fixture) if args.fixture else None
    facts=load_facts(args.seed or root/'joint_state_seed.json',args.spouse_export,losses)
    if bundle and bundle.get('facts') and not (args.seed or args.spouse_export or losses):facts=bundle['facts']
    # Current owner state is DATA, never executable code. Explicit path wins; remote
    # runs fetch current main state for confirmed observations/substitute preferences.
    if args.state_file:state=json.loads(Path(args.state_file).read_text())
    elif args.repo:state=json.loads((root/'state.json').read_text())
    elif bundle:state={}
    else:
        with urllib.request.urlopen(RAW+'main/state.json',timeout=30) as response:state=json.load(response)
    if not isinstance(state,dict):raise ValueError('State must be a JSON object')
    if not bundle or args.state_file:
        facts['substitutes']=normalize_substitutes(state.get('substitutes',facts.get('substitutes',{})))
    if not bundle:facts=merge_observed_facts(facts,(state.get('_joint') or {}).get('observed_facts'))
    normalize_substitutes(state.get('substitutes',{}))
    now=pd.Timestamp(args.now or (bundle.get('clock') if bundle else pd.Timestamp.now(tz='America/New_York')))
    if now.tzinfo is None:raise ValueError('Report clock needs a timezone')
    now=now.tz_convert('America/New_York');cal=xc.get_calendar('XNYS');today=pd.Timestamp(now.date())
    label='SYNTHETIC/OFFLINE' if bundle else 'PUBLIC-DATA READ-ONLY'
    print(label+' '+mode+' monitor at '+now.isoformat())
    if bundle and not bundle.get('data_ready',True):
        print(blocked_preview(facts,bundle.get('blocked_reason','data unavailable'),now.isoformat()));return 2
    if not cal.is_session(today):print('MARKET CLOSED: no session today.');return 0
    close=cal.session_close(today).tz_convert('America/New_York')
    cutoff=close-pd.Timedelta(minutes=10)
    if mode=='post' and now<close+pd.Timedelta(minutes=2):
        print(blocked_preview(facts,'Too early: session closes '+str(close)+'; wait two minutes for publication.',now.isoformat()));return 2
    if mode=='moc' and now>=cutoff:
        print('MOC CUTOFF PASSED: no submit-now instruction; confirm any existing order manually.');return 2
    if mode=='moc':
        previous=cal.previous_session(today)
        calc=cal.session_close(previous).tz_convert('America/New_York')+pd.Timedelta(minutes=30)
    else:calc=now
    # No delivery and no persistence are reachable through these wrappers.
    def forbidden(*a,**k):raise AssertionError('Scheduled preview cannot deliver or persist')
    bot.send_telegram=forbidden;bot.save_state=forbidden
    try:
        output=bot.main(current_et=calc.to_pydatetime(),dry_run=True,persist=False,monitor_mode=True,
            data_bundle=bundle,facts_override=facts,state_override=state,publication_grace_minutes=2,historical_only=mode=='moc')
        if not output:raise ValueError('Required completed-session inputs unavailable')
        # Supplied broker close observations only verify raw prices, never replace adjusted history.
        for symbol,(price,day) in quotes.items():
            if symbol not in ('SPY','QQQ'):raise ValueError('Unsupported close cross-check '+symbol)
            if day!=output['signal_date']:raise ValueError(symbol+' supplied close date mismatch')
            frame=bundle['prices'][symbol] if bundle else bot.fetch_etf_yfinance(symbol)
            raw=float(frame.loc[pd.Timestamp(day),'RawClose'])
            if abs(raw-price)>max(.01,abs(price)*.0001):raise ValueError(symbol+' supplied raw close mismatch')
        orders=output.get('moc_orders',[])
        if mode=='moc':
            text='\n'.join(render_moc_orders(orders,output['facts'],now.date(),cutoff.strftime('%H:%M'),output.get('unemployment_data_line')))
            text='Based on prior completed signal close '+output['signal_date']+'. Confirm actual fills since that close.\n'+text
        else:text=output.get('short_text') or output['text']
        print('MESSAGE START');print(readable(text));print('MESSAGE END')
        if mode=='post':
            preliminary=bool(output.get('nasdaq_preliminary'));key=output.get('top7_key','')
            print('TOP7_KEY: '+key)
            print('NASDAQ_PRELIMINARY: '+('yes' if preliminary else 'no'))
            if preliminary and 'guarded' not in key:
                print('FINAL_CHECK_AT: '+(close+pd.Timedelta(minutes=81)).isoformat())
            if args.expect_top7_key is not None:
                print('FINAL_CHECK: '+('NOT FINAL YET' if preliminary or 'guarded' in key else 'UNCHANGED' if key==args.expect_top7_key else 'CHANGED'))
        if mode=='post' and orders:
            nxt=cal.next_session(today);nextclose=cal.session_close(nxt).tz_convert('America/New_York');deadline=nextclose-pd.Timedelta(minutes=10)
            reminder=pd.Timestamp(str(nxt.date())+' 12:28',tz='America/New_York')
            reminder=min(reminder,deadline-pd.Timedelta(minutes=90))
            print('MOC_REMINDER_AT: '+reminder.isoformat())
            print('MOC_REMINDER_TEXT START');print('\n'.join(render_moc_orders(orders,output['facts'],nxt.date(),deadline.strftime('%H:%M'),output.get('unemployment_data_line'))));print('MOC_REMINDER_TEXT END')
        return 0
    except Exception as exc:
        print(blocked_preview(facts,str(exc),now.isoformat()));return 2

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print('DATA NOT READY: '+str(exc)+'; no trade instruction.');sys.exit(2)
