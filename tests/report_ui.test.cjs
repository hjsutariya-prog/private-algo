// Dependency-free tests of the production report model and DOM rendering.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ''; this.value = ''; this.attributes = {}; this.listeners = {}; this.hidden = false; }
  append(...nodes) { this.children.push(...nodes); if (this.tagName === 'select' && !this.value && nodes[0]) this.value = nodes[0].value; }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  focus() { this.focused = true; }
  fire(type, extra = {}) { this.listeners[type]?.({ preventDefault() {}, ...extra }); }
}
function walk(node, predicate) { return [...(predicate(node) ? [node] : []), ...node.children.flatMap((child) => walk(child, predicate))]; }
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
function byTag(node, tag) { return walk(node, (el) => el.tagName === tag); }
function byLabel(node, label) { return walk(node, (el) => el.attributes['aria-label'] === label)[0]; }
function env() {
  const document = { createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag) };
  const roots = { 'replay-result': new Element('div'), 'replay-empty': new Element('div') };
  const context = vm.createContext({ document, Intl, Date, Map, Set, console,
    $: (id) => roots[id], pointFormat: new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }),
    replayDateFormat: new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeZone: 'Asia/Kolkata' }),
    marketTime: (value) => new Date(value).toISOString(), validReplayTime: (v) => typeof v === 'string' && Number.isFinite(Date.parse(v)),
    replayTime: (v) => v.slice(11, 19) + ' IST', replayDay: (v) => v, friendly: (v) => v,
  });
  const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
  vm.runInContext(source.slice(source.indexOf('  function el('), source.indexOf('  function friendly(')), context);
  vm.runInContext(source.slice(source.indexOf('  function spotPoints('), source.indexOf('  function renderKite(')), context);
  return { ...context, roots };
}
const signal = (day, side = 'bull') => ({ side, level: 54000, bar_start: day + 'T09:15:00+05:30', bar_end: day + 'T10:15:00+05:30', scheduled_check_at: day + 'T10:15:20+05:30' });
const day = (date = '2026-09-28') => ({ date, status: 'complete', signal_count: 1, signals: [signal(date)], received_candle_count: 375, completed_hour_count: 6, bars: [] });
const position = (extra = {}) => ({ position_id: 'bull-1', side: 'bull', status: 'closed', entry_at: '2026-09-28T10:16:00+05:30', signal_at: '2026-09-28T10:15:20+05:30', signal_level: 54000, entry_spot: 54100, stop_spot: 53600, target_spot: 55100, exit_at: '2026-09-28T12:01:00+05:30', exit_spot: 55100, exit_reason: 'target', exit_price_basis: 'threshold_touched_within_minute', spot_points_proxy: 1000, ...extra });
const range = (days = [day()]) => ({ start_date: days[0].date, end_date: days.at(-1).date, days, total_signals: days.reduce((n, d) => n + (d.signal_count || 0), 0), source: 'TEST ONLY' });
const proxy = (extra = {}) => ({ status: 'complete', closed_positions: [position()], open_positions: [], events: [], starting_state: 'flat', open_spot_points_proxy: 0, session_count: 1, last_session: '2026-09-28', assumptions: [], ...extra });

test('permanent source summary distinguishes recorded, mixed, model-only and spot-only results', () => {
  const app=env();
  for(const [counts,expected] of [
    [[5,2,0],/recorded and estimated prices/], [[5,0,0],/recorded price observations/],
    [[0,0,7],/a pricing model/], [[3,0,4],/recorded and estimated prices/],
  ]) {
    const output=text(app.reportPricingSummary({pricing_summary:{recorded:{positions:counts[0]},mixed:{positions:counts[1]},estimated:{positions:counts[2]}}}));
    assert.match(output,expected);assert.match(output,/price proxies, not broker fills/);
    assert.match(output,/price legs/);
  }
  assert.match(text(app.reportPricingSummary(null)),/Spot-only result/);
});

