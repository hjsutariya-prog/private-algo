const {test}=require('node:test');
const assert=require('node:assert/strict');
const {derive,remainingLots,money}=require('../static/trading-desk.js');
const {replayView,liveReplayView,deploymentRecords,runtimeData,deploymentStatistics}=require('../static/trading-desk.js');

test('historical positions do not become paper fills, protection or live inventory',()=>{
  const {data}=fixture(), before=derive(data,'paper',now);
  data.paper.historical_replay={snapshot:{kind:'historical_paper_replay',is_execution:false,legs:[{direction:'SELL',quantity:9999}]}};
  const after=derive(data,'paper',now);
  assert.deepEqual(after.groups,before.groups);assert.equal(after.protection,before.protection);
  assert.equal(derive(data,'live',now).groups.length,0);
});
test('replay view separates expired historical contracts and recorded vs modelled prices',()=>{
  const snapshot={kind:'historical_paper_replay',is_execution:false,legs:[
    {direction:'SELL',option_type:'PE',expiry:'2026-09-29',strike:57000,entry_pricing:{basis:'recorded'},mark_pricing:{basis:'estimated'}},
    {direction:'BUY',option_type:'PE',expiry:'2026-09-29',strike:55000},
    {direction:'BUY',option_type:'CE',expiry:'2026-10-27',strike:null}]};
  const v=replayView({snapshot,through_latest_close:false,note:'Missing later sessions'});
  assert.equal(v.groups.length,2);assert.equal(v.shorts,1);assert.equal(v.hedges,2);
  assert.match(v.status,/not current/);
  assert.match(v.groups[0].legs[0].entryBasis,/Recorded price/);
  assert.match(v.groups[0].legs[0].markBasis,/Modelled estimate/);
  assert.match(v.groups[1].legs[0].title,/Strike unavailable/);
  assert.equal(replayView({snapshot:{...snapshot,is_execution:true}}),null);
  assert.equal(replayView(null),null);
});
test('deployment summary keeps a historical replay separate from paper executions',()=>{
  const historical={snapshot:{kind:'historical_paper_replay',is_execution:false,config:{sell_lots:4,ce_hedge_lots:4,pe_hedge_lots:4},
    strategy:{label:'Hourly boundary · fixed signal-hour close SL/target'},as_of:'2026-09-30T10:00:00Z',
    combined:{open_net:250},legs:[{symbol:'BANKNIFTY26OCT54000CE',quantity:-120,entry_premium:100,mark_premium:90,net_pnl:250}]}};
  const records=deploymentRecords({groups:[]},{paper:{historical_replay:historical}},'paper');
  assert.equal(records.length,1);assert.equal(records[0].strategy,'BANKNIFTY hourly model');assert.equal(records[0].multiplier,4);
  assert.equal(records[0].execution,'Paper');assert.match(records[0].note,/not an executed paper order/);
  assert.equal(records[0].legs[0].entry,100);assert.equal(records[0].legs[0].mark,90);
  assert.deepEqual(deploymentRecords({groups:[]},{paper:{historical_replay:historical}},'live'),[]);
});
test('saved deployments expose only their own paper executor state',()=>{
  const input={state:{deployments:[
    {id:'one',mode:'paper_only',strategy_name:'One',strategy_version:'v1',state:'linked_unverified',updated_at:at,multiplier:4,
      executor:{linked:true,strategy_id:'999056854',delivery:'unverified'},runner:{mode:'paper_only',armed:false,automatic_runner:'unavailable'}},
    {id:'two',mode:'paper_only',strategy_name:'Two',strategy_version:'v1',state:'not_linked',updated_at:at,multiplier:1,
      executor:{linked:false},runner:{mode:'paper_only',armed:false,automatic_runner:'unavailable'}}
  ]}};
  const records=deploymentRecords({groups:[]},input,'paper');
  assert.equal(records.length,2);
  assert.equal(records[0].deploymentId,'one');assert.match(records[0].status,/Link saved/);assert.equal(records[0].executor.strategy_id,'999056854');
  assert.equal(records[1].deploymentId,'two');assert.match(records[1].status,/Not linked/);assert.match(records[1].note,/Connect Tradetron/);
  assert.equal(records[0].runner.armed,false);
});
test('fresh stream quotes update only open replay legs and never turn them into fills',()=>{
  const at=new Date(now).toISOString(), historical={snapshot:{kind:'historical_paper_replay',is_execution:false,
    validation_status:'validated',as_of:'2026-09-30T10:00:00Z',combined:{open_net:250},legs:[
      {status:'open',direction:'SELL',contract:{symbol:'BANKNIFTY26OCT54000CE'},quantity:120,entry_premium:100,mark_premium:90,net_pnl:250,charges:10},
      {status:'closed',direction:'BUY',contract:{symbol:'BANKNIFTY26OCT55000CE'},quantity:120,entry_premium:40,mark_premium:30,net_pnl:50}]}};
  const input={paper:{historical_replay:historical,stream:{quotes:[{symbol:'NFO:BANKNIFTY26OCT54000CE',last_price:80,timestamp:at,received_at:at},{symbol:'NFO:BANKNIFTY26OCT55000CE',last_price:20,timestamp:at,received_at:at}]}}};
  const record=deploymentRecords({groups:[]},input,'paper',now)[0];
  assert.match(record.status,/Live marks 1\/1/);assert.equal(record.legs[0].mark,80);assert.equal(record.legs[0].pnl,2390);assert.equal(record.legs[0].liveMark,true);
  assert.equal(record.legs[1].mark,30);assert.equal(record.legs[1].liveMark,false);assert.equal(record.pnl,2390);assert.match(record.note,/not an executed paper order/);
});
test('runtime data labels a local reconstruction and does not claim Tradetron read-back',()=>{
  const record={id:'historical-replay',valuationAt:at,legs:[
    {direction:'SELL',option_type:'CE',contract:{symbol:'BANKNIFTY26OCT54000CE'},quantity:120,expiry:'2026-10-27',entry_pricing:{basis:'recorded'},mark_pricing:{basis:'estimated'},risk_reference_spot:54500,stop_spot:54000,target_spot:55500},
    {direction:'BUY',option_type:'CE',contract:{symbol:'BANKNIFTY26OCT56000CE'},quantity:120,expiry:'2026-10-27',entry_pricing:{basis:'recorded'}}
  ]};
  const data=runtimeData(record,{tradetron:{linked:true,delivery:'verified',strategy_id:'999056854'}});
  assert.equal(data.source,'Research replay');assert.match(data.summary,/not an executed paper order/);
  assert.equal(data.directions.length,1);assert.equal(data.directions[0].short.contract.symbol,'BANKNIFTY26OCT54000CE');
  assert.equal(data.directions[0].hedge.contract.symbol,'BANKNIFTY26OCT56000CE');
  assert.deepEqual(data.directions[0].risk,{reference:54500,stop:54000,target:55500});
  assert.match(data.bridge.note,/cannot read Tradetron/);assert.equal(data.strategyId,'Strategy 999056854');
});
test('deployment statistics keep source, quantities, pricing coverage and risk distances explicit',()=>{
  const record={id:'historical-replay',pnl:450,legs:[
    {direction:'SELL',option_type:'CE',quantity:120,strike:54000,pnl:500,entry_pricing:{basis:'recorded'},mark_pricing:{basis:'estimated'},risk_reference_spot:54500,stop_spot:54000,target_spot:55500},
    {direction:'BUY',option_type:'CE',quantity:120,strike:56000,pnl:-200,entry_pricing:{basis:'recorded'}},
    {direction:'SELL',option_type:'PE',quantity:120,strike:54500,pnl:200,entry_pricing:{basis:'estimated'}},
    {direction:'BUY',option_type:'PE',quantity:120,strike:53000,pnl:-50}
  ]};
  const data=deploymentStatistics(record);
  assert.equal(data.source,'Research replay');assert.equal(data.pnlLabel,'Open estimate');
  assert.equal(data.openLegs,4);assert.equal(data.shortUnits,240);assert.equal(data.hedgeUnits,240);
  assert.deepEqual(data.coverage,{recorded:1,mixed:1,estimated:1,unavailable:1});
  assert.equal(data.directions[0].hedgeDistance,2000);assert.deepEqual(data.directions[0].risk,{stopDistance:500,targetDistance:1000});
  assert.match(data.note,/not broker fills/);
});
test('positions view uses the same complete live replay mark and refuses a partial total',()=>{
  const at=new Date(now).toISOString(), snapshot={kind:'historical_paper_replay',is_execution:false,combined:{open_net:100},legs:[
    {status:'open',direction:'SELL',contract:{symbol:'BANKNIFTY26OCT54000CE'},quantity:30,entry_premium:100,mark_premium:90,net_pnl:300},
    {status:'open',direction:'BUY',contract:{symbol:'BANKNIFTY26OCT56000CE'},quantity:30,entry_premium:50,mark_premium:40,net_pnl:-300}]};
  const full=liveReplayView({snapshot},{paper:{stream:{quotes:[{symbol:'NFO:BANKNIFTY26OCT54000CE',last_price:80,timestamp:at,received_at:at},{symbol:'NFO:BANKNIFTY26OCT56000CE',last_price:60,timestamp:at,received_at:at}]}}},now);
  assert.equal(full.allOpenLegsLive,true);assert.equal(full.pnl,900);assert.equal(full.legs[0].liveMark,true);
  const partial=liveReplayView({snapshot},{paper:{stream:{quotes:[{symbol:'NFO:BANKNIFTY26OCT54000CE',last_price:80,timestamp:at,received_at:at}]}}},now);
  assert.equal(partial.allOpenLegsLive,false);assert.equal(partial.pnl,100);assert.equal(partial.legs[1].liveMark,false);
});
const now=Date.parse('2026-09-30T06:00:00Z'), at=new Date(now).toISOString();
function fixture() {
  const data={state:{strategies:[{id:'draft',lots:4}]},kite:{connected:true,session_expires_at:'2026-10-01T00:00:00Z'},paper:{busy:true,paper:{paused:false,fill_count:2},
    stream:{state:'connected',quotes:['NSE:NIFTY BANK','NFO:BANKNIFTY26OCT56000CE'].map(symbol=>({symbol,timestamp:at,received_at:at}))},
    desk:{as_of:at,positions:[],orders:[],fills:[],events:[],contracts:{}},valuation:{as_of:at,positions:[]}}};
  const add=(symbol,quantity,strike,kind='CE',expiry='2026-10-27',strategy='test')=>{
    const d=data.paper.desk,id='order-'+d.orders.length;
    d.positions.push({strategy,symbol,quantity});
    d.orders.push({id,created:at,status:'COMPLETE',filled:Math.abs(quantity),intent:{strategy,symbol,expiry,quantity:Math.abs(quantity),side:quantity>0?'BUY':'SELL'}});
    d.fills.push({order_id:id,strategy,symbol,quantity:Math.abs(quantity),price:500,side:quantity>0?'BUY':'SELL'});
    d.contracts[symbol]={instrument_type:kind,strike,expiry};
    data.paper.valuation.positions.push({strategy,symbol,quantity,open_net:100,pnl_basis:'recorded',marks:[{quantity,pricing:{premium:505,basis:'recorded',source:{observed_at:at}}}]});
  };
  add('short',-30,56000);add('hedge',30,57000);
  return {data,add};
}
test('live never exposes paper positions, profits or orders even when connected',()=>{
  const {data}=fixture(), v=derive(data,'live',now);
  assert.equal(v.connected,true);assert.equal(v.active,false);assert.equal(v.groups.length,0);assert.equal(v.orders.length,0);assert.equal(v.activity.length,0);
  assert.match(v.protection,/Unknown/);assert.ok(v.issues.some(i=>i.id==='live-data'));
});
test('confirmed paper protection requires matching executed quantities and metadata',()=>{
  const {data}=fixture(), v=derive(data,'paper',now);
  assert.equal(v.groups[0].protection,'Protected · paper fills');assert.equal(v.groups[0].uncovered,0);assert.equal(v.groups[0].pnl,200);assert.equal(v.groups[0].legs[0].entry,500);
});
test('pending hedge is not protection and incomplete execution is actionable',()=>{
  const {data}=fixture();data.paper.desk.positions.pop();data.paper.desk.fills.pop();Object.assign(data.paper.desk.orders[1],{status:'OPEN',filled:0});
  const v=derive(data,'paper',now);assert.equal(v.groups[0].uncovered,30);assert.equal(v.groups[0].pending.length,1);assert.ok(v.issues.some(i=>i.title==='Incomplete paper execution'));
});
test('partial hedge covers only executed units',()=>{
  const {data}=fixture();data.paper.desk.positions[1].quantity=20;data.paper.desk.orders[1].filled=20;data.paper.desk.orders[1].status='PARTIAL';data.paper.desk.fills[1].quantity=20;
  assert.equal(derive(data,'paper',now).groups[0].uncovered,10);
});
test('different expiries, types, strategies and wrong-side strikes cannot cover a short',()=>{
  for(const variant of ['expiry','kind','strategy','strike']) {
    const {data}=fixture();
    if(variant==='expiry'){data.paper.desk.orders[1].intent.expiry='2026-11-24';data.paper.desk.contracts.hedge.expiry='2026-11-24';}
    if(variant==='kind')data.paper.desk.contracts.hedge.instrument_type='PE';
    if(variant==='strategy'){data.paper.desk.positions[1].strategy='other';data.paper.desk.orders[1].intent.strategy='other';data.paper.desk.fills[1].strategy='other';}
    if(variant==='strike')data.paper.desk.contracts.hedge.strike=55000;
    assert.equal(derive(data,'paper',now).groups.find(g=>g.legs.some(l=>l.symbol==='short')).uncovered,30,variant);
  }
});
test('one hedge cannot be counted twice',()=>{
  const {data,add}=fixture();add('short2',-30,56500);const g=derive(data,'paper',now).groups[0];assert.equal(g.shortQuantity,60);assert.equal(g.uncovered,30);
});
test('PE protection requires lower strike',()=>{
  const {data}=fixture();data.paper.desk.contracts.short.instrument_type='PE';data.paper.desk.contracts.hedge.instrument_type='PE';data.paper.desk.contracts.hedge.strike=55000;
  assert.equal(derive(data,'paper',now).groups[0].uncovered,0);
});
test('unknown order, absent contract details and inconsistent fills never claim protection',()=>{
  for(const variant of ['unknown','metadata','fills']) {
    const {data}=fixture();
    if(variant==='unknown')data.paper.desk.orders[1].status='UNKNOWN';
    if(variant==='metadata')delete data.paper.desk.contracts.hedge;
    if(variant==='fills')data.paper.desk.fills[1].quantity=10;
    assert.notEqual(derive(data,'paper',now).protection,'Paper shorts protected',variant);
  }
});
test('stale quotes differ from broker connection and remove current valuation',()=>{
  const {data}=fixture();data.paper.desk.as_of=new Date(now+6000).toISOString();
  const v=derive(data,'paper',now+6000);assert.equal(v.connected,true);assert.equal(v.pricesFresh,false);assert.equal(v.groups[0].pnl,null);assert.ok(v.issues.some(i=>i.id==='prices'));
});
test('expired sign-in and a missing snapshot fail closed',()=>{
  const {data}=fixture();data.kite.session_expires_at=at;assert.equal(derive(data,'paper',now).pricesFresh,false);
  delete data.paper.desk;const v=derive(data,'paper',now);assert.match(v.protection,/Unknown/);assert.equal(v.active,false);assert.ok(v.issues.some(i=>i.id==='ledger'));
});
test('unknown unfilled orders cannot imply a verified flat or protected account',()=>{
  const {data}=fixture();data.paper.desk.positions=[];data.paper.desk.orders=[{id:'unknown',status:'UNKNOWN',filled:0,intent:{strategy:'test',symbol:'unknown',quantity:30,expiry:'2026-10-27'}}];
  assert.equal(derive(data,'paper',now).protection,'Protection unverified');
});
test('expired protective contracts cannot claim confirmed protection',()=>{
  const {data}=fixture();for(const c of Object.values(data.paper.desk.contracts))c.expiry='2026-09-29';for(const o of data.paper.desk.orders)o.intent.expiry='2026-09-29';
  assert.equal(derive(data,'paper',now).protection,'Protection unverified');
});
test('estimated marks are labelled and unavailable marks never become zero',()=>{
  const {data}=fixture();const value=data.paper.valuation.positions[0];value.pnl_basis='mixed';value.marks[0].pricing={basis:'estimated',premium:480,spot_observed_at:at};
  let v=derive(data,'paper',now);assert.equal(v.groups[0].legs[0].basis,'mixed');assert.equal(v.groups[0].legs[0].mark,480);
  value.marks[0].pricing=null;v=derive(data,'paper',now);assert.equal(v.groups[0].pnl,null);assert.equal(v.groups[0].legs[0].mark,null);
});
test('empty valid paper snapshot has no invented attention problems',()=>{
  const {data}=fixture();Object.assign(data.paper.desk,{positions:[],orders:[],fills:[]});data.paper.busy=false;data.paper.stream={state:'stopped'};
  const v=derive(data,'paper',now);assert.equal(v.issues.length,0);assert.equal(v.protection,'No open paper shorts');
});
test('retained exposure and rejected orders carry affected position and reason',()=>{
  const {data}=fixture();data.paper.busy=false;data.paper.desk.orders.push({id:'rejected',status:'REJECTED',reason:'No usable quote',intent:{strategy:'test',symbol:'replacement',expiry:'2026-10-27'},filled:0});
  const v=derive(data,'paper',now);assert.ok(v.issues.some(i=>i.id.startsWith('retained:')));assert.ok(v.issues.some(i=>i.affected==='replacement'&&i.explanation==='No usable quote'));
});
test('entry prices use remaining FIFO fills; unknown money is not zero',()=>{
  assert.deepEqual(remainingLots([{quantity:30,price:100,side:'BUY'},{quantity:30,price:200,side:'BUY'},{quantity:30,price:110,side:'SELL'}]),[{quantity:30,price:200,sign:1}]);
  assert.equal(money(null),'Unavailable');assert.match(money(1234567.8),/12,34,567.80/);
});
