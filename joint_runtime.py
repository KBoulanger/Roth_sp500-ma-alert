"""Read-only facts adapters and shared rendering for the bot and both monitor scripts.

No broker SDK, order endpoint, account mutation, or Telegram delivery lives here.
The export contract uses tax_cost_basis as total USD for an open lot; tax_cost_per_share
can be supplied explicitly. average_buy_price is never substituted for adjusted tax cost.
"""
from copy import deepcopy
from datetime import date,timedelta
from pathlib import Path
from html import escape
import json,re
import numpy as np
import pandas as pd
from joint_strategy import StrategyData,POLICIES,SYMBOLS,replay_joint,defensive_holding

SEED=Path(__file__).with_name('joint_state_seed.json')
FUNDS=('TQQQ','QLD','UPRO','SSO','IEF','USFR')
ROTH_BUY_REMINDER="Reminder: keep dividend reinvestment OFF in both Roth IRAs (yours and Katelyn's). Check it is still off before you buy."

def et_date(value):
    p=pd.Timestamp(value)
    if p.tzinfo is not None:return p.tz_convert('America/New_York').date()
    if 'T' in str(value) or ' ' in str(value):raise ValueError('Timestamp must have a timezone')
    return p.date() # explicitly supplied trade date in ET

def loss_argument(text):
    symbol,stamp,gain,account=text.split('@',3)
    if re.search(r'\d{6,}',account):raise ValueError('Use a label or last four digits, not a full account number')
    gain=float(gain)
    if not np.isfinite(gain) or gain>=0:return None
    return dict(symbol=symbol.upper(),date=et_date(stamp).isoformat(),gain=gain,account=account.replace('_',' '),taxable=True,source='real caller-supplied taxable loss')

def open_wash_windows(symbol,on,losses):
    on=et_date(on)
    return [x for x in losses if x.get('taxable') is True and x['symbol'].upper()==symbol.upper() and 0<=(on-et_date(x['date'])).days<=30]

def wash_text(symbol,on,losses):
    hits=open_wash_windows(symbol,on,losses)
    if not hits:return f'{symbol}: no supplied taxable loss in prior 30 days (coverage may be incomplete)'
    return '; '.join(f"{symbol}: taxable loss {x['date']} ({x.get('account',x.get('account_last4','household'))}); Roth/IRA purchase window through {(et_date(x['date'])+timedelta(days=30)).isoformat()}; day 31 {(et_date(x['date'])+timedelta(days=31)).isoformat()}. Taxable replacements may defer the loss." for x in hits)

def _account_last4(x):
    v=str(x.get('account_last4',''))
    if v and not re.fullmatch(r'\d{4}',v):raise ValueError('Export account identifiers must be last four only')
    return v

def _trade_dates(t):
    executions=t.get('executions') or []
    ds=[]
    for e in executions:
        if float(e.get('quantity') or 0)>0:
            stamp=e.get('timestamp_utc') or e.get('timestamp')
            if stamp:ds.append(et_date(stamp))
    stated=t.get('trade_date_et') or t.get('date')
    if ds:
        if stated and et_date(stated) not in ds:raise ValueError('Export ET trade date disagrees with execution timestamp')
        return sorted(set(ds))
    if stated:return [et_date(stated)]
    stamp=t.get('last_transaction_at_utc')
    return [et_date(stamp)] if stamp else []

