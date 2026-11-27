"""Independent spec-first joint-account engine. Does not import Claude code."""
from dataclasses import dataclass, asdict
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
FUND={0:'CASH',1:'T',2:'Q',3:'O',4:'IEF',5:'BILL'}
COLS=['r_tqqq_3x','r_qld_2x','r_qqq_1x','r_ief','r_tbill']
COST=(1-.0008)**2
@dataclass(frozen=True)
class Policy:
 name:str
 plan:str='single'
 re:int=1
 be:int=4
 rf:int=1
 bf:int=1
 dip:bool=True
 ief:bool=True
 strict:bool=True
 cushion:float=.30
 guard:float=-1
 window:int=30
 or_age:bool=False
 ratio:float=1
 partial:bool=False
 out_lots:bool=True
 skip_first:bool=False
 post_dist:bool=False
 sticky:bool=True
 tacked_age:bool=False
 terminal_cost:bool=True
 no_rebuy:bool=False
 guard_out:bool=False

def unemployment_rising(change):
 """Compare in tenths of a percentage point; 0.10000000000000009 is not >0.1."""
 if change is None or not np.isfinite(float(change)):raise ValueError('First-release unemployment change unavailable')
 return bool(np.rint(float(change)*10)>1)

def defensive_holding(change,top7=False):
 return 'IEF' if unemployment_rising(change) else ('SP500' if top7 else 'USFR')

def signal_positions(price,ma,cap,exit_closes=1,delay_exit=False,dip=False,override_index=None,armed_override=None,active_override=None):
 p=pd.Series(price).reset_index(drop=True);good=((p>p.rolling(ma).mean())&(p.pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)<cap)).to_numpy()
 state=False;pending=False;bad=0;base=[];metadata=[]
 for g in good:
  base.append(state)
  if delay_exit:
   if pending:state=False;pending=False
   if g:state=True
   elif state:pending=True
   bad=0 if g else bad+1
  elif g:state=True;bad=0
  else:
   bad+=1
   if bad>=exit_closes:state=False
  metadata.append(dict(raw_pass=bool(g),failing_closes=bad,pending_exit=bool(pending)))
 next_base=state;dd=(p/p.cummax()-1).to_numpy();armed=True;active=False;positions=[];dip_meta=[]
 for i in range(len(base)+1):
  normal=base[i] if i<len(base) else next_base;draw=dd[i-1] if i else np.nan
  if i==override_index:
   if armed_override is not None:armed=bool(armed_override)
   if active_override is not None:active=bool(active_override)
  if draw>-.001:armed=True;active=False
  if normal:active=False
  elif dip and armed and draw<=-.2+1e-12:active=True;armed=False
  positions.append(bool(normal or active));dip_meta.append(dict(armed=armed,active=active,drawdown=float(draw) if np.isfinite(draw) else None))
 return np.array(positions[:-1],bool),positions[-1],dict(**metadata[-1],**dip_meta[-1],normal_in=bool(next_base))

class StrategyData:
 def __init__(self,frame,sleeve,signal_seed=None):
  self.df=frame.copy();self.sleeve=sleeve
  self.dates=pd.to_datetime(self.df.date);self.days=self.dates.to_numpy().astype('datetime64[D]').astype(int);self.years=self.dates.dt.year.to_numpy()
  c3='r_tqqq_3x' if sleeve=='TQQQ' else 'r_upro_3x';c2='r_qld_2x' if sleeve=='TQQQ' else 'r_sso_2x'
  self.returns=np.column_stack([self.df[c3],self.df[c2],np.zeros(len(frame)),self.df.r_ief,self.df.r_tbill]).astype(float)
  self.out=np.where(np.rint(self.df.unemp_3m_change.shift(1).to_numpy()*10)>1,4,5)
  self.pos={};self.next_pos={};self.signal_meta={}
  for e in (1,2,4):
   for kind in ('base','dip20'):
    key='roth' if e==1 else 'brokerage';override_index=None;arm=None;active=None
    if signal_seed and sleeve=='TQQQ':
     override_index=int(self.dates.searchsorted(pd.Timestamp(signal_seed['as_of'])))+1
     arm=signal_seed.get('dip_armed');arm=arm.get(key) if isinstance(arm,dict) else arm
     active=signal_seed.get('dip_active');active=active.get(key) if isinstance(active,dict) else active
     if active is None and signal_seed.get(key,{}).get('holding') in ('USFR','IEF'):active=False
    pos,nxt,meta=signal_positions(self.df.adj_price_index,150 if sleeve=='TQQQ' else 300,.31 if sleeve=='TQQQ' else .21,e,delay_exit=sleeve=='UPRO',dip=kind=='dip20' and sleeve=='TQQQ',override_index=override_index,armed_override=arm,active_override=active)
    self.pos[kind,e]=pos;self.next_pos[kind,e]=nxt;self.signal_meta[kind,e]=meta
 def bounds(self,start,end):return int(self.dates.searchsorted(pd.Timestamp(start))),int(self.dates.searchsorted(pd.Timestamp(end),side='right'))
 def append_decision_day(self,day):
  # A decision-only row: evaluates the last completed close, without a fictitious return.
  current=float(self.df.unemp_3m_change.iloc[-1]);self.dates=pd.concat([self.dates,pd.Series([pd.Timestamp(day)])],ignore_index=True)
  self.days=self.dates.to_numpy().astype('datetime64[D]').astype(int);self.years=self.dates.dt.year.to_numpy();self.returns=np.vstack([self.returns,np.zeros(5)]);self.out=np.r_[self.out,4 if unemployment_rising(current) else 5]
  for k,v in self.pos.items():self.pos[k]=np.r_[v,self.next_pos[k]]

