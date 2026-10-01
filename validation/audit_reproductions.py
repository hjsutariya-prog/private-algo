"""Read-only engine comparison; writes only audit results beside this script."""
import sys, json, inspect, types, sqlite3, hashlib
from pathlib import Path
from datetime import date, datetime, timedelta
from decimal import Decimal

ROOT = Path(__file__).resolve().parent
OLD = Path(r'C:\Users\Harshal Sutariya\buildweek\algo')
sys.path[:0] = [str(ROOT.parent), str(OLD)]
from engine import IST
from tests.test_spot_proxy import make_day
from replay import replay_signals
from range_spot import replay_spot_range
from spot_proxy import build_spot_proxy_ledger
from app import spot_proxy_for_range
from option_model import simulate_options, movement_contribution, premium_value
from backtest_config import model_config
from algo_engine.domain import Candle
import algo_engine.thousand_levels_hourly as old_spot
import algo_engine.short_option_hedge_model as old_option

results = {}
def old_bars(rows):
    return [Candle(datetime.fromisoformat(r['timestamp_ist']), *[round(r[k]*100) for k in ('open','high','low','close')]) for r in rows]

# Reproduce the repository's comparison variant without modifying its source.
source = inspect.getsource(old_spot)
for a,b in [('if entries[week][side] >= 1 or positions[side] is not None or pending[side] is not None:', 'if positions[side] is not None or pending[side] is not None:'), ('if entries[current_week][side] >= 1 or positions[side] is not None:', 'if positions[side] is not None:')]:
    assert source.count(a) == 1
    source = source.replace(a,b)
variant = types.ModuleType('algo_engine.audit_unlimited')
variant.__package__ = 'algo_engine'
sys.modules[variant.__name__] = variant
exec(compile(source, '<audit-unlimited>', 'exec'), variant.__dict__)
variant.reference_grid_cents = lambda close: 100_000

def new_spot(rows, skip=False):
    first,last = rows[0]['timestamp_ist'][:10], rows[-1]['timestamp_ist'][:10]
    checked = replay_spot_range(rows,first,last,datetime(2026,9,30,23,tzinfo=IST),complete_hours_only=True,empty_days_are_holidays=True,ignore_missing_minutes=skip,strategy_mode='hour_boundary_uncertain_v1')
    return spot_proxy_for_range(rows,checked), checked

def ledger(rows,events=()):
    return {'status':'complete','last_session':rows[-1]['timestamp_ist'][:10],'events':list(events)}

def event(kind,at,spot,parent='bull-1',**extra):
    return {'type':kind,'at':at,'spot':spot,'position_id':parent,'side':'bull','intended_short_option_side':'PE',**extra}

# Arithmetic grid: both option types, both theta settings and several holding periods.
cfg=model_config(); mismatches=[]; count=0
for movement in range(-4000,4001,25):
    assert movement_contribution(movement,cfg)==old_option._delta_area(Decimal(movement))
    for side in ('CE','PE'):
        for minutes in (1,60,1440,4320):
            for premium,theta in ((1000,20),(500,30)):
                a=datetime(2026,9,24,9,15,tzinfo=IST); b=a+timedelta(minutes=minutes)
                n=premium_value(side,56000,56000+movement,premium,a,b,theta,cfg)
                o=old_option._model_leg(option_type=side,entry_spot_cents=5600000,exit_spot_cents=(56000+movement)*100,entry_at=a,exit_at=b,entry_points=Decimal(premium),theta_per_day=Decimal(theta))
                count+=1
                if n['premium'] != o['exit_premium_points']: mismatches.append([movement,side,minutes,premium])
results['premium_parity']={'cases':count,'mismatches':mismatches}

rows=make_day(date(2026,9,24),{0:'bull'})
rows[60].update(open=54000,high=54000,low=54000,close=54000)
rows[61].update(open=54200,high=54200,low=54200,close=54200)
n,_=new_spot(rows); o=variant.thousand_levels_hourly_replay(old_bars(rows),date(2026,9,24),date(2026,9,24),stop_cents=50000,target_cents=100000)
results['entry_delay']={'old':{k:o.trades[0][k] for k in ('entryTime','entry','stop','target')},'new':{k:n['positions'][0][k] for k in ('entry_at','entry_spot','stop_spot','target_spot')}}

rows=make_day(date(2026,9,24),{0:'bull'}); rows[70].update(open=54000,high=55000,low=53500,close=54000)
n,_=new_spot(rows); o=variant.thousand_levels_hourly_replay(old_bars(rows),date(2026,9,24),date(2026,9,24),stop_cents=50000,target_cents=100000)
results['both_touched']={'old':o.trades[0], 'new':n['positions'][0]}