def load_facts(seed_file=SEED,export_file=None,extra_losses=()):
    seed=json.loads(Path(seed_file).read_text()) if seed_file else dict(schema_version=1,sleeves={},taxable_loss_sales=[],source='MODEL only; no observed holdings')
    facts=deepcopy(seed);losses=list(facts.get('taxable_loss_sales',[]));notes=[]
    for x in extra_losses:
        item=loss_argument(x) if isinstance(x,str) else x
        if item and item.get('taxable') is True:
            gain=item.get('gain')
            if gain is None and item.get('classification')!='loss':raise ValueError('A loss classification or negative gain is required')
            if gain is None or float(gain)<0:losses.append(item)
    if export_file and not Path(export_file).exists():
        notes.append('Spouse export not present; using dated fixed snapshot, not live holdings');export_file=None
    if export_file:
        export=json.loads(Path(export_file).read_text());stamp=export.get('fetched_at')
        if not stamp:raise ValueError('Export fetched_at is required')
        asof=et_date(stamp).isoformat();accounts={_account_last4(a):a for a in export.get('accounts',[])}
        positions=export.get('positions',[]);lots=export.get('open_lots',[]);tx=export.get('transactions',[])
        for a in list(accounts.values())+positions+lots+tx+export.get('loss_sales',[]):_account_last4(a)
        def taxable(t):
            value=t.get('taxable',accounts.get(_account_last4(t),{}).get('taxable'))
            if value is not None and not isinstance(value,bool):raise ValueError('taxable must be a boolean')
            return value
        # Export loss_sales is the exporter's classified list; original RH gain is preferred.
        for t in export.get('loss_sales',[])+[t for t in tx if t.get('side','').lower()=='sell']:
            gain=t.get('realized_gain_rh',t.get('gain',t.get('realized_gain')))
            classified=t in export.get('loss_sales',[])
            if taxable(t) is not True or (gain is None and not classified) or (gain is not None and float(gain)>=0):continue
            ds=_trade_dates(t)
            if not ds:raise ValueError('Taxable loss has no ET trade date or execution timestamp')
            for d in ds:losses.append(dict(symbol=t['symbol'].upper(),date=d.isoformat(),gain=None if gain is None else float(gain),account_last4=_account_last4(t),taxable=True,source='real spouse export classified loss'))
        explicit=export.get('strategy_facts') or export.get('sleeves')
        if explicit:
            for sleeve,item in explicit.items():
                if sleeve in POLICIES:facts.setdefault('sleeves',{})[sleeve]=dict(deepcopy(item),as_of=item.get('as_of',asof))
        else:
            # Read holdings and total adjusted tax basis; do not guess a split between cash sleeves.
            for sleeve,mapping in SYMBOLS.items():
                old=deepcopy(facts.setdefault('sleeves',{}).get(sleeve,{}));old['as_of']=asof
                for key,last4 in [('roth','6134'),('brokerage','1514')]:
                    ps=[p for p in positions if _account_last4(p)==last4 and float(p.get('quantity') or 0)>0]
                    equity=[p for p in ps if p.get('symbol') in [mapping[1],mapping[2]]]
                    defensive=[p for p in ps if p.get('symbol') in ['IEF','USFR']]
                    chosen=equity[0] if len(equity)==1 else defensive[0] if not equity and len(defensive)==1 else None
                    item=old.setdefault(key,{})
                    item.update(account_last4=last4,holding=chosen.get('symbol') if chosen else None,source='real export positions' if chosen else 'export does not uniquely identify sleeve holding')
                    if chosen:
                        q=float(chosen['quantity']);ls=[l for l in lots if _account_last4(l)==last4 and l.get('symbol')==chosen['symbol']]
                        lq=sum(float(l.get('quantity') or 0) for l in ls)
                        basis=sum(float(l['tax_cost_basis']) if l.get('tax_cost_basis') is not None else float(l['tax_cost_per_share'])*float(l['quantity']) for l in ls) if ls and all(l.get('tax_cost_basis') is not None or l.get('tax_cost_per_share') is not None for l in ls) else None
                        item.update(quantity=q,tax_cost_per_share=(basis/q if basis is not None and abs(lq-q)<1e-6 else None))
                facts['sleeves'][sleeve]=old
        # Every retirement buy counts, including DRIPs and partial fills; use ET execution dates.
        buys=[]
        for t in tx:
            if t.get('side','').lower()!='buy' or taxable(t) is not False:continue
            filled=float(t.get('cumulative_quantity') or 0)>0 or any(float(e.get('quantity') or 0)>0 for e in t.get('executions',[]))
            if not filled and t.get('state','').lower()!='filled':continue
            for d in _trade_dates(t):buys.append(dict(symbol=t['symbol'].upper(),date=d.isoformat(),account_last4=_account_last4(t),placed_agent=t.get('placed_agent'),source='real export retirement purchase'))
        facts['retirement_purchases']=list(facts.get('retirement_purchases',[]))+buys
        for b in buys:
            if b.get('placed_agent')=='drip' and b['date']>='2026-11-27':notes.append('OBSERVED DRIP after Roth reinvestment was confirmed OFF: real purchase retained for wash monitoring; check the account setting')
        for sleeve,item in facts.get('sleeves',{}).items():
            ds=[p['date'] for p in facts.get('retirement_purchases',[]) if p['symbol']==sleeve and p['account_last4']=='6134']
            if ds:item.setdefault('roth',{})['last_3x_buy']=max(ds)
            ds=[l['date'] for l in losses if l['symbol']==sleeve and l.get('account_last4')=='1514']
            if ds:item.setdefault('brokerage',{})['last_3x_loss']=max(ds)
        facts['as_of']=asof;facts['source']='REAL spouse export plus explicitly supplied household facts'
    dedup={}
    for x in losses:
        if x.get('taxable') is not True:continue
        x=deepcopy(x);x['symbol']=x['symbol'].upper();x['date']=et_date(x['date']).isoformat()
        _account_last4(x)
        if re.search(r'\d{6,}',str(x.get('account',''))):raise ValueError('Use only last four account digits')
        key=(x['symbol'],x['date'],x.get('account_last4',x.get('account')))
        dedup[key]=x
    facts['taxable_loss_sales']=list(dedup.values());facts['notes']=list(facts.get('notes',[]))+notes
    return facts

