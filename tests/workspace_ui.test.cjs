// Exercise the production navigation and selection code without a browser dependency.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.hidden = false; this.value = ''; this.textContent = ''; this.dataset = {};
    this.classes = new Set();
    this.classList = { toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name) };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  focus() { this.focused = true; }
  closest() { return this; }
}

function environment(hash = '#main') {
  const roots = new Map();
  const $ = (id) => { if (!roots.has(id)) roots.set(id, new Element()); return roots.get(id); };
  const hashes = ['#main', '#strategies-heading', '#deployments', '#research', '#kite-connection', '#positions-orders', '#activity'];
  const links = hashes.map((href) => { const link = new Element('a'); link.setAttribute('href', href); return link; });
  const views = ['overview', 'strategies', 'deployments', 'backtests', 'market-data', 'safety-checks', 'paper-trading', 'research', 'positions', 'activity'].map((view) => {
    const node = new Element('section'); node.dataset.view = view; return node;
  });
  const docEvents = {}, winEvents = {}, historyCalls = [];
  const location = { hash, pathname: '/', search: '?kite=connected' };
  const history = {};
  for (const name of ['pushState', 'replaceState']) history[name] = (_, __, url) => { historyCalls.push([name, url]); location.hash = url.slice(url.indexOf('#')); };
  const window = { location, history, addEventListener: (name, fn) => { winEvents[name] = fn; }, scrollTo: () => { window.scrolled = true; } };
  const document = { createElement: (tag) => new Element(tag), title: '', addEventListener: (name, fn) => { docEvents[name] = fn; },
    querySelectorAll: (selector) => selector === '.nav-item' ? links : views };
  const context = vm.createContext({ $, document, window, Intl, Date, JSON, console, state: {strategies: [], recent_runs: []}, replay: {settingsKey:null}, renderJourney:()=>{}, renderRunBrief:()=>{} });
  vm.runInContext(source.slice(source.indexOf('  function el('), source.indexOf('  function friendly(')), context);
  vm.runInContext(source.slice(source.indexOf('  const workspaceRoutes ='), source.indexOf('  $("strategy-form").addEventListener')), context);
  return {context, $, roots, links, views, window, document, docEvents, winEvents, historyCalls};
}

test('every route synchronises the view, sidebar, breadcrumb and page title', () => {
  const e = environment();
  for (const [hash, view, title] of [
    ['#main','overview','Trading desk'], ['#strategies-heading','strategies','Strategies'],
    ['#deployments','deployments','Paper deployments'],
    ['#past-data-check','backtests','Model simulator'], ['#kite-connection','market-data','Connections'], ['#safety-checks','safety-checks','Order checks'], ['#paper-trading','paper-trading','Paper trading'],
    ['#research','research','Research'], ['#positions-orders','positions','Positions & orders'], ['#activity','activity','Activity'],
  ]) {
    e.window.location.hash = hash; e.context.updateNavigation();
    assert.deepEqual(e.views.filter(v => !v.hidden).map(v => v.dataset.view), [view]);
    const nav={'#past-data-check':'#research','#safety-checks':'#research','#paper-trading':'#positions-orders'}[hash] || hash;
    assert.deepEqual(e.links.filter(l => l.getAttribute('aria-current')).map(l => l.getAttribute('href')), [nav]);
    assert.equal(e.$('page-title').textContent, title); assert.equal(e.$('page-location').textContent, title);
    assert.equal(e.document.title, title + ' · Private Algo Desk');
    assert.equal(e.$('create-strategy').hidden, view !== 'strategies');
  }
});

test('backtest deep links do not select market data and reload starts at heading', () => {
  const e = environment('#past-data-check');
  assert.equal(e.$('page-title').textContent, 'Model simulator');
  assert.equal(e.links[3].getAttribute('aria-current'), 'page');
  e.window.scrolled = false; e.winEvents.load(); assert.equal(e.window.scrolled, true);
  assert.equal(e.window.history.scrollRestoration, 'manual');
});