test('complete report totals sum finite closed points without lot multiplication', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ closed_positions: [position(), position({ spot_points_proxy: -500, exit_reason: 'stop' })] }));
  assert.equal(model.closedPoints, 500); assert.equal(model.openPoints, 0); assert.equal(model.partial, false);
  assert.equal(model.targets, 1); assert.equal(model.stops, 1);
});
test('missing final-session minute is a review date even with six complete signal bars', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ status: 'partial', blocked_from: { date: '2026-09-28' }, open_spot_points_proxy: 250 }));
  assert.equal(model.gapDates.size, 1); assert.equal(model.days[0].category, 'review'); assert.equal(model.signalSessions, 1);
  assert.equal(model.openPoints, null); assert.equal(model.closedPoints, 1000);
});
test('verified holidays excluded, unverified blank weekdays and special weekends remain review dates', () => {
  const app = env(); const model = app.reportModel(range([
    { date: '2026-09-14', status: 'no_price_data', received_candle_count: 0, verified_closed_date: 'Holiday' },
    { date: '2026-09-15', status: 'no_price_data', received_candle_count: 0 },
    { date: '2026-09-19', status: 'weekend', received_candle_count: 200 },
  ]), proxy({ status: 'partial', blocked_from: { date: '2026-09-19' } }));
  assert.equal(model.gapDates.size, 2); assert.equal(model.days[0].category, 'closed'); assert.equal(model.days[2].category, 'review');
});
test('unavailable position calculation does not invent zero points', () => {
  const app = env(); const model = app.reportModel(range(), null);
  assert.equal(model.available, false); assert.equal(model.closedPoints, null); assert.equal(model.openPoints, null);
  app.renderSpotRangeResult(range(), null);
  assert.match(text(app.roots['replay-result']), /No position result available/);
});
test('signals join decisions by direction AND review time', () => {
  const app = env(); const d = day(); d.signals.push(signal(d.date, 'bear')); d.signal_count = 2;
  const model = app.reportModel(range([d]), proxy({ events: [
    { type: 'proxy_entry', side: 'bull', signal_at: d.signals[0].scheduled_check_at, at: position().entry_at, spot: 54100 },
    { type: 'signal_skipped', side: 'bear', signal_at: d.signals[1].scheduled_check_at, reason: 'side_already_open' },
  ] }));
  assert.equal(model.signals[0].outcome, 'Position opened'); assert.equal(model.signals[1].outcome, 'Skipped');
  assert.match(model.signals[1].detail, /already open/);
});
test('signals after a gap stay not modelled, never assumed to be trades', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ status: 'partial' }));
  assert.equal(model.signals[0].outcome, 'Not modelled'); assert.match(model.signals[0].detail, /not evidence of a trade/);
});
test('ledger hides stale open points but retains historical stop and target levels', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ status: 'partial', open_positions: [position({ status: 'open', spot_points_proxy: 321.25, mark_spot: 54421.25, mark_at: position().entry_at })] }));
  const ledger = app.reportPositions(model); const output = text(ledger);
  assert.match(output, /Unknown after gap/); assert.doesNotMatch(output, /\+321.25/);
  assert.match(output, /53,600 \/ 55,100/); assert.match(output, /During this minute/);
  assert.doesNotMatch(output, /IST IST/);
});
test('table pages, reverses, filters and searches without dropping rows', () => {
  const app = env(); const rows = Array.from({ length: 45 }, (_, id) => ({ id, filters: [id % 2 ? 'odd' : 'even'] }));
  const table = app.reportTable({ title: 'Test', rows, columns: ['ID'], filters: [['all','All'],['even','Even']], searchText: (row) => String(row.id), cells: (row) => [String(row.id)], empty: 'Empty' });
  const body = byTag(table, 'tbody')[0], buttons = byTag(table, 'button');
  assert.equal(body.children.length, 20); assert.equal(text(body.children[0]).trim(), '44');
  buttons.find((b) => b.textContent === 'Next →').fire('click'); assert.equal(text(body.children[0]).trim(), '24');
  buttons.find((b) => b.textContent === 'Next →').fire('click'); assert.equal(body.children.length, 5);
  assert.equal(buttons.find((b) => b.textContent === 'Next →').disabled, true);
  const filter = byLabel(table, 'Test filter'); filter.value = 'even'; filter.fire('change');
  assert.match(text(table), /1–20 of 23 rows/);
  const search = byTag(table, 'input')[0]; search.value = '44'; search.fire('input'); assert.equal(body.children.length, 1);
  search.value = 'nothing'; search.fire('input'); assert.match(text(table), /No rows match/);
  search.value = ''; search.fire('input'); const order = byLabel(table, 'Test order'); order.value = 'oldest'; order.fire('change');
  assert.equal(text(body.children[0]).trim(), '0');
});
test('five-year tables remain bounded to 20 rows per view', () => {
  const app = env(); const rows = Array.from({ length: 1827 }, (_, id) => ({ id, filters: [] }));
  const view = app.reportTable({ title: 'Long range', rows, columns: ['ID'], filters: [['all','All']], searchText: (row) => String(row.id), cells: (row) => [row.id], empty: 'Empty' });
  assert.equal(byTag(view, 'tbody')[0].children.length, 20); assert.match(text(view), /of 1827 rows/);
});
test('data quality defaults to gaps and includes missing-minute detail', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ status: 'partial', blocked_from: { date: '2026-09-28', reason: 'Missing final minute', missing_times: ['2026-09-28T15:29:00+05:30'] } }));
  const view = app.reportQuality(model); assert.equal(byLabel(view, 'Data quality filter').value, 'review');
  assert.match(text(view), /15:29:00 IST/); assert.match(text(view), /Missing final minute/);
});
test('tabs have one selected panel, lazy creation and keyboard navigation', () => {
  const app = env(); app.renderSpotRangeResult(range(), proxy()); const root = app.roots['replay-result'];
  const tabs = walk(root, (n) => n.attributes.role === 'tab'), panels = walk(root, (n) => n.attributes.role === 'tabpanel');
  assert.equal(tabs.length, 6); assert.equal(panels.filter((n) => !n.hidden).length, 1); assert.equal(panels[1].children.length, 0);
  tabs[0].fire('keydown', { key: 'ArrowRight' }); assert.equal(tabs[1].attributes['aria-selected'], 'true'); assert.equal(tabs[1].focused, true);
  assert.equal(panels[1].children.length, 1); assert.equal(panels[0].hidden, true);
  tabs[1].fire('keydown', { key: 'End' }); assert.equal(tabs[5].attributes['aria-selected'], 'true');
  assert.match(text(panels[5]), /Hedge history unavailable/);
  assert.equal(app.roots['replay-empty'].hidden, true);
});
test('hedge plan never presents planned rules as historical positions', () => {
  const app = env(); const output = text(app.reportHedges());
  assert.match(output, /historical/i); assert.match(output, /not yet calculated/); assert.match(output, /Buy replacement fully, then sell/);
  assert.match(output, /Monday 15:15 expiry-week opening/); assert.match(output, /Call hedge/); assert.match(output, /Put hedge/);
});
test('chart is ordered by exit and names spot points rather than cash equity', () => {
  const app = env(); const model = app.reportModel(range(), proxy({ closed_positions: [position({ exit_at: '2026-09-29T12:00:00+05:30' }), position({ exit_at: '2026-09-28T12:00:00+05:30', spot_points_proxy: -500 })] }));
  assert.equal(model.chronological[0].spot_points_proxy, -500);
  const chart = app.reportChart(model); assert.match(byTag(chart, 'svg')[0].attributes['aria-label'], /spot points/);
  assert.match(text(chart), /Excludes open positions, hedges and all costs/);
});