def snapshot_unemployment(path,on):
    raw=json.loads(Path(path).read_text())
    if raw.get('first_release') is not True:raise ValueError('A verified first-release unemployment snapshot is required')
    if et_date(raw['as_of'])<et_date(on):raise ValueError('First-release unemployment snapshot is older than the signal date')
    rows=raw.get('observations',[])
    if len(rows)<4:raise ValueError('Four first-release observations required')
    for r in rows:
        if et_date(r['release_date'])>et_date(on):raise ValueError('Future unemployment release cannot be used')
    df=pd.DataFrame({'DATE':pd.to_datetime([r['date'] for r in rows]),'UNRATE':[float(r['value']) for r in rows],'release_date':pd.to_datetime([r['release_date'] for r in rows])})
    df.attrs['first_release']=True;return df.sort_values('DATE')

def evaluate_sleeve(sleeve,prices,index,unemp_delta,next_session,facts,start=None,unemployment_history=None):
    # Signals use full adjusted history (QQQ ATH included); returns need only the real common interval.
    symbol3,symbol2=SYMBOLS[sleeve][1],SYMBOLS[sleeve][2]
    common=index.index
    frame=pd.DataFrame({'date':common,'adj_price_index':index.to_numpy(),'unemp_3m_change':np.nan})
    if unemployment_history is not None:
        frame['unemp_3m_change']=unemployment_history.reindex(common).to_numpy()
    frame.loc[len(frame)-1,'unemp_3m_change']=float(unemp_delta)
    # Unknown older releases are never backfilled with today's observation.
    for ticker,col in [(symbol3,'r_tqqq_3x' if sleeve=='TQQQ' else 'r_upro_3x'),(symbol2,'r_qld_2x' if sleeve=='TQQQ' else 'r_sso_2x'),('IEF','r_ief'),('USFR','r_tbill')]:
        s=prices[ticker]['Close'].pct_change(fill_method=None).reindex(common)
        frame[col]=s.to_numpy()
    sf=deepcopy(facts.get('sleeves',{}).get(sleeve))
    data=StrategyData(frame,sleeve,signal_seed=sf)
    data.roth_cash_returns={}
    for slot,ticker in SYMBOLS[sleeve].items():
        if slot not in (1,2,4,5):continue
        p=prices[ticker];raw_close=p.get('RawClose',p['Close']);pr=raw_close.pct_change(fill_method=None);dy=p.get('Dividends',pd.Series(0.,index=p.index))/raw_close.shift(1)
        for d in common:
            if pd.notna(pr.get(d)) and pd.notna(dy.get(d)):data.roth_cash_returns[int(np.datetime64(d,'D').astype(int)),slot]=(float(pr.loc[d]),float(dy.loc[d]))
    data.append_decision_day(next_session)
    valid=frame.dropna(subset=[c for c in frame if c.startswith('r_')])
    if valid.empty:raise ValueError('No common adjusted fund returns for joint replay')
    # A known snapshot is the operational initial condition, not an invented historic fill path.
    sf=deepcopy(facts.get('sleeves',{}).get(sleeve))
    replay_start=start or str(valid.date.iloc[0].date())
    raw={}
    for ticker in [symbol3,symbol2,'IEF','USFR']:
        p=prices[ticker]
        for d,v in p.get('RawClose',p['Close']).items():raw.setdefault(str(pd.Timestamp(d).date()),{})[ticker]=float(v)
    result,ledger,_=replay_joint(data,POLICIES[sleeve],replay_start,str(pd.Timestamp(next_session).date()),ledger=True,liquidate=False,decision_only=True,facts=sf,household_losses=facts.get('taxable_loss_sales',[]),market_prices=raw)
    latest=pd.Timestamp(common[-1]).date();today_events=ledger[ledger.date==latest.isoformat()].to_dict('records')
    result.update(sleeve=sleeve,signal_date=latest.isoformat(),signal_meta={key:data.signal_meta['dip20' if sleeve=='TQQQ' else 'base',e] for key,e in [('roth',1),('brokerage',4 if sleeve=='TQQQ' else 1)]},events=today_events,observed=sf or {},source=facts.get('source','MODEL only'))
    for acct,key in [('R','roth'),('B','brokerage')]:
        model=result['model_accounts'][acct];ticker=model['holding'];p=raw.get(latest.isoformat(),{}).get(ticker)
        model['tax_cost_per_share']=None if p is None or model['wealth']<=0 else p*model['tax_basis']/model['wealth']
        if acct=='R' and ticker==sleeve:
            actual=(sf or {}).get(key,{}).get('holding')
            if actual!=sleeve and open_wash_windows(sleeve,latest,facts.get('taxable_loss_sales',[])):
                model['holding']=symbol2;model['household_purchase_blocked']=True
    return result

