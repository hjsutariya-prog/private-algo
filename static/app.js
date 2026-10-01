"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { strategies: [], deployments: [], scenario_catalog: [], recent_runs: [] };
  const kite = { status: null, instruments: null, quote: null, busy: false, expiryTimer: null };
  const tradetron = { status: null, runner: null, busy: false };
  let activeDeploymentId = null;
  const replay = { busy: false, version: 0, jobId: null, progress: null, cancelRequested: false, cancelSent: false, settingsKey: null };
  let selectedId = null;
  let editingId = null;
  let running = false;
  let saving = false;
  let showingRunId = null;
  let paperSnapshot = null;
  let paperBusy = false;
  let paperPolling = false;
  let lastModelResult = null;
  let lastPriceCoverage = null;
  let backtestHistory = [];
  let backtestHistoryAvailable = null;
  let sessionBacktest = null;
  const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
  const dateFormat = new Intl.DateTimeFormat("en-IN", { year: "numeric", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" });
  const draftDateFormat = new Intl.DateTimeFormat("en-IN", { year: "numeric", day: "numeric", month: "short", timeZone: "Asia/Kolkata" });
  const quoteFormat = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const marketDateFormat = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Kolkata" });
  const replayDateFormat = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeZone: "Asia/Kolkata" });
  const replayClockFormat = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "Asia/Kolkata" });
  const replayCheckClockFormat = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: "Asia/Kolkata" });
  const pointFormat = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
  const actions = {
    BUY_NEW: "Request new hedge purchase (test)",
    HOLD_OLD: "Do not issue a new sale",
    SELL_OLD: "Permit old hedge sale (test)",
    ROLL_DATE_CHECK: "Check the rollover date and time",
    EXIT: "Exit this short position instead of rolling (test)",
    ROLL: "Request the next-month position (test)",
    HOLD: "Keep this position unchanged",
    BLOCKED: "Block the mismatched order update",
    ONE_SALE_INTENT: "Permit only one old-hedge sale (test)",
  };
  const labels = {
    underlying: "Underlying", instrument: "Instrument", timeframe: "Signal candle", signal_timeframe: "Signal candle",
    candle_anchor: "Candle start", signal: "Entry signal", entry: "Entry rule", entry_rule: "Entry rule", bullish_entry: "Bullish entry", bearish_entry: "Bearish entry",
    short_selection: "Short option selection", short_strike_selection: "Short option selection", short_premium: "Short premium reference", target_premium: "Quoted premium reference",
    lots: "Lot count", capital: "Planned capital", capital_per_direction: "Capital per direction", max_per_direction: "Open trades per direction",
    stop_points: "Spot stop", stop_loss_points: "Spot stop", target_points: "Spot target", stop_target: "Spot stop and target", expiry: "Expiry", expiry_type: "Expiry",
    rollover: "Monthly rollover", rollover_day: "Rollover day", rollover_time: "Rollover time", rollover_policy: "Rollover rule",
    hedge: "Hedge protection", hedges: "Hedge protection", hedge_replacement: "Hedge replacement", hedge_replacement_order: "Hedge replacement order", hedge_trigger: "Hedge replacement trigger",
    execution: "Order handling", execution_mode: "Order mode", live_enabled: "Live trading", rule_version: "Rule version",
    preserve_short_reference: "Original short reference", no_compounding: "Fixed position size", reentry: "Re-entry", sizing: "Position size",
    holiday_handling: "Holiday handling", holiday_roll: "Holiday rollover", short_entry: "Short option entry", schedule: "Signal schedule", source: "Rule source",
    shorts: "Short positions", order_safety: "Order safety", session: "Trading session", signal_rules: "Signal rules", spot_exits: "Spot exits", risk: "Risk controls", capital_note: "Capital planning",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function friendly(key) {
    if (labels[key]) return labels[key];
    const words = String(key).replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function readable(value, depth = 0) {
    if (value === null || value === undefined) return "Not yet set";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (Array.isArray(value)) return value.map((item) => readable(item, depth + 1)).join(" · ");
    if (typeof value === "object") {
      if (typeof value.description === "string" && Object.keys(value).length <= 3) return value.description;
      if (depth > 3) return "See saved rule details";
      return Object.entries(value).map(([key, item]) => `${friendly(key)}: ${readable(item, depth + 1)}`).join("\n");
    }
    return String(value);
  }

  function formattedDate(raw) {
    if (!raw) return "Time not recorded";
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? "Time not recorded" : dateFormat.format(date);
  }

  function showError(id, message) {
    $(id).textContent = message || "";
    $(id).hidden = !message;
  }

  function announce(message) { $("page-status").textContent = message; }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.method && options.method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Local-Request"] = "PrivateAlgoDesk";
    }
    let response;
    try { response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" }); }
    catch { throw new Error("The local app is not responding. Check that it is still running, then try again."); }
    let body;
    try { body = await response.json(); }
    catch { throw new Error("The local app returned an unreadable reply. Nothing on this page has been confirmed saved."); }
    if (!response.ok) {
      let message = body?.error?.message || body?.message || body?.error;
      if (typeof message !== "string") message = "This action could not be completed. Please try again.";
      const failure = new Error(message);
      if (response.status === 422 && body?.quality && typeof body.quality === "object" && !Array.isArray(body.quality)) failure.quality = body.quality;
      throw failure;
    }
    return body;
  }

  function selectedStrategy() { return state.strategies.find((strategy) => strategy.id === selectedId); }

  function selectStrategy(id) {
    if (running || !state.strategies.some((strategy) => strategy.id === id)) return;
    selectedId = id;
    renderCards();
    renderDetail();
    renderRecentRuns();
    clearResult();
    announce(`Selected ${selectedStrategy().name}.`);
  }

  function renderStrategySelect() {
    const select = $("check-strategy-select");
    select.replaceChildren();
    if (!state.strategies.length) select.append(el("option", "", "Create a strategy first"));
    for (const strategy of state.strategies) {
      const option = el("option", "", strategy.name);
      option.value = strategy.id;
      select.append(option);
    }
    select.value = selectedId || "";
    select.disabled = running || !state.strategies.length;
  }

  function strategyCell(label, value, className = "") {
    const cell = el("td", className);
    cell.dataset.label = label;
    cell.append(value instanceof Node ? value : el("span", "", value));
    return cell;
  }

  function openStrategyDetail(strategy) {
    if (running) return;
    selectStrategy(strategy.id);
    if (window.location.hash !== "#strategy-detail") {
      window.history.pushState(null, "", window.location.pathname + window.location.search + "#strategy-detail");
      updateNavigation({ focus: true });
    }
  }

  function strategyMenu(strategy) {
    const menu = el("details", "row-menu");
    const toggle = el("summary", "row-menu-trigger", "⋮");
    toggle.setAttribute("aria-label", `Controls for ${strategy.name}`);
    const actions = el("div", "row-menu-actions");
    actions.setAttribute("role", "menu");
    const view = el("a", "text-link", "View details");
    view.href = "#strategy-detail";
    view.setAttribute("role", "menuitem");
    view.addEventListener("click", (event) => { event.preventDefault(); openStrategyDetail(strategy); });
    const edit = el("button", "menu-action", "Edit draft");
    edit.type = "button";
    edit.disabled = saving;
    edit.setAttribute("role", "menuitem");
    edit.addEventListener("click", () => { if (!saving) { selectStrategy(strategy.id); openForm(strategy); } });
    actions.append(view, edit);
    menu.append(toggle, actions);
    return menu;
  }

  function savedDraftDate(raw) {
    const value = new Date(raw);
    return Number.isNaN(value.getTime()) ? "Not recorded" : draftDateFormat.format(value);
  }

  function renderCards() {
    const list = $("strategy-list");
    list.replaceChildren();
    $("strategy-count").textContent = state.strategies.length;
    $("draft-count").textContent = `${state.strategies.length} ${state.strategies.length === 1 ? "DRAFT" : "DRAFTS"}`;
    if (!state.strategies.length) {
      list.append(el("div", "desk-empty", "No saved strategy drafts yet."));
      return;
    }
    const panel = el("div", "desk-table-panel strategy-draft-table");
    const table = el("table", "desk-table strategy-draft-list");
    table.setAttribute("aria-label", "Strategy drafts");
    const head = el("thead"), headRow = el("tr");
    ["Strategy", "Status", "Size / leg", "Planned capital", "Last saved", "Controls"].forEach((label) => headRow.append(el("th", "", label)));
    head.append(headRow); table.append(head);
    const body = el("tbody");
    for (const strategy of state.strategies) {
      const row = el("tr");
      const name = el("a", "text-link", strategy.name);
      name.href = "#strategy-detail";
      name.addEventListener("click", (event) => { event.preventDefault(); openStrategyDetail(strategy); });
      row.append(
        strategyCell("Strategy", name),
        strategyCell("Status", el("span", "state-label draft-status", strategy.phase ? friendly(strategy.phase) : "Draft")),
        strategyCell("Size / leg", strategySize(strategy), "numeric"),
        strategyCell("Planned capital", money.format(strategy.capital), "numeric"),
        strategyCell("Last saved", savedDraftDate(strategy.updated_at || strategy.created_at)),
        strategyCell("Controls", strategyMenu(strategy), "table-controls")
      );
      body.append(row);
    }
    table.append(body); panel.append(table); list.append(panel);
  }

  function strategySize(strategy) {
    const lots = Number(strategy?.lots), multiplier = Number(strategy?.multiplier ?? 1), effective = Number(strategy?.effective_lots ?? lots * multiplier);
    if (![lots, multiplier, effective].every(Number.isSafeInteger)) return "Unavailable";
    return multiplier === 1 ? `${effective}` : `${lots} × ${multiplier} = ${effective}`;
  }

  function renderRules(strategy) {
    const container = $("strategy-rules");
    container.replaceChildren();
    const rules = strategy.rules || {};
    const entries = Array.isArray(rules)
      ? rules.map((rule, index) => [rule.label || rule.title || `Rule ${index + 1}`, rule.description ?? rule.value ?? rule])
      : Object.entries(rules);
    if (!entries.length) {
      container.append(el("p", "section-description", "Rules have not been recorded for this draft."));
      return;
    }
    for (const [key, value] of entries) {
      const text = readable(value);
      const item = el("article", "rule-item" + (text.length > 220 ? " wide" : ""));
      item.append(el("h4", "", friendly(key)), el("p", "", text));
      container.append(item);
    }
  }

  function renderReadiness(strategy) {
    const list = $("readiness-list");
    list.replaceChildren();
    const readiness = Array.isArray(strategy.readiness) ? strategy.readiness : [];
    const items = readiness.length ? readiness : [
      { label: "Broker connection not set up", ready: false },
      { label: "Real market testing not completed", ready: false },
      { label: "Order handling not verified with Zerodha", ready: false },
      { label: "Live trading unavailable in this prototype", ready: false },
    ];
    for (const check of items) {
      const ready = check.ready === true;
      const item = el("li");
      const icon = el("span", "readiness-icon" + (ready ? " ready" : ""), ready ? "✓" : "—");
      icon.setAttribute("aria-hidden", "true");
      item.append(icon, el("span", "", `${check.label || check.name || "Readiness check"}${check.status ? " — " + check.status : ready ? " — recorded" : " — pending"}`));
      list.append(item);
    }
  }

  function renderDetail() {
    renderStrategySelect();
    const strategy = selectedStrategy();
    $("empty-detail").hidden = !!strategy;
    $("detail-content").hidden = !strategy;
    if (!strategy) {
      $("scenario-strategy").textContent = "Create a strategy draft before running a check.";
      updateControls();
      return;
    }
    $("detail-title").textContent = strategy.name;
    $("detail-status").textContent = "DRAFT · NOT DEPLOYED";
    $("detail-updated").textContent = `Last saved ${formattedDate(strategy.updated_at || strategy.created_at)}`;
    $("detail-lots").textContent = strategySize(strategy);
    $("detail-capital").textContent = money.format(strategy.capital);
    $("detail-phase").textContent = strategy.phase ? friendly(strategy.phase) : "Draft";
    $("detail-updated").textContent += ` IST · ${strategy.version || "Version not recorded"}`;
    $("detail-summary").textContent = "Fixed hourly BANKNIFTY rules · 500-point spot stop · 1,000-point spot target · monthly hedge rollover. This draft is not deployed.";
    const existingDeployment = state.deployments.some((deployment) => deployment.strategy_id === strategy.id && deployment.strategy_version === strategy.version);
    $("deploy-strategy").disabled = saving || existingDeployment;
    $("deploy-strategy").textContent = existingDeployment ? "Paper deployment created" : "Create paper deployment";
    $("scenario-strategy").textContent = `Checking: ${strategy.name}`;
    renderRules(strategy);
    updateControls();
    renderJourney();
  }

  function renderScenarios() {
    const select = $("scenario-select");
    const previous = select.value;
    select.replaceChildren();
    if (!state.scenario_catalog.length) {
      const option = el("option", "", "No checks available");
      option.value = "";
      select.append(option);
    } else {
      for (const scenario of state.scenario_catalog) {
        const option = el("option", "", scenario.title);
        option.value = scenario.id;
        select.append(option);
      }
      if (state.scenario_catalog.some((scenario) => scenario.id === previous)) select.value = previous;
    }
    updateScenarioDescription();
    updateControls();
  }

  function updateScenarioDescription() {
    const scenario = state.scenario_catalog.find((item) => item.id === $("scenario-select").value);
    $("scenario-description").textContent = scenario?.description || "";
  }

  function updateControls() {
    $("check-strategy-select").disabled = running || !state.strategies.length;
    $("scenario-select").disabled = running || !state.scenario_catalog.length;
    $("run-check").disabled = running || !selectedStrategy() || !$("scenario-select").value;
    $("run-check").textContent = running ? "Checking…" : "Run check";
    $("create-strategy").disabled = saving;
    $("edit-strategy").disabled = saving || !selectedStrategy();
    $("deploy-strategy").disabled = saving || !selectedStrategy() || state.deployments.some((deployment) => deployment.strategy_id === selectedId && deployment.strategy_version === selectedStrategy()?.version);
  }

  function clearResult() {
    showingRunId = null;
    $("check-result").replaceChildren();
    $("check-result").hidden = true;
    $("check-empty").hidden = false;
    showError("check-error", "");
  }

  function resultBadge(run) {
    const passed = run.result === "passed";
    return el("span", "result-badge " + (passed ? "passed" : "failed"), passed ? "✓ Example scenario passed" : "! Example scenario failed");
  }

  function renderRun(run) {
    showingRunId = run.id;
    if (state.scenario_catalog.some((scenario) => scenario.id === run.scenario_id)) {
      $("scenario-select").value = run.scenario_id;
      updateScenarioDescription();
    }
    const result = $("check-result");
    result.replaceChildren();
    const heading = el("div", "result-heading");
    heading.append(el("h3", "", run.title || friendly(run.scenario_id)), resultBadge(run));
    result.append(heading, el("p", "result-summary", run.summary || "Check completed."));
    const strategy = state.strategies.find((item) => item.id === run.strategy_id);
    const savedName = run.strategy_name || strategy?.name;
    const context = savedName ? ` · ${savedName}` : "";
    result.append(el("p", "test-data-note", `Made-up test data · ${formattedDate(run.created_at)} IST${context} · ${run.strategy_version || "Draft version not recorded"} · No orders placed`));
    if (Array.isArray(run.events) && run.events.length) {
      const events = el("ol", "event-list");
      run.events.forEach((event, index) => {
        const row = el("li", "event-item");
        const number = el("span", "event-number", event.step ?? index + 1);
        number.setAttribute("aria-hidden", "true");
        const content = el("div", "event-content");
        content.append(el("p", "event-title", friendly(event.event || `Step ${index + 1}`)));
        if (event.action) content.append(el("p", "event-action", actions[event.action] || friendly(readable(event.action))));
        if (event.detail) content.append(el("p", "event-detail", readable(event.detail)));
        row.append(number, content);
        events.append(row);
      });
      result.append(events);
    }
    if (Array.isArray(run.checks) && run.checks.length) {
      const checks = el("ul", "result-checks");
      checks.setAttribute("aria-label", "Check results");
      for (const check of run.checks) {
        const passed = check.passed === true;
        const item = el("li");
        const icon = el("span", passed ? "check-pass" : "check-fail", passed ? "✓" : "×");
        icon.setAttribute("aria-hidden", "true");
        item.append(icon, el("span", "", `${check.name || "Check"} — ${passed ? "passed" : "failed"}`));
        checks.append(item);
      }
      result.append(checks);
    }
    result.hidden = false;
    $("check-empty").hidden = true;
  }

  function renderRecentRuns() {
    const recorded = state.recent_runs.slice(0, 20);
    const distinct = new Set(recorded.map(run => run.scenario_id));
    $("check-count").textContent = distinct.size;
    $("check-count-note").textContent = recorded.length
      ? `${distinct.size} different scenarios in ${recorded.length} recent example runs · not market validation`
      : "No checks recorded yet";
    const runs = $("recent-runs");
    runs.replaceChildren();
    const selectedRuns = state.recent_runs.filter((run) => run.strategy_id === selectedId);
    $("recent-section").hidden = !selectedRuns.length;
    for (const run of selectedRuns.slice(0, 5)) {
      const button = el("button", "recent-run");
      button.type = "button";
      const label = el("span", "recent-run-name", run.title || friendly(run.scenario_id));
      const strategy = state.strategies.find((item) => item.id === run.strategy_id);
      const savedName = run.strategy_name || strategy?.name;
      label.append(el("span", "recent-run-time", `${formattedDate(run.created_at)} IST${savedName ? ` · ${savedName}` : ""} · ${run.strategy_version || "Draft version not recorded"} · Check ${run.id}`));
      button.append(label, resultBadge(run));
      button.addEventListener("click", async () => {
        if (running || showingRunId === run.id) return;
        const requestedStrategyId = selectedId;
        button.disabled = true;
        showError("check-error", "");
        try {
          const response = await api(`/api/runs/${encodeURIComponent(run.id)}`);
          if (selectedId !== requestedStrategyId) return;
          if (response.run?.strategy_id !== selectedId) throw new Error("This check belongs to another strategy.");
          renderRun(response.run);
          announce("Previous offline check loaded.");
        } catch (error) { showError("check-error", error.message); }
        finally { button.disabled = false; }
      });
      runs.append(button);
    }
    renderJourney();
  }

  async function loadState() {
    const body = await api("/api/state");
    if (body.mode !== "offline" || body.live_enabled !== false) throw new Error("This page expects an offline-only app. It has stopped loading because the app’s safety settings do not match.");
    state.strategies = Array.isArray(body.strategies) ? body.strategies : [];
    state.deployments = Array.isArray(body.deployments) ? body.deployments : [];
    state.scenario_catalog = Array.isArray(body.scenario_catalog) ? body.scenario_catalog : [];
    state.recent_runs = Array.isArray(body.recent_runs) ? body.recent_runs : [];
    if (!state.strategies.some((item) => item.id === selectedId)) selectedId = state.strategies.slice().sort((a,b) => Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at))[0]?.id || null;
    renderCards();
    renderScenarios();
    renderDetail();
    renderRecentRuns();
  }

  function kiteSessionExpired() {
    const expiry = Date.parse(kite.status?.session_expires_at || "");
    return Number.isFinite(expiry) && expiry <= Date.now();
  }

  function kiteConnected() { return kite.status?.connected === true && !kiteSessionExpired(); }

  function marketTime(raw) {
    if (!raw) return "Time not supplied";
    const value = new Date(raw);
    return Number.isNaN(value.getTime()) ? "Time unavailable" : `${marketDateFormat.format(value)} IST`;
  }

  function quoteFreshness(quote) {
    if (!quote) return null;
    const age = Date.now() - Date.parse(quote.timestamp);
    if (!Number.isFinite(age)) return "Quote time unavailable — do not treat this as current market data.";
    if (age < -30000) return "Quote time is ahead of this computer — check the clock before using this data.";
    if (age > 120000) return "Older than 2 minutes — do not treat this as current market data.";
    return "Recent snapshot, not a streaming feed.";
  }

  function lastCompletedRegularDate() {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date()).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    const day = new Date(Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)));
    if (Number(parts.hour) < 15 || (Number(parts.hour) === 15 && Number(parts.minute) < 30)) day.setUTCDate(day.getUTCDate() - 1);
    while (day.getUTCDay() === 0 || day.getUTCDay() === 6) day.setUTCDate(day.getUTCDate() - 1);
    return day.toISOString().slice(0, 10);
  }

  function syncReplayDate() {
    const input = $("replay-date");
    input.max = /^\d{4}-\d{2}-\d{2}$/.test(kite.status?.latest_completed_date || "") ? kite.status.latest_completed_date : lastCompletedRegularDate();
    const [year, month, day] = input.max.split("-").map(Number);
    const lastDay = new Date(Date.UTC(year - 5, month, 0)).getUTCDate();
    input.min = `${year - 5}-${String(month).padStart(2, "0")}-${String(Math.min(day, lastDay)).padStart(2, "0")}`;
    if (!input.value) input.value = input.max;
  }

  function replayTime(raw, withSeconds = false) {
    const value = new Date(raw);
    if (Number.isNaN(value.getTime())) return "Time unavailable";
    return `${(withSeconds ? replayCheckClockFormat : replayClockFormat).format(value)} IST`;
  }

  function replaySide(side) {
    if (side === "bull") return "Bullish crossing";
    if (side === "bear") return "Bearish crossing";
    return "Spot crossing";
  }

  function validReplayTime(value) { return typeof value === "string" && !Number.isNaN(Date.parse(value)); }

  function validReplaySignal(signal) {
    return signal && typeof signal === "object" && ["bull", "bear"].includes(signal.side) && Number.isFinite(signal.level) && signal.level > 0
      && ["bar_start", "bar_end", "scheduled_check_at"].every((key) => validReplayTime(signal[key]));
  }

  function renderReplayAvailability() {
    const connected = kiteConnected();
    $("replay-submit").disabled = replay.busy || !connected;
    for (const id of ["replay-date", "strategy-mode", "model-sell-lots", "model-ce-lots", "model-pe-lots", "model-units", "model-return-base"]) $(id).disabled = replay.busy;
    $("replay-submit").textContent = replay.busy ? "Running simulation…" : "Run simulation";
    $("replay-cancel").hidden = !replay.busy;
    $("replay-cancel").disabled = replay.cancelRequested;
    $("replay-progress").hidden = !replay.busy;
    $("replay-progress-message").textContent = replay.cancelRequested ? "Stopping after the current step…" : replay.progress?.message || "Preparing historical spot data…";
    const progress = replay.progress;
    if (progress?.total > 0 && progress.completed > 0) $("replay-progressbar").value = Math.min(100, 100 * progress.completed / progress.total);
    else $("replay-progressbar").removeAttribute("value");
    $("replay-availability").textContent = connected
      ? "Zerodha connected · Historical prices only"
      : kite.status ? "Connect Zerodha to load historical spot prices." : "Checking Zerodha…";
    $("replay-connect-link").hidden = connected;
    renderRunBrief();
    renderJourney();
  }

  function replayDay(date) {
    const parsed = new Date(`${date}T00:00:00+05:30`);
    return Number.isNaN(parsed.getTime()) ? date : replayDateFormat.format(parsed);
  }

  function validSpotRange(range, startDate) {
    if (!range || range.start_date !== startDate || !/^\d{4}-\d{2}-\d{2}$/.test(range.end_date || "")
      || !Array.isArray(range.days) || !Number.isSafeInteger(range.total_signals) || range.total_signals < 0) return false;
    const allowed = new Set(["complete", "partial_hours", "incomplete", "no_price_data", "weekend", "holiday"]);
    let signalCount = 0;
    for (const day of range.days) {
      if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day.date || "") || !allowed.has(day.status)) return false;
      if (!["complete", "partial_hours"].includes(day.status)) continue;
      if (!Array.isArray(day.signals) || !Number.isSafeInteger(day.signal_count)
        || day.signal_count !== day.signals.length || !day.signals.every((signal) => validReplaySignal(signal))) return false;
      signalCount += day.signal_count;
    }
    return signalCount === range.total_signals;
  }

  function backtestTiming(mode) {
    return mode === "hour_close_boundary_uncertain_v2" ? "10:15 boundary"
      : mode === "hour_close_review_20s_stop_first_v2" ? "10:16 review"
      : "Legacy timing";
  }

  function backtestQuality(item) {
    if (item.approximate) return "Approximate";
    if (item.validation === "validated") return "Validated";
    return item.validation ? friendly(item.validation) : "Review report";
  }

  function backtestSummary(item) {
    if (!item) return "No completed backtest saved yet.";
    const dates = item.start_date && item.end_date ? `${replayDay(item.start_date)} – ${replayDay(item.end_date)}` : "Saved backtest";
    const quality = backtestQuality(item);
    const signals = Number.isSafeInteger(item.signals) ? `${item.signals} signals` : "Signals in report";
    return `${dates} · ${backtestTiming(item.strategy_mode)} · ${quality} · ${signals}`;
  }

  function updateResearchSummary() {
    const latest = backtestHistory[0] || sessionBacktest;
    $("research-backtest-status").textContent = latest ? backtestSummary(latest) : lastModelResult || "No completed backtest saved yet.";
    $("research-backtest-action").textContent = latest ? "Review latest →" : "Open simulator →";
  }

  function renderBacktestHistory() {
    const target = $("backtest-history");
    target.replaceChildren();
    const items = [...backtestHistory];
    if (sessionBacktest && !items.some(item => item.id === sessionBacktest.id)) items.push(sessionBacktest);
    if (!items.length) {
      target.append(el("p", "essential-note", backtestHistoryAvailable === false
        ? "Saved history will be available after the local service restarts."
        : "No completed backtests are saved yet. Run the model once, then reopen the result here."));
      updateResearchSummary();
      return;
    }
    const table = el("table", "research-history-table");
    table.setAttribute("aria-label", "Previous backtests");
    const head = el("thead"), headRow = el("tr");
    ["Saved", "Period", "Timing", "Quality", ""].forEach(label => headRow.append(el("th", "", label)));
    head.append(headRow);
    const body = el("tbody");
    items.forEach(item => {
      const row = el("tr");
      row.append(el("td", "", item.session_only ? "This app session" : formattedDate(item.created_at)));
      row.append(el("td", "", item.start_date && item.end_date ? `${replayDay(item.start_date)} – ${replayDay(item.end_date)}` : "Not recorded"));
      row.append(el("td", "", backtestTiming(item.strategy_mode)));
      row.append(el("td", item.approximate ? "status-approximate" : "", backtestQuality(item)));
      const action = el("button", "text-link compact-text-action", "Review →");
      action.type = "button";
      action.addEventListener("click", () => openSavedBacktest(item));
      const actionCell = el("td"); actionCell.append(action); row.append(actionCell);
      body.append(row);
    });
    table.append(head, body);
    const wrap = el("div", "research-table-wrap"); wrap.tabIndex = 0; wrap.append(table);
    target.append(wrap);
    if (typeof updateResearchSummary === "function") updateResearchSummary();
  }

  function restoreBacktestControls(response) {
    const resultMode = response.spot_proxy?.strategy?.id || "review_20s_stop_first_v1";
    $("replay-date").value = response.range.start_date;
    $("strategy-mode").value = ["hour_close_boundary_uncertain_v2", "hour_close_review_20s_stop_first_v2"].includes(resultMode)
      ? resultMode : resultMode === "review_20s_stop_first_v1" ? "hour_close_review_20s_stop_first_v2" : "hour_close_boundary_uncertain_v2";
    if (response.option_model?.config) {
      for (const [id, key] of [["model-sell-lots", "sell_lots"], ["model-ce-lots", "ce_hedge_lots"], ["model-pe-lots", "pe_hedge_lots"], ["model-units", "units_per_lot"]]) $(id).value = response.option_model.config[key];
    }
    if (Number.isSafeInteger(response.option_model?.reporting?.return_base)) {
      $("model-return-base").value = new Intl.NumberFormat("en-IN").format(response.option_model.reporting.return_base);
    }
    replay.settingsKey = replaySettingsKey();
    if (resultMode !== $("strategy-mode").value) {
      const previousSettings = JSON.parse(replay.settingsKey); previousSettings[1] = resultMode;
      replay.settingsKey = JSON.stringify(previousSettings);
    }
    updateReplaySettingsNotice();
  }

  async function openSavedBacktest(item) {
    try {
      const saved = item.session_only ? item : (await api(`/api/backtests/${encodeURIComponent(item.id)}`)).backtest;
      const response = saved?.result;
      if (!validSpotRange(response?.range, response?.range?.start_date)) throw new Error("This saved backtest is incomplete and cannot be shown safely.");
      renderSpotRangeResult(response.range, response.spot_proxy, response.option_model);
      lastModelResult = backtestSummary(item);
      lastPriceCoverage = {summary: response.option_model?.pricing_summary, end: response.range.end_date};
      restoreBacktestControls(response);
      renderJourney();
      if (window.location.hash !== "#past-data-check") window.location.hash = "#past-data-check";
      announce("Saved backtest opened. This is a historical report; no orders were placed.");
    } catch (error) {
      showError("replay-error", error.message);
      if (window.location.hash !== "#past-data-check") window.location.hash = "#past-data-check";
    }
  }

  async function loadBacktestHistory() {
    let saved = [];
    try {
      const response = await api("/api/backtests");
      saved = Array.isArray(response.backtests) ? response.backtests : [];
      backtestHistoryAvailable = true;
    } catch (_) {
      // The current result remains reviewable on an older local service; saved history appears after restart.
      backtestHistoryAvailable = false;
    }
    backtestHistory = saved;
    $("backtest-history-note").textContent = backtestHistoryAvailable === false
      ? "Restart the local service once to begin saving completed backtests."
      : "Completed results are saved locally. Open one to review the original report and settings.";
    try {
      const current = (await api("/api/replay/spot-range/jobs/current")).job;
      if (current?.status === "complete" && validSpotRange(current.result?.range, current.start_date)
        && !backtestHistory.some(item => item.id === current.history_id)) {
        const options = current.result.option_model || {}, spot = current.result.spot_proxy || {};
        sessionBacktest = {id: current.id, session_only: true, result: current.result, created_at: current.result.range.checked_at,
          purpose: current.purpose || "research", start_date: current.start_date, end_date: current.end_date,
          signals: current.result.range.total_signals, strategy_mode: spot.strategy?.id,
          validation: options.validation_status || spot.validation_status,
          approximate: Boolean(options.approximate || spot.approximate || options.approximate_due_to_missing_minutes || options.approximate_due_to_assumed_holidays),
          combined_net: options.combined?.total_net};
      } else sessionBacktest = null;
    } catch (_) { sessionBacktest = null; }
    renderBacktestHistory();
  }

  function clearSpotRangeResult() {
    replay.version += 1;
    replay.settingsKey = null;
    $("replay-settings-changed").hidden = true;
    showError("replay-error", "");
    $("replay-result").replaceChildren();
    $("replay-result").hidden = true;
    $("replay-empty").hidden = false;
  }

  function spotPoints(value) {
    if (!Number.isFinite(value)) return "Not available";
    return `${value > 0 ? "+" : value < 0 ? "−" : ""}${pointFormat.format(Math.abs(value))} spot points`;
  }

  // Report calculations are separate from rendering so partial histories stay explicit.
  function reportModel(range, proxy) {
    const gapDates = new Set(range.days.filter((day) =>
      ["incomplete", "no_price_data", "partial_hours"].includes(day.status) && !(day.verified_closed_date && day.received_candle_count === 0)
    ).map((day) => day.date));
    if (proxy?.blocked_from?.date) gapDates.add(proxy.blocked_from.date);
    const skippedDates = new Set((proxy?.data_gaps || []).map((gap) => gap.date));
    const available = ["complete", "partial"].includes(proxy?.status);
    const partial = !available || proxy.status !== "complete" || [...gapDates].some((date) => !skippedDates.has(date));
    const assumedDays = range.days.filter((day) => day.assumed_holiday && !day.verified_closed_date).map((day) => day.date);
    const uncertain = proxy?.uncertain_positions || [];
    const approximate = proxy?.approximate === true || proxy?.approximate_due_to_missing_minutes === true || assumedDays.length > 0 || uncertain.length > 0;
    const closed = Array.isArray(proxy?.closed_positions) ? proxy.closed_positions : [];
    const open = Array.isArray(proxy?.open_positions) ? proxy.open_positions : [];
    const chronological = closed.slice().sort((a, b) => Date.parse(a.exit_at) - Date.parse(b.exit_at));
    const measured = chronological.filter((p) => Number.isFinite(p.spot_points_proxy));
    const events = new Map();
    for (const event of proxy?.events || []) {
      if (["proxy_entry", "signal_skipped"].includes(event.type)) events.set(event.side + "|" + event.signal_at, event);
    }
    const signals = range.days.flatMap((day) => ["complete", "partial_hours"].includes(day.status) ? day.signals.map((signal) => {
      const event = events.get(signal.side + "|" + signal.scheduled_check_at);
      let outcome = "Not modelled", detail = "No position decision is available for this signal.";
      if (event?.type === "proxy_entry") {
        outcome = "Position opened"; detail = "Modelled entry: " + marketTime(event.at) + " · " + reportNumber(event.spot) + " spot";
      } else if (event?.type === "signal_skipped") {
        outcome = "Skipped";
        detail = event.reason === "market_closed_at_entry" ? "Market closed before the next-minute entry; signal not carried to the next session."
          : event.reason === "entry_price_missing" ? "The scheduled entry minute has no price; this entry was skipped without inventing a fill."
          : event.reason === "entry_outside_hour_close_risk" ? "Entry skipped: the available price was already at or beyond the stop or target fixed from the signal hour’s close."
          : event.reason === "side_already_open" ? "A position in this direction was already open."
          : event.reason === "not_provably_after_prior_exit" ? "The signal was not provably after the previous exit."
          : friendly(event.reason || "No entry");
      } else if (partial) detail = "Position tracking is incomplete; this signal is not evidence of a trade.";
      return { ...signal, date: day.date, outcome, detail };
    }) : []);
    const days = range.days.map((day) => {
      const issue = gapDates.has(day.date);
      const holiday = day.status === "holiday" || (day.verified_closed_date && day.received_candle_count === 0);
      const closedDay = !issue && (holiday || day.status === "weekend");
      return { ...day, issue, category: issue ? "review" : closedDay ? "closed" : "checked",
        displayStatus: issue ? skippedDates.has(day.date) ? "Missing minutes skipped" : "Needs review" : holiday ? day.assumed_holiday && !day.verified_closed_date ? "Assumed holiday" : "Exchange holiday" : day.status === "weekend" ? "Weekend" : day.special_session ? "Special session checked" : "Signals checked" };
    });
    return {
      range, proxy, partial, approximate, available, closed, open, uncertain, assumedDays, chronological, signals, days, gapDates,
      skippedMinutes: proxy?.skipped_minute_count || 0,
      measured, closedPoints: available && measured.length === closed.length ? measured.reduce((sum, p) => sum + p.spot_points_proxy, 0) : null,
      openPoints: available && !partial && Number.isFinite(proxy?.open_spot_points_proxy) ? proxy.open_spot_points_proxy : null,
      targets: closed.filter((p) => p.exit_reason === "target").length,
      stops: closed.filter((p) => p.exit_reason === "stop").length,
      signalSessions: range.days.filter((day) => ["complete", "partial_hours"].includes(day.status) && day.completed_hour_count > 0).length,
      checkedHours: range.days.reduce((total, day) => total + (day.completed_hour_count || 0), 0),
      positionSessions: proxy?.session_count || 0,
    };
  }

  function reportNumber(value) {
    return Number.isFinite(value) ? pointFormat.format(value) : "—";
  }

  function reportSigned(value) {
    return Number.isFinite(value) ? (value > 0 ? "+" : value < 0 ? "−" : "") + reportNumber(Math.abs(value)) : "—";
  }

  function reportTone(value) {
    return value > 0 ? "gain" : value < 0 ? "loss" : "";
  }

  function reportButton(label, handler, className = "button subtle compact") {
    const button = el("button", className, label);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  function reportStack(primary, secondary, className = "") {
    const stack = el("div", "report-stack " + className);
    stack.append(el("span", "report-primary", primary));
    if (secondary) stack.append(el("span", "report-secondary", secondary));
    return stack;
  }

  function reportDateTime(value, note = "") {
    if (!validReplayTime(value)) return reportStack("—", note);
    // Read the calendar date in IST, not the computer's local time zone.
    return reportStack(replayDateFormat.format(new Date(value)), replayTime(value, true) + (note ? " · " + note : ""));
  }

  function reportMetric(label, value, note, tone = "") {
    const card = el("div", "report-metric " + tone);
    card.append(el("span", "report-metric-label", label), el("strong", "", value), el("span", "report-metric-note", note));
    return card;
  }

  function reportHeading(title, subtitle) {
    const heading = el("div", "report-section-heading");
    heading.append(el("h4", "", title));
    if (subtitle) heading.append(el("p", "", subtitle));
    return heading;
  }

  function reportFacts(pairs) {
    const list = el("dl", "report-facts");
    for (const [label, value] of pairs) {
      const item = el("div");
      item.append(el("dt", "", label), el("dd", "", value));
      list.append(item);
    }
    return list;
  }

  function reportDisclosure(label, content) {
    const details = el("details", "report-disclosure");
    details.append(el("summary", "", label), content);
    return details;
  }

  // Every report table renders a bounded page, even for a five-year history.
  function reportTable({ title, columns, rows, filters, searchText, cells, empty, initialFilter = "all", units = "spot points" }) {
    const section = el("section", "report-table-section");
    const toolbar = el("div", "report-table-toolbar");
    const filterLabel = el("label", "report-filter");
    filterLabel.append(el("span", "", "Show"));
    const filter = el("select");
    filter.setAttribute("aria-label", title + " filter");
    for (const [value, label] of filters) {
      const option = el("option", "", label); option.value = value; filter.append(option);
    }
    filter.value = initialFilter;
    filterLabel.append(filter);
    const searchLabel = el("label", "report-search");
    searchLabel.append(el("span", "sr-only", "Search " + title.toLowerCase()));
    const search = el("input");
    search.type = "search"; search.placeholder = "Search date, direction or status";
    searchLabel.append(search);
    const order = el("select");
    order.setAttribute("aria-label", title + " order");
    for (const [value, label] of [["newest", "Newest first"], ["oldest", "Oldest first"]]) {
      const option = el("option", "", label); option.value = value; order.append(option);
    }
    toolbar.append(filterLabel, searchLabel, order);
    const scroller = el("div", "report-table-scroll");
    scroller.tabIndex = 0; scroller.setAttribute("role", "region"); scroller.setAttribute("aria-label", title + " table; scroll horizontally for all columns");
    const table = el("table", "report-table");
    table.append(el("caption", "sr-only", title + ". All times are in IST; " + units + "."));
    const head = el("thead"), tr = el("tr"), body = el("tbody");
    columns.forEach((name) => { const th = el("th", "", name); th.scope = "col"; tr.append(th); });
    head.append(tr); table.append(head, body); scroller.append(table);
    const noRows = el("p", "report-no-rows");
    const footer = el("div", "report-table-footer"), count = el("span");
    count.setAttribute("role", "status");
    const pages = el("div", "report-pagination");
    let page = 0;
    const pageSize = 20;
    const previous = reportButton("← Previous", () => { page -= 1; draw(); });
    const next = reportButton("Next →", () => { page += 1; draw(); });
    const pageLabel = el("span");
    pages.append(previous, pageLabel, next);
    footer.append(count, pages);
    section.append(toolbar, el("p", "report-scroll-hint", "Scroll sideways for all columns · 20 rows per page"), scroller, noRows, footer);
    function draw() {
      const query = search.value.trim().toLowerCase();
      const selected = rows.filter((row) => (filter.value === "all" || row.filters.includes(filter.value)) && (!query || searchText(row).toLowerCase().includes(query)));
      if (order.value === "newest") selected.reverse();
      const pageCount = Math.max(1, Math.ceil(selected.length / pageSize));
      page = Math.max(0, Math.min(page, pageCount - 1));
      body.replaceChildren();
      for (const row of selected.slice(page * pageSize, (page + 1) * pageSize)) {
        const item = el("tr");
        for (const content of cells(row)) {
          const cell = el("td");
          if (typeof content === "string" || typeof content === "number") cell.textContent = String(content);
          else cell.append(content);
          item.append(cell);
        }
        body.append(item);
      }
      scroller.hidden = !selected.length;
      noRows.hidden = Boolean(selected.length);
      noRows.textContent = rows.length ? "No rows match these filters. Change Show or clear the search." : empty;
      count.textContent = selected.length ? (page * pageSize + 1) + "–" + Math.min((page + 1) * pageSize, selected.length) + " of " + selected.length + " rows" : "0 rows";
      pageLabel.textContent = "Page " + (page + 1) + " / " + pageCount;
      previous.disabled = page === 0; next.disabled = page + 1 >= pageCount;
      pages.hidden = !selected.length;
    }
    for (const input of [filter, search, order]) input.addEventListener(input === search ? "input" : "change", () => { page = 0; draw(); });
    draw();
    return section;
  }

  function reportChart(model) {
    const card = el("div", "report-chart-card");
    card.append(reportHeading("Closed-position path", "Cumulative spot points · closed positions only" + (model.partial ? " · checked portion only" : "")));
    if (!model.available || !model.measured.length || !Number.isFinite(model.closedPoints)) {
      card.append(el("p", "report-chart-empty", "No closed positions to plot. A flat or empty result does not mean a profitable strategy."));
      return card;
    }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 760 240"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Cumulative closed-position spot points, ordered by exit. Final value " + spotPoints(model.closedPoints) + (model.partial ? ". Partial history only." : "."));
    const values = [0]; let total = 0;
    for (const p of model.measured) { total += p.spot_points_proxy; values.push(total); }
    const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1;
    const legend = el("div", "report-chart-legend");
    legend.append(el("span", "", "High " + reportSigned(high) + " pts"), el("span", "", "Low " + reportSigned(low) + " pts"));
    card.append(legend);
    const y = (v) => 20 + (high - v) / span * 175;
    const x = (i) => 78 + i / (values.length - 1) * 660;
    function shape(tag, attrs, text) {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
      if (text !== undefined) node.textContent = text;
      svg.append(node); return node;
    }
    for (const value of [...new Set([high, (high + low) / 2, low])]) {
      shape("line", { x1: 78, x2: 738, y1: y(value), y2: y(value), class: "report-chart-grid" });
      shape("text", { x: 65, y: y(value) + 4, "text-anchor": "end", class: "report-chart-label" }, reportNumber(value));
    }
    shape("line", { x1: 78, x2: 738, y1: y(0), y2: y(0), class: "report-chart-zero" });
    let path = "M " + x(0) + " " + y(values[0]);
    for (let i = 1; i < values.length; i += 1) path += " H " + x(i) + " V " + y(values[i]);
    shape("path", { d: path, class: "report-chart-line" });
    shape("circle", { cx: x(values.length - 1), cy: y(total), r: 4, class: "report-chart-dot" });
    shape("text", { x: 78, y: 228, class: "report-chart-label" }, "Start · 0 pts");
    shape("text", { x: 738, y: 228, "text-anchor": "end", class: "report-chart-label" }, model.measured.length + " closed positions");
    const axis = el("div", "report-chart-mobile-axis");
    axis.append(el("span", "", "Start · 0 pts"), el("span", "", model.measured.length + " closed positions"));
    card.append(svg, axis, el("p", "report-footnote", "Equal spacing per exit, not per day. Excludes open positions, hedges and all costs."));
    return card;
  }

  function reportStatistic(label, value, note, tone = "") {
    const item = el("div", "report-stat " + tone);
    item.append(el("span", "", label), el("strong", "", value));
    if (note) item.append(el("small", "", note));
    return item;
  }

  function reportAdvancedStatistics(model) {
    const panel = el("div", "report-statistics-body");
    const values = model.measured.map((position) => position.spot_points_proxy).filter(Number.isFinite);
    panel.append(el("p", "", "These statistics use completed spot trades only. They help review the model’s trade behaviour; they are not option cash profit, broker fills or a prediction of future returns."));
    if (!values.length) {
      panel.append(el("p", "report-no-rows", "No completed spot positions are available for statistics."));
      return panel;
    }
    const wins = values.filter((value) => value > 0), losses = values.filter((value) => value < 0), flat = values.length - wins.length - losses.length;
    const average = values.reduce((total, value) => total + value, 0) / values.length;
    const positiveTotal = wins.reduce((total, value) => total + value, 0);
    const lossTotal = losses.reduce((total, value) => total + Math.abs(value), 0);
    const profitFactor = lossTotal ? (positiveTotal / lossTotal).toFixed(2) : "—";
    let running = 0, peak = 0, maximumDrawdown = 0;
    for (const value of values) { running += value; peak = Math.max(peak, running); maximumDrawdown = Math.min(maximumDrawdown, running - peak); }
    let currentWinStreak = 0, currentLossStreak = 0, longestWinStreak = 0, longestLossStreak = 0;
    for (const value of values) {
      if (value > 0) { currentWinStreak += 1; currentLossStreak = 0; }
      else if (value < 0) { currentLossStreak += 1; currentWinStreak = 0; }
      else { currentWinStreak = 0; currentLossStreak = 0; }
      longestWinStreak = Math.max(longestWinStreak, currentWinStreak);
      longestLossStreak = Math.max(longestLossStreak, currentLossStreak);
    }
    const winRate = ((wins.length / values.length) * 100).toFixed(1) + "%";
    const statistics = el("div", "report-stat-grid");
    statistics.append(
      reportStatistic("Positive closes", winRate, wins.length + " of " + values.length + " closed trades", wins.length > losses.length ? "gain" : ""),
      reportStatistic("Average close", reportSigned(average) + " pts", "Closed spot trades only", reportTone(average)),
      reportStatistic("Maximum drawdown", reportSigned(maximumDrawdown) + " pts", "Largest closed-point fall from a prior high", maximumDrawdown < 0 ? "attention" : ""),
      reportStatistic("Profit factor", profitFactor, lossTotal ? "Gross gains ÷ gross losses" : "No losing close in this result", profitFactor !== "—" && Number(profitFactor) >= 1 ? "gain" : "")
    );
    panel.append(statistics);
    panel.append(reportFacts([
      ["Closed trade outcomes", wins.length + " positive · " + losses.length + " negative" + (flat ? " · " + flat + " flat" : "")],
      ["Best / worst close", reportSigned(Math.max(...values)) + " pts / " + reportSigned(Math.min(...values)) + " pts"],
      ["Longest run", longestWinStreak + " positive / " + longestLossStreak + " negative closes"],
      ["Excluded from these statistics", model.uncertain.length + " uncertain exit(s), " + model.open.length + " open position(s)"],
    ]));
    return panel;
  }

  function reportOverview(model) {
    const panel = el("div", "report-overview");
    const grid = el("div", "report-overview-grid");
    grid.append(reportChart(model));
    const outcomes = el("div", "report-outcomes");
    outcomes.append(reportHeading("Exit outcomes", model.partial ? "Checked history only — not the full range" : "Modelled closed positions"));
    outcomes.append(reportFacts([
      ["Target exits", String(model.targets)], ["Stop exits", String(model.stops)],
      ["Other exits", String(model.closed.length - model.targets - model.stops)],
      ["Positive closes", model.measured.length ? model.measured.filter((p) => p.spot_points_proxy > 0).length + " / " + model.measured.length : "—"],
      ["Average closed · pts", model.measured.length && Number.isFinite(model.closedPoints) ? reportSigned(model.closedPoints / model.measured.length) : "—"],
      ["Best position · pts", model.measured.length ? reportSigned(Math.max(...model.measured.map((p) => p.spot_points_proxy))) : "—"],
      ["Worst position · pts", model.measured.length ? reportSigned(Math.min(...model.measured.map((p) => p.spot_points_proxy))) : "—"],
    ]));
    grid.append(outcomes); panel.append(grid);
    panel.append(reportDisclosure("Advanced trade statistics", reportAdvancedStatistics(model)));
    const coverage = el("div", "report-coverage");
    coverage.append(reportHeading("What was actually checked", "Signals and positions use different coverage requirements."));
    coverage.append(reportFacts([
      ["Signal sessions", model.signalSessions + " dates · " + model.checkedHours + " complete 60-minute candles"],
      ["Position sessions", model.positionSessions + (model.range.missing_minute_policy === "skip" ? " dates · available minutes only; missing prices skipped" : " dates · all scheduled trading minutes required, including short periods")],
      ["Position history ends", model.proxy?.last_session ? replayDay(model.proxy.last_session) : "No complete position history"],
      ["Starting positions", model.proxy?.starting_state === "explicit_carry_in" ? "Supplied carry-in positions" : "None — assumed flat at the selected start"],
    ]));
    panel.append(coverage);
    const method = el("div", "report-method-body");
    method.append(el("p", "", "Bullish: entry − 500 stop / entry + 1,000 target. Bearish: entry + 500 stop / entry − 1,000 target. These levels stay fixed; positions can carry overnight."));
    const assumptions = el("ul");
    for (const text of model.proxy?.assumptions || []) if (typeof text === "string") assumptions.append(el("li", "", text));
    method.append(assumptions);
    panel.append(reportDisclosure("Calculation rules & limitations", method));
    return panel;
  }

  function reportPositions(model) {
    const panel = el("div");
    panel.append(reportHeading("Position ledger", "Hypothetical spot positions, not executed option trades. Points are not multiplied by lots."));
    if (model.partial) panel.append(el("p", "report-inline-warning", "Open positions below are only last known before tracking stopped. Their later outcome is unknown."));
    const rows = [...model.closed, ...model.open, ...model.uncertain].sort((a, b) => Date.parse(a.entry_at) - Date.parse(b.entry_at)).map((p) => ({ ...p, filters: [p.status, p.side, p.exit_reason].filter(Boolean) }));
    panel.append(reportTable({
      title: "Positions", rows,
      filters: [["all", "All positions"], ["closed", "Closed"], ["open", "Open / last known"], ["bull", "Bullish"], ["bear", "Bearish"], ["target", "Target exits"], ["stop", "Stop exits"]],
      searchText: (p) => [p.position_id, p.side, p.side === "bull" ? "bullish put PE" : "bearish call CE", p.status, p.exit_reason, p.entry_at, p.exit_at, marketTime(p.entry_at)].join(" "),
      columns: ["Position / direction", "Entry · IST", "Entry spot", "Stop / target", "Exit or last mark · IST", "Exit / mark spot", "Outcome", "Spot points"],
      empty: model.available ? "No positions were modelled in the checked history. Review Signals to see the decisions." : "Position history is unavailable. Check Data quality for the reason.",
      cells: (p) => {
        const open = p.status === "open", uncertain = p.status === "uncertain", unknown = uncertain || open && model.partial;
        const detail = reportFacts([
          ["Position ID", p.position_id || "—"], ["Signal level · spot", reportNumber(p.signal_level)],
          ["Signal review · IST", validReplayTime(p.signal_at) ? marketTime(p.signal_at) : "Carry-in / unavailable"],
          ["SL / target reference", p.risk_reference_basis === "completed_signal_hour_close" ? reportNumber(p.risk_reference_spot) + " · signal hour close" : "Entry-price reference · legacy / supplied position"],
          ["Reference hour ended · IST", validReplayTime(p.risk_reference_at) ? marketTime(p.risk_reference_at) : "Not recorded"],
          ["Entry basis", p.entry_price_basis === "next_full_minute_open" ? "Next full minute open (not an actual fill)" : friendly(p.entry_price_basis || "Unavailable")],
          ["Exit basis", uncertain ? "Both stop and target touched; no exact fill or P&L assigned" : open ? "Last observed minute close; not an exit" : p.exit_price_basis === "threshold_touched_within_minute" ? "During the displayed minute; exact second unknown" : "Minute open, including any gap"],
        ]);
        const exitEvent = model.proxy?.events?.find((event) => event.type === "proxy_exit" && event.position_id === p.position_id);
        if (exitEvent?.both_stop_and_target_touched) detail.append(el("p", "report-inline-warning", "Both stop and target were touched in one minute. The model used the stop first; the actual order is unknown."));
        const direction = reportStack(p.side === "bull" ? "↗ Bullish" : "↘ Bearish", p.side === "bull" ? "Put (PE) intent only" : "Call (CE) intent only", p.side === "bull" ? "bullish" : "bearish");
        direction.append(reportDisclosure("Details", detail));
        return [
          direction, reportDateTime(p.entry_at), reportStack(reportNumber(p.entry_spot), "spot"),
          reportStack(reportNumber(p.stop_spot) + " / " + reportNumber(p.target_spot), p.risk_reference_basis === "completed_signal_hour_close" ? "Based on hour close " + reportNumber(p.risk_reference_spot) : "stop / target · spot"),
          reportDateTime(open ? p.mark_at : p.exit_at, open ? "Last mark, not exit" : p.exit_price_basis === "threshold_touched_within_minute" ? "During this minute" : "Minute open"),
          reportStack(reportNumber(open ? p.mark_spot : p.exit_spot), "spot"),
          el("span", "report-tag " + (unknown ? "warning" : open ? "info" : p.exit_reason === "target" ? "gain" : p.exit_reason === "stop" ? "loss" : ""), uncertain ? "Uncertain exit" : unknown ? "Unknown after gap" : open ? "Open" : p.exit_reason === "target" ? "Target" : p.exit_reason === "stop" ? "Stop" : friendly(p.exit_reason || "Closed")),
          reportStack(unknown ? "Unknown" : reportSigned(p.spot_points_proxy), unknown ? "Last state only" : open ? "At last mark" : "Closed", unknown ? "" : reportTone(p.spot_points_proxy)),
        ];
      },
    }));
    return panel;
  }

  function reportSignals(model) {
    const panel = el("div");
    panel.append(reportHeading("Signal history", "Only complete 60-minute candles can signal an entry. " + (model.proxy?.strategy?.label || "Legacy 20-second review · next-minute entry") + ". Session-close signals are skipped, not carried to the next session."));
    const rows = model.signals.slice().sort((a, b) => Date.parse(a.scheduled_check_at) - Date.parse(b.scheduled_check_at)).map((s) => ({ ...s, filters: [s.side, s.outcome] }));
    panel.append(reportTable({
      title: "Signals", rows,
      filters: [["all", "All signals"], ["bull", "Bullish"], ["bear", "Bearish"], ["Position opened", "Position opened"], ["Skipped", "Skipped"], ["Not modelled", "Not modelled"]],
      searchText: (s) => [s.date, replayDay(s.date), s.side, s.side === "bull" ? "bullish" : "bearish", s.level, s.outcome, s.detail].join(" "),
      columns: ["Review time · IST", "Direction", "Crossed level", "Hourly bar · IST", "Position decision"],
      empty: "No signals in the checked hourly sessions. Missing dates are listed under Data quality, not counted as zero-signal days.",
      cells: (s) => [
        reportDateTime(s.scheduled_check_at), el("span", "report-tag " + (s.side === "bull" ? "gain" : "info"), s.side === "bull" ? "↗ Bullish" : "↘ Bearish"),
        reportStack(reportNumber(s.level), "spot"), reportStack(replayTime(s.bar_start) + " – " + replayTime(s.bar_end), replayDay(s.date)),
        reportStack(s.outcome, s.detail),
      ],
    }));
    return panel;
  }

  function reportDayDetail(day, model) {
    const content = el("div", "report-day-detail");
    const blocked = model.proxy?.blocked_from?.date === day.date ? model.proxy.blocked_from : null;
    const reason = blocked?.reason || day.reason || (day.verified_closed_date ? day.verified_closed_date + " · verified exchange holiday" : day.issue ? "No complete price history. This date is not assumed to be an exchange holiday." : day.status === "weekend" ? "No regular trading session." : "Complete hourly bars checked.");
    content.append(el("p", "", reason));
    if (day.session_windows?.length) content.append(el("p", "", "Trading hours · IST: " + day.session_windows.map((window) => replayTime(window.open) + "–" + replayTime(window.close)).join(" / ")));
    for (const bar of day.ignored_bars || []) content.append(el("p", "", "Ignored for new entries: " + replayTime(bar.bar_start) + "–" + replayTime(bar.bar_end) + " · " + bar.received_unique_minutes + " unique minutes · " + friendly(bar.reason) + "."));
    for (const [label, values] of [["Missing minute starts", [...new Set([...(day.missing_times || []), ...(blocked?.missing_times || [])])]], ["Repeated minute starts", [...new Set([...(day.duplicate_times || []), ...(blocked?.duplicate_times || [])])]]]) {
      if (values.length) content.append(el("p", "", label + " · IST: " + values.filter(validReplayTime).map((v) => replayTime(v)).join(", ")));
    }
    if (day.bars?.length) {
      const table = el("table", "report-mini-table"), head = el("thead"), row = el("tr");
      for (const label of ["Hour · IST", "Open", "High", "Low", "Close"]) { const th = el("th", "", label); th.scope = "col"; row.append(th); }
      head.append(row); table.append(head);
      const body = el("tbody");
      for (const bar of day.bars) {
        const item = el("tr");
        for (const text of [replayTime(bar.bar_start) + "–" + replayTime(bar.bar_end), ...["open", "high", "low", "close"].map((key) => reportNumber(bar[key]))]) item.append(el("td", "", text));
        body.append(item);
      }
      table.append(body); content.append(table);
    }
    return reportDisclosure("View details", content);
  }

  function reportQuality(model) {
    const panel = el("div");
    panel.append(reportHeading("Data quality & daily prices", "Entries require 60 unique consecutive minute candles in one scheduled hour. Short periods and incomplete hours are ignored for new entries. " + (model.range.missing_minute_policy === "skip" ? "Missing prices are skipped for existing positions; monitoring resumes at the next available minute. Triggers inside gaps are unknown and results are approximate." : "Existing positions still require every scheduled trading minute.")));
    panel.append(el("p", "report-footnote", "Your rule: a whole day with no candles after a successful Zerodha download is an assumed holiday, not an independently verified closure. Partial days and failed requests are not holidays. Known special-session hours and breaks are supported; unconfigured hours remain flagged."));
    const rows = model.days.slice().sort((a, b) => a.date.localeCompare(b.date)).map((day) => ({ ...day, filters: [day.category] }));
    panel.append(reportTable({
      title: "Data quality", rows, initialFilter: model.gapDates.size ? "review" : "all",
      filters: [["all", "All dates"], ["review", "Needs review"], ["checked", "Signals checked"], ["closed", "Weekends / holidays"]],
      searchText: (day) => [day.date, replayDay(day.date), day.displayStatus, day.verified_closed_date, day.reason].join(" "),
      columns: ["Date", "Data status", "Minutes received", "Hourly bars", "Signals", "Price details"],
      empty: "No dates in this replay.",
      cells: (day) => [
        replayDay(day.date), el("span", "report-tag " + (day.issue ? "warning" : day.category === "closed" ? "" : "info"), day.displayStatus),
        reportNumber(day.received_candle_count), ["complete", "partial_hours"].includes(day.status) ? day.completed_hour_count + " / " + (day.expected_hour_count ?? 6) : reportNumber(day.completed_hour_count),
        ["complete", "partial_hours"].includes(day.status) ? String(day.signal_count) : "—", reportDayDetail(day, model),
      ],
    }));
    return panel;
  }

  function reportHedges() {
    const panel = el("div", "report-hedges");
    panel.append(reportHeading("Hedge plan", "Strategy rules are recorded. Historical hedge positions are not yet calculated."));
    const alert = el("div", "report-status warning");
    alert.append(el("strong", "", "Hedge history unavailable"), el("p", "", "No hedge opening, strike, expiry, replacement or price records exist in this replay. Hedges are excluded from every result above."));
    panel.append(alert);
    const grid = el("div", "report-hedge-grid");
    for (const name of ["Call hedge · CE", "Put hedge · PE"]) {
      const card = el("div", "report-hedge-card");
      card.append(el("h5", "", name), reportFacts([["Tracking", "Independent hedge"], ["Opening rule", "Monday 15:15 expiry-week opening"], ["Replacement", "Immediately when its spot target is reached"], ["Order sequence", "Buy replacement fully, then sell existing hedge"]]));
      grid.append(card);
    }
    panel.append(grid, el("p", "report-footnote", "Monthly rollover: Monday of the actual expiry week at 15:15 IST; first exchange session if Monday is a holiday. Preserve original short-position stops and targets. These are planned rules, not verified historical actions."));
    return panel;
  }

  function reportMoney(value) {
    return Number.isFinite(value) ? new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value) : "—";
  }

  // A backtest's option ledger is made of individual selling and hedge legs.
  // Keep that distinction visible: a leg close is not a broker fill or a complete trade.
  function optionPerformance(model) {
    const ledgerKinds = ["selling", "ce_hedge", "pe_hedge"];
    const allLegs = ledgerKinds.flatMap((ledger) => (Array.isArray(model?.ledgers?.[ledger]) ? model.ledgers[ledger] : [])
      .filter((position) => position && typeof position === "object")
      .map((position, index) => ({ ...position, ledger, ledgerIndex: index })));
    const closedLegs = allLegs.filter((position) => position.status === "closed" && Number.isFinite(position.net_pnl) && validReplayTime(position.exit_at))
      .sort((a, b) => Date.parse(a.exit_at) - Date.parse(b.exit_at) || a.ledger.localeCompare(b.ledger) || a.ledgerIndex - b.ledgerIndex);
    const openLegs = allLegs.filter((position) => position.status === "open" && Number.isFinite(position.net_pnl));
    let cumulative = 0, peak = 0, maximumDrawdown = 0;
    const path = closedLegs.map((position) => {
      cumulative += position.net_pnl;
      peak = Math.max(peak, cumulative);
      maximumDrawdown = Math.min(maximumDrawdown, cumulative - peak);
      return { ...position, cumulative, drawdown: cumulative - peak };
    });
    const calculatedClosedNet = closedLegs.reduce((total, position) => total + position.net_pnl, 0);
    const calculatedOpenNet = openLegs.reduce((total, position) => total + position.net_pnl, 0);
    const combined = model?.combined || {};
    const closedNet = Number.isFinite(combined.realised_net) ? combined.realised_net : calculatedClosedNet;
    const openNet = Number.isFinite(combined.open_net) ? combined.open_net : calculatedOpenNet;
    const totalNet = Number.isFinite(combined.total_net) ? combined.total_net
      : Number.isFinite(closedNet) && Number.isFinite(openNet) ? closedNet + openNet : null;
    const monthMap = new Map();
    for (const position of path) {
      const date = new Date(position.exit_at);
      const key = date.getUTCFullYear() + "-" + String(date.getUTCMonth() + 1).padStart(2, "0");
      if (!monthMap.has(key)) monthMap.set(key, { key, date, net: 0, legs: 0, positive: 0, negative: 0 });
      const month = monthMap.get(key);
      month.net += position.net_pnl;
      month.legs += 1;
      if (position.net_pnl > 0) month.positive += 1;
      if (position.net_pnl < 0) month.negative += 1;
    }
    // End-of-session marks are the only honest source for daily and monthly
    // results.  Older saved reports do not have them, so retain their
    // closed-leg path as a clearly labelled fallback rather than inventing
    // daily values from exits.
    const suppliedDaily = Array.isArray(model?.daily_pnl) ? model.daily_pnl : [];
    const dailyPnl = [];
    let priorMarkedTotal = 0, dailyPeak = 0, dailyMaximumDrawdown = 0;
    for (const record of [...suppliedDaily].filter((item) => item && typeof item === "object" && /^\d{4}-\d{2}-\d{2}$/.test(item.date || "") && Number.isFinite(item.total_net))
      .sort((a, b) => a.date.localeCompare(b.date) || String(a.at || "").localeCompare(String(b.at || "")))) {
      const totalNet = record.total_net;
      const dailyNet = Number.isFinite(record.daily_net) ? record.daily_net : totalNet - priorMarkedTotal;
      dailyPeak = Math.max(dailyPeak, totalNet);
      const drawdown = totalNet - dailyPeak;
      dailyMaximumDrawdown = Math.min(dailyMaximumDrawdown, drawdown);
      dailyPnl.push({ ...record, totalNet, dailyNet, drawdown });
      priorMarkedTotal = totalNet;
    }
    const dailyMonthMap = new Map();
    let totalBeforeDay = 0;
    for (const day of dailyPnl) {
      const key = day.date.slice(0, 7);
      if (!dailyMonthMap.has(key)) dailyMonthMap.set(key, { key, date: new Date(day.date + "T00:00:00+05:30"), startNet: totalBeforeDay, endNet: day.totalNet, days: 0, positive: 0, negative: 0, approximate: false });
      const month = dailyMonthMap.get(key);
      month.endNet = day.totalNet;
      month.days += 1;
      if (day.dailyNet > 0) month.positive += 1;
      if (day.dailyNet < 0) month.negative += 1;
      month.approximate = month.approximate || day.approximate === true;
      totalBeforeDay = day.totalNet;
    }
    const dailyMonths = [...dailyMonthMap.values()].sort((a, b) => a.key.localeCompare(b.key)).map((month) => ({ ...month, net: month.endNet - month.startNet }));
    const values = closedLegs.map((position) => position.net_pnl);
    const positive = values.filter((value) => value > 0), negative = values.filter((value) => value < 0);
    const dailyValues = dailyPnl.map((day) => day.dailyNet);
    const positiveDays = dailyValues.filter((value) => value > 0), negativeDays = dailyValues.filter((value) => value < 0);
    return {
      allLegs, closedLegs, openLegs, path, months: [...monthMap.values()].sort((a, b) => a.key.localeCompare(b.key)),
      dailyPnl, dailyMonths,
      closedNet, openNet, totalNet, charges: model?.charges?.total,
      positiveCount: positive.length, negativeCount: negative.length,
      averageClose: values.length ? calculatedClosedNet / values.length : null,
      bestClose: values.length ? Math.max(...values) : null,
      worstClose: values.length ? Math.min(...values) : null,
      maximumDrawdown: path.length ? maximumDrawdown : null, currentDrawdown: path.length ? path.at(-1).drawdown : null,
      positiveDays: positiveDays.length, negativeDays: negativeDays.length,
      averageDaily: dailyValues.length ? dailyValues.reduce((sum, value) => sum + value, 0) / dailyValues.length : null,
      bestDay: dailyValues.length ? Math.max(...dailyValues) : null,
      worstDay: dailyValues.length ? Math.min(...dailyValues) : null,
      dailyMaximumDrawdown: dailyPnl.length ? dailyMaximumDrawdown : null,
      dailyCurrentDrawdown: dailyPnl.length ? dailyPnl.at(-1).drawdown : null,
      returnBase: Number.isSafeInteger(model?.reporting?.return_base) && model.reporting.return_base > 0 ? model.reporting.return_base : null,
      modelOrderCount: Array.isArray(model?.orders) ? model.orders.length : 0,
      rolloverCount: Array.isArray(model?.rollovers) ? model.rollovers.length : 0,
      pricing: model?.pricing_summary || {},
    };
  }

  function reportOptionCurve(performance) {
    const card = el("div", "report-chart-card report-option-curve");
    const usingDailyMarks = performance.dailyPnl.length > 0;
    const points = usingDailyMarks ? performance.dailyPnl : performance.path;
    const pointValue = (point) => usingDailyMarks ? point.totalNet : point.cumulative;
    const countLabel = usingDailyMarks ? points.length + " supplied sessions" : points.length + " closed legs";
    card.append(reportHeading(
      usingDailyMarks ? "Daily marked P&L path" : "Closed-leg P&L path",
      usingDailyMarks ? "Cumulative modelled ₹ result · end-of-session option marks" : "Cumulative modelled ₹ result · selling and hedge legs · closed legs only"
    ));
    if (!points.length) {
      card.append(el("p", "report-chart-empty", "No daily end-of-session values are available in this saved report. Run it again to calculate the daily path; open-position estimates are not converted into exits."));
      return card;
    }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 760 240"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", (usingDailyMarks ? "Cumulative modelled end-of-session option profit and loss. Final value " : "Cumulative modelled closed-leg option profit and loss, ordered by exit. Final value ") + reportMoney(pointValue(points.at(-1))) + ".");
    const values = [0, ...points.map(pointValue)];
    const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1;
    const legend = el("div", "report-chart-legend");
    legend.append(el("span", "", "High " + reportMoney(high)), el("span", "", "Low " + reportMoney(low)));
    card.append(legend);
    const y = (value) => 20 + (high - value) / span * 175;
    const x = (index) => 78 + index / (values.length - 1) * 660;
    function shape(tag, attrs, text) {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
      if (text !== undefined) node.textContent = text;
      svg.append(node); return node;
    }
    for (const value of [...new Set([high, (high + low) / 2, low])]) {
      shape("line", { x1: 78, x2: 738, y1: y(value), y2: y(value), class: "report-chart-grid" });
      shape("text", { x: 65, y: y(value) + 4, "text-anchor": "end", class: "report-chart-label" }, reportMoney(value));
    }
    shape("line", { x1: 78, x2: 738, y1: y(0), y2: y(0), class: "report-chart-zero" });
    let path = "M " + x(0) + " " + y(values[0]);
    for (let index = 1; index < values.length; index += 1) path += " H " + x(index) + " V " + y(values[index]);
    shape("path", { d: path, class: "report-chart-line report-option-curve-line" });
    shape("circle", { cx: x(values.length - 1), cy: y(values.at(-1)), r: 4, class: "report-chart-dot report-option-curve-dot" });
    shape("text", { x: 78, y: 228, class: "report-chart-label" }, "Start · ₹0");
    shape("text", { x: 738, y: 228, "text-anchor": "end", class: "report-chart-label" }, countLabel);
    const axis = el("div", "report-chart-mobile-axis");
    axis.append(el("span", "", "Start · ₹0"), el("span", "", countLabel));
    card.append(svg, axis, el("p", "report-footnote", usingDailyMarks
      ? "One mark per supplied session. Open legs are valued at that session's close without adding a closing order or exit charge."
      : "Equal spacing per completed leg, not per day. Includes the modelled charges allocated to each closed leg. Open-position estimates are excluded."));
    return card;
  }

  function reportHeatClass(value, scale) {
    if (!Number.isFinite(value) || value === 0) return "heat-flat";
    const level = Math.max(1, Math.min(5, Math.ceil(Math.abs(value) / Math.max(scale, 1) * 5)));
    return (value > 0 ? "heat-gain-" : "heat-loss-") + level;
  }

  function reportReturn(value, returnBase) {
    return Number.isFinite(value) && Number.isSafeInteger(returnBase) && returnBase > 0 ? (value / returnBase * 100).toFixed(1) + "%" : "—";
  }

  function reportCompactMoney(value) {
    if (!Number.isFinite(value)) return "—";
    return "₹" + new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }

  function reportDailyTable(performance) {
    return reportTable({
      title: "Daily modelled option P&L", rows: performance.dailyPnl.map((day) => ({ ...day, filters: [day.approximate ? "approximate" : "checked"] })),
      units: "modelled option P&L in ₹; end-of-session marks, not broker MTM or fills",
      filters: [["all", "All sessions"], ["checked", "Checked sessions"], ["approximate", "Approximate sessions"]],
      searchText: (day) => day.date + " " + (day.approximate ? "approximate" : "checked"),
      columns: ["Date", "Daily P&L", "Cumulative P&L", "Closed P&L", "Open P&L estimate", "Open legs", "Data status"],
      empty: "No daily end-of-session marks are available.", initialFilter: "all",
      cells: (day) => [
        new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }).format(new Date(day.date + "T00:00:00+05:30")),
        reportStack(reportMoney(day.dailyNet), "Change from prior supplied session", reportTone(day.dailyNet)),
        reportStack(reportMoney(day.totalNet), "End-of-session model mark", reportTone(day.totalNet)),
        reportMoney(day.realised_net), reportMoney(day.open_net), String(day.open_leg_count), day.approximate ? "Approximate" : "Checked",
      ],
    });
  }

  function reportDailyCalendar(performance, year, scale) {
    const byDate = new Map(performance.dailyPnl.map((day) => [day.date, day]));
    const calendar = el("div", "report-heatmap-board");
    calendar.setAttribute("role", "img");
    calendar.setAttribute("aria-label", "Daily modelled option profit and loss calendar for " + year + ". Dark red squares are losses and dark green squares are gains.");
    const first = new Date(Date.UTC(year, 0, 1));
    const mondayOffset = (first.getUTCDay() + 6) % 7;
    const monthLabels = el("div", "report-heatmap-months");
    for (let month = 0; month < 12; month += 1) {
      const firstOfMonth = new Date(Date.UTC(year, month, 1));
      const dayIndex = Math.floor((firstOfMonth.getTime() - first.getTime()) / 86400000);
      const label = el("span", "", new Intl.DateTimeFormat("en-IN", { month: "short", timeZone: "UTC" }).format(firstOfMonth));
      // Use a stylesheet class rather than an inline grid declaration. The page's
      // safety policy retains inline style text but does not apply it.
      const gridColumnStart = Math.floor((mondayOffset + dayIndex) / 7) + 1;
      label.setAttribute("class", "report-heatmap-week-" + gridColumnStart);
      monthLabels.append(label);
    }
    const weekdayLabels = el("div", "report-heatmap-weekdays");
    weekdayLabels.append(el("span", "", "M"), el("span", "", ""), el("span", "", "W"), el("span", "", ""), el("span", "", "F"), el("span", "", ""), el("span", "", ""));
    const days = el("div", "report-heatmap-days");
    const last = new Date(Date.UTC(year + 1, 0, 1));
    for (let current = new Date(first); current < last; current = new Date(current.getTime() + 86400000)) {
      const key = current.toISOString().slice(0, 10);
      const day = byDate.get(key);
      const dayIndex = Math.floor((current.getTime() - first.getTime()) / 86400000);
      const square = el("span", "report-heatmap-day " + (day ? reportHeatClass(day.dailyNet, scale) : "heat-empty"));
      square.setAttribute("style", "grid-column:" + (Math.floor((mondayOffset + dayIndex) / 7) + 1) + ";grid-row:" + ((mondayOffset + dayIndex) % 7 + 1));
      const dayLabel = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" }).format(current);
      const tooltip = day
        ? dayLabel + " · Daily P&L " + reportMoney(day.dailyNet) + " · " + reportReturn(day.dailyNet, performance.returnBase) + " of return base · " + (day.approximate ? "Approximate" : "Checked")
        : dayLabel + " · No supplied end-of-session mark";
      square.setAttribute("title", tooltip); square.setAttribute("aria-label", tooltip);
      days.append(square);
    }
    calendar.append(monthLabels, weekdayLabels, days);
    return calendar;
  }

  function reportOptionDays(performance) {
    const section = el("section", "report-performance-section report-performance-days");
    section.append(reportHeading("Daily P&L", "One square per calendar day. Darker red means a larger modelled loss; darker green means a larger gain. Hover a square for its value."));
    if (!performance.dailyPnl.length) {
      section.append(el("p", "report-inline-warning", "This saved report has no daily end-of-session marks. Run the backtest again to calculate daily and monthly P&L; completed-leg results are kept separate below."));
      return section;
    }
    const scale = Math.max(...performance.dailyPnl.map((day) => Math.abs(day.dailyNet)), 1);
    const years = [...new Set(performance.dailyPnl.map((day) => Number(day.date.slice(0, 4))))].sort((a, b) => a - b);
    const heatmap = el("div", "report-daily-heatmap");
    const toolbar = el("div", "report-heatmap-toolbar");
    const legend = el("div", "report-heatmap-legend");
    legend.append(el("span", "", "Loss"), el("i", "report-heatmap-day heat-loss-4"), el("i", "report-heatmap-day heat-loss-2"), el("i", "report-heatmap-day heat-flat"), el("i", "report-heatmap-day heat-gain-2"), el("i", "report-heatmap-day heat-gain-4"), el("span", "", "Gain"));
    toolbar.append(legend);
    const output = el("div", "report-heatmap-scroll");
    if (years.length > 1) {
      const label = el("label", "report-heatmap-year");
      label.append(el("span", "", "Year"));
      const select = el("select");
      for (const year of years) { const option = el("option", "", String(year)); option.value = String(year); select.append(option); }
      select.value = String(years.at(-1));
      label.append(select); toolbar.append(label);
      const draw = () => output.replaceChildren(reportDailyCalendar(performance, Number(select.value), scale));
      select.addEventListener("change", draw); draw();
    } else output.append(reportDailyCalendar(performance, years[0], scale));
    heatmap.append(toolbar, output);
    section.append(heatmap, reportDisclosure("View daily figures", reportDailyTable(performance)));
    return section;
  }

  function reportMonthlyTable(performance) {
    return reportTable({
      title: "Monthly modelled option P&L", rows: performance.dailyMonths.map((month) => ({ ...month, filters: [month.approximate ? "approximate" : "checked"] })),
      units: "modelled option P&L in ₹; month-end end-of-session marks, not broker MTM or fills",
      filters: [["all", "All months"], ["checked", "Checked months"], ["approximate", "Approximate months"]], searchText: (month) => month.key + " " + (month.approximate ? "approximate" : "checked"),
      columns: ["Month", "Monthly P&L", "Month-end P&L", "Supplied sessions", "Positive / negative days", "Data status"],
      empty: "No monthly end-of-session values are available.", initialFilter: "all",
      cells: (month) => [
        new Intl.DateTimeFormat("en-IN", { month: "short", year: "numeric", timeZone: "Asia/Kolkata" }).format(month.date),
        reportStack(reportMoney(month.net), "Change from prior month-end", reportTone(month.net)), reportMoney(month.endNet), String(month.days), month.positive + " / " + month.negative,
        month.approximate ? "Approximate" : "Checked",
      ],
    });
  }

  function reportMonthlyGrid(performance) {
    const scale = Math.max(...performance.dailyMonths.map((month) => Math.abs(month.net)), 1);
    const byKey = new Map(performance.dailyMonths.map((month) => [month.key, month]));
    const years = [...new Set(performance.dailyMonths.map((month) => Number(month.key.slice(0, 4))))].sort((a, b) => a - b);
    const scroll = el("div", "report-monthly-scroll");
    const grid = el("div", "report-monthly-grid");
    grid.setAttribute("role", "img");
    grid.setAttribute("aria-label", "Monthly modelled option performance. Each populated square shows percentage return against the selected return base.");
    grid.append(el("span", "report-monthly-heading report-monthly-year-heading", ""));
    for (let month = 0; month < 12; month += 1) grid.append(el("span", "report-monthly-heading", new Intl.DateTimeFormat("en-IN", { month: "short", timeZone: "UTC" }).format(new Date(Date.UTC(2026, month, 1)))));
    grid.append(el("span", "report-monthly-heading report-monthly-total-heading", "Year"));
    for (const year of years) {
      grid.append(el("strong", "report-monthly-year", String(year)));
      let yearNet = 0, yearDays = 0, approximate = false;
      for (let index = 0; index < 12; index += 1) {
        const month = byKey.get(year + "-" + String(index + 1).padStart(2, "0"));
        if (!month) { grid.append(el("span", "report-monthly-cell heat-empty", "")); continue; }
        yearNet += month.net; yearDays += month.days; approximate = approximate || month.approximate;
        const cell = el("span", "report-monthly-cell " + reportHeatClass(month.net, scale));
        cell.append(el("strong", "", reportReturn(month.net, performance.returnBase)), el("small", "", month.days + " d"));
        const label = new Intl.DateTimeFormat("en-IN", { month: "long", year: "numeric", timeZone: "Asia/Kolkata" }).format(month.date);
        cell.setAttribute("title", label + " · Monthly P&L " + reportMoney(month.net) + " · " + reportReturn(month.net, performance.returnBase) + " of return base · " + month.days + " supplied sessions · " + (month.approximate ? "Approximate" : "Checked"));
        grid.append(cell);
      }
      const total = el("span", "report-monthly-total " + reportHeatClass(yearNet, scale));
      total.append(el("strong", "", reportReturn(yearNet, performance.returnBase)), el("small", "", reportCompactMoney(yearNet)));
      total.setAttribute("title", year + " · P&L " + reportMoney(yearNet) + " · " + reportReturn(yearNet, performance.returnBase) + " of return base · " + yearDays + " supplied sessions · " + (approximate ? "Approximate" : "Checked"));
      grid.append(total);
    }
    scroll.append(grid);
    return scroll;
  }

  function reportOptionMonths(performance) {
    const section = el("section", "report-performance-section report-performance-months");
    section.append(reportHeading("Monthly performance", "Each populated month shows its modelled return against the return base. The first month starts at the selected backtest date; hover for ₹ P&L and data quality."));
    if (!performance.dailyMonths.length) {
      section.append(el("p", "report-no-rows", "Monthly performance needs daily end-of-session marks. This saved report only has completed-leg information."));
      return section;
    }
    if (!performance.returnBase) section.append(el("p", "report-inline-warning", "Set a return base when you run the backtest to show percentages. ₹ P&L remains available on hover and in the detailed figures."));
    section.append(reportMonthlyGrid(performance), reportDisclosure("View monthly figures", reportMonthlyTable(performance)));
    return section;
  }

  function reportOptionPerformance(model) {
    const panel = el("div", "report-option-performance");
    const sourceDescription = model?.pricing_policy === "recorded_first_estimate_fallback_v1"
      ? "A backtest estimate from recorded option-price observations and labelled model estimates. It is not broker P&L or an execution record."
      : "A backtest estimate from modelled option premiums. It is not broker P&L or an execution record.";
    panel.append(reportHeading("Option performance", sourceDescription));
    if (!model) {
      panel.append(el("p", "report-no-rows", "This saved result predates the option model. Run the backtest again to calculate option performance."));
      return panel;
    }
    const approximate = model.approximate === true || model.approximate_due_to_missing_minutes === true || model.approximate_due_to_assumed_holidays === true;
    if (model.status === "unavailable") {
      panel.append(el("p", "report-inline-warning", model.blocked_from?.reason || "Option performance is unavailable for this result."));
      return panel;
    }
    const performance = optionPerformance(model);
    const sourceCounts = ["recorded", "mixed", "estimated"].map((basis) => ({ basis, positions: performance.pricing[basis]?.positions || 0 }));
    const resultNote = model.status === "partial" ? "Checked portion only; later outcomes are unknown."
      : approximate ? "Approximate result; review data quality before relying on the totals."
        : model.validation_status === "validated" ? "Data and supplied strategy events were validated."
          : "Model result completed; see validation details in Option P&L.";
    panel.append(el("p", "report-performance-note", resultNote + " " + sourceCounts.map((item) => item.positions + " " + item.basis).join(" · ") + " price legs."));
    const metrics = el("div", "report-metrics option-metrics report-performance-metrics");
    metrics.append(
      reportMetric("Closed net P&L", reportMoney(performance.closedNet), performance.closedLegs.length + " completed legs · after modelled charges", reportTone(performance.closedNet)),
      reportMetric("Open P&L estimate", reportMoney(performance.openNet), performance.openLegs.length + " open legs · not realised cash", reportTone(performance.openNet)),
      reportMetric("Combined result", reportMoney(performance.totalNet), "Closed P&L + open estimate", reportTone(performance.totalNet)),
      reportMetric("Modelled charges", reportMoney(performance.charges), performance.modelOrderCount + " grouped model orders", "")
    );
    panel.append(metrics);
    if (model.as_of) panel.append(el("p", "report-footnote result-valuation", "Valued through " + marketTime(model.as_of) + ". Open values use the last checked modelled price, not an exit."));
    const overview = el("div", "report-performance-grid");
    overview.append(reportOptionCurve(performance));
    const risk = el("div", "report-performance-risk");
    const hasDailyMarks = performance.dailyPnl.length > 0;
    risk.append(reportHeading(hasDailyMarks ? "Daily risk" : "Close statistics", hasDailyMarks
      ? "Based on end-of-session model marks. It includes both open and closed option legs."
      : "Completed option legs only; rollovers and hedge replacements remain separate legs."));
    const statistics = el("div", "report-stat-grid report-option-stat-grid");
    if (hasDailyMarks) {
      const totalDays = performance.dailyPnl.length;
      const positiveRate = totalDays ? (performance.positiveDays / totalDays * 100).toFixed(1) + "%" : "—";
      statistics.append(
        reportStatistic("Positive days", positiveRate, performance.positiveDays + " of " + totalDays + " supplied sessions", performance.positiveDays > performance.negativeDays ? "gain" : ""),
        reportStatistic("Average daily P&L", reportMoney(performance.averageDaily), "End-of-session model mark", reportTone(performance.averageDaily)),
        reportStatistic("Largest daily loss", reportMoney(performance.worstDay), "One supplied session", performance.worstDay < 0 ? "loss" : ""),
        reportStatistic("Maximum drawdown", reportMoney(performance.dailyMaximumDrawdown), "Fall from a prior daily high", performance.dailyMaximumDrawdown < 0 ? "attention" : "")
      );
      risk.append(statistics, reportFacts([
        ["Best day", reportMoney(performance.bestDay)],
        ["Current drawdown", reportMoney(performance.dailyCurrentDrawdown)],
        ["Monthly rollovers", String(performance.rolloverCount)],
        ["Open legs included in marks", String(performance.openLegs.length)],
      ]));
    } else {
      const totalClosed = performance.closedLegs.length;
      const positiveRate = totalClosed ? (performance.positiveCount / totalClosed * 100).toFixed(1) + "%" : "—";
      statistics.append(
        reportStatistic("Positive closes", positiveRate, performance.positiveCount + " of " + totalClosed + " completed legs", performance.positiveCount > performance.negativeCount ? "gain" : ""),
        reportStatistic("Average close", reportMoney(performance.averageClose), "Net P&L per completed leg", reportTone(performance.averageClose)),
        reportStatistic("Largest closed loss", reportMoney(performance.worstClose), "One modelled option leg", performance.worstClose < 0 ? "loss" : ""),
        reportStatistic("Maximum drawdown", reportMoney(performance.maximumDrawdown), "Fall from a previous closed-leg high", performance.maximumDrawdown < 0 ? "attention" : "")
      );
      risk.append(statistics, reportFacts([
        ["Best closed leg", reportMoney(performance.bestClose)],
        ["Current closed-leg drawdown", reportMoney(performance.currentDrawdown)],
        ["Monthly rollovers", String(performance.rolloverCount)],
        ["Open legs excluded from path", String(performance.openLegs.length)],
      ]));
    }
    overview.append(risk); panel.append(overview);
    const scope = el("div", "report-method-body report-performance-scope");
    const priceLabel = sourceCounts.map((item) => item.positions + " " + reportPriceLabel(item.basis).toLowerCase()).join(" · ");
    scope.append(reportFacts([
      ["Price coverage", priceLabel],
      ["Validation", model.validation_status || "Not supplied"],
      ["Strategy events", model.event_validation ? model.event_validation.processed + " / " + model.event_validation.supplied + " processed" : "Not recorded"],
      ["Reconciliation", hasDailyMarks && Number.isFinite(performance.totalNet) && Math.abs(performance.dailyPnl.at(-1).totalNet - performance.totalNet) < 0.02 ? "Last daily mark matches model total" : !hasDailyMarks && Number.isFinite(performance.closedNet) && Math.abs(performance.closedNet - performance.path.reduce((sum, position) => sum + position.net_pnl, 0)) < 0.02 ? "Closed path matches model total" : "See Option P&L ledger"],
    ]));
    scope.append(el("p", "", hasDailyMarks
      ? "Daily and monthly P&L are end-of-session option marks, not broker MTM or execution fills. CAGR, Sharpe and similar return ratios remain unavailable because this replay has no verified capital or margin use."
      : "CAGR, Sharpe and similar return ratios are intentionally not shown. This replay does not have verified capital, margin use or a daily marked-to-market option equity series."));
    panel.append(reportOptionDays(performance), reportOptionMonths(performance));
    panel.append(reportDisclosure("Data coverage & calculation limits", scope));
    return panel;
  }

  function reportLeg(kind) {
    return { selling: "Option selling", ce_hedge: "Call hedge · CE", pe_hedge: "Put hedge · PE" }[kind] || kind;
  }

  function reportChargeName(key) {
    return { brokerage: "Brokerage", exchange: "Exchange charge", ipft: "IPFT · investor protection fund", sebi: "SEBI · regulator fee", gst: "GST · goods and services tax", stamp_duty: "Stamp duty", stt: "STT · securities transaction tax", total: "Total charges" }[key] || friendly(key);
  }

  function reportPriceLabel(basis) {
    return { recorded: "Recorded prices", mixed: "Mixed prices", estimated: "Estimated premiums", unavailable: "Price unavailable" }[basis] || "Estimated premiums";
  }

  function reportPriceSource(pricing) {
    if (!pricing || pricing.basis !== "recorded") return pricing?.fallback_reason || "Premium model estimate";
    const source = pricing.source || {};
    return [friendly(source.kind || "recorded quote"), validReplayTime(source.observed_at) ? marketTime(source.observed_at) : "", source.source, source.symbol].filter(Boolean).join(" · ");
  }

  function reportOptionPositions(model, selection = "selling") {
    const panel = el("div", "report-option-ledger");
    const isHedge = selection === "hedges";
    panel.append(reportHeading(isHedge ? "Independent hedge ledgers" : "Selling ledger", isHedge
      ? "Call and put hedges continue between selling trades. Each hedge is counted once, never assigned repeatedly to short trades."
      : "Bullish signals sell PE; bearish signals sell CE. Each rollover creates a new option record linked to the same spot trade."));
    if (model.status !== "complete") panel.append(el("p", "report-inline-warning", "Checked portion only. Open estimates are last-known values, not outcomes after missing data."));
    if (isHedge) for (const pending of model.pending_replacements || []) {
      panel.append(el("p", "report-inline-warning", pending.option_type + " replacement pending at " + marketTime(pending.due_at) + ". " + pending.status));
    }
    const rows = (isHedge ? [...(model.ledgers?.ce_hedge || []), ...(model.ledgers?.pe_hedge || [])] : model.ledgers?.selling || [])
      .slice().sort((a, b) => Date.parse(a.entry_at) - Date.parse(b.entry_at)).map((p) => ({ ...p, filters: [p.option_type, p.status, p.exit_reason, p.entry_reason] }));
    panel.append(reportTable({
      title: isHedge ? "Hedge positions" : "Selling positions", rows, units: "spot levels in points; premiums in ₹ per unit; P&L and charges in ₹",
      filters: [["all", "All positions"], ["CE", "Calls · CE"], ["PE", "Puts · PE"], ["closed", "Closed"], ["open", "Open / last known"], ["monthly_rollover", "Monthly rollover"], ...(isHedge ? [["hedge_replacement", "Target replacement"]] : [])],
      searchText: (p) => [p.position_id, p.parent_spot_trade, p.option_type, p.status, p.entry_at, p.exit_at, p.expiry, p.strike, p.exit_reason, p.entry_reason].join(" "),
      columns: ["Leg / reference", "Entry · IST", "Entry spot / premium", "Strike / expiry", "Quantity", "Exit or mark · IST", "Exit / mark spot & premium", "Gross / charges", "Net P&L", "Reason"],
      empty: model.status === "unavailable" ? "Option history is unavailable; see the Option model tab." : isHedge ? "No hedge positions. Check the configured hedge lots." : "No selling entries in the checked history. Hedges may still be active.",
      cells: (p) => {
        const open = p.status === "open", v = p.valuation || {};
        const facts = reportFacts([
          ["Record", p.position_id], ["Parent spot trade", p.parent_spot_trade || "Independent hedge"],
          ["Entry reason", friendly(p.entry_reason)], ["Valuation basis", open ? "Last checked close, not an exit" : friendly(p.exit_price_basis)],
          ["P&L price source", reportPriceLabel(p.pnl_basis)],
          ["Entry premium source", reportPriceSource(p.entry_pricing)],
          [open ? "Mark premium source" : "Exit premium source", reportPriceSource(open ? p.mark_pricing : p.exit_pricing)],
          ["Option contract", p.contract?.symbol || "Model contract / not selected"],
          ["Contract selection", p.contract_selection || "Legacy model rule"],
          ["Signed spot movement", reportSigned(v.net_spot_movement) + " pts · CE current − entry; PE entry − current"],
          ["Premium movement contribution", reportMoney(v.movement_contribution) + " / unit"],
          ["Time decay", reportMoney(v.time_decay) + " / unit"], ["Elapsed time", reportNumber(v.elapsed_minutes) + " minutes"],
          ["Premium P&L", reportMoney(p.premium_points_pnl) + " / unit"],
          ["Hedge trigger · spot", Number.isFinite(p.trigger_spot) ? reportNumber(p.trigger_spot) + " · " + reportNumber(p.replacement_distance) + " pts from entry" : "Not applicable"],
          ["Entry charges", reportMoney(p.entry_charges?.total)], ["Exit charges", open ? "Not charged — still open" : reportMoney(p.exit_charges?.total)],
        ]);
        const identity = reportStack(p.direction + " " + p.option_type, p.parent_spot_trade || "Independent " + p.option_type + " hedge");
        identity.append(reportDisclosure("Calculation details", facts));
        return [identity, reportDateTime(p.entry_at), reportStack(reportNumber(p.entry_spot) + " spot", reportMoney(p.entry_premium) + " / unit"),
          reportStack(p.strike === null ? "Not selected" : reportNumber(p.strike), p.expiry + " expiry"),
          reportStack(p.quantity + " units", p.lots + " lots × " + p.units_per_lot),
          reportDateTime(open ? p.mark_at : p.exit_at, open ? "Last mark · not exit" : "Closed"),
          reportStack(reportNumber(open ? p.mark_spot : p.exit_spot) + " spot", reportMoney(open ? p.mark_premium : p.exit_premium) + " / unit"),
          reportStack(reportMoney(p.gross_pnl), reportMoney(p.charges) + (open ? " entry charges" : " charges")),
          reportStack(reportMoney(p.net_pnl), (open ? "Open estimate · " : "Closed · ") + reportPriceLabel(p.pnl_basis), reportTone(p.net_pnl)),
          reportStack(open ? "Open" : friendly(p.exit_reason), "Opened: " + friendly(p.entry_reason))];
      },
    }));
    return panel;
  }

  function reportOptionModel(model) {
    const panel = el("div", "report-options");
    const recordedFirst = model?.pricing_policy === "recorded_first_estimate_fallback_v1";
    panel.append(reportHeading(recordedFirst ? "Option P&L" : "Modelled option premiums", recordedFirst ? "Recorded prices first; estimates fill missing prices. Neither is a broker fill." : "Estimated premiums, not actual option quotes or fills."));
    if (!model) {
      panel.append(el("p", "report-no-rows", "This saved result predates the option model. Run the backtest again to calculate selling, independent hedges and charges."));
      return panel;
    }
    const approximate = model.approximate === true || model.approximate_due_to_missing_minutes === true || model.approximate_due_to_assumed_holidays === true;
    const state = el("div", "report-status " + (model.status === "complete" && !approximate ? "complete" : "warning"));
    state.append(el("strong", "", model.status === "complete" ? approximate ? "Option simulation finished — approximate result" : model.validation_status === "validated" ? "Option simulation complete — data and events validated" : "Option simulation complete — legacy validation" : model.status === "partial" ? "Partial option simulation — checked portion only" : "Option calculation unavailable"));
    if (model.approximate_due_to_missing_minutes) state.append(el("p", "", model.skipped_minute_count + (model.skipped_minute_count === 1 ? " missing minute skipped. " : " missing minutes skipped. ") + "Stops, targets or hedge triggers inside gaps may be missed; P&L is approximate, not a complete-price result."));
    for (const warning of model.warnings || []) state.append(el("p", "", warning));
    state.append(el("p", "", "Strategy: " + (model.strategy?.label || "Legacy 20-second review · next-minute entry · stop-first assumption")));
    if (model.event_validation) state.append(el("p", "", model.event_validation.processed + " / " + model.event_validation.supplied + " supplied strategy events processed."));
    if (model.as_of) state.append(el("p", "", "Valued through " + marketTime(model.as_of) + ". Open values below are estimates at this time, not realised profit."));
    if (model.blocked_from) state.append(el("p", "", model.blocked_from.reason + " Later outcomes are unknown."));
    panel.append(model.status === "complete" ? reportDisclosure(approximate ? "Validation details · Approximate option result" : "Validation details & valuation time", state) : state);
    if (model.status === "unavailable") return panel;
    if (recordedFirst && model.pricing_summary) {
      const entries = Object.entries(model.pricing_summary);
      panel.append(el("p", "report-footnote", entries.map(([basis, s]) => `${reportPriceLabel(basis)}: ${s.positions} legs`).join(" · ")));
      const sources = reportFacts(entries.map(([basis, s]) => [reportPriceLabel(basis), `${s.positions} legs · closed net ${reportMoney(s.closed_net)} · open net ${reportMoney(s.open_net)}`]));
      sources.append(el("p", "", "Mixed means one price was recorded and the other estimated. A recorded candle close is a price proxy, not a guaranteed fill at a stop or target. Missing prices are never filled from a different contract or a later time."));
      if (model.option_data) sources.append(el("p", "", `${model.option_data.contracts} contracts in the available archive · ${model.option_data.conflicting_minutes} conflicting minutes excluded · ${model.option_data.rejected_rows} unusable rows excluded.`));
      if (model.option_history) {
        const history = model.option_history;
        sources.append(el("p", "", `This replay checked ${history.candidate_contracts_checked} eligible short-option contracts at their entry minutes and ${history.lifecycle_contracts_checked} exact selected short/hedge contracts across their lifetimes. It saved ${history.candidate_candles_saved + history.lifecycle_candles_saved} recorded option candles.`));
        if (history.rejected_candles) sources.append(el("p", "report-inline-warning", `${history.rejected_candles} provider option candle${history.rejected_candles === 1 ? " was" : "s were"} outside accepted session or price rules and excluded. It was not used as an actual premium.`));
        for (const note of history.notes || []) sources.append(el("p", "", note));
      }
      panel.append(reportDisclosure("Price coverage & contribution to P&L", sources));
    }
    const combined = model.combined, metrics = el("div", "report-metrics option-metrics");
    metrics.append(
      reportMetric("Closed net · ₹", reportMoney(combined.realised_net), "Realised in model · after closed-position charges", reportTone(combined.realised_net)),
      reportMetric("Open estimate · ₹", reportMoney(combined.open_net), "Last checked value · entry charges only", reportTone(combined.open_net)),
      reportMetric("Total charges · ₹", reportMoney(model.charges.total), model.orders.length + " grouped model orders · not per lot"),
      reportMetric("Combined net · ₹", reportMoney(combined.total_net), "Closed net + open estimate · not realised cash", reportTone(combined.total_net)),
    );
    panel.append(metrics);
    if (model.as_of) panel.append(el("p", "report-footnote result-valuation", "Valued through " + marketTime(model.as_of) + " · " + model.config.sell_lots + " selling / " + model.config.ce_hedge_lots + " call hedge / " + model.config.pe_hedge_lots + " put hedge lots · " + model.config.units_per_lot + " units per lot (assumed)."));
    panel.append(reportHeading("Result breakdown", "Charges are already deducted from net totals; do not subtract them again."));
    const scroller = el("div", "report-table-scroll"); scroller.tabIndex = 0;
    scroller.setAttribute("role", "region"); scroller.setAttribute("aria-label", "Option profit summary; scroll for all columns");
    const table = el("table", "report-table option-summary"), head = el("thead"), header = el("tr"), body = el("tbody");
    table.append(el("caption", "sr-only", "Modelled option profit summary in rupees"));
    for (const label of ["Ledger", "Closed / open", "Closed gross", "Closed charges", "Closed net", "Open gross estimate", "Open entry charges", "Open net estimate"]) { const th = el("th", "", label); th.scope = "col"; header.append(th); }
    head.append(header); table.append(head, body);
    for (const [kind, s] of [...Object.entries(model.summary), ["Combined", combined]]) {
      const row = el("tr");
      const name = el("th", "", reportLeg(kind)); name.scope = "row"; row.append(name);
      row.append(el("td", "", kind === "Combined" ? "All ledgers" : s.closed_count + " / " + s.open_count));
      for (const key of ["realised_gross", "realised_charges", "realised_net", "open_gross", "open_entry_charges", "open_net"]) row.append(el("td", key.endsWith("net") ? reportTone(s[key]) : "", reportMoney(s[key])));
      body.append(row);
    }
    scroller.append(table);
    const summary = el("table", "report-table option-net-summary"), summaryHead = el("thead"), summaryHeader = el("tr"), summaryBody = el("tbody");
    summary.append(el("caption", "sr-only", "Net modelled option profit in rupees"));
    for (const label of ["Ledger", "Closed net", "Open estimate", "Combined net"]) { const th = el("th", "", label); th.scope = "col"; summaryHeader.append(th); }
    summaryHead.append(summaryHeader); summary.append(summaryHead, summaryBody);
    for (const [kind, s] of [...Object.entries(model.summary), ["Combined", combined]]) {
      const row = el("tr"), name = el("th", "", reportLeg(kind)); name.scope = "row"; row.append(name);
      const total = Number.isFinite(s.realised_net) && Number.isFinite(s.open_net) ? s.realised_net + s.open_net : null;
      for (const value of [s.realised_net, s.open_net, total]) row.append(el("td", reportTone(value), reportMoney(value)));
      summaryBody.append(row);
    }
    const summaryScroll = el("div", "report-table-scroll"); summaryScroll.tabIndex = 0;
    summaryScroll.setAttribute("role", "region"); summaryScroll.setAttribute("aria-label", "Net option result by ledger; scroll for all columns");
    summaryScroll.append(summary);
    panel.append(summaryScroll, reportDisclosure("Gross profit & charge breakdown", scroller));
    const config = model.config, method = el("div", "report-method-body");
    method.append(reportFacts([
      ["Instrument / shared model", config.instrument + " / " + config.version],
      ["Lots: selling / CE / PE", config.sell_lots + " / " + config.ce_hedge_lots + " / " + config.pe_hedge_lots],
      ["Comparison size", config.units_per_lot + " units per lot · fixed assumption, not historical lot sizes"],
      ["Sold entry premium", reportMoney(Number(config.sold_entry_premium)) + (recordedFirst ? " target / unit · recorded ATM/ITM selection when available" : " / unit · strike not selected")],
      ["Decay per calendar day", "Selling ₹" + config.sold_daily_decay + " / hedge ₹" + config.hedge_daily_decay + " per unit"],
      ["Calendar version", model.calendar_version], ["Charges version", model.fee_version],
    ]));
    method.append(el("p", "", "Premium movement is calculated separately for each " + config.band_points + " spot-point band. Start at " + config.initial_delta + " premium per spot point; add " + config.delta_step + " per positive band up to 1, or subtract it per negative band down to 0. Use net movement from each option's own entry."));
    method.append(el("p", "", "Hedge model reference premium = floor(entry spot / " + config.hedge_premium_divisor + ") × " + config.hedge_premium_step + ". Floor means round down. Replace only the side that moves favourably by " + config.replacement_multiple + " × this model reference, measured in spot points. Recorded premiums change P&L, not this trigger. Estimated time decay includes nights and weekends."));
    const assumptions = el("ul"); for (const rule of model.assumptions || []) assumptions.append(el("li", "", rule));
    method.append(assumptions); panel.append(reportDisclosure("Model settings, calculation rules & limitations", method));
    panel.append(reportDisclosure("Selling positions · " + (model.ledgers?.selling?.length || 0), reportOptionPositions(model)));
    const fees = el("div");
    fees.append(reportFacts(Object.entries(model.charges).map(([key, value]) => [reportChargeName(key), reportMoney(value)])));
    fees.append(reportTable({
      title: "Model orders", units: "premiums in ₹ per unit; charges in ₹", rows: model.orders.map((o) => ({ ...o, filters: [o.ledger, o.side] })),
      filters: [["all", "All orders"], ["selling", "Selling leg"], ["ce_hedge", "Call hedge"], ["pe_hedge", "Put hedge"], ["BUY", "Buys"], ["SELL", "Sells"]],
      searchText: (o) => [o.order_id, o.position_id, o.at, o.reason, o.ledger, o.side].join(" "),
      columns: ["Order / position", "Time · IST", "Leg / action", "Premium / quantity", "Charges", "Reason"], empty: "No modelled orders.",
      cells: (o) => {
        const cost = reportStack(reportMoney(o.charges.total), "Total charges");
        cost.append(reportDisclosure("Fee breakdown", reportFacts([...["brokerage", "exchange", "ipft", "sebi", "gst", "stamp_duty", "stt"].map((key) => [reportChargeName(key), reportMoney(o.charges[key])]), ["Schedule effective from", o.charges.effective_from]])));
        return [reportStack(o.order_id, o.position_id), reportDateTime(o.at), reportStack(reportLeg(o.ledger), o.side), reportStack(reportMoney(o.premium) + " / unit", o.quantity + " units · " + o.lots + " lots · " + reportPriceLabel(o.pricing?.basis)), cost, friendly(o.reason)];
      },
    }));
    panel.append(reportDisclosure("Charges & model order history", fees));
    const rolls = reportTable({
      title: "Monthly rollovers", units: "spot levels in points", rows: model.rollovers.map((r) => ({ ...r, filters: [] })), filters: [["all", "All rollovers"]],
      searchText: (r) => [r.at, r.old_expiry, r.new_expiry].join(" "), columns: ["Rollover · IST", "Spot", "Old expiry", "New expiry", "Selling legs reopened"], empty: "No monthly rollover in the checked history.",
      cells: (r) => [reportDateTime(r.at), reportNumber(r.spot), r.old_expiry, r.new_expiry, String(r.selling_positions_reopened)],
    });
    panel.append(reportDisclosure("Monthly rollover history · " + model.rollovers.length, rolls));
    if (model.delayed_actions?.length) panel.append(reportDisclosure("Actions delayed by missing prices · " + model.delayed_actions.length, reportTable({
      title: "Delayed actions", units: "scheduled and actual model times in IST", rows: model.delayed_actions.map((action) => ({ ...action, filters: [] })),
      filters: [["all", "All delayed actions"]], searchText: (action) => [action.type, action.option_type, action.scheduled_at, action.at].join(" "),
      columns: ["Action", "Scheduled · IST", "Next available price · IST"], empty: "No delayed actions.",
      cells: (action) => [friendly(action.type) + (action.option_type ? " · " + action.option_type : ""), reportDateTime(action.scheduled_at), reportDateTime(action.at)],
    })));
    return panel;
  }

  function reportPricingSummary(options) {
    const box = el("div", "pricing-summary");
    const groups = options?.pricing_summary;
    if (!options) {
      box.append(el("strong", "", "Spot-only result"), el("span", "", "No option return has been calculated."));
      return box;
    }
    const recorded = groups?.recorded?.positions || 0, mixed = groups?.mixed?.positions || 0, estimated = groups?.estimated?.positions || 0;
    box.append(el("strong", "", "Option P&L source"));
    const source = mixed || (recorded && estimated) ? "recorded and estimated prices" : recorded ? "recorded price observations" : "a pricing model";
    box.append(el("span", "", `Uses ${source} · ${recorded} recorded / ${mixed} mixed / ${estimated} estimated price legs · price proxies, not broker fills.`));
    return box;
  }

  function renderSpotRangeResult(range, proxy, optionModel = null) {
    const result = $("replay-result"), model = reportModel(range, proxy);
    result.replaceChildren();
    const header = el("div", "report-header"), title = el("div");
    title.append(el("h3", "", replayDay(range.start_date) + " — " + replayDay(range.end_date)));
    header.append(title, el("span", "report-tag " + (model.partial || model.approximate ? "warning" : "info"), model.partial ? "INCOMPLETE RESULT" : model.uncertain.length ? "UNCERTAIN EXITS" : model.assumedDays.length ? "APPROXIMATE · UNVERIFIED DAYS" : model.skippedMinutes ? "FINISHED · MISSING MINUTES SKIPPED" : model.approximate ? "APPROXIMATE · ASSUMED EXIT ORDER" : "SPOT REPLAY COMPLETE"));
    result.append(header);
    result.append(reportPricingSummary(optionModel));
    let riskReference = "";
    if (proxy?.strategy?.risk_reference_basis === "completed_signal_hour_close") {
      riskReference = "SL & target: signal hour close, fixed for each trade. Entry price is separate.";
    } else if (proxy) {
      result.append(el("p", "report-inline-warning", "Legacy result: stops and targets used entry-price references. Run again to apply the hourly-close rule."));
    }
    if (range.source?.startsWith("SYNTHETIC")) result.append(el("p", "report-inline-warning", range.source));
    const status = el("div", "report-status " + (model.partial || model.approximate ? "warning" : "complete"));
    if (model.partial) {
      status.append(el("strong", "", proxy?.blocked_from?.date ? "Position tracking stopped on " + replayDay(proxy.blocked_from.date) : "A full-period position result is unavailable"));
      status.append(el("p", "", proxy?.blocked_from?.reason || "Price coverage or position calculations are incomplete."));
      status.append(el("p", "", "The figures below cover only " + model.positionSessions + " checked position sessions" + (proxy?.last_session ? ", through " + replayDay(proxy.last_session) : "") + ". Later position outcomes are unknown."));
    } else {
      status.append(el("strong", "", (model.approximate ? "Replay continued through " : "Position history checked through ") + (proxy?.last_session ? replayDay(proxy.last_session) : replayDay(range.end_date))));
      status.append(el("p", "", "Historical spot model only. This does not validate option execution or profitability."));
    }
    status.append(el("p", "", "Strategy: " + (proxy?.strategy?.label || "Legacy 20-second review · next-minute entry · stop-first assumption")));
    if (model.skippedMinutes) status.append(el("p", "", model.skippedMinutes + (model.skippedMinutes === 1 ? " missing minute was skipped, " : " missing minutes were skipped, ") + "without filling in prices. Stops, targets and hedge triggers inside these gaps cannot be checked. Results are approximate; see Data quality for the affected dates."));
    if (model.assumedDays.length) status.append(el("p", "", "Unverified missing whole days: " + model.assumedDays.join(", ") + ". Assumed holidays are not verified closures; missed trades and exits remain possible. Totals are approximate."));
    if (model.uncertain.length) status.append(el("p", "", model.uncertain.length + " uncertain exits: both stop and target touched within a minute. No exact price or P&L is assigned. Closed totals exclude these positions and are not the full strategy result."));
    if (proxy?.assumed_stop_first_count) status.append(el("p", "", proxy.assumed_stop_first_count + " collisions use the selected stop-first assumption. The actual minute path is unknown; totals are approximate."));
    const qualityNotes = [];
    if (model.partial) qualityNotes.push("Incomplete history — figures cover the checked portion only.");
    if (model.skippedMinutes) qualityNotes.push(model.skippedMinutes + " missing minute(s)");
    if (model.assumedDays.length) qualityNotes.push(model.assumedDays.length + " unverified day(s)");
    if (model.uncertain.length) qualityNotes.push(model.uncertain.length + " uncertain exit(s), excluded from closed totals");
    if (proxy?.assumed_stop_first_count) qualityNotes.push(proxy.assumed_stop_first_count + " assumed stop-first exit(s)");
    if (qualityNotes.length) {
      result.append(el("p", "report-inline-warning", qualityNotes.join(" · ") + " — results are approximate. See Data quality."));
      result.append(reportDisclosure("Data checks & limitations", status));
    }
    result.append(el("p", "report-run-settings", [riskReference, "Timing: " + (proxy?.strategy?.label || "Legacy 20-second review · next-minute entry · stop-first assumption")].filter(Boolean).join(" · ")));
    const metrics = el("div", "report-metrics");
    metrics.append(
      reportMetric(model.partial ? "Closed points · checked portion" : "Closed spot points", reportSigned(model.closedPoints), model.available ? model.closed.length + " closed positions · not ₹ P&L" : "No position result available", reportTone(model.closedPoints)),
      reportMetric(model.partial ? "Open result · incomplete" : "Open spot points", model.partial ? "Unknown" : reportSigned(model.openPoints), model.open.length + (model.partial ? " last-known open · later outcome unknown" : " open positions · at last checked price")),
      reportMetric("Hourly signals", String(range.total_signals), model.checkedHours + " complete hours · " + model.signalSessions + " dates"),
      reportMetric("Data review", String(new Set([...model.gapDates, ...model.assumedDays]).size), "Dates with missing or unverified data", model.gapDates.size || model.assumedDays.length ? "attention" : ""),
    );
    const spotSummary = el("div");
    spotSummary.append(metrics, el("p", "report-limit-note", "Spot points are not rupee P&L. They exclude option premiums, hedges and charges."));
    const tabs = el("div", "report-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "Backtest report sections");
    const contents = el("div", "report-tab-content");
    const definitions = [
      ...(optionModel ? [["performance", "Performance", () => reportOptionPerformance(optionModel)]] : []),
      ["overview", "Spot trades", () => { spotSummary.append(reportOverview(model)); return spotSummary; }],
      ["positions", "Trades", () => reportPositions(model)],
      ["signals", "Signals", () => reportSignals(model)],
      ["quality", "Data quality", () => reportQuality(model)],
      ["options", "Option P&L", () => reportOptionModel(optionModel)],
      ["hedges", "Hedges", () => optionModel ? reportOptionPositions(optionModel, "hedges") : reportHedges()],
    ];
    const buttons = [], panels = [];
    definitions.forEach(([id, label, render], index) => {
      const panel = el("div", "report-tab-panel"); panel.id = "report-panel-" + id;
      panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", "report-tab-" + id); panel.tabIndex = 0; panel.hidden = true;
      let built = false;
      const button = reportButton(label, () => select(index), "report-tab");
      button.id = "report-tab-" + id; button.setAttribute("role", "tab"); button.setAttribute("aria-controls", panel.id);
      button.addEventListener("keydown", (event) => {
        let next = index;
        if (event.key === "ArrowRight") next = (index + 1) % definitions.length;
        else if (event.key === "ArrowLeft") next = (index + definitions.length - 1) % definitions.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = definitions.length - 1;
        else return;
        event.preventDefault(); select(next); buttons[next].focus();
      });
      buttons.push(button); panels.push({ panel, build: () => { if (!built) { panel.append(render()); built = true; } } });
      tabs.append(button); contents.append(panel);
    });
    function select(index) {
      buttons.forEach((button, i) => { button.setAttribute("aria-selected", String(i === index)); button.tabIndex = i === index ? 0 : -1; panels[i].panel.hidden = i !== index; });
      panels[index].build();
    }
    // Keep spot figures in their own tab; do not mix them with the option cash summary.
    const spotPanelIndex = optionModel ? 1 : 0;
    if (optionModel) { panels[spotPanelIndex].panel.append(spotSummary); }
    result.append(tabs, contents);
    const meta = el("div", "report-provenance");
    meta.append(el("span", "", "Source: " + (range.source || "Not supplied")), el("span", "", "All times IST" + (range.checked_at ? " · Generated " + marketTime(range.checked_at) : "")));
    result.append(meta);
    select(0);
    result.hidden = false;
    $("replay-empty").hidden = true;
  }

  function renderKite() {
    syncReplayDate();
    const status = kite.status;
    const connected = kiteConnected();
    const configured = status?.configured === true;
    const expired = configured && kiteSessionExpired();
    $("kite-status-badge").textContent = connected ? "SIGNED IN" : expired ? "SESSION EXPIRED" : configured ? "SIGN IN NEEDED" : status ? "NOT SET UP" : "UNAVAILABLE";
    $("broker-status").textContent = connected ? "Connected" : expired ? "Session expired" : status ? "Not connected" : "Unavailable";
    $("nav-connection-status").textContent = connected ? "Zerodha connected" : expired ? "Zerodha expired" : status ? "Connect Zerodha" : "Zerodha unavailable";
    $("nav-connection-dot").classList.toggle("connected", connected);
    $("broker-status-note").textContent = connected ? (kite.quote ? (quoteFreshness(kite.quote).startsWith("Recent") ? "Recent quote fetched · no order functions" : "Quote fetched but not current · no order functions") : "Fetch a quote to check data access") : configured ? "App details loaded · sign in needed" : "No active data session";
    $("kite-message").textContent = expired ? "The data session has expired. Sign in at Zerodha again before requesting data." : status?.message || (connected ? "A sign-in session is held locally. Fetch a quote to confirm data access. No order functions are included." : configured ? "Your app details are loaded. Complete sign-in at Zerodha to request market data." : "Add your Kite app details, then sign in at Zerodha.");
    $("kite-key-status").textContent = configured ? status.masked_api_key || "Configured · key hidden" : "Not configured";
    $("kite-session-status").textContent = expired ? "Expired · sign in again" : connected ? (status.session_expires_at ? `Expires ${marketTime(status.session_expires_at)}` : "Connected · expiry not supplied") : "Not connected";
    $("kite-memory-status").textContent = status?.credential_storage_warning ? "Saved details need attention" : status?.credentials_saved === true ? "Saved · Windows account protected" : status?.credentials_in_memory === true ? "In running app memory only" : "None loaded";
    $("kite-redirect-url").textContent = status?.redirect_url || "http://127.0.0.1:8765/api/kite/callback";
    $("kite-configure").textContent = configured ? "Change app details" : "Set up connection";
    $("kite-login").textContent = connected ? "Sign in again" : "Sign in at Zerodha";
    for (const id of ["kite-refresh", "kite-configure", "kite-forget"]) $(id).disabled = kite.busy;
    $("kite-forget").hidden = !configured && !status?.credential_storage_warning;
    $("kite-forget-note").hidden = !configured && !status?.credential_storage_warning;
    $("kite-login").disabled = kite.busy || !configured;
    $("kite-sync-instruments").disabled = kite.busy || !connected;
    $("kite-fetch-spot").disabled = kite.busy || !connected;

    const instruments = kite.instruments;
    const available = instruments?.available === true;
    $("kite-instrument-count").textContent = available ? `${Number(instruments.instrument_count || 0).toLocaleString("en-IN")} instruments` : "Not downloaded";
    $("kite-instruments-note").textContent = available
      ? `${Number(instruments.banknifty_contract_count || 0).toLocaleString("en-IN")} BANKNIFTY contracts · downloaded ${marketTime(instruments.synced_at)}`
      : instruments?.message || (connected ? "Download the contract list to inspect listed lot sizes and expiry dates." : "Sign in before downloading the current contract list.");
    const contractParts = [];
    if (available && Array.isArray(instruments.lot_sizes) && instruments.lot_sizes.length) contractParts.push(`Listed lot sizes: ${instruments.lot_sizes.join(", ")}`);
    if (available && Array.isArray(instruments.expiries) && instruments.expiries.length) contractParts.push(`Listed expiries: ${instruments.expiries.join(", ")}`);
    $("kite-contract-details").textContent = contractParts.join(". ");
    $("kite-contract-details").hidden = !contractParts.length;
    $("kite-sync-instruments").textContent = available ? "Refresh instrument list" : "Download instrument list";

    if (!connected) kite.quote = null;
    const quote = kite.quote;
    $("kite-spot-price").textContent = quote ? quoteFormat.format(quote.last_price) : "Not fetched";
    $("kite-quote-note").textContent = quote
      ? `Quote time: ${marketTime(quote.timestamp)}. Fetched: ${marketTime(quote.fetched_at)}. ${quoteFreshness(quote)}`
      : "No price has been requested. This is not a streaming feed.";
    $("kite-quote-source").textContent = `Source: ${quote?.source || "Zerodha Kite Connect"}${quote?.instrument ? ` · ${quote.instrument}` : ""}`;
    $("kite-fetch-spot").textContent = quote ? "Refresh spot quote" : "Fetch spot quote";
    if (kite.expiryTimer) window.clearTimeout(kite.expiryTimer);
    kite.expiryTimer = null;
    const expiry = Date.parse(status?.session_expires_at || "");
    const wakeAt = [];
    if (connected && Number.isFinite(expiry)) wakeAt.push(expiry + 20);
    if (quote && quoteFreshness(quote).startsWith("Recent")) wakeAt.push(Date.parse(quote.timestamp) + 120020);
    if (wakeAt.length) kite.expiryTimer = window.setTimeout(renderKite, Math.min(Math.max(Math.min(...wakeAt) - Date.now(), 20), 86400000));
    renderReplayAvailability();
    const detailQuantityNote = $("detail-quantity-note");
    if (selectedStrategy() && detailQuantityNote) detailQuantityNote.textContent = draftQuantityNote(selectedStrategy().lots, kite.instruments);
    updateDraftQuantity();
  }

  async function refreshKiteStatus() {
    if (kite.busy) return;
    kite.busy = true;
    renderKite();
    showError("kite-error", "");
    try {
      const response = await api("/api/kite/status");
      if (!response.status || typeof response.status.configured !== "boolean" || typeof response.status.connected !== "boolean") throw new Error("The local app did not return a valid connection status.");
      kite.status = response.status;
      try {
        const result = await api("/api/kite/instruments/status");
        kite.instruments = result.status || null;
      } catch { kite.instruments = null; showError("kite-error", "Connection status loaded, but the instrument-list status is unavailable. You can try Refresh status again."); }
    } catch (error) {
      kite.status = null;
      kite.quote = null;
      showError("kite-error", `${error.message} Offline strategy checks are still separate and do not need this connection.`);
    } finally { kite.busy = false; renderKite(); }
  }

  function clearTradetronInputs() { $("tradetron-strategy-id").value = ""; $("tradetron-api-link").value = ""; }

  function renderTradetron() {
    const status = tradetron.status;
    const runner = tradetron.runner;
    const linked = status?.linked === true;
    $("tradetron-status-badge").textContent = linked && status?.delivery === "verified" ? "PAPER LINK VERIFIED" : linked && status?.delivery === "needs_relink" ? "LINK NEEDS UPDATING" : linked && status?.delivery === "unverified" ? "PAPER LINK UNVERIFIED" : status ? "NOT LINKED" : "UNAVAILABLE";
    $("tradetron-message").textContent = status?.message || "The executor-link status is unavailable.";
    $("tradetron-strategy-status").textContent = linked ? `Strategy ${status.strategy_id}` : "Not linked";
    $("tradetron-delivery-status").textContent = status?.delivery === "verified" ? "Verified · paper delivery only" : status?.delivery === "unverified" ? "Not tested yet" : status?.delivery === "needs_relink" ? "Rejected · replace API link" : status?.delivery === "disabled" ? "Unavailable" : "Unavailable";
    $("tradetron-storage-status").textContent = status?.credential_storage_warning ? "Saved link needs attention" : linked && status.credentials_saved ? "Windows account protected" : "Not saved";
    $("tradetron-runner-status").textContent = runner?.legacy ? "Restart local app to load runner" : runner?.blocked ? "Blocked · review Tradetron" : runner?.armed ? "Armed · paper values only" : runner?.automatic_runner === "unavailable" ? "Off · automatic sending unavailable" : "Not checked";
    $("tradetron-configure").textContent = linked ? "Change executor link" : "Link paper executor";
    $("tradetron-configure").disabled = tradetron.busy || status?.credential_storage_available !== true;
    $("tradetron-forget").hidden = !linked && !status?.credential_storage_warning;
    $("tradetron-forget").disabled = tradetron.busy;
  }

  async function refreshTradetronStatus() {
    if (tradetron.busy) return;
    tradetron.busy = true;
    renderTradetron();
    try {
      const response = await api("/api/tradetron/status");
      if (!response.status || typeof response.status.linked !== "boolean" || !["unverified", "verified", "needs_relink", "disabled"].includes(response.status.delivery)) throw new Error("The local app did not return a valid executor-link status.");
      tradetron.status = response.status;
      // Static files can update before a local Python process is restarted.
      // Keep the linked executor usable and explain that mixed-version state
      // rather than reporting the protected link itself as unavailable.
      tradetron.runner = response.runner?.mode === "paper_only" ? response.runner : {
        mode: "paper_only", legacy: true, armed: false, blocked: false,
        automatic_runner: "restart_required",
      };
      showError("tradetron-error", "");
    } catch (error) {
      tradetron.status = null;
      tradetron.runner = null;
      showError("tradetron-error", error.message);
    } finally { tradetron.busy = false; renderTradetron(); renderJourney(); }
  }

  function activeDeployment() {
    return state.deployments.find((deployment) => deployment.id === activeDeploymentId) || null;
  }

  function openTradetronForm(deployment = null) {
    if (tradetron.busy) return;
    activeDeploymentId = deployment?.id || null;
    const executor = deployment?.executor;
    clearTradetronInputs();
    $("tradetron-dialog-title").textContent = deployment ? "Link this paper deployment" : "Link legacy Tradetron executor";
    $("tradetron-dialog-description").textContent = deployment
      ? `This link belongs only to ${deployment.strategy_name || "this paper deployment"}. It stays protected by this Windows account and is never shown again.`
      : "This old app-wide link can be moved into one paper deployment from that deployment’s three-dot menu.";
    $("tradetron-strategy-id").value = executor?.strategy_id || (!deployment ? tradetron.status?.strategy_id || "" : "");
    showError("tradetron-form-error", "");
    setTradetronFormBusy(false);
    $("tradetron-dialog").showModal();
    $("tradetron-strategy-id").focus();
  }

  function closeTradetronForm() {
    clearTradetronInputs();
    if (!tradetron.busy) {
      $("tradetron-dialog").close();
      activeDeploymentId = null;
    }
  }

  function setTradetronFormBusy(busy) {
    for (const id of ["tradetron-save-config", "tradetron-close-dialog", "tradetron-cancel-dialog", "tradetron-strategy-id", "tradetron-api-link"]) $(id).disabled = busy;
    $("tradetron-save-config").textContent = busy ? "Saving protected link…" : "Save protected link";
  }

  $("tradetron-configure").addEventListener("click", openTradetronForm);
  $("tradetron-close-dialog").addEventListener("click", closeTradetronForm);
  $("tradetron-cancel-dialog").addEventListener("click", closeTradetronForm);
  $("tradetron-dialog").addEventListener("cancel", (event) => { clearTradetronInputs(); if (tradetron.busy) event.preventDefault(); });
  $("tradetron-dialog").addEventListener("close", () => { clearTradetronInputs(); activeDeploymentId = null; });
  window.addEventListener("pagehide", clearTradetronInputs);
  $("tradetron-config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (tradetron.busy || !$("tradetron-config-form").reportValidity()) return;
    let payload = { strategy_id: $("tradetron-strategy-id").value.trim(), api_link: $("tradetron-api-link").value.trim(), remember: true };
    clearTradetronInputs();
    tradetron.busy = true;
    setTradetronFormBusy(true);
    renderTradetron();
    showError("tradetron-form-error", "");
    try {
      const deployment = activeDeployment();
      const endpoint = deployment ? `/api/deployments/${encodeURIComponent(deployment.id)}/tradetron/link` : "/api/tradetron/link";
      const pending = api(endpoint, { method: "POST", body: JSON.stringify(payload) });
      payload = null;
      const response = await pending;
      const status = deployment ? response.deployment?.executor : response.status;
      if (!status || status.linked !== true || status.delivery !== "unverified") throw new Error("Executor link not confirmed");
      if (deployment) await loadState(); else tradetron.status = response.status;
      $("tradetron-dialog").close();
      announce(deployment ? "Paper deployment executor linked with Windows account protection. Automatic sending remains off." : "Legacy Tradetron paper executor linked with Windows account protection. Move it into a paper deployment before using it.");
    } catch (error) {
      showError("tradetron-form-error", `${error.message} The API-link field has been cleared; paste it again to retry.`);
    } finally {
      payload = null;
      clearTradetronInputs();
      tradetron.busy = false;
      setTradetronFormBusy(false);
      renderTradetron();
      renderJourney();
    }
  });
  $("tradetron-forget").addEventListener("click", async () => {
    if (tradetron.busy || (!tradetron.status?.linked && !tradetron.status?.credential_storage_warning)) return;
    tradetron.busy = true;
    renderTradetron();
    showError("tradetron-error", "");
    try {
      const response = await api("/api/tradetron/disconnect", { method: "POST", body: "{}" });
      if (!response.status || response.status.linked !== false) throw new Error("The local app did not confirm the executor link was removed.");
      tradetron.status = response.status;
      announce("Tradetron paper executor link removed. No Tradetron signals can be sent.");
    } catch (error) { showError("tradetron-error", error.message); }
    finally { tradetron.busy = false; renderTradetron(); renderJourney(); }
  });

  window.addEventListener("deployment-link", (event) => {
    const deployment = state.deployments.find((item) => item.id === event.detail?.id);
    if (deployment) openTradetronForm(deployment);
  });
  window.addEventListener("deployment-disconnect", async (event) => {
    const deployment = state.deployments.find((item) => item.id === event.detail?.id);
    if (!deployment || tradetron.busy) return;
    if (!window.confirm(`Remove the protected Tradetron link from ${deployment.strategy_name || "this paper deployment"}? No signal will be sent.`)) return;
    tradetron.busy = true;
    try {
      const response = await api(`/api/deployments/${encodeURIComponent(deployment.id)}/tradetron/disconnect`, {method:"POST", body:"{}"});
      if (response.deployment?.executor?.linked !== false) throw new Error("The app did not confirm the deployment link was removed.");
      await loadState();
      announce("Paper deployment executor link removed. Automatic sending remains off.");
    } catch (error) { showError("page-error", error.message); }
    finally { tradetron.busy = false; renderTradetron(); renderJourney(); }
  });
  window.addEventListener("deployment-migrate-legacy", async (event) => {
    const deployment = state.deployments.find((item) => item.id === event.detail?.id);
    if (!deployment || tradetron.busy) return;
    if (!window.confirm(`Move the old protected Tradetron link into ${deployment.strategy_name || "this paper deployment"}? The old app-wide copy will be removed. No signal will be sent.`)) return;
    tradetron.busy = true;
    try {
      const response = await api(`/api/deployments/${encodeURIComponent(deployment.id)}/tradetron/migrate-legacy`, {method:"POST", body:JSON.stringify({confirmation:"move-legacy-paper-link"})});
      if (response.deployment?.executor?.linked !== true) throw new Error("The app did not confirm the moved deployment link.");
      tradetron.busy = false;
      await refreshTradetronStatus();
      await loadState();
      announce("Legacy link moved into this paper deployment. Automatic sending remains off.");
    } catch (error) { showError("page-error", error.message); }
    finally { tradetron.busy = false; renderTradetron(); renderJourney(); }
  });

  function clearKiteInputs() { $("kite-api-key").value = ""; $("kite-api-secret").value = ""; }

  function openKiteForm() {
    if (kite.busy) return;
    clearKiteInputs();
    $("kite-remember").checked = kite.status?.credentials_saved === true;
    setKiteFormBusy(false);
    showError("kite-form-error", "");
    $("kite-dialog").showModal();
    $("kite-api-key").focus();
  }

  function closeKiteForm() {
    clearKiteInputs();
    if (!kite.busy) $("kite-dialog").close();
  }

  function setKiteFormBusy(busy) {
    for (const id of ["kite-save-config", "kite-close-dialog", "kite-cancel-dialog", "kite-api-key", "kite-api-secret"]) $(id).disabled = busy;
    $("kite-remember").disabled = busy || kite.status?.credential_storage_available !== true;
    $("kite-remember-note").textContent = kite.status?.credential_storage_available === true ? "Optional. Saves the app key and secret, never your password, one-time code or login token. Unchecking it removes previously saved details." : "Remember is unavailable in this app session. You can still use the details in memory only.";
    $("kite-save-config").textContent = busy ? "Saving app details…" : "Continue";
  }

  $("kite-configure").addEventListener("click", openKiteForm);
  $("kite-close-dialog").addEventListener("click", closeKiteForm);
  $("kite-cancel-dialog").addEventListener("click", closeKiteForm);
  $("kite-dialog").addEventListener("cancel", (event) => { clearKiteInputs(); if (kite.busy) event.preventDefault(); });
  $("kite-dialog").addEventListener("close", clearKiteInputs);
  window.addEventListener("pagehide", clearKiteInputs);
  $("kite-refresh").addEventListener("click", refreshKiteStatus);

  $("kite-config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (kite.busy || !$("kite-config-form").reportValidity()) return;
    let payload = { api_key: $("kite-api-key").value.trim(), api_secret: $("kite-api-secret").value.trim(), remember: $("kite-remember").checked && !$("kite-remember").disabled };
    clearKiteInputs();
    if (!payload.api_key || !payload.api_secret) { payload = null; showError("kite-form-error", "Enter both app credentials. The fields have been cleared; please enter them again."); return; }
    kite.busy = true;
    setKiteFormBusy(true);
    renderKite();
    showError("kite-form-error", "");
    try {
      const pending = api("/api/kite/config", { method: "POST", body: JSON.stringify(payload) });
      payload = null;
      const response = await pending;
      if (!response.status || response.status.configured !== true) throw new Error("Configuration not confirmed");
      kite.status = response.status;
      kite.instruments = null;
      kite.quote = null;
      $("kite-dialog").close();
      showError("kite-error", "");
      announce(response.status.credentials_saved ? "App details saved with Windows account protection. Sign in at Zerodha to request market data." : "App details loaded for this session only. Sign in at Zerodha to request market data.");
    } catch (error) {
      showError("kite-form-error", `${error.message} Your credential fields have been cleared; please re-enter them to retry.`);
    } finally {
      payload = null;
      clearKiteInputs();
      kite.busy = false;
      setKiteFormBusy(false);
      renderKite();
    }
  });

  $("kite-login").addEventListener("click", async () => {
    if (kite.busy || !kite.status?.configured) return;
    kite.busy = true;
    showError("kite-error", "");
    renderKite();
    try {
      const response = await api("/api/kite/login-url");
      const target = new URL(response.login_url);
      if (target.origin !== "https://kite.zerodha.com" || !["/connect/login", "/connect/login/"].includes(target.pathname) || target.username || target.password) throw new Error("The app did not return the expected Zerodha sign-in address. Navigation was blocked.");
      clearKiteInputs();
      window.location.assign(target.href);
    } catch (error) { showError("kite-error", error.message); kite.busy = false; renderKite(); }
  });

  $("kite-forget").addEventListener("click", async () => {
    if (kite.busy || (!kite.status?.configured && !kite.status?.credential_storage_warning)) return;
    kite.busy = true;
    clearKiteInputs();
    showError("kite-error", "");
    renderKite();
    try {
      const response = await api("/api/kite/disconnect", { method: "POST", body: "{}" });
      if (!response.status || response.status.configured !== false || response.status.connected !== false) throw new Error("The local app did not confirm that connection details were forgotten. Refresh status before continuing.");
      kite.status = response.status;
      kite.instruments = null;
      kite.quote = null;
      announce("Connection details cleared from this app's memory and any saved copy removed. The Zerodha session itself has not been revoked.");
    } catch (error) { kite.instruments = null; kite.quote = null; showError("kite-error", error.message); await refreshAfterDataFailure(); }
    finally { kite.busy = false; clearKiteInputs(); renderKite(); }
  });

  async function refreshAfterDataFailure() {
    try { const response = await api("/api/kite/status"); kite.status = response.status || null; }
    catch { kite.status = null; }
  }

  $("kite-sync-instruments").addEventListener("click", async () => {
    if (kite.busy || !kiteConnected()) return;
    kite.busy = true;
    showError("kite-error", "");
    renderKite();
    $("kite-sync-instruments").textContent = "Downloading…";
    try {
      const response = await api("/api/kite/instruments/sync", { method: "POST", body: "{}" });
      if (!response.status || response.status.available !== true) throw new Error("The app did not confirm a downloaded instrument list.");
      kite.instruments = response.status;
      announce("Instrument list downloaded. No orders placed and no strategy started.");
    } catch (error) { showError("kite-error", error.message); await refreshAfterDataFailure(); }
    finally { kite.busy = false; renderKite(); }
  });

  $("kite-fetch-spot").addEventListener("click", async () => {
    if (kite.busy || !kiteConnected()) return;
    kite.busy = true;
    showError("kite-error", "");
    renderKite();
    $("kite-fetch-spot").textContent = "Fetching…";
    try {
      const response = await api("/api/kite/spot");
      const quote = response.quote;
      if (!quote || !Number.isFinite(quote.last_price) || quote.last_price <= 0 || !quote.timestamp || Number.isNaN(Date.parse(quote.timestamp))) throw new Error("A valid price with a quote time was not returned. No new price is shown.");
      kite.quote = quote;
      announce("One BANK NIFTY spot quote loaded from Zerodha. Offline checks still use made-up test data.");
    } catch (error) { kite.quote = null; showError("kite-error", error.message); await refreshAfterDataFailure(); }
    finally { kite.busy = false; renderKite(); }
  });

  function replaySettingsKey() {
    return JSON.stringify(["replay-date", "strategy-mode", "model-sell-lots", "model-ce-lots", "model-pe-lots", "model-units", "model-return-base"].map((id) => $(id).value));
  }

  function updateReplaySettingsNotice() {
    $("replay-settings-changed").hidden = !replay.settingsKey || replay.settingsKey === replaySettingsKey();
    renderRunBrief();
  }

  $("replay-date").addEventListener("change", updateReplaySettingsNotice);
  for (const id of ["strategy-mode", "model-sell-lots", "model-ce-lots", "model-pe-lots", "model-units", "model-return-base"]) {
    $(id).addEventListener("input", updateReplaySettingsNotice);
    $(id).addEventListener("change", updateReplaySettingsNotice);
  }
  // Expand invalid advanced inputs so the browser can focus and explain them.
  $("replay-form").addEventListener("invalid", (event) => {
    const details = event.target.closest("details");
    if (details) details.open = true;
  }, true);

  $("replay-cancel").addEventListener("click", () => {
    replay.cancelRequested = true;
    renderReplayAvailability();
  });

  async function watchReplayJob(initialJob, requestVersion) {
    let job = initialJob;
    while (true) {
      if (!job || typeof job.id !== "string" || !["running", "complete", "failed", "cancelled"].includes(job.status)) throw new Error("The app returned an invalid replay status.");
      replay.jobId = job.id;
      replay.progress = job.progress;
      if (job.cancel_requested) replay.cancelRequested = true;
      renderReplayAvailability();
      if (job.status !== "running") {
        if (requestVersion !== replay.version) return;
        if (job.status === "failed") throw new Error(job.error || "The replay could not finish.");
        if (job.status === "cancelled") { $("replay-empty").hidden = false; announce("Replay stopped."); return; }
        const response = job.result;
        if (!validSpotRange(response?.range, job.start_date)) throw new Error("The app returned an incomplete date-range reply. No spot result is shown.");
        renderSpotRangeResult(response.range, response.spot_proxy, response.option_model);
        lastModelResult = `${replayDay(response.range.start_date)} – ${replayDay(response.range.end_date)} · ${response.option_model?.validation_status || response.spot_proxy?.validation_status || "See data-quality notes"}. Shared model, not a draft version.`;
        lastPriceCoverage = {summary: response.option_model?.pricing_summary, end: response.range.end_date};
        renderJourney();
        restoreBacktestControls(response);
        await loadBacktestHistory();
        await refreshPaper();
        showError("paper-replay-error", job.paper_replay_warning || "");
        if (job.history_warning) showError("replay-error", job.history_warning);
        announce(`Backtest ready: ${response.range.total_signals} signals found.${response.spot_proxy?.status === "complete" ? "" : " The spot position result is incomplete."}${response.spot_proxy?.skipped_minute_count ? " Missing minutes were skipped; results are approximate." : ""} ${response.option_model ? "See Option model and Hedge ledgers for price sources, premiums and charges; these are not actual fills." : "This saved result contains spot points only. Replay again for the option model."}`);
        return;
      }
      if ((replay.cancelRequested || requestVersion !== replay.version) && !replay.cancelSent) {
        replay.cancelSent = true;
        job = (await api(`/api/replay/spot-range/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: "{}" })).job;
        continue;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      job = (await api(`/api/replay/spot-range/jobs/${encodeURIComponent(job.id)}`)).job;
    }
  }

  async function restoreReplayJob() {
    const requestVersion = replay.version;
    try {
      const response = await api("/api/replay/spot-range/jobs/current");
      if (!response.job || replay.busy || requestVersion !== replay.version || response.job.status === "cancelled") return;
      $("replay-date").value = response.job.start_date;
      replay.busy = true;
      $("replay-empty").hidden = true;
      try { await watchReplayJob(response.job, requestVersion); }
      finally { replay.busy = false; replay.jobId = null; replay.cancelRequested = false; replay.cancelSent = false; replay.progress = null; renderReplayAvailability(); }
    } catch (error) { showError("replay-error", error.message); }
  }

  $("replay-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (replay.busy || !kiteConnected()) return;
    syncReplayDate();
    if (!$("replay-form").reportValidity()) return;
    const startDate = $("replay-date").value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(startDate)) { showError("replay-error", "Choose a valid start date."); return; }
    clearSpotRangeResult();
    const requestVersion = replay.version;
    replay.busy = true;
    replay.cancelRequested = false;
    replay.cancelSent = false;
    replay.progress = null;
    $("replay-empty").hidden = true;
    renderReplayAvailability();
    announce("Replaying past BANK NIFTY spot prices. No order will be placed.");
    try {
      const returnBase = parseCapital($("model-return-base").value);
      if (!Number.isSafeInteger(returnBase) || returnBase <= 0 || returnBase > 1000000000) {
        showError("replay-error", "Enter a whole-number return base from ₹1 to ₹1,00,00,00,000.");
        $("model-return-base").focus();
        return;
      }
      const quantity = { sell_lots: Number($("model-sell-lots").value), ce_hedge_lots: Number($("model-ce-lots").value), pe_hedge_lots: Number($("model-pe-lots").value), units_per_lot: Number($("model-units").value) };
      const response = await api("/api/replay/spot-range/jobs", { method: "POST", body: JSON.stringify({ start_date: startDate, model_config: quantity, strategy_mode: $("strategy-mode").value, return_base: returnBase }) });
      await watchReplayJob(response.job, requestVersion);
    } catch (error) {
      if (requestVersion !== replay.version) return;
      showError("replay-error", error.message);
      await refreshAfterDataFailure();
      renderKite();
    } finally { replay.busy = false; replay.jobId = null; replay.cancelRequested = false; replay.cancelSent = false; replay.progress = null; renderReplayAvailability(); }
  });

  $("paper-replay-refresh").addEventListener("click", async () => {
    if(replay.busy || !kiteConnected())return;
    clearSpotRangeResult();
    const requestVersion=replay.version;
    replay.busy=true;replay.cancelRequested=false;replay.cancelSent=false;replay.progress=null;
    showError("paper-replay-error", "");
    $("paper-replay-refresh").disabled=true;
    $("replay-date").value="2026-09-01";
    window.location.hash="#past-data-check";
    renderReplayAvailability();
    announce("Updating the September historical positions. No paper or real orders are submitted.");
    try {
      const response=await api("/api/paper/replay/refresh",{method:"POST",body:"{}"});
      await watchReplayJob(response.job,requestVersion);
      if(requestVersion === replay.version && !replay.cancelRequested)window.location.hash="#paper-trading";
    } catch(error) {
      showError("paper-replay-error",error.message);showError("replay-error",error.message);
      await refreshAfterDataFailure();
    } finally {
      replay.busy=false;replay.jobId=null;replay.cancelRequested=false;replay.cancelSent=false;replay.progress=null;
      renderReplayAvailability();await refreshPaper();
    }
  });

  function openForm(strategy = null) {
    if (saving) return;
    editingId = strategy?.id || null;
    $("dialog-title").textContent = strategy ? "Edit draft details" : "Create draft from BANKNIFTY template";
    $("save-strategy").textContent = strategy ? "Save changes" : "Save draft";
    $("strategy-name").value = strategy?.name || "";
    $("strategy-lots").value = strategy?.lots ?? 1;
    $("strategy-multiplier").value = strategy?.multiplier ?? 1;
    const multiplierSupported = state.strategies.some((item) => Number.isSafeInteger(item.effective_lots));
    $("strategy-multiplier").disabled = !multiplierSupported;
    $("strategy-multiplier-note").hidden = multiplierSupported;
    $("strategy-multiplier-note").textContent = multiplierSupported ? "" : "This active app session preserves the saved multiplier. Restart the local app before changing it.";
    $("strategy-capital").value = new Intl.NumberFormat("en-IN").format(strategy?.capital ?? 1000000);
    updateDraftQuantity();
    showError("form-error", "");
    $("strategy-dialog").showModal();
    $("strategy-name").focus();
  }

  function closeForm() {
    if (!saving) $("strategy-dialog").close();
  }

  $("create-strategy").addEventListener("click", () => openForm());
  $("create-first-strategy").addEventListener("click", () => openForm());
  $("edit-strategy").addEventListener("click", () => { if (selectedStrategy()) openForm(selectedStrategy()); });
  $("deploy-strategy").addEventListener("click", async () => {
    const strategy = selectedStrategy();
    if (!strategy || saving) return;
    saving = true; updateControls(); showError("deployment-create-error", "");
    try {
      const response = await api("/api/deployments", {method: "POST", body: JSON.stringify({strategy_id: strategy.id})});
      if (!response.deployment?.id || response.deployment.mode !== "paper_only") throw new Error("The app did not confirm the paper deployment record.");
      await loadState();
      announce("Paper deployment record created. It is not linked, running, or able to send a Tradetron signal.");
      window.location.hash = "#deployments";
    } catch (error) { showError("deployment-create-error", error.message); }
    finally { saving = false; updateControls(); }
  });
  $("close-dialog").addEventListener("click", closeForm);
  $("cancel-dialog").addEventListener("click", closeForm);
  $("strategy-dialog").addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  $("scenario-select").addEventListener("change", () => { clearResult(); updateScenarioDescription(); updateControls(); });
  $("check-strategy-select").addEventListener("change", () => selectStrategy($("check-strategy-select").value));

  function parseCapital(raw) {
    return /^[\d,\s]+$/.test(raw) ? Number(raw.replace(/[,\s]/g, "")) : NaN;
  }

  function draftQuantityNote(lots, multiplier, instruments) {
    const sizes = instruments?.available ? instruments.lot_sizes || [] : [];
    if (!Number.isSafeInteger(Number(lots)) || Number(lots) < 1 || !Number.isSafeInteger(Number(multiplier)) || Number(multiplier) < 1) return "Enter whole numbers from 1 to 100 for base lots and multiplier.";
    const effective = Number(lots) * Number(multiplier), size = `${lots} base ${Number(lots) === 1 ? "lot" : "lots"} × ${multiplier} multiplier = ${effective} effective ${effective === 1 ? "lot" : "lots"}`;
    return sizes.length === 1 ? `${size} × ${sizes[0]} listed units = ${effective * sizes[0]} units for each short or hedge. Today's listed size, not a historical assumption.`
      : `${size} for each short or hedge. Units cannot be confirmed until a current contract lot size is available.`;
  }

  function updateDraftQuantity() {
    $("strategy-quantity-note").textContent = draftQuantityNote($("strategy-lots").value, $("strategy-multiplier").value, kite.instruments);
  }

  function renderRunBrief() {
    const start = $("replay-date").value, end = $("replay-date").max;
    $("run-date-coverage").textContent = start && end ? `${start} to ${end} (IST). Requested range; actual usable coverage is checked during the run.` : "Choose a start date. Coverage is checked during the run.";
    const values = ["model-sell-lots", "model-ce-lots", "model-pe-lots", "model-units"].map(id => Number($(id).value));
    $("run-quantity-summary").textContent = values.every(Number.isSafeInteger) && values[0] > 0 && values[3] > 0 && values[1] >= 0 && values[2] >= 0
      ? `${values[3]} assumed units per lot · Shorts ${values[0]} lots (${values[0] * values[3]} units) per direction · CE hedge ${values[1]} lots (${values[1] * values[3]} units) · PE hedge ${values[2]} lots (${values[2] * values[3]} units). Not historical lot sizes or draft quantities.`
      : "Enter valid whole-number quantities in Settings & assumptions.";
    const returnBase = parseCapital($("model-return-base").value);
    $("run-return-base").textContent = Number.isSafeInteger(returnBase) && returnBase > 0
      ? new Intl.NumberFormat("en-IN", {style:"currency", currency:"INR", maximumFractionDigits:0}).format(returnBase) + " for performance percentages. Planning only, not a margin check."
      : "Enter a return base to calculate performance percentages.";
    const coverage = lastPriceCoverage?.summary;
    $("run-price-coverage").textContent = coverage && replay.settingsKey === replaySettingsKey() && end === lastPriceCoverage.end
      ? `Last matching result: ${coverage.recorded?.positions || 0} recorded-price legs, ${coverage.mixed?.positions || 0} mixed-price legs, ${coverage.estimated?.positions || 0} estimated-price legs. Not complete minute-by-minute coverage or actual fills.`
      : "Not checked for these settings and dates. Each result shows recorded, mixed and estimated prices.";
  }

  // One readiness calculation feeds Research, Connections and the Paper order lab.
  function workspaceStatus(status, instruments, data, now = Date.now()) {
    const configured = status?.configured === true;
    const expiry = Date.parse(status?.session_expires_at || "");
    const connected = status?.connected === true && !(Number.isFinite(expiry) && expiry <= now);
    const contracts = instruments?.available === true;
    const stream = data?.stream || {}, ledger = data?.paper || {};
    const usable = (stream.quotes || []).filter(q => [q.timestamp, q.received_at].every(stamp => {
      const age = now - Date.parse(stamp); return Number.isFinite(age) && age >= -1000 && age <= 5000;
    }));
    const fresh = connected && stream.state === "connected" && stream.fresh_count > 0 && usable.length > 0;
    const hasSpot = usable.some(q => q.symbol === "NSE:NIFTY BANK");
    const hasOptions = usable.some(q => q.symbol?.startsWith("NFO:BANKNIFTY"));
    const open = data?.calendar?.open_now === true;
    const retained = ledger.positions?.some(p => p.quantity !== 0) || false;
    const streaming = ["connecting", "connected", "reconnecting"].includes(stream.state);
    let next = "refresh", title = "Check connection status", note = "The local app's connection status is not available.", action = "Refresh status";
    if (status && !configured) { next = "configure"; title = "Add your Kite app details"; note = "Keep your app key and secret in the local form, never in chat."; action = "Add app details"; }
    else if (configured && !connected) { next = "login"; title = "Sign in at Zerodha"; note = "App details are loaded. A current sign-in is needed to request prices."; action = "Sign in at Zerodha"; }
    else if (connected && !contracts) { next = "contracts"; title = "Load the current contracts"; note = "This checks available BANKNIFTY options, expiry dates and lot sizes."; action = "Load contracts"; }
    else if (connected && !data) { title = "Check price-service status"; note = "Prices and the exchange calendar have not been confirmed."; }
    else if (connected && !open) { next = "closed"; title = data.calendar.verified_today ? "Market closed for paper tests" : "Trading session not verified"; note = "Historical research is still available. Price streaming needs a verified open session."; action = "Refresh status"; }
    else if (connected && fresh) { next = "paper"; title = "Prices are arriving"; note = "Broker sign-in and fresh quotes are separate checks. The paper test checks the selected contracts again before filling."; action = "Open paper order lab"; }
    else if (connected && streaming) { next = "waiting"; title = "Waiting for fresh prices"; note = stream.message || "The connection is active, but usable quotes are not yet confirmed."; action = "Waiting for prices…"; }
    else if (connected) { next = "start"; title = "Start live prices"; note = "Read-only BANKNIFTY spot and option prices. No orders are placed."; action = "Start prices"; }
    let blocker = !connected ? "Sign in at Zerodha in Connections before testing." : !data ? "Paper-service status is unavailable. Refresh status before continuing."
      : !open ? data.calendar.verified_today ? "The market is closed. Return during a verified trading session." : "This trading session is unverified; paper tests are blocked. Review the calendar in Connections."
      : !fresh ? stream.state === "error" ? `Price connection failed: ${stream.message}` : "Start prices and wait for fresh quotes in Connections."
      : !hasSpot || !hasOptions ? "Waiting for both BANKNIFTY spot and option quotes. Check live prices in Connections." : "";
    let paperState = "Not started";
    if (data?.busy) paperState = "Running";
    else if (ledger.paused) paperState = "Paused";
    else if (retained || blocker) paperState = "Blocked";
    else if (data?.checks?.[0]?.status === "passed") paperState = "Completed";
    if (retained) blocker = "Open paper positions remain. Review the ledger and close the remaining positions before another test.";
    if (ledger.paused) blocker = retained ? blocker : "Paper orders are paused. Check the saved ledger before resuming.";
    if (data?.busy) blocker = "The order-sequence test is running. Each simulated fill is being recorded.";
    return {configured, connected, contracts, fresh, open, retained, streaming, next, title, note, action, blocker, paperState,
      canStart: connected && contracts && open && !!data && !streaming && !data.busy,
      canTest: connected && fresh && hasSpot && hasOptions && open && !retained && !ledger.paused && !data?.busy,
      canPause: !ledger.paused && (data?.busy || retained),
      canClose: connected && fresh && open && retained && !data?.busy};
  }

  function renderJourney() {
    const s = workspaceStatus(kite.status, kite.instruments, paperSnapshot);
    if (typeof window !== "undefined") window.TradingDesk?.render({state, kite: kite.status, paper: paperSnapshot, tradetron: tradetron.status});
    $("journey-broker").textContent = s.connected ? "Signed in" : s.configured ? "Sign-in needed" : kite.status ? "Not set up" : "Status unavailable";
    $("journey-prices").textContent = s.fresh ? "Fresh quotes arriving" : s.streaming ? "No fresh quotes confirmed" : "No live prices";
    $("journey-session").textContent = !paperSnapshot ? "Not checked" : s.open ? "Open · verified" : paperSnapshot.calendar.verified_today ? "Closed" : "Unverified · blocked";
    const completed = [s.configured, s.connected, s.connected && s.contracts, s.fresh];
    ["setup-details", "setup-login", "setup-contracts", "setup-prices"].forEach((id, i) => {
      $(id).className = completed[i] ? "step-complete" : completed.slice(0,i).every(Boolean) ? "step-current" : "";
      if (!completed[i] && completed.slice(0,i).every(Boolean)) $(id).setAttribute("aria-current", "step"); else $(id).removeAttribute("aria-current");
    });
    $("setup-next-title").textContent = s.title; $("setup-next-note").textContent = s.note;
    $("setup-next-action").textContent = s.action;
    $("setup-next-action").disabled = kite.busy || paperBusy || s.next === "waiting";
    // Completed/setup maintenance actions stay secondary; only the next step is prominent.
    $("kite-configure").hidden = !s.configured;
    $("kite-login").hidden = !s.connected;
    $("kite-sync-instruments").hidden = !s.contracts;
    $("stream-start").hidden = !s.streaming;
    $("stream-start").disabled = paperBusy || kite.busy || !s.canStart;
    $("stream-stop").hidden = !s.streaming;
    $("stream-blocker").textContent = s.fresh ? "Fresh quotes are price observations, not guaranteed fills." : !s.connected ? "Complete the connection steps above before starting prices." : s.note;
    $("paper-state").textContent = s.paperState;
    $("paper-state").className = "state-label " + (s.paperState === "Blocked" || s.paperState === "Paused" ? "warning" : s.paperState === "Completed" ? "success" : "");
    $("paper-check").disabled = paperBusy || !s.canTest;
    $("paper-summary").textContent = s.blocker || "Ready to check available quotes for a one-lot order test. This does not deploy a strategy.";
    $("paper-pause").disabled = !s.canPause;
    $("paper-pause").hidden = !s.canPause;
    $("paper-resume").hidden = !paperSnapshot?.paper.paused;
    $("paper-resume").disabled = paperBusy || paperSnapshot?.busy || s.retained;
    $("paper-close-retained").disabled = true;
    $("paper-close-note").hidden = !s.retained;
    $("paper-remedy").hidden = !s.blocker || s.retained || paperSnapshot?.paper.paused || paperSnapshot?.busy;
    $("paper-remedy").textContent = !s.connected ? "Complete connection setup →" : !s.open ? "Review market session →" : "Check live prices →";
    $("research-backtest-action").setAttribute("href", "#past-data-check");
    if (typeof updateResearchSummary === "function") updateResearchSummary();
  }

  $("strategy-lots").addEventListener("input", updateDraftQuantity);
  $("strategy-multiplier").addEventListener("input", updateDraftQuantity);
  $("strategy-capital").addEventListener("input", () => {
    const input = $("strategy-capital"), raw = input.value, caret = input.selectionStart;
    if (!raw || !/^[\d,]+$/.test(raw)) return;
    const digitsBefore = raw.slice(0, caret).replace(/,/g, "").length;
    const value = parseCapital(raw);
    if (!Number.isSafeInteger(value)) return;
    input.value = new Intl.NumberFormat("en-IN").format(value);
    let count = 0, position = 0;
    while (position < input.value.length && count < digitsBefore) { if (/\d/.test(input.value[position])) count++; position++; }
    input.setSelectionRange(position, position);
  });
  $("setup-next-action").addEventListener("click", () => {
    const next = workspaceStatus(kite.status, kite.instruments, paperSnapshot).next;
    if (kite.busy || paperBusy) return;
    if (next === "paper") { window.history.pushState(null, "", window.location.pathname + window.location.search + "#paper-trading"); updateNavigation({focus:true}); }
    else if (next === "configure") openKiteForm();
    else if (next === "login") $("kite-login").click();
    else if (next === "contracts") $("kite-sync-instruments").click();
    else if (next === "start") paperAction("/api/paper/stream/start", "stream-error");
    else if (next !== "waiting") { refreshKiteStatus(); refreshPaper(); }
  });

  const workspaceRoutes = {
    "#main": { view: "overview", title: "Trading desk", description: "Protection, exposure and anything that needs your attention." },
    "#strategies-heading": { view: "strategies", title: "Strategies", description: "Saved BANKNIFTY templates. Drafts are not deployments." },
    "#strategy-detail": { view: "strategies", title: "Strategy draft", nav: "#strategies-heading", description: "Fixed rules and saved sizing for this draft." },
    "#deployments": { view: "deployments", title: "Deployments", description: "One block per recorded deployment." },
    "#positions-orders": { view: "positions", title: "Positions & orders", description: "Executed quantities, protection and outstanding orders. Modes never share records." },
    "#research": { view: "research", title: "Research", description: "Historical simulations and artificial checks, separate from trading." },
    "#activity": { view: "activity", title: "Activity", description: "Order events for the selected mode. Research signals remain in their reports." },
    "#past-data-check": { view: "backtests", title: "Model simulator", nav: "#research", description: "Historical research with independent settings—not a saved strategy test." },
    "#kite-connection": { view: "market-data", title: "Connections", description: "Connect prices, not trading. Live execution is unavailable." },
    "#safety-checks": { view: "safety-checks", title: "Order checks", nav: "#research", description: "Artificial scenarios test order behaviour—not market performance." },
    "#paper-trading": { view: "paper-trading", title: "Paper trading", nav: "#positions-orders", description: "September strategy replay and separate real-quote order tests. No automatic trading." },
  };
  let activeWorkspaceHash = null;

  function updateNavigation({ focus = false } = {}) {
    if (window.location.hash === "#page-title") { $("page-title").focus(); return; }
    const activeHash = workspaceRoutes[window.location.hash] ? window.location.hash : "#main";
    const route = workspaceRoutes[activeHash];
    if (activeHash === "#paper-trading") window.TradingDesk?.setMode("paper");
    if (window.location.hash !== activeHash) window.history.replaceState(null, "", window.location.pathname + window.location.search + activeHash);
    for (const view of document.querySelectorAll("[data-view]")) {
      view.hidden = view.dataset.view !== route.view;
      view.inert = view.hidden;
    }
    for (const link of document.querySelectorAll(".nav-item")) {
      const active = link.getAttribute("href") === (route.nav || activeHash);
      link.classList.toggle("active", active);
      if (active) { link.setAttribute("aria-current", "page"); link.scrollIntoView?.({ block: "nearest", inline: "nearest" }); }
      else link.removeAttribute("aria-current");
    }
    $("page-title").textContent = route.title;
    $("page-location").textContent = route.title;
    $("page-description").textContent = route.description;
    $("strategy-workspace").dataset.detail = String(activeHash === "#strategy-detail");
    $("mobile-menu").textContent = "Menu · " + route.title;
    $("mobile-menu").setAttribute("aria-expanded", "false");
    $("workspace-nav").dataset.open = "false";
    $("create-strategy").hidden = route.view !== "strategies";
    document.title = route.title + " · Private Algo Desk";
    if (focus) $("page-title").focus({ preventScroll: true });
    if (focus || activeWorkspaceHash !== activeHash) window.scrollTo({ top: 0, behavior: "instant" });
    activeWorkspaceHash = activeHash;
  }
  // Keep native links (including opening in a new tab) and browser history working.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const hash = link.getAttribute("href");
    if (hash === "#page-title") { event.preventDefault(); $("page-title").focus(); return; }
    if (!workspaceRoutes[hash]) return;
    event.preventDefault();
    if (window.location.hash !== hash) window.history.pushState(null, "", window.location.pathname + window.location.search + hash);
    updateNavigation({ focus: true });
  });
  window.addEventListener("hashchange", () => updateNavigation({ focus: true }));
  window.addEventListener("popstate", () => updateNavigation({ focus: true }));
  // Native fragment restoration runs after a deferred script on a fresh page.
  window.history.scrollRestoration = "manual";
  window.addEventListener("load", () => window.scrollTo({ top: 0, behavior: "instant" }));
  updateNavigation();
  $("mobile-menu").addEventListener("click", () => {
    const opened = $("mobile-menu").getAttribute("aria-expanded") !== "true";
    $("mobile-menu").setAttribute("aria-expanded", String(opened));
    $("workspace-nav").dataset.open = String(opened);
  });
  $("workspace-nav").addEventListener("keydown", event => {
    if (event.key === "Escape") { $("workspace-nav").dataset.open = "false"; $("mobile-menu").setAttribute("aria-expanded", "false"); $("mobile-menu").focus(); }
  });
  $("mobile-menu").addEventListener("keydown", event => {
    if (event.key === "Escape") { $("workspace-nav").dataset.open = "false"; $("mobile-menu").setAttribute("aria-expanded", "false"); }
  });

  $("strategy-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || !$("strategy-form").reportValidity()) return;
    const payload = { name: $("strategy-name").value.trim(), lots: Number($("strategy-lots").value), capital: parseCapital($("strategy-capital").value) };
    // The already-running local app may predate multiplier support. Until its
    // next restart, leave this optional field out rather than rejecting saves.
    if (state.strategies.some((strategy) => Number.isSafeInteger(strategy.effective_lots))) payload.multiplier = Number($("strategy-multiplier").value);
    if (!payload.name) { showError("form-error", "Please enter a strategy name."); $("strategy-name").focus(); return; }
    if (!Number.isSafeInteger(payload.lots) || payload.lots < 1 || payload.lots > 100) { showError("form-error", "Use a whole number from 1 to 100 for base lots."); return; }
    if ("multiplier" in payload && (!Number.isSafeInteger(payload.multiplier) || payload.multiplier < 1 || payload.multiplier > 100)) { showError("form-error", "Use a whole number from 1 to 100 for the multiplier."); return; }
    if (!Number.isSafeInteger(payload.capital) || payload.capital <= 0 || payload.capital > 1000000000) { showError("form-error", "Enter planned capital in whole rupees, from ₹1 to ₹1,00,00,00,000."); return; }
    saving = true;
    showError("form-error", "");
    $("save-strategy").disabled = true;
    $("close-dialog").disabled = true;
    $("cancel-dialog").disabled = true;
    $("save-strategy").textContent = "Saving…";
    updateControls();
    try {
      const response = await api(editingId ? `/api/strategies/${encodeURIComponent(editingId)}` : "/api/strategies", { method: editingId ? "PATCH" : "POST", body: JSON.stringify(payload) });
      if (!response.strategy?.id) throw new Error("The app did not confirm a saved strategy. Your form entries have been kept.");
      const index = state.strategies.findIndex((item) => item.id === response.strategy.id);
      if (index >= 0) state.strategies[index] = response.strategy;
      else state.strategies.push(response.strategy);
      selectedId = response.strategy.id;
      renderCards();
      renderDetail();
      renderRecentRuns();
      clearResult();
      $("strategy-dialog").close();
      if (window.location.hash !== "#strategy-detail") {
        window.history.pushState(null, "", window.location.pathname + window.location.search + "#strategy-detail");
        updateNavigation({ focus: true });
      }
      announce(`${response.strategy.name} saved as a local draft. No orders placed.`);
    } catch (error) { showError("form-error", error.message); }
    finally {
      saving = false;
      $("save-strategy").disabled = false;
      $("close-dialog").disabled = false;
      $("cancel-dialog").disabled = false;
      $("save-strategy").textContent = editingId ? "Save changes" : "Save draft";
      updateControls();
    }
  });

  $("scenario-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const strategy = selectedStrategy();
    const scenarioId = $("scenario-select").value;
    if (running || !strategy || !scenarioId) return;
    running = true;
    clearResult();
    updateControls();
    $("check-empty").hidden = true;
    announce("Running an offline rule check. No broker orders are involved.");
    try {
      const response = await api("/api/scenarios/run", { method: "POST", body: JSON.stringify({ strategy_id: strategy.id, scenario_id: scenarioId }) });
      if (!response.run?.id) throw new Error("The app did not return a completed check. Please try again.");
      renderRun(response.run);
      state.recent_runs = [response.run, ...state.recent_runs.filter((run) => run.id !== response.run.id)];
      renderRecentRuns();
      announce(`Offline check ${response.run.result === "passed" ? "passed" : "failed"}. This is not a market test.`);
    } catch (error) { showError("check-error", error.message); $("check-empty").hidden = false; }
    finally { running = false; updateControls(); }
  });

  function renderPaper(data) {
    paperSnapshot = data;
    $("paper-replay-refresh").disabled = replay.busy || !kiteConnected();
    $("paper-replay-note").textContent = replay.busy ? "A historical replay is running. Progress and cancellation are under Research." : !kiteConnected() ? "Sign in in Connections, then update September positions. No orders are placed." : `Update from 1 September through ${data.historical_replay?.latest_completed_date || "the latest completed session"}. Uses the displayed shared model settings, not a saved draft. No orders are placed.`;
    const stream = data.stream, calendar = data.calendar, ledger = data.paper;
    const fresh = workspaceStatus(kite.status, kite.instruments, data).fresh;
    const spot = stream.quotes?.find(q => q.symbol === "NSE:NIFTY BANK");
    $("stream-spot").textContent = spot ? Number(spot.last_price).toLocaleString("en-IN", {minimumFractionDigits:2,maximumFractionDigits:2}) : "Waiting for BANK NIFTY";
    $("stream-spot-time").textContent = spot ? `Exchange time: ${marketTime(spot.timestamp)} · streamed spot price` : "No fresh streamed spot price. Old values are not carried forward.";
    $("stream-badge").textContent = fresh ? "RECEIVING" : stream.state.toUpperCase();
    $("stream-summary").textContent = fresh ? `${stream.fresh_count} fresh prices · monthly expiry ${data.expiry}. Read-only connection.` : stream.message;
    $("stream-start").disabled = paperBusy || data.busy || ["connecting", "connected", "reconnecting"].includes(stream.state);
    $("stream-stop").disabled = stream.state === "stopped";
    $("stream-details").textContent = `${stream.accepted_ticks} accepted updates · ${stream.rejected_packets} blocked packets · ${stream.retries}/3 reconnect retries. Last update: ${stream.last_packet ? marketTime(stream.last_packet) : "none"}.`;
    if (stream.archive_error) $("stream-details").textContent += " " + stream.archive_error;
    $("calendar-summary").textContent = `${calendar.message} Reviewed ${calendar.reviewed_at}; coverage: ${calendar.covered_year}.`;
    $("calendar-limits").textContent = Object.entries(calendar.pending_dates).map(([day, message]) => `${day}: ${message}`).join(" ") + " " + calendar.limitation;
    $("calendar-sources").replaceChildren(...calendar.sources.map(source => { const li = el("li"); const a = el("a", "text-link", source.title); a.href = source.url; a.target = "_blank"; a.rel = "noopener noreferrer"; li.append(a); return li; }));
    $("paper-check").disabled = paperBusy || data.busy || ledger.paused || ledger.positions.some(p => p.quantity !== 0) || !fresh || !calendar.open_now;
    $("paper-check").textContent = paperBusy || data.busy ? "Testing…" : "Run paper test";
    $("paper-summary").textContent = ledger.paused ? "Paused. Check retained paper positions before resuming." : data.busy ? "Checking real quotes and recording each paper fill…" : fresh && calendar.open_now ? "Ready for a one-lot paper check. Saved strategies will not be deployed." : "Start live prices in Connections during an open session to run this check.";
    $("paper-pause").hidden = ledger.paused;
    $("paper-resume").hidden = !ledger.paused;
    $("paper-resume").disabled = data.busy;
    $("paper-close-retained").hidden = !ledger.positions.some(p => p.quantity !== 0);
    $("paper-close-retained").disabled = true;
    $("paper-ledger-summary").textContent = `${ledger.fill_count} saved paper fills. ${ledger.limitation}`;
    const valuation = data.valuation;
    const money = value => Number.isFinite(value) ? "₹" + Number(value).toLocaleString("en-IN", {minimumFractionDigits:2,maximumFractionDigits:2}) : "Unknown";
    $("paper-valuation").textContent = !ledger.fill_count ? "Paper P&L appears after the first fill." : valuation ? `Closed net ${money(valuation.closed_net)} · Open mark ${money(valuation.open_net)} · Combined ${money(valuation.combined_net)}. Paper result, not broker profit.` : "Paper valuation unavailable.";
    $("paper-positions").replaceChildren(...ledger.positions.filter(p => p.quantity !== 0).map(p => {
      const mark = valuation?.positions?.find(v => v.strategy === p.strategy && v.symbol === p.symbol);
      const basis = {recorded:"Recorded option quote",mixed:"Estimated premium fallback",unavailable:"Price unavailable"}[mark?.pnl_basis] || "Price unavailable";
      return el("p", "", `${p.symbol}: ${p.quantity} units still open (test ${p.strategy}). ${basis} · open net ${money(mark?.open_net)}.`);
    }));
    $("paper-readiness").replaceChildren(...data.readiness.map(item => { const li = el("li"); li.append(el("span", "", item.label), el("span", "", item.status)); return li; }));
    for (const strategy of state.strategies) strategy.readiness = data.readiness;
    const selected = selectedStrategy();
    if (selected) renderReadiness(selected);
    $("paper-results").replaceChildren(...data.checks.slice(0, 3).map(check => { const div = el("div", "paper-result"); div.append(el("strong", "", `${check.status === "passed" ? "Paper check passed" : "Paper check " + check.status} · ${formattedDate(check.created_at)}`), el("p", "", check.message)); return div; }));
    renderJourney();
  }
  async function refreshPaper() {
    if (paperPolling) return;
    paperPolling = true;
    try { renderPaper(await api("/api/paper/status")); }
    catch (error) { paperSnapshot = null; renderJourney(); $("paper-summary").textContent = error.message; $("paper-check").disabled = true; $("paper-replay-refresh").disabled = true; $("stream-summary").textContent = error.message; }
    finally { paperPolling = false; }
  }
  async function paperAction(path, errorId) {
    if (paperBusy && !path.endsWith("/pause") && !path.endsWith("/stop")) return;
    const blocking = !path.endsWith("/pause") && !path.endsWith("/stop");
    if (blocking) paperBusy = true;
    renderJourney();
    showError(errorId, "");
    $("paper-check").disabled = true;
    try { await api(path, {method: "POST", body: "{}"}); }
    catch (error) { showError(errorId, error.message); }
    finally { if (blocking) paperBusy = false; await refreshPaper(); }
  }
  $("stream-start").addEventListener("click", () => paperAction("/api/paper/stream/start", "stream-error"));
  $("stream-stop").addEventListener("click", () => paperAction("/api/paper/stream/stop", "stream-error"));
  $("paper-check").addEventListener("click", () => paperAction("/api/paper/check", "paper-error"));
  $("paper-pause").addEventListener("click", () => paperAction("/api/paper/pause", "paper-error"));
  $("paper-resume").addEventListener("click", () => paperAction("/api/paper/resume", "paper-error"));
  // Recovery closing stays unavailable until short-first/protection-safe closure is verified.
  $("paper-evidence").addEventListener("click", async () => {
    try { const data = await api("/api/paper/evidence"); const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"})); const link = el("a"); link.href = url; link.download = "paper-test-evidence.json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    catch (error) { showError("paper-error", error.message); }
  });
  refreshPaper();
  setInterval(() => { if (!document.hidden) { refreshPaper(); refreshKiteStatus(); refreshTradetronStatus(); } }, 3000);
  window.addEventListener("focus", () => { refreshKiteStatus(); refreshTradetronStatus(); refreshPaper(); });

  loadState().catch((error) => {
    showError("page-error", error.message);
    $("strategy-list").replaceChildren(el("p", "loading", "Your workspace could not be loaded. Refresh this page to try again."));
    $("create-strategy").disabled = true;
    $("strategy-count").textContent = "—";
  });
  syncReplayDate();
  refreshKiteStatus();
  refreshTradetronStatus();
  restoreReplayJob();
  loadBacktestHistory();
})();