test('advanced trade statistics stay optional and never call spot points cash profit', () => {
  const app = env();
  const model = app.reportModel(range(), proxy({ closed_positions: [
    position({ spot_points_proxy: 1000, exit_at: '2026-09-28T11:00:00+05:30' }),
    position({ spot_points_proxy: -500, exit_at: '2026-09-28T12:00:00+05:30' }),
    position({ spot_points_proxy: 1000, exit_at: '2026-09-28T13:00:00+05:30' }),
  ] }));
  const overview = app.reportOverview(model), disclosure = byTag(overview, 'details').find((item) => text(item.children[0]) === 'Advanced trade statistics');
  assert.ok(disclosure); assert.notEqual(disclosure.open, true);
  const output = text(disclosure);
  assert.match(output, /Positive closes/); assert.match(output, /66.7%/); assert.match(output, /Maximum drawdown/);
  assert.match(output, /Profit factor/); assert.match(output, /not option cash profit/);
});

function optionModel() {
  const entryCharges = { total: 25, brokerage: 20, exchange: 1, ipft: 0, sebi: 0, gst: 4, stamp_duty: 0, stt: 0, effective_from: '2026-04-01' };
  const hedge = { position_id: 'option-1', ledger: 'ce_hedge', option_type: 'CE', direction: 'BUY', status: 'open', parent_spot_trade: null,
    entry_at: '2026-09-28T09:15:00+05:30', entry_spot: 56000, entry_premium: 500, entry_reason: 'initial_hedge', expiry: '2026-09-29', strike: 57000,
    lots: 4, units_per_lot: 30, quantity: 120, mark_at: '2026-09-28T12:00:00+05:30', mark_spot: 56100, mark_premium: 550, entry_charges: entryCharges,
    valuation: { net_spot_movement: 100, movement_contribution: 50, time_decay: 0, elapsed_minutes: 165 }, premium_points_pnl: 50, gross_pnl: 6000, charges: 25, net_pnl: 5975, trigger_spot: 57000, replacement_distance: 1000 };
  const zero = { realised_gross: 0, realised_charges: 0, realised_net: 0, open_gross: 0, open_entry_charges: 0, open_net: 0, closed_count: 0, open_count: 0 };
  return { status: 'complete', as_of: hedge.mark_at, blocked_from: null,
    config: { instrument: 'BANKNIFTY', version: 'test', sell_lots: 4, ce_hedge_lots: 4, pe_hedge_lots: 4, units_per_lot: 30, sold_entry_premium: '1000', sold_daily_decay: '20', hedge_daily_decay: '30', band_points: '200', initial_delta: '0.50', delta_step: '0.05', hedge_premium_divisor: '10000', hedge_premium_step: '100', replacement_multiple: '2' },
    calendar_version: 'test-calendar', fee_version: 'test-fees', assumptions: ['Invented prices for testing.'], ledgers: { selling: [], ce_hedge: [hedge], pe_hedge: [] },
    summary: { selling: zero, ce_hedge: { ...zero, open_count: 1, open_gross: 6000, open_entry_charges: 25, open_net: 5975 }, pe_hedge: zero },
    combined: { ...zero, open_gross: 6000, open_entry_charges: 25, open_net: 5975, total_net: 5975 }, charges: entryCharges,
    orders: [{ order_id: 'order-1', position_id: 'option-1', ledger: 'ce_hedge', at: hedge.entry_at, side: 'BUY', premium: 500, quantity: 120, lots: 4, charges: entryCharges, reason: 'initial_hedge' }], rollovers: [], pending_replacements: [] };
}