def account_plan(result,account,facts):
    key='roth' if account=='R' else 'brokerage';label='Roth' if account=='R' else 'Brokerage';model=result['model_accounts'][account];observed=result['observed'];item=observed.get(key,{})
    actual=item.get('holding');target=model['holding'];on=result['signal_date'];asof=observed.get('as_of')
    if asof and asof>on:instruction=f'{label}: no current trade instruction — completed prices predate the observed snapshot'
    elif actual is None and observed:instruction=f'{label}: confirm current holding before acting; MODEL target {target}'
    elif actual==target:instruction=f'{label}: hold {target} (target matches the observed snapshot; confirm intervening fills)'
    else:instruction=f'{label}: buy {target}' + (f' / sell {actual}' if actual else '') + ' — MODEL plan; real fills are not confirmed'
    if account=='R' and target==result['sleeve'] and open_wash_windows(target,on,facts.get('taxable_loss_sales',[])) and actual!=target:raise AssertionError('A blocked retirement 3x purchase reached the renderer')
    pending=result['signal_meta'][key].get('pending_exit') if result['sleeve']=='UPRO' else False
    timing='same day, minutes after the close (after-hours LIMIT)'
    if pending and target in ['UPRO','SSO']:timing='EXIT scheduled for the next session close (MOC); the current model remains invested until then'
    lines=[f"{result['sleeve']} — {label}",f"OBSERVED {asof or 'none'}: {actual or 'UNKNOWN'} ({item.get('source','no broker facts supplied')})",f'MODEL target at {on}: {target}',instruction]
    if account=='R' and instruction.startswith('Roth: buy ') and target in ('TQQQ','QLD','UPRO','SSO'):lines.append(ROTH_BUY_REMINDER)
    lines.append('Timing: '+timing)
    if account=='R':
        lines.append('Household wash-sale WATCH before any retirement purchase: '+wash_text(target,on,facts.get('taxable_loss_sales',[])))
        lines.append(f"3x eligibility: brokerage cushion {30 if result['sleeve']=='TQQQ' else 10}% and no recent taxable loss; MODEL last brokerage 3x loss {result.get('last_brokerage_3x_loss') or 'none in model'}; MODEL last Roth 3x purchase {result.get('last_roth_3x_buy') or 'none in model'}.")
        if target!=result['sleeve'] and open_wash_windows(result['sleeve'],on,facts.get('taxable_loss_sales',[])):
            lines.append('3x purchase blocked: '+wash_text(result['sleeve'],on,facts.get('taxable_loss_sales',[])))
    else:
        lines.append('Tax cost per share: '+(f"{model['tax_cost_per_share']:.4f}" if model['tax_cost_per_share'] is not None else 'not applicable / unavailable')+' — '+model['cost_source'])
        lines.append('Brokerage 3x rebuys are permitted; its internal loss is deferred, not a reason to buy the 2x fund.')
    if result.get('basis_unknown'):lines.append('Required adjusted tax cost is unknown: Roth cushion eligibility is not confirmed; use the 2x route until tax-lot facts arrive.')
    if result['sleeve']=='TQQQ':
        s=result['signal_meta'][key];lines.append(f"Dip: armed={s['armed']}, active={s['active']}; failing closes={s['failing_closes']}")
    return lines

