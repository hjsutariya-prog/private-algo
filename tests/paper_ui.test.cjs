const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
class Element {
  constructor(){this.children=[];this.textContent='';this.hidden=false;this.disabled=false;this.attributes={};this.value='';}
  setAttribute(k,v){this.attributes[k]=v;}
  removeAttribute(k){delete this.attributes[k];}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
}
function setup(){
  const roots=new Map();
  const $=id=>{if(!roots.has(id))roots.set(id,new Element());return roots.get(id);};
  const strategy={id:'first'};
  const context=vm.createContext({$,document:{createElement:()=>new Element()},state:{strategies:[strategy],recent_runs:[]},
    selectedStrategy:()=>strategy,renderReadiness:s=>{$('strategy-readiness').textContent=s.readiness.map(r=>r.status).join('|');},
    marketTime:x=>x,formattedDate:x=>x,paperBusy:false,paperSnapshot:null,replay:{busy:false},lastModelResult:null,lastPriceCoverage:null,
    kite:{status:{configured:true,connected:true},instruments:{available:true},busy:false}});
  vm.runInContext(source.slice(source.indexOf('  function el('),source.indexOf('  function friendly(')),context);
  vm.runInContext(source.slice(source.indexOf('  function kiteSessionExpired('),source.indexOf('  function marketTime(')),context);
  vm.runInContext(source.slice(source.indexOf('  function renderPaper('),source.indexOf('  async function refreshPaper(')),context);
  vm.runInContext(source.slice(source.indexOf('  function parseCapital('),source.indexOf('  $("strategy-lots").addEventListener')),context);
  vm.runInContext(source.slice(source.indexOf('  function replaySettingsKey('),source.indexOf('  $("replay-date").addEventListener')),context);
  const data={stream:{state:'stopped',message:'Not started',fresh_count:0,accepted_ticks:0,rejected_packets:0,retries:0,last_packet:null},
    calendar:{message:'Verified',reviewed_at:'2026-09-30',covered_year:2026,pending_dates:{'2026-11-08':'Hours unknown'},limitation:'Unknown dates blocked',sources:[],open_now:true,verified_today:true},
    paper:{paused:false,positions:[],fill_count:0,limitation:'Estimated fills'},busy:false,checks:[],
    readiness:[{label:'Real-quote paper tests',status:'Awaiting market-hours acceptance'}]};
  return {$,context,data,strategy};
}
function freshStream(data) {
  const now=new Date().toISOString();
  Object.assign(data.stream,{state:'connected',fresh_count:2,quotes:['NSE:NIFTY BANK','NFO:BANKNIFTY26OCT56000CE'].map(symbol=>({symbol,timestamp:now,received_at:now,last_price:500}))});
}
test('stopped stream disables paper test and synchronises checklist with strategy',()=>{
  const e=setup();e.context.renderPaper(e.data);
  assert.equal(e.$('paper-check').disabled,true);
  assert.match(e.$('strategy-readiness').textContent,/Awaiting market-hours/);
  assert.match(e.$('calendar-limits').textContent,/2026-11-08/);
});

test('September update needs sign-in, never needs a live stream and blocks duplicate jobs',()=>{
  const e=setup();e.context.renderPaper(e.data);
  assert.equal(e.$('paper-replay-refresh').disabled,false);
  e.context.replay.busy=true;e.context.renderPaper(e.data);
  assert.equal(e.$('paper-replay-refresh').disabled,true);
  e.context.replay.busy=false;e.context.kite.status.connected=false;e.context.renderPaper(e.data);
  assert.equal(e.$('paper-replay-refresh').disabled,true);
  assert.match(e.$('paper-replay-note').textContent,/Sign in/);
  e.context.kite.status.connected=true;e.context.kite.status.session_expires_at='2020-01-01T00:00:00Z';e.context.renderPaper(e.data);
  assert.equal(e.$('paper-replay-refresh').disabled,true);
});