POLICIES={'TQQQ':Policy('TQQQ',plan='D',re=1,be=4,dip=True,cushion=.30,guard=.05,guard_out=True),
          'UPRO':Policy('UPRO',plan='D',re=1,be=1,dip=False,cushion=.10,no_rebuy=True)}
SYMBOLS={'TQQQ':{0:'CASH',1:'TQQQ',2:'QLD',3:'UNUSED',4:'IEF',5:'USFR'},
         'UPRO':{0:'CASH',1:'UPRO',2:'SSO',3:'UNUSED',4:'IEF',5:'USFR'}}

class Account:
 def __init__(self,name,wealth):
  self.name=name;self.cash=0.;self.w=wealth;self.basis=wealth;self.interest=0.;self.f=0;self.open_day=0;self.held_days=0;self.lot=0
  self.st=0.;self.lt=0.;self.inc=0.;self.dist=0.;self.taxpaid=0.
  self.pending=[];self.adjustments=[]
 def capital(self,g,long):
  if long:self.lt+=g
  else:self.st+=g
 def tax(self):
  st,lt=self.st,self.lt
  if st<0<lt:v=min(-st,lt);st+=v;lt-=v
  if lt<0<st:v=min(-lt,st);lt+=v;st-=v
  tax=max(st,0)*.363+max(lt,0)*.238+self.inc*.278+self.dist*.328
  tax=max(tax,0.)
  if tax>self.w:raise ArithmeticError('Tax exceeds account')
  self.basis*=((self.w-tax)/self.w)
  self.w-=tax;self.taxpaid+=tax
  self.st=min(st,0);self.lt=min(lt,0);self.inc=0.;self.dist=0.
  return tax