def render_joint(result,account,facts,html=False):
    lines=account_plan(result,account,facts)
    return [escape(x,quote=False) for x in lines] if html else lines


def first_release_delta_history(observations,index):
    """Use initial values on/after publication, never backdate to the observation month."""
    if 'release_date' not in observations:return pd.Series(np.nan,index=index)
    u=observations.copy().sort_values('DATE');u['change']=u.UNRATE.diff(3)
    u=u.dropna(subset=['change']).sort_values(['release_date','DATE']).drop_duplicates('release_date',keep='last')
    if u.empty:return pd.Series(np.nan,index=index)
    return pd.Series(u.change.to_numpy(),index=pd.DatetimeIndex(u.release_date).normalize()).reindex(pd.DatetimeIndex(index),method='ffill')

def render_order(account,sell,buy,when,losses,on):
    retirement='roth' in account.lower() or 'ira' in account.lower()
    if retirement and buy in ('TQQQ','UPRO') and open_wash_windows(buy,on,losses):buy={'TQQQ':'QLD','UPRO':'SSO'}[buy]
    lines=[f'{account}: sell {sell} / buy {buy} — {when}']
    if 'roth' in account.lower() and buy in ('TQQQ','QLD','UPRO','SSO'):lines.append(ROTH_BUY_REMINDER)
    if retirement:lines.append('Household wash-sale WATCH: '+wash_text(buy,on,losses))
    return lines