test('paper valuation labels estimates and unknown values without implying fills',()=>{
  const e=setup();e.data.paper.fill_count=1;e.data.paper.positions=[{quantity:30,symbol:'NFO:BANKNIFTY',strategy:'test'}];
  e.data.valuation={closed_net:100,open_net:200,combined_net:300,positions:[{symbol:'NFO:BANKNIFTY',strategy:'test',pnl_basis:'mixed',open_net:200}]};
  e.context.renderPaper(e.data);
  assert.match(e.$('paper-valuation').textContent,/not broker profit/);
  assert.match(e.$('paper-positions').children[0].textContent,/Estimated premium fallback/);
  e.data.valuation.open_net=null;e.data.valuation.combined_net=null;e.data.valuation.positions[0].open_net=null;e.data.valuation.positions[0].pnl_basis='unavailable';
  e.context.renderPaper(e.data);
  assert.match(e.$('paper-valuation').textContent,/Open mark Unknown/);
  assert.match(e.$('paper-positions').children[0].textContent,/Price unavailable/);
  assert.equal(e.$('paper-check').disabled,true);
});
test('connected fresh quotes enable paper check only in an open session',()=>{
  const e=setup();freshStream(e.data);
  e.context.renderPaper(e.data);assert.equal(e.$('paper-check').disabled,false);
  e.data.calendar.open_now=false;e.context.renderPaper(e.data);assert.equal(e.$('paper-check').disabled,true);
});
test('stale stream cannot show a receiving badge or enable a test',()=>{
  const e=setup();e.data.stream.state='connected';e.context.renderPaper(e.data);
  assert.notEqual(e.$('stream-badge').textContent,'RECEIVING');
  assert.equal(e.$('paper-check').disabled,true);
});
test('paused or retained exposure blocks new tests and reveals recovery',()=>{
  const e=setup();freshStream(e.data);
  e.data.paper.paused=true;e.data.paper.positions=[{quantity:20,symbol:'NFO:BANKNIFTY',strategy:'test'}];
  e.context.renderPaper(e.data);
  assert.equal(e.$('paper-check').disabled,true);assert.equal(e.$('paper-resume').hidden,false);
  assert.equal(e.$('paper-close-retained').hidden,false);assert.equal(e.$('paper-close-retained').disabled,true);
  assert.equal(e.$('paper-close-note').hidden,false);
  e.data.paper.paused=false;e.context.renderPaper(e.data);assert.equal(e.$('paper-check').disabled,true);
});

test('unconfigured, unsigned and expired sessions block prices and paper with a remedy',()=>{
  for(const status of [{configured:false,connected:false},{configured:true,connected:false},{configured:true,connected:true,session_expires_at:'2020-01-01T00:00:00Z'}]) {
    const e=setup();e.context.kite.status=status;freshStream(e.data);e.context.renderPaper(e.data);
    assert.equal(e.$('paper-check').disabled,true);assert.equal(e.$('stream-start').disabled,true);
    assert.match(e.$('paper-summary').textContent,/Sign in/);assert.equal(e.$('paper-remedy').hidden,false);
    assert.equal(e.$('paper-pause').hidden,true);assert.equal(e.$('paper-state').textContent,'Blocked');
  }
});

test('connection setup highlights exactly the next action',()=>{
  const e=setup();const inputs=[
    [{configured:false,connected:false},null,'configure'],
    [{configured:true,connected:false},null,'login'],
    [{configured:true,connected:true},null,'contracts'],
    [{configured:true,connected:true},{available:true},'start'],
  ];
  for(const [status,contracts,next] of inputs) assert.equal(e.context.workspaceStatus(status,contracts,e.data).next,next);
});

test('stale cached counts and fresh spot without option quotes cannot enable paper testing',()=>{
  const e=setup();freshStream(e.data);
  e.data.stream.quotes.forEach(q=>q.timestamp='2020-01-01T00:00:00Z');e.context.renderPaper(e.data);
  assert.equal(e.$('paper-check').disabled,true);assert.notEqual(e.$('journey-prices').textContent,'Fresh quotes arriving');
  freshStream(e.data);e.data.stream.quotes.pop();e.context.renderPaper(e.data);
  assert.equal(e.$('paper-check').disabled,true);assert.match(e.$('paper-summary').textContent,/both BANKNIFTY/);
});

test('closed market allows historical research but clearly blocks live-quote tests',()=>{
  const e=setup();e.data.calendar.open_now=false;e.context.renderPaper(e.data);
  assert.equal(e.$('journey-session').textContent,'Closed');assert.equal(e.$('stream-start').disabled,true);
  assert.match(e.$('paper-summary').textContent,/market is closed/);
  assert.equal(e.$('research-backtest-action').attributes.href,'#past-data-check');
});

test('paper states distinguish paused, running, completed and synthetic-only evidence',()=>{
  const e=setup();freshStream(e.data);e.context.renderPaper(e.data);assert.equal(e.$('paper-state').textContent,'Not started');
  e.data.checks=[{status:'synthetic_passed',created_at:'now',message:'Synthetic'}];e.context.renderPaper(e.data);assert.equal(e.$('paper-state').textContent,'Not started');
  e.data.checks[0].status='passed';e.context.renderPaper(e.data);assert.equal(e.$('paper-state').textContent,'Completed');
  e.data.busy=true;e.context.renderPaper(e.data);assert.equal(e.$('paper-state').textContent,'Running');
  e.data.busy=false;e.data.paper.paused=true;e.context.renderPaper(e.data);assert.equal(e.$('paper-state').textContent,'Paused');
});