test('unknown and empty routes normalise to overview without dropping the query', () => {
  for (const hash of ['', '#does-not-exist']) {
    const e = environment(hash);
    assert.deepEqual(e.historyCalls[0], ['replaceState', '/?kite=connected#main']);
    assert.equal(e.$('page-title').textContent, 'Trading desk');
  }
});

test('navigation supports history, focus, same-link return and native modified clicks', () => {
  const e = environment(); let prevented = false;
  const event = {target:e.links[3], button:0, preventDefault:()=>{prevented=true;}};
  e.docEvents.click(event);
  assert.equal(prevented,true); assert.equal(e.$('page-title').focused,true);
  assert.deepEqual(e.historyCalls.at(-1), ['pushState','/?kite=connected#research']);
  const pushes = e.historyCalls.length;
  e.window.scrolled = false; e.docEvents.click(event);
  assert.equal(e.historyCalls.length,pushes); assert.equal(e.window.scrolled,true);
  prevented = false; e.docEvents.click({...event,ctrlKey:true}); assert.equal(prevented,false);
  e.window.location.hash = '#main'; e.winEvents.popstate(); assert.equal(e.$('page-title').textContent,'Trading desk');
  e.window.location.hash = '#past-data-check'; e.winEvents.hashchange(); assert.equal(e.$('page-title').textContent,'Model simulator');
});

test('navigation preserves report DOM, form values and expanded details', () => {
  const e = environment('#past-data-check'); const report = e.$('replay-result');
  report.append(new Element('table')); e.$('replay-date').value = '2025-01-01'; e.$('settings').open = true;
  e.window.location.hash='#strategies-heading'; e.context.updateNavigation();
  e.window.location.hash='#past-data-check'; e.context.updateNavigation();
  assert.equal(report.children.length,1); assert.equal(e.$('replay-date').value,'2025-01-01'); assert.equal(e.$('settings').open,true);
});

test('all six replay inputs mark existing results stale and reverting restores the match', () => {
  const e = environment();
  vm.runInContext(source.slice(source.indexOf('  function replaySettingsKey('),source.indexOf('  $("replay-date").addEventListener')),e.context);
  const ids=['replay-date','strategy-mode','model-sell-lots','model-ce-lots','model-pe-lots','model-units'];
  for (const id of ids) e.$(id).value=id;
  e.context.replay.settingsKey=e.context.replaySettingsKey();
  for (const id of ids) {
    e.$(id).value='changed'; e.context.updateReplaySettingsNotice(); assert.equal(e.$('replay-settings-changed').hidden,false);
    e.$(id).value=id; e.context.updateReplaySettingsNotice(); assert.equal(e.$('replay-settings-changed').hidden,true);
  }
  e.context.replay.settingsKey=null; e.$('replay-date').value='new'; e.context.updateReplaySettingsNotice();
  assert.equal(e.$('replay-settings-changed').hidden,true);
});

function selectionEnvironment() {
  const e=environment('#safety-checks');
  e.context.state.strategies=[{id:'a',name:'Strategy A'},{id:'b',name:'Strategy B'}];
  e.context.state.recent_runs=[{id:'a1',strategy_id:'a',scenario_id:'scenario-a',title:'Check A',result:'passed'},{id:'b1',strategy_id:'b',scenario_id:'scenario-b',title:'Check B',result:'passed'}];
  Object.assign(e.context,{renderCards:()=>{},renderDetail:()=>e.context.renderStrategySelect(),clearResult:()=>{e.$('check-result').hidden=true;},
    announce:()=>{},formattedDate:()=>'',friendly:(v)=>v,resultBadge:()=>new Element('span'),showError:()=>{},api:async()=>({run:{id:'a1',strategy_id:'a'}}),renderRun:()=>{e.loaded=true;}});
  vm.runInContext('let selectedId="a", running=false, showingRunId=null;',e.context);
  vm.runInContext(source.slice(source.indexOf('  function selectedStrategy('),source.indexOf('  function renderCards(')),e.context);
  vm.runInContext(source.slice(source.indexOf('  function renderRecentRuns('),source.indexOf('  async function loadState(')),e.context);
  e.context.renderRecentRuns(); return e;
}