def load_data_bundle(path):
    """Explicit offline input. A not-ready receipt yields a blocked preview, never an instruction."""
    import gzip
    raw=json.loads(gzip.open(path,'rt').read() if str(path).endswith('.gz') else Path(path).read_text());bundle=dict(raw)
    bundle['prices']={t:pd.DataFrame({'Close':v['adjusted'],'RawClose':v.get('raw',v['adjusted']),'Dividends':v.get('dividends',[0.]*len(v['dates']))},index=pd.to_datetime(v['dates'])) for t,v in raw.get('prices',{}).items()}
    if 'nasdaq' in raw:bundle['nasdaq']=pd.DataFrame({'NASDAQCOM':raw['nasdaq']['values']},index=pd.to_datetime(raw['nasdaq']['dates']))
    if 'basket' in raw:bundle['basket']=pd.DataFrame(raw['basket']['values'],index=pd.to_datetime(raw['basket']['dates']))
    if 'unemployment' in raw:
        bundle['unemployment']=pd.DataFrame(raw['unemployment']);bundle['unemployment']['DATE']=pd.to_datetime(bundle['unemployment']['DATE']);bundle['unemployment']['release_date']=pd.to_datetime(bundle['unemployment']['release_date'])
    return bundle


def blocked_preview(facts,reason,clock):
    lines=[f'READ-ONLY PREVIEW — {clock}','DATA NOT READY: '+reason,'No current trade instruction; no delivery, account mutation or assumed real fill.',f"Observed source: {facts.get('source','unknown')}"]
    for sleeve,item in facts.get('sleeves',{}).items():
        for key,label in [('roth','Roth'),('brokerage','Brokerage')]:lines.append(f"{sleeve} {label}: observed {item.get(key,{}).get('holding') or 'UNKNOWN'} as of {item.get('as_of')}; model target unavailable")
    return '\n'.join(lines)


def moc_orders_from_payload(payload):
    """Only next-close instructions; same-day entries/cushion swaps are never silently moved to MOC."""
    return payload.get('moc_orders',[])


def render_moc_orders(orders,facts,on,cutoff):
    lines=[f'MODEL MOC reminder for {on}; submit before {cutoff} ET only after confirming holdings/fills.']
    if not orders:lines.append('No model market-on-close instruction is due. Same-day leveraged entries/swaps are not MOC orders.')
    for o in orders:
        lines.extend(render_order(o['account'],o['sell'],o['buy'],'next-session MOC',facts.get('taxable_loss_sales',[]),on))
        if o.get('sleeve')=='Top-7' and o['buy']=='TOP-7 STOCKS':
            role='roth' if 'roth' in o['account'].lower() else 'brokerage'
            identity=(facts.get('top7_accounts') or {}).get(role) or {}
            if isinstance(identity,str):identity={'label':identity}
            lines.extend(top7_buy_warnings(identity.get('label',o['account']),o.get('tickers',[]),on,facts,identity.get('account_last4'),role=='brokerage'))
    lines.extend(top7_rebalance_warnings(facts))
    return lines


def cached_observed_facts(facts):
    """Retain dated observations, not model fills or account sizes, in public bot state."""
    out=deepcopy(facts)
    for item in out.get('sleeves',{}).values():
        for key in ['roth','brokerage']:
            item.get(key,{}).pop('quantity',None)
    for field in ['taxable_loss_sales','retirement_purchases']:
        for item in out.get(field,[]):
            item.pop('quantity',None)
            if 'gain' in item:item['gain']=None;item['classification']='loss'
    return out


def merge_observed_facts(current,cached):
    if not cached:return current
    out=deepcopy(current)
    for sleeve,item in cached.get('sleeves',{}).items():
        if item.get('as_of','')>out.get('sleeves',{}).get(sleeve,{}).get('as_of',''):
            out.setdefault('sleeves',{})[sleeve]=deepcopy(item)
    losses={}
    for item in list(current.get('taxable_loss_sales',[]))+list(cached.get('taxable_loss_sales',[])):
        losses[item['symbol'],item['date'],item.get('account_last4',item.get('account'))]=item
    out['taxable_loss_sales']=list(losses.values());return out