def replay_joint(data,cfg,start,end,ledger=False,paths=False,*,liquidate=True,decision_only=False,facts=None,household_losses=(),market_prices=None):
 a,b=data.bounds(start,end)
 original_a=a
 if cfg.skip_first:a+=1
 if b<=a:raise ValueError('empty window')
 r=Account('R',cfg.ratio);t=Account('B',1.)
 rp=data.pos['dip20' if cfg.dip else 'base',cfg.re];bp=data.pos['dip20' if cfg.dip else 'base',cfg.be]
 records=[];losses=[];roth_buys=[];last_loss={1:-10**9,2:-10**9,3:-10**9};last_r={1:-10**9,2:-10**9,3:-10**9}
 curves=[];taxes=[];last_guard_end=-10**9
 active_losses=[]
 waiting_events=[];guard_sales=0
 facts_applied=False;basis_unknown=False;unknown_pending_until=-10**9
 observed_model={};market_prices=market_prices or {}
 def observe(day):
  nonlocal facts_applied,basis_unknown,unknown_pending_until
  if not facts or facts_applied or day < int(np.datetime64(facts['as_of'],'D').astype(int)):return
  facts_applied=True
  # A real snapshot replaces assumed fills. Unknown historic deferred dollars are never fabricated.
  losses.clear();active_losses.clear();roth_buys.clear()
  for ac,key in [(r,'roth'),(t,'brokerage')]:
   item=facts.get(key,{})
   if item.get('holding') is None:continue
   symbol=item['holding'];reverse={v:k for k,v in SYMBOLS[data.sleeve].items()}
   if symbol not in reverse:raise ValueError('Unknown sleeve holding in facts')
   prior_f,prior_cash,prior_wealth=ac.f,ac.cash,ac.w
   ac.cash=0.;ac.f=reverse[symbol];ac.w=1.;ac.basis=1.;ac.interest=0.;ac.st=ac.lt=ac.inc=ac.dist=0.;ac.open_day=day;ac.held_days=0
   quantity=item.get('quantity');cost=item.get('tax_cost_per_share');price=market_prices.get(str(np.datetime64(day,'D')),{}).get(symbol)
   if cost is not None and price is not None:
    q=float(quantity) if quantity is not None else 1.
    ac.w=q*float(price);ac.basis=q*float(cost)
   if ac is r and prior_cash>0 and prior_f==ac.f:
    ac.w=prior_wealth;ac.cash=prior_cash # an observation alone does not reinvest accumulated cash
   if ac is t and ac.f==1:basis_unknown=cost is None
  rd=facts.get('roth',{}).get('last_3x_buy')
  if rd:
   last_r[1]=int(np.datetime64(rd,'D').astype(int));roth_buys.append(dict(day=last_r[1],fund=1,dollars=None))
  bd=facts.get('brokerage',{}).get('last_3x_loss')
  if bd:
   last_loss[1]=int(np.datetime64(bd,'D').astype(int));unknown_pending_until=last_loss[1]+cfg.window
  observed_model.update(facts)
 def record(ac,side,day,f,g=0.,long=False,loss_id=-1):
  if ledger:records.append(dict(date=str(np.datetime64(int(day),'D')),account=ac.name,fund=FUND[f],side=side,dollars=ac.w,gain=g,basis=ac.basis,interest_taxed=ac.interest,holding_days=ac.held_days+day-ac.open_day,long_term=int(long),lot_id=ac.lot,loss_id=loss_id))
 def clash(ev):
  matches=[p for p in roth_buys if p['fund']==ev['fund'] and abs(p['day']-ev['day'])<=cfg.window]
  if not matches:return 0.
  return min(sum(p['dollars'] for p in matches)/ev['sold'],1.) if cfg.partial else 1.
 def sync_loss(ev):
  fraction=clash(ev) if cfg.strict else 0.
  extra=(fraction-ev['fraction'])*ev['loss']
  if extra>1e-16:
   # Undo only the part of an internal basis deferral invalidated by Roth buying.
   if ev['rebound']:
    if ev['rebound_lot']==t.lot:
     t.basis-=extra
     if fraction>=1:t.held_days=0
    else:t.capital(extra,ev['rebound_long'])
   ev['fraction']=fraction
 def expire(day,final=False):
  for ev in active_losses[:]:
   sync_loss(ev)
   if not ev['resolved'] and (day-ev['day']>cfg.window or final):
    if not ev['rebound']:t.capital(-ev['loss']*(1-ev['fraction']),ev['long'])
    ev['resolved']=True;active_losses.remove(ev)
 def close(ac,day):
  gain=ac.w-ac.basis-ac.interest;long=ac.held_days+day-ac.open_day>365;lid=-1
  if ac is t:
   for ev in losses:
    if ev['rebound'] and ev['rebound_lot']==t.lot:ev['rebound_long']=long
   if gain < -1e-12 and ac.f in (1,2,3):
    lid=len(losses);ev=dict(id=lid,day=day,fund=ac.f,loss=-gain,sold=ac.w,long=long,age=ac.held_days+day-ac.open_day,fraction=0.,rebound=False,rebound_lot=-1,rebound_long=False,resolved=False)
    losses.append(ev);active_losses.append(ev);last_loss[ac.f]=day;sync_loss(ev)
   else:ac.capital(gain,long)
  record(ac,'SELL',day,ac.f,gain,long,lid)
 def buy(ac,day,target,first=False):
  nonlocal basis_unknown
  if ac is t and target==1 and facts_applied and day<=unknown_pending_until:basis_unknown=True
  if not first:ac.w*=COST
  ac.cash=0.;ac.f=target;ac.basis=ac.w;ac.interest=0.;ac.open_day=day;ac.held_days=0;ac.lot+=1
  if ac is r and target in (1,2,3):
   last_r[target]=day;roth_buys.append(dict(day=day,fund=target,dollars=ac.w))
   for ev in active_losses:sync_loss(ev)
  if ac is t and target in (1,2,3):
   eligible=[ev for ev in active_losses if ev['fund']==target and not ev['rebound'] and not ev['resolved'] and ev['fraction']<1 and 0<=day-ev['day']<=cfg.window]
   for ev in eligible:
    ac.basis+=ev['loss']*(1-ev['fraction']);ev['rebound']=True;ev['rebound_lot']=ac.lot
    # Held duration carried, excluding the gap out of market.
    ac.held_days=max(ac.held_days,ev['age'])
  record(ac,'BUY',day,target)
 def choices(i,day,post_exits=False):
  rin=rp[i];bin=bp[i];out=int(data.out[i]) if cfg.ief else 5
  rt=cfg.rf if rin else out;bt=cfg.bf if bin else out
  if cfg.plan=='loss' and rin:rt=1 if cfg.sticky and r.f==1 else (2 if day-last_loss[1]<=cfg.window else 1)
  if cfg.plan=='C':
   if rin:rt=1 if (t.f!=1 and day-last_loss[1]>cfg.window) or r.f==1 else 3
   if bin:
    effective_last_r=day if rt==1 and r.f!=1 else last_r[1]
    bt=1 if (cfg.sticky and t.f==1) or (rt==1 and day-effective_last_r>cfg.window) else 2
  if cfg.plan=='D':
   if rin:
    permitted=t.f==1 and (t.w>=t.basis*(1+cfg.cushion)-1e-12*abs(t.basis) or (cfg.or_age and day-t.open_day+(t.held_days if cfg.tacked_age else 0)>cfg.window)) and day-last_loss[1]>cfg.window
    household_block=any(x['symbol']==data.sleeve and 0<=(pd.Timestamp(str(np.datetime64(day,'D'))).date()-pd.Timestamp(x['date']).date()).days<=cfg.window for x in household_losses)
    permitted=permitted and not household_block and not basis_unknown
    rt=1 if r.f==1 or permitted else 2
   bt=1 if bin else out
   if cfg.guard>=0 and bin:
    within=day-last_r[1]<=cfg.window
    if within and ((t.f==1 and not basis_unknown and t.w<t.basis*(1+cfg.guard)-1e-12*abs(t.basis)) or t.f!=1):bt=out if cfg.guard_out else 3
   if cfg.no_rebuy and bin and t.f!=1 and day-last_r[1]<=cfg.window:bt=out
  if not cfg.out_lots:
   if not rin:rt=5
   if not bin or bt in (4,5):bt=5
  return rt,bt
 for i in range(a,b):
  day=int(data.days[i-1]) if i else int(data.days[i])-1
  observe(day)
  expire(day)
  # Exits caused by account IN/OUT signals happen before either account's new purchase.
  out=(int(data.out[i]) if cfg.ief else 5) if cfg.out_lots else 5
  rin=bool(rp[i]);bin=bool(bp[i])
  exited_r=False;exited_b=False
  prev_b=t.f;prev_b_basis=t.basis;prev_b_wealth=t.w
  _,planned_bt=choices(i,day)
  effective_bin=(planned_bt in (1,2,3)) if cfg.no_rebuy or cfg.guard_out else bin
  if cfg.guard_out and bin and t.f==1 and planned_bt in (4,5):guard_sales+=1
  for ac,want_in in [(r,rin),(t,effective_bin)]:
   if ac.f and ((not want_in and ac.f in (1,2,3)) or (want_in and ac.f in (4,5)) or (not want_in and ac.f!=out)):
    close(ac,day);ac.f=0
    if ac is r:exited_r=True
    else:exited_b=True
  rt,bt=choices(i,day)
  # Determine fund swaps, close both before Roth purchase. Re-evaluate after realized losses.
  if r.f and r.f!=rt:close(r,day);r.f=0;exited_r=True
  if t.f and t.f!=bt:close(t,day);t.f=0;exited_b=True
  rt,bt=choices(i,day)
  if r.f==0:buy(r,day,rt,first=(i==a))
  # Roth buy may change brokerage eligibility, especially guard and C1.
  _,bt=choices(i,day)
  if t.f and t.f!=bt:close(t,day);t.f=0;exited_b=True
  if t.f==0:buy(t,day,bt,first=(i==a))
  if (cfg.no_rebuy or cfg.guard_out) and bin and t.f in (4,5):
   waiting_events.append(dict(return_date=str(data.dates.iloc[i].date()),trade_date=str(np.datetime64(day,'D')),prior_holding=FUND[prev_b],holding=FUND[t.f],prior_wealth=prev_b_wealth,prior_basis=prev_b_basis,roth_T_purchase=str(np.datetime64(last_r[1],'D')),days_since_roth_buy=day-last_r[1],guard_sale=int(cfg.guard_out and prev_b==1),entry_wait=int(prev_b in (0,4,5))))
  if decision_only and i==b-1:
   curves.append((r.w,t.w));break
  for ac in [r,t]:
   pre=ac.w
   actual_f=ac.f if cfg.out_lots or ac.f in (1,2,3) else (int(data.out[i]) if cfg.ief else 5)
   ret=float(data.returns[i,actual_f-1])
   cash_mark=getattr(data,'roth_cash_returns',{}).get((int(data.days[i]),actual_f))
   if ac is r and int(data.days[i])>=int(np.datetime64('2026-11-27','D').astype(int)) and cash_mark is not None:
    price_return,dividend_yield=cash_mark;invested=ac.w-ac.cash;ac.cash+=invested*dividend_yield;ac.w=invested*(1+price_return)+ac.cash
   elif ac is r and ac.cash>0:ac.w=(ac.w-ac.cash)*(1+ret)+ac.cash
   else:ac.w*=1+ret
   if ac is t:
    if actual_f==5:interest=pre*data.returns[i,4]
    elif actual_f==4:interest=pre*max(data.returns[i,4]+.008/252,0)
    else:interest=0.;ac.dist+=(ac.w if cfg.post_dist else pre)*.0045/252
    ac.interest+=interest;ac.inc+=interest
  final=i==b-1
  if final and liquidate:
   close(r,int(data.days[i]));close(t,int(data.days[i]));expire(int(data.days[i]),final=True)
   if cfg.terminal_cost and b-a>1:r.w*=COST;t.w*=COST
  if (final and liquidate) or (not final and data.years[i]!=data.years[i+1]):taxes.append((str(data.dates.iloc[i].date()),t.tax()))
  curves.append((r.w,t.w))
 c=np.array(curves);n=b-a;years=n/252
 # Legacy annualisation reconciled separately; exact elapsed-year metric retained.
 duration=(data.days[b-1]-(data.days[original_a] if cfg.skip_first else data.days[max(a-1,0)]))/365.25
 if duration<=0:duration=1/365.25
 result=dict(name=cfg.name,start=str(data.dates.iloc[a].date()),end=str(data.dates.iloc[b-1].date()),days=n,R_cagr=None if facts_applied or decision_only else 100*((r.w/cfg.ratio)**(1/years)-1),B_cagr=None if facts_applied or decision_only else 100*(t.w**(1/years)-1),R_cagr_calendar=None if facts_applied or decision_only else 100*((r.w/cfg.ratio)**(1/duration)-1),B_cagr_calendar=None if facts_applied or decision_only else 100*(t.w**(1/duration)-1),R_dd=100*(np.r_[cfg.ratio,c[:,0]]/np.maximum.accumulate(np.r_[cfg.ratio,c[:,0]])-1).min(),B_dd=100*(np.r_[1.,c[:,1]]/np.maximum.accumulate(np.r_[1.,c[:,1]])-1).min(),loss_sales=len(losses),clashes=sum(clash(e)>0 for e in losses),before=sum(any(p['fund']==e['fund'] and e['day']-cfg.window<=p['day']<=e['day'] for p in roth_buys) for e in losses),after=sum(any(p['fund']==e['fund'] and e['day']<p['day']<=e['day']+cfg.window for p in roth_buys) for e in losses),permanent_loss=sum(e['loss']*clash(e) for e in losses) if cfg.strict else 0.,tax_paid=t.taxpaid,terminal_R=r.w,terminal_B=t.w)
 result.update(waiting_days=len(waiting_events),guard_sales=guard_sales)
 result['model_accounts']={ac.name:dict(holding=SYMBOLS[data.sleeve][ac.f],wealth=ac.w,cash=ac.cash,cash_source="model cash, not an observed account balance",tax_basis=ac.basis,interest_taxed=ac.interest,lot_date=str(np.datetime64(ac.open_day,'D')),cost_source='real tax lot override' if facts_applied and facts.get('brokerage',{}).get('tax_cost_per_share') is not None and ac is t else 'model estimate including modeled deferred losses') for ac in [r,t]}
 result.update(last_roth_3x_buy=str(np.datetime64(last_r[1],'D')) if last_r[1]>-10**8 else None,last_brokerage_3x_loss=str(np.datetime64(last_loss[1],'D')) if last_loss[1]>-10**8 else None,basis_unknown=basis_unknown,facts_applied=facts_applied)
 frame=pd.DataFrame(records);frame.attrs['waiting_events']=waiting_events;frame.attrs['tax_events']=taxes
 return result,frame,c if paths else None