test('new report defaults to option performance and keeps detailed cash and spot views separate', () => {
  const app = env(); app.renderSpotRangeResult(range(), proxy(), optionModel());
  const root = app.roots['replay-result'], tabs = walk(root, (n) => n.attributes.role === 'tab');
  assert.equal(tabs.length, 7); assert.equal(tabs[0].attributes['aria-selected'], 'true');
  const output = text(root); assert.match(output, /Option performance/); assert.match(output, /₹5,975.00/);
  assert.doesNotMatch(output, /undefined/);
  assert.match(output, /Closed spot points/); assert.match(output, /not realised cash/);
  const spotPanel = walk(root, (n) => n.id === 'report-panel-overview')[0];
  const cashPanel = walk(root, (n) => n.id === 'report-panel-options')[0];
  const performancePanel = walk(root, (n) => n.id === 'report-panel-performance')[0];
  assert.equal(spotPanel.hidden, true);
  assert.equal(cashPanel.hidden, true); assert.equal(performancePanel.hidden, false);
  tabs[5].fire('click');
  assert.match(text(cashPanel), /Modelled option premiums/); assert.match(text(cashPanel), /do not subtract them again/);
  assert.match(text(cashPanel), /No selling entries/);
  assert.match(text(spotPanel), /Closed spot points/);
  assert.doesNotMatch(text(cashPanel), /Closed spot points/);
  assert.match(text(cashPanel), /4 selling \/ 4 call hedge \/ 4 put hedge lots/);
  assert.match(text(cashPanel), /Valued through/);
});