def top7_buy_warnings(account,tickers,on,facts,account_id=None,taxable=None):
    """Text only. Never replace a basket constituent or alter a signal/weight."""
    retirement=(taxable is False) or 'roth' in account.lower() or 'ira' in account.lower()
    on=et_date(on);lines=[];mapping={str(k).upper():str(v).strip() for k,v in facts.get('substitutes',{}).items() if v}
    for ticker in tickers:
        ticker=ticker.upper();hits=open_wash_windows(ticker,on,facts.get('taxable_loss_sales',[]))
        if not hits:continue
        if retirement:
            latest=max(hits,key=lambda x:et_date(x['date']));safe=(et_date(latest['date'])+timedelta(days=31)).isoformat()
            choice=mapping.get(ticker)
            replacement=f'{choice} (your configured comparable holding)' if choice else 'a comparable holding of your choice'
            lines.append(f"WASH-SALE WARNING: {ticker} was sold at a loss in a taxable account on {latest['date']}. A Roth/IRA buy before {safe} permanently loses that loss. Buy {replacement} instead and swap to {ticker} on {safe}.")
            if 'roth' in account.lower() and choice in ('TQQQ','QLD','UPRO','SSO'):lines.append(ROTH_BUY_REMINDER)
        else:
            def own(x):
                if account_id is not None:return str(x.get('account_last4',x.get('account','')))==str(account_id)
                return str(x.get('account','')).strip().casefold()==account.strip().casefold()
            if any(own(x) for x in hits):
                lines.append(f"Note: rebuying {ticker} in the taxable account within 30 days only postpones the loss (it is added to the new shares' cost). No action needed.")
            else:lines.append(f'Household watch: {ticker} has a taxable loss window; the target taxable account is not identified as the selling account. Confirm replacement-lot tax basis.')
    return lines


def top7_rebalance_warnings(facts):
    """Warn about a USER-supplied January plan. This creates no new rebalance signal."""
    plan=facts.get('top7_rebalance_plan',[]);lines=[]
    buys=list(facts.get('retirement_purchases',[]))
    for t in plan:
        if t.get('side','').lower()=='buy':
            account=t.get('account','Roth' if t.get('taxable') is False else 'Brokerage')
            lines.extend(top7_buy_warnings(account,[t['symbol']],t['date'],facts,t.get('account_last4'),t.get('taxable')))
            if t.get('taxable') is False or 'roth' in account.lower() or 'ira' in account.lower():buys.append(dict(t))
    sales=[dict(t,planned=True) for t in plan if t.get('side','').lower()=='sell' and t.get('taxable') is True and (float(t.get('gain',t.get('expected_gain',0)) or 0)<0 or t.get('classification')=='loss')]
    # Actual recent loss records also expose the before-purchase half of a January window.
    sales+=list(facts.get('taxable_loss_sales',[]))
    seen=set()
    for s in sales:
        if s.get('taxable') is not True:continue
        for p in buys:
            if p.get('symbol','').upper()!=s['symbol'].upper():continue
            distance=(et_date(s['date'])-et_date(p['date'])).days
            key=(s['symbol'],s['date'],p['date'])
            if 0<=distance<=30 and key not in seen:
                seen.add(key);verb='planned taxable loss sale' if s.get('planned') else 'taxable loss sale'
                lines.append(f"WASH-SALE WARNING: {s['symbol']} {verb} on {s['date']} is within 30 days after a Roth/IRA buy on {p['date']}. The Roth purchase counts in the BEFORE window and would permanently lose that loss.")
    return lines


def normalize_substitutes(mapping):
    if not isinstance(mapping,dict):raise ValueError('substitutes must be a ticker-to-ticker object')
    out={}
    for key,value in mapping.items():
        if value in (None,''):continue
        a,b=str(key).upper(),str(value).upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',a) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',b):raise ValueError('substitutes entries must be ticker symbols chosen by the owner')
        out[a]=b
    return out