# A fresh hedge opened at 15:15 must still be monitored during that minute.
rows=make_day(date(2026,9,28),base=56000); rows[360]['high']=57000
n=simulate_options(rows,ledger(rows))
results['rollover_minute_trigger']={'status':n['status'],'ce_hedges':[{k:p.get(k) for k in ('entry_at','exit_at','exit_reason','trigger_spot')} for p in n['ledgers']['ce_hedge']]}
assert len(n['ledgers']['ce_hedge'])==3 and n['ledgers']['ce_hedge'][-1]['status']=='open'
old_option.hedge_entry_premium_points=lambda cents: Decimal(cents//1_000_000)*100
old_hedges=old_option.model_monthly_hedges(old_bars(rows),monday_rollover=True)
results['rollover_minute_trigger']['old_ce_hedges']=[p for p in old_hedges if p['option_type']=='CE']

# Future intraminute target is processed ahead of the rollover open.
exit_at='2026-09-28T15:15:00+05:30'
ev=[event('proxy_entry','2026-09-28T10:16:00+05:30',56000),event('proxy_exit',exit_at,57000,reason='target',price_basis='threshold_touched_within_minute')]
n=simulate_options(rows,ledger(rows,ev))
results['rollover_future_exit']={'rollovers':n['rollovers'],'selling':n['ledgers']['selling'],'orders_at_cutoff':[x for x in n['orders'] if x['at']==exit_at]}

# Accepted adapter events that do not belong to the price stream vanish silently.
rows=make_day(date(2026,9,24),base=56000)
ev=[event('proxy_entry','2026-09-24T10:16:20+05:30',56000)]
n=simulate_options(rows,ledger(rows,ev))
results['unmatched_event']={'status':n['status'],'selling_count':len(n['ledgers']['selling']),'blocked':n['blocked_from']}

# A missing true trading day is labelled a holiday without setting option approximation.
rows=make_day(date(2026,9,24),base=56000)+make_day(date(2026,9,28),base=56000)
spot,checked=new_spot(rows,True)
holidays={date.fromisoformat(x['date']) for x in checked['days'] if x.get('assumed_holiday')}
n=simulate_options(rows,spot,assumed_holidays=holidays)
results['empty_trading_day']={'assumed_holidays':sorted(map(str,holidays)),'option_status':n['status'],'approximate':n['approximate_due_to_missing_minutes'],'skipped_minutes':n['skipped_minute_count'],'validation_status':n['validation_status'],'warnings':n['warnings']}

# Identical cached September data, read-only. No API or credentials are used.
db=sqlite3.connect((OLD/'.local/history.sqlite3').as_uri()+'?mode=ro',uri=True)
record=db.execute("SELECT id,identity,payload FROM downloads WHERE json_extract(identity,'$.symbol')='NIFTY BANK' AND json_extract(identity,'$.from')='2026-09-01' AND json_extract(identity,'$.to')='2026-09-23' AND json_extract(identity,'$.interval')='minute' ORDER BY rowid DESC LIMIT 1").fetchone()
db.close()
raw=json.loads(record[2]); values=raw['data']['candles'] if isinstance(raw,dict) else raw
rows=[dict(timestamp_ist=r[0],open=r[1],high=r[2],low=r[3],close=r[4]) for r in values]
spot,checked=new_spot(rows,True)
bars=old_bars(rows); start=bars[0].session; end=bars[-1].session
o=variant.thousand_levels_hourly_replay(bars,start,end,stop_cents=50000,target_cents=100000)
weekly=old_spot.thousand_levels_hourly_replay(bars,start,end,stop_cents=50000,target_cents=100000)
holidays={date.fromisoformat(x['date']) for x in checked['days'] if x.get('assumed_holiday')}
opt=simulate_options(rows,spot,assumed_holidays=holidays)
results['cached_sample']={'source_hash':record[0],'identity':json.loads(record[1]),'rows':len(rows),'first':rows[0]['timestamp_ist'],'last':rows[-1]['timestamp_ist'],'range_counts':checked['status_counts'],'new_spot_status':spot['status'],'skipped_minutes':spot.get('skipped_minute_count'),'old_unlimited_trades':o.trades,'old_weekly_trades':weekly.trades,'new_positions':spot['positions'],'new_closed_points':spot['closed_spot_points_proxy'],'old_closed_points':round(sum(t.get('spot_points') or 0 for t in o.trades if t['state']=='CLOSED'),2),'new_option_status':opt['status'],'new_option_combined':opt['combined']}
old_cash=old_option.build_comparison(o.trades,bars,monday_rollover=True)
results['cached_sample']['old_option_quantity_comparison']=old_cash['quantity_comparison']
results['cached_sample']['old_option_legs']={k:old_cash[k] for k in ('short_legs','hedge_legs')}

source_root=Path(r'C:\Users\Harshal Sutariya\Documents\Codex\2026-09-19\files-pasted-by-the-user-build\private-algo')
assert results['premium_parity']['cases'] == 5136 and not results['premium_parity']['mismatches']
assert results['entry_delay']['new']['entry_at'] == '2026-09-24T10:15:00+05:30'
assert results['both_touched']['new']['status'] == 'uncertain'
assert results['both_touched']['new']['exit_spot'] is None
assert results['rollover_future_exit']['rollovers'][0]['selling_positions_reopened'] == 1
assert len(results['rollover_future_exit']['selling']) == 2
assert results['unmatched_event']['status'] == 'unavailable'
assert results['empty_trading_day']['validation_status'] == 'approximate'
results['reproduction_checks'] = 'passed: arithmetic, entry timing, uncertain collision, both rollover defects, rejected unmatched event, whole-day uncertainty'
results['candidate_source_hashes']={str(p.relative_to(source_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_root.rglob('*') if p.is_file() and p.suffix in ('.py','.js','.cjs','.md')}
(ROOT/'audit-reproductions.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in results.items() if k in ('premium_parity','entry_delay','rollover_minute_trigger','unmatched_event','empty_trading_day')},indent=2))
print('CACHED SAMPLE',json.dumps({k:v for k,v in results['cached_sample'].items() if 'trades' not in k and k!='new_positions'},indent=2))