test('option performance uses closed ledger cash outcomes and excludes open estimates from the path', () => {
  const app = env(), model = optionModel();
  const closed = (id, at, net) => ({ ...model.ledgers.ce_hedge[0], position_id: id, status: 'closed', exit_at: at, net_pnl: net, gross_pnl: net, charges: 0, exit_reason: 'target' });
  model.ledgers = {
    selling: [closed('sell-1', '2026-09-01T12:00:00+05:30', 200), closed('sell-2', '2026-09-02T12:00:00+05:30', -500)],
    ce_hedge: [closed('ce-1', '2026-10-02T12:00:00+05:30', 100), model.ledgers.ce_hedge[0]], pe_hedge: [],
  };
  model.combined = { realised_net: -200, open_net: 5975, total_net: 5775 };
  model.pricing_summary = { recorded: { positions: 2 }, mixed: { positions: 1 }, estimated: { positions: 1 } };
  const performance = app.optionPerformance(model);
  assert.equal(performance.closedLegs.length, 3); assert.equal(performance.openLegs.length, 1);
  assert.equal(performance.closedNet, -200); assert.equal(performance.maximumDrawdown, -500);
  assert.equal(performance.currentDrawdown, -400); assert.equal(performance.months.length, 2);
  const output = text(app.reportOptionPerformance(model));
  assert.match(output, /Cumulative modelled ₹ result/); assert.match(output, /3 completed legs/);
  assert.match(output, /Maximum drawdown/); assert.match(output, /Open legs excluded from path 1/);
  assert.match(output, /CAGR, Sharpe and similar return ratios are intentionally not shown/);
});

test('option performance shows visible daily and monthly end-of-session P&L when supplied by the engine', () => {
  const app = env(), model = optionModel();
  model.reporting = { return_base: 100000 };
  model.daily_pnl = [
    { date: '2026-09-29', at: '2026-09-29T15:30:00+05:30', realised_net: 0, open_net: -120, total_net: -120, daily_net: -120, open_leg_count: 3, valuation: 'end_of_session_mark', approximate: false },
    { date: '2026-09-30', at: '2026-09-30T15:30:00+05:30', realised_net: 0, open_net: 120, total_net: 120, daily_net: 240, open_leg_count: 3, valuation: 'end_of_session_mark', approximate: false },
    { date: '2026-10-01', at: '2026-10-01T15:30:00+05:30', realised_net: 0, open_net: 5975, total_net: 5975, daily_net: 5855, open_leg_count: 3, valuation: 'end_of_session_mark', approximate: true },
  ];
  const performance = app.optionPerformance(model);
  assert.equal(performance.dailyPnl.length, 3); assert.equal(performance.dailyMonths.length, 2);
  assert.equal(performance.dailyPnl.at(-1).totalNet, model.combined.total_net);
  assert.equal(performance.dailyMonths[0].net, 120); assert.equal(performance.dailyMonths[1].net, 5855);
  const panel = app.reportOptionPerformance(model), output = text(panel);
  assert.match(output, /Daily P&L/); assert.match(output, /Monthly performance/); assert.match(output, /end-of-session option marks/);
  assert.match(output, /30 Sept 2026/); assert.match(output, /Approximate/);
  assert.match(output, /5.9%/); assert.match(output, /View daily figures/); assert.match(output, /View monthly figures/);
  const dailySquares = walk(panel, n => n.className === 'report-heatmap-days')[0]; assert.equal(dailySquares.children.length, 365);
  const monthLabels = byTag(panel, 'span').filter((node) => /^report-heatmap-week-\d+$/.test(node.attributes.class || ''));
  assert.equal(monthLabels.length, 12); assert.equal(monthLabels[0].attributes.class, 'report-heatmap-week-1');
  const chart = byTag(panel, 'svg')[0]; assert.match(chart.attributes['aria-label'], /end-of-session option profit and loss/);
});