test('strategy choice and recent checks agree while overview counts remain workspace-wide', () => {
  const e=selectionEnvironment();
  assert.equal(e.$('recent-runs').children[0].children[0].textContent,'Check A');
  e.context.selectStrategy('b');
  assert.equal(e.$('check-strategy-select').value,'b'); assert.equal(e.$('check-result').hidden,true);
  assert.equal(e.$('recent-runs').children.length,1); assert.equal(e.$('recent-runs').children[0].children[0].textContent,'Check B');
  assert.equal(e.$('check-count').textContent,2);
  e.context.selectStrategy('missing'); assert.equal(e.context.selectedStrategy().id,'b');
  vm.runInContext('running=true',e.context); e.context.selectStrategy('a'); assert.equal(e.context.selectedStrategy().id,'b');
});

test('a slow saved-check response cannot overwrite a newly selected strategy', async () => {
  const e=selectionEnvironment(); let finish;
  e.context.api=()=>new Promise(resolve=>{finish=resolve;});
  const loading=e.$('recent-runs').children[0].listeners.click();
  e.context.selectStrategy('b'); finish({run:{id:'a1',strategy_id:'a'}}); await loading;
  assert.equal(e.loaded,undefined); assert.equal(e.context.selectedStrategy().id,'b');
});

test('a mismatched saved-check response is rejected', async () => {
  const e=selectionEnvironment(); let error;
  e.context.api=async()=>({run:{id:'b1',strategy_id:'b'}}); e.context.showError=(_,message)=>{error=message;};
  await e.$('recent-runs').children[0].listeners.click();
  assert.match(error,/another strategy/); assert.equal(e.loaded,undefined);
});

test('mobile menu opens explicitly, closes on navigation and supports Escape', () => {
  const e=environment();
  e.$('mobile-menu').listeners.click();
  assert.equal(e.$('workspace-nav').dataset.open,'true');
  assert.equal(e.$('mobile-menu').getAttribute('aria-expanded'),'true');
  e.window.location.hash='#paper-trading';e.context.updateNavigation();
  assert.equal(e.$('workspace-nav').dataset.open,'false');
  e.$('mobile-menu').listeners.click();e.$('workspace-nav').listeners.keydown({key:'Escape'});
  assert.equal(e.$('workspace-nav').dataset.open,'false');assert.equal(e.$('mobile-menu').focused,true);
  e.$('mobile-menu').listeners.click();e.$('mobile-menu').listeners.keydown({key:'Escape'});
  assert.equal(e.$('workspace-nav').dataset.open,'false');assert.equal(e.$('mobile-menu').getAttribute('aria-expanded'),'false');
});

test('draft detail deep link keeps Strategies selected and Back restores the list', () => {
  const e=environment('#strategy-detail');
  assert.equal(e.links[1].getAttribute('aria-current'),'page');
  assert.equal(e.$('strategy-workspace').dataset.detail,'true');
  e.window.location.hash='#strategies-heading';e.context.updateNavigation();
  assert.equal(e.$('strategy-workspace').dataset.detail,'false');
});

test('repeated example checks do not inflate distinct scenario count', () => {
  const e=selectionEnvironment();
  e.context.state.recent_runs.push({id:'a2',strategy_id:'a',scenario_id:'scenario-a',title:'Check A',result:'passed'});
  e.context.renderRecentRuns();assert.equal(e.$('check-count').textContent,2);
  assert.match(e.$('check-count-note').textContent,/3 recent example runs/);
  assert.match(e.$('recent-runs').children[0].children[0].children[0].textContent,/Draft version not recorded.*Check a1/);
});