test('lost service status disables controls and does not reuse prior fresh status',()=>{
  const e=setup();freshStream(e.data);e.context.renderPaper(e.data);e.context.paperSnapshot=null;e.context.renderJourney();
  assert.equal(e.$('paper-check').disabled,true);assert.equal(e.$('stream-start').disabled,true);
  assert.equal(e.$('journey-session').textContent,'Not checked');assert.match(e.$('paper-summary').textContent,/unavailable/);
});

test('Indian capital parsing and lot quantities retain explicit assumptions',()=>{
  const e=setup();assert.equal(e.context.parseCapital('10,00,000'),1000000);assert.ok(Number.isNaN(e.context.parseCapital('1e6')));
  assert.ok(Number.isNaN(e.context.parseCapital('-100')));assert.ok(Number.isNaN(e.context.parseCapital('1.50')));
  assert.match(e.context.draftQuantityNote(1,4,{available:true,lot_sizes:[30]}),/120 units/);
  assert.match(e.context.draftQuantityNote(1,4,null),/cannot be confirmed/);
  e.$('model-sell-lots').value='2';e.$('model-ce-lots').value='1';e.$('model-pe-lots').value='0';e.$('model-units').value='30';
  e.$('replay-date').value='2026-09-01';e.$('replay-date').max='2026-09-30';e.context.renderRunBrief();
  assert.match(e.$('run-quantity-summary').textContent,/Shorts 2 lots \(60 units\)/);
  assert.match(e.$('run-quantity-summary').textContent,/not historical lot sizes/i);
  assert.match(e.$('run-date-coverage').textContent,/actual usable coverage is checked/);
});

test('pre-run price coverage belongs to the exact settings and end date only',()=>{
  const e=setup();e.$('replay-date').value='2026-09-01';e.$('replay-date').max='2026-09-30';
  e.context.lastPriceCoverage={end:'2026-09-30',summary:{recorded:{positions:4},mixed:{positions:3},estimated:{positions:7}}};
  e.context.replay.settingsKey=e.context.replaySettingsKey();e.context.renderRunBrief();
  assert.match(e.$('run-price-coverage').textContent,/4 recorded-price legs, 3 mixed-price legs, 7 estimated-price legs/);
  e.$('model-sell-lots').value='2';e.context.renderRunBrief();assert.match(e.$('run-price-coverage').textContent,/Not checked/);
  e.context.replay.settingsKey=e.context.replaySettingsKey();e.$('replay-date').max='2026-10-01';e.context.renderRunBrief();assert.match(e.$('run-price-coverage').textContent,/Not checked/);
});

test('missing retired strategy-detail field cannot block the Zerodha sign-in button',()=>{
  const roots=new Map();
  const $=id=>{
    if(id==='detail-quantity-note') return null;
    if(!roots.has(id)) { const element=new Element(); element.classList={toggle:()=>{}}; roots.set(id,element); }
    return roots.get(id);
  };
  const context=vm.createContext({$,kite:{status:{configured:true,connected:false},instruments:null,quote:null,busy:false,expiryTimer:null},
    syncReplayDate:()=>{},kiteConnected:()=>false,kiteSessionExpired:()=>false,quoteFreshness:()=>'',renderReplayAvailability:()=>{},
    selectedStrategy:()=>({lots:4}),draftQuantityNote:()=> '4 lots',updateDraftQuantity:()=>{context.updated=true;},marketTime:value=>value,window:{clearTimeout:()=>{},setTimeout:()=>0},Date});
  vm.runInContext(source.slice(source.indexOf('  function renderKite('),source.indexOf('  async function refreshKiteStatus(')),context);
  assert.doesNotThrow(()=>context.renderKite());
  assert.equal(roots.get('kite-login').disabled,false);
  assert.equal(context.updated,true);
});
test('busy check does not permit duplicate clicks and result list is bounded',()=>{
  const e=setup();e.data.busy=true;e.data.checks=Array.from({length:10},(_,i)=>({status:'blocked',created_at:String(i),message:'No prices'}));
  e.context.renderPaper(e.data);
  assert.equal(e.$('paper-check').textContent,'Testing…');assert.equal(e.$('paper-results').children.length,3);
});