test('compact option breakdown retains gross amounts and fees in expandable details', () => {
  const app = env(); const panel = app.reportOptionModel(optionModel());
  const compact = walk(panel, n => n.className === 'report-table option-net-summary')[0];
  assert.equal(byTag(compact, 'thead')[0].children[0].children.length, 4);
  assert.match(text(compact), /₹5,975.00/);
  const detail = byTag(panel, 'details').find(n => text(n.children[0]) === 'Gross profit & charge breakdown');
  assert.ok(detail); assert.notEqual(detail.open, true);
  assert.match(text(detail), /Closed gross/); assert.match(text(detail), /Open entry charges/);
});

test('compact option breakdown does not invent combined net when a value is missing', () => {
  const app = env(), model = optionModel(); model.summary.selling.realised_net = null;
  const panel = app.reportOptionModel(model);
  const compact = walk(panel, n => n.className === 'report-table option-net-summary')[0];
  const selling = byTag(compact, 'tbody')[0].children[0];
  assert.equal(selling.children[3].textContent, '—');
});

test('hedge records show entry charges only and independent calculation details', () => {
  const app = env(); const output = text(app.reportOptionPositions(optionModel(), 'hedges'));
  assert.match(output, /Independent CE hedge/); assert.match(output, /Not charged — still open/);
  assert.match(output, /Premium movement contribution/); assert.match(output, /Time decay/);
  assert.match(output, /120 units/); assert.match(output, /57,000/); assert.match(output, /Open estimate/);
});

test('unavailable and partial models never claim a full period cash result', () => {
  const app = env(), model = optionModel();
  model.status = 'unavailable'; model.blocked_from = { reason: 'Missing fee schedule' };
  let output = text(app.reportOptionModel(model));
  assert.match(output, /unavailable/); assert.match(output, /Missing fee schedule/); assert.doesNotMatch(output, /₹5,975/);
  model.status = 'partial'; output = text(app.reportOptionModel(model));
  assert.match(output, /checked portion only/); assert.match(output, /Later outcomes are unknown/);
  assert.match(text(app.reportOptionModel(null)), /Run the backtest again/);
});

test('whole-empty-day holiday is explicit, not a zero-signal or verified day', () => {
  const app = env(); const model = app.reportModel(range([{ date: '2026-09-25', status: 'holiday', assumed_holiday: true, received_candle_count: 0 }]), proxy());
  assert.equal(model.gapDates.size, 0); assert.equal(model.days[0].displayStatus, 'Assumed holiday');
  assert.equal(model.signalSessions, 0); assert.match(text(app.reportQuality(model)), /failed requests are not holidays/);
  assert.equal(model.approximate, true);
});

test('assumed whole days warn in both reports even with zero missing minute count', () => {
  const app = env(), d = {date:'2026-09-25',status:'holiday',assumed_holiday:true,received_candle_count:0};
  const r = range([d]), p = proxy({assumed_holidays:['2026-09-25'],approximate:true});
  const o = {...optionModel(), approximate:true, validation_status:'approximate', skipped_minute_count:0,
    approximate_due_to_assumed_holidays:true, warnings:['Unverified missing whole days: 2026-09-25. Totals are approximate.']};
  app.renderSpotRangeResult(r,p,o);
  const root = app.roots['replay-result'];
  const tabs = walk(root, (n) => n.attributes.role === 'tab');
  tabs[5].fire('click');
  const output = text(root);
  assert.match(output,/APPROXIMATE · UNVERIFIED DAYS/);
  assert.match(output,/Option simulation finished — approximate result/);
  assert.match(output,/2026-09-25/);
  assert.doesNotMatch(output,/data and events validated|0 missing minutes skipped/);
});

test('uncertain positions remain visible and are never normal closed trades', () => {
  const app = env(), p = proxy({closed_positions:[],uncertain_positions:[position({status:'uncertain',spot_points_proxy:null,exit_spot:null})]});
  const model = app.reportModel(range(),p);
  assert.equal(model.closed.length,0); assert.equal(model.uncertain.length,1);
  assert.match(text(app.reportPositions(model)),/Uncertain exit/);
  app.renderSpotRangeResult(range(),p);
  assert.match(text(app.roots['replay-result']),/Closed totals exclude these positions/);
});

test('named strategy is visible and an old report is not relabelled comparison mode', () => {
  const app = env();
  const o = {...optionModel(), validation_status:'validated', strategy:{label:'Hourly boundary · uncertain collisions'},event_validation:{supplied:4,processed:4}};
  assert.match(text(app.reportOptionModel(o)),/Hourly boundary · uncertain collisions/);
  assert.match(text(app.reportOptionModel(o)),/4 \/ 4 supplied strategy events processed/);
  assert.match(text(app.reportOptionModel(optionModel())),/Legacy 20-second review/);
});

test('special day reports one full hour, ignored short period and closed-market skip', () => {
  const app = env(), d = day('2024-03-02');
  d.completed_hour_count = 1; d.expected_hour_count = 1; d.special_session = true;
  d.ignored_bars = [{ bar_start: '2024-03-02T09:15:00+05:30', bar_end: '2024-03-02T10:00:00+05:30', received_unique_minutes: 45, reason: 'short_session_fragment' }];
  const model = app.reportModel(range([d]), proxy({ events: [{ type: 'signal_skipped', side: 'bull', signal_at: d.signals[0].scheduled_check_at, reason: 'market_closed_at_entry' }] }));
  assert.equal(model.checkedHours, 1); assert.equal(model.days[0].displayStatus, 'Special session checked');
  const output = text(app.reportQuality(model)); assert.match(output, /1 \/ 1/); assert.match(output, /45 unique minutes/);
  assert.doesNotMatch(output, /6 \/ 6/); assert.match(model.signals[0].detail, /not carried to the next session/);
});

test('valid hours on a partial day stay visible without claiming position coverage', () => {
  const app = env(), d = day(); d.status = 'partial_hours'; d.completed_hour_count = 5; d.expected_hour_count = 6;
  const model = app.reportModel(range([d]), proxy({ status: 'partial' }));
  assert.equal(model.signals.length, 1); assert.equal(model.checkedHours, 5); assert.equal(model.partial, true);
  assert.match(text(app.reportQuality(model)), /5 \/ 6/);
});

test('skipped minutes continue totals but show approximate-result warnings', () => {
  const app = env(), d = day('2025-12-15');
  d.status = 'partial_hours'; d.missing_minutes_ignored = true;
  d.missing_times = ['2025-12-15T15:15:00+05:30'];
  const r = {...range([d]), missing_minute_policy:'skip'};
  const p = proxy({missing_minute_policy:'skip', skipped_minute_count:1, approximate_due_to_missing_minutes:true,
    data_gaps:[{date:d.date,count:1,missing_times:d.missing_times}], open_spot_points_proxy:42});
  const model = app.reportModel(r,p);
  assert.equal(model.partial,false); assert.equal(model.approximate,true); assert.equal(model.openPoints,42);
  assert.equal(model.days[0].displayStatus,'Missing minutes skipped');
  app.renderSpotRangeResult(r,p);
  const output = text(app.roots['replay-result']);
  assert.match(output,/FINISHED · MISSING MINUTES SKIPPED/);
  assert.match(output,/Results are approximate/); assert.doesNotMatch(output,/INCOMPLETE RESULT|SPOT REPLAY COMPLETE/);
  assert.match(text(app.reportQuality(model)),/15:15:00 IST/);
});

test('missing-price entry decisions and delayed option actions are explicit', () => {
  const app = env(), d = day();
  const model = app.reportModel(range([d]), proxy({events:[{type:'signal_skipped',side:'bull',signal_at:d.signals[0].scheduled_check_at,reason:'entry_price_missing'}]}));
  assert.match(model.signals[0].detail,/entry minute has no price/);
  const options = optionModel();
  options.approximate_due_to_missing_minutes = true; options.skipped_minute_count = 2;
  options.delayed_actions = [{type:'monthly_rollover',scheduled_at:'2026-09-28T15:15:00+05:30',at:'2026-09-28T15:17:00+05:30'}];
  const output = text(app.reportOptionModel(options));
  assert.match(output,/P&L is approximate/); assert.match(output,/Actions delayed by missing prices · 1/);
  assert.match(output,/15:15:00 IST/); assert.match(output,/15:17:00 IST/);
});

test('hour-close risk is separate from execution and legacy results are not relabelled', () => {
  const app = env();
  const p = position({entry_spot:54100,risk_reference_spot:53700,risk_reference_at:'2026-09-28T10:15:00+05:30',risk_reference_basis:'completed_signal_hour_close',stop_spot:53200,target_spot:54700,exit_spot:54700,spot_points_proxy:600});
  const modern = proxy({closed_positions:[p],strategy:{id:'hour_close_review_20s_stop_first_v2',risk_reference_basis:'completed_signal_hour_close'}});
  const content = text(app.reportPositions(app.reportModel(range(),modern)));
  assert.match(content,/53,700/); assert.match(content,/54,100/); assert.match(content,/Based on hour close/);
  assert.match(content,/Reference hour ended/);
  assert.match(content,/2026-09-28T04:45:00.000Z/); // Harness marketTime returns UTC; production renders IST.
  app.renderSpotRangeResult(range(),modern);
  assert.match(text(app.roots['replay-result']),/Entry price is separate/);
  assert.doesNotMatch(text(app.roots['replay-result']),/Legacy result:/);
  app.renderSpotRangeResult(range(),proxy());
  assert.match(text(app.roots['replay-result']),/Legacy result: stops and targets used entry-price references/);
});

test('skipped entry beyond hourly-close levels has a plain-language reason',()=>{
  const app=env(),d=day();
  const model=app.reportModel(range([d]),proxy({events:[{type:'signal_skipped',side:'bull',signal_at:d.signals[0].scheduled_check_at,reason:'entry_outside_hour_close_risk'}]}));
  assert.equal(model.signals[0].outcome,'Skipped');
  assert.match(model.signals[0].detail,/already at or beyond the stop or target/);
});

test('recorded-first report shows price coverage without claiming broker profit',()=>{
  const app=env(),model=optionModel();
  model.pricing_policy='recorded_first_estimate_fallback_v1';
  model.pricing_summary={recorded:{positions:5,closed_net:100,open_net:20},mixed:{positions:2,closed_net:30,open_net:-10},estimated:{positions:1,closed_net:-4,open_net:0}};
  model.option_data={contracts:77,conflicting_minutes:5,rejected_rows:12};
  const output=text(app.reportOptionModel(model));
  assert.match(output,/Recorded prices: 5 legs/); assert.match(output,/Mixed prices: 2 legs/);
  assert.match(output,/Estimated premiums: 1 legs/); assert.match(output,/Neither is a broker fill/);
  assert.match(output,/5 conflicting minutes excluded/); assert.match(output,/Price coverage & contribution/);
});

test('per-leg details distinguish recorded entry from estimated mark',()=>{
  const app=env(),model=optionModel(),p=model.ledgers.ce_hedge[0];
  p.pnl_basis='mixed';p.entry_pricing={basis:'recorded',source:{kind:'minute_open',observed_at:p.entry_at,source:'kite_cached_history',symbol:'BANKNIFTY26SEP57000CE'}};
  p.mark_pricing={basis:'estimated',fallback_reason:'No matching recorded option price at this time'};
  const output=text(app.reportOptionPositions(model,'hedges'));
  assert.match(output,/Mixed prices/); assert.match(output,/kite_cached_history/);
  assert.match(output,/No matching recorded option price at this time/);
  assert.match(output,/Entry premium source/); assert.match(output,/Mark premium source/);
});
