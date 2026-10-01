"use strict";

// Read-only presentation of saved paper evidence. No order or broker endpoints.
(function (root) {
  const active = new Set(["OPEN", "PARTIAL", "WAITING", "UNKNOWN"]);
  const finite = x => typeof x === "number" && Number.isFinite(x);
  const money = x => finite(x) ? new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR",minimumFractionDigits:2}).format(x) : "Unavailable";
  const when = x => Number.isFinite(Date.parse(x)) ? new Intl.DateTimeFormat("en-IN", {dateStyle:"medium",timeStyle:"medium",timeZone:"Asia/Kolkata"}).format(new Date(x)) + " IST" : "Time not recorded";
  const fresh = (x, now, limit=5000) => Number.isFinite(Date.parse(x)) && now-Date.parse(x) >= 0 && now-Date.parse(x) <= limit;
  const sumKnown = values => values.length && values.every(finite) ? values.reduce((a,b)=>a+b,0) : null;

  function remainingLots(fills) {
    const lots=[];
    for (const f of fills) {
      let left=f.quantity; const sign=f.side === "BUY" ? 1 : -1;
      while (left && lots.length && lots[0].sign !== sign) {
        const take=Math.min(left,lots[0].quantity); left-=take; lots[0].quantity-=take;
        if (!lots[0].quantity) lots.shift();
      }
      if (left) lots.push({quantity:left,sign,price:f.price});
    }
    return lots;
  }

  function derive(input, mode="live", now=Date.now()) {
    const paper=input.paper, desk=paper?.desk, kite=input.kite;
    const connected=kite?.connected === true && !(Date.parse(kite.session_expires_at) <= now);
    const quotes=paper?.stream?.quotes || [];
    const usable=quotes.filter(q=>fresh(q.timestamp,now) && fresh(q.received_at,now));
    const pricesFresh=connected && paper?.stream?.state === "connected" && usable.some(q=>q.symbol === "NSE:NIFTY BANK") && usable.some(q=>q.symbol?.startsWith("NFO:BANKNIFTY"));
    const current=!!desk && fresh(desk.as_of,now,10000);
    const orders=mode === "paper" && current ? desk.orders || [] : [];
    const positions=mode === "paper" && current ? (desk.positions || []).filter(p=>p.quantity !== 0) : [];
    const groups=new Map(), issues=[];
    const add=(id,title,explanation,affected,href="#positions-orders",at=null)=>issues.push({id,title,explanation,affected,href,at});
    const today=new Intl.DateTimeFormat("en-CA",{year:"numeric",month:"2-digit",day:"2-digit",timeZone:"Asia/Kolkata"}).format(now);
    for (const p of positions) {
      const related=orders.filter(o=>o.intent.strategy === p.strategy && o.intent.symbol === p.symbol);
      const meta=desk.contracts?.[p.symbol] || {};
      const expiries=[...new Set(related.map(o=>o.intent.expiry))];
      const expiry=expiries.length === 1 ? expiries[0] : null;
      const kind=meta.instrument_type;
      const metadataKnown=expiry && meta.expiry === expiry && ["CE","PE"].includes(kind) && finite(meta.strike) && meta.strike>0;
      const key=[p.strategy,expiry || "unknown",kind || "unknown"].join("|");
      if (!groups.has(key)) groups.set(key,{key,strategy:p.strategy,expiry,kind,legs:[],orders:[],uncertain:false});
      const g=groups.get(key);
      const fills=(desk.fills || []).filter(f=>f.strategy === p.strategy && f.symbol === p.symbol);
      const lots=remainingLots(fills), quantity=lots.reduce((n,l)=>n+l.quantity*l.sign,0);
      const matched=quantity === p.quantity && related.every(o=>o.filled === fills.filter(f=>f.order_id === o.id).reduce((n,f)=>n+f.quantity,0));
      const v=paper.valuation?.positions?.find(v=>v.strategy === p.strategy && v.symbol === p.symbol && v.quantity === p.quantity);
      const markCurrent=connected && fresh(paper.valuation?.as_of,now) && v?.marks?.every(m=>m.pricing && fresh(m.pricing.basis === "estimated" ? m.pricing.spot_observed_at : m.pricing.source?.observed_at,now));
      const priced=(v?.marks || []).map(m=>m.pricing?.premium);
      const mark=markCurrent && priced.length && priced.every(finite) ? v.marks.reduce((n,m)=>n+Math.abs(m.quantity)*m.pricing.premium,0)/Math.abs(p.quantity) : null;
      const leg={...p,expiry,kind,strike:metadataKnown ? meta.strike : null,metadataKnown,matched,
        entry:matched && lots.length ? lots.reduce((n,l)=>n+l.price*l.quantity,0)/Math.abs(p.quantity) : null,
        mark,openNet:markCurrent && finite(v?.open_net) ? v.open_net : null,
        basis:markCurrent ? v?.pnl_basis : "unavailable",fills,orders:related};
      g.legs.push(leg); g.orders.push(...related);
      g.uncertain ||= !metadataKnown || !matched || expiry < today || (expiry === today && !paper.calendar?.open_now) || related.some(o=>o.status === "UNKNOWN");
    }
    for (const g of groups.values()) {
      g.orders=orders.filter(o=>o.intent.strategy === g.strategy && o.intent.expiry === g.expiry && (desk.contracts?.[o.intent.symbol]?.instrument_type === g.kind || g.legs.some(l=>l.symbol === o.intent.symbol)));
      g.uncertain ||= orders.some(o=>o.intent.strategy === g.strategy && o.intent.expiry === g.expiry && o.status === "UNKNOWN");
      const shorts=g.legs.filter(l=>l.quantity<0), longs=g.legs.filter(l=>l.quantity>0).map(l=>({...l,left:l.quantity}));
      // Allocate each executed hedge unit once, only to a matching expiry/type
      // and correct protective strike. Pending buys never contribute quantity.
      for (const short of shorts.sort((a,b)=>g.kind === "CE" ? b.strike-a.strike : a.strike-b.strike)) {
        let remaining=-short.quantity;
        for (const hedge of longs) {
          const eligible=short.metadataKnown && hedge.metadataKnown && (g.kind === "CE" ? hedge.strike>short.strike : hedge.strike<short.strike);
          if (!eligible) continue;
          const used=Math.min(remaining,hedge.left); remaining-=used; hedge.left-=used;
        }
        short.uncovered=remaining;
      }
      g.shortQuantity=shorts.reduce((n,l)=>n-l.quantity,0);
      g.uncovered=shorts.reduce((n,l)=>n+l.uncovered,0);
      g.protection=g.uncertain ? "Protection unverified" : !g.shortQuantity ? "No short exposure" : g.uncovered ? "Missing protection" : "Protected · paper fills";
      g.pnl=sumKnown(g.legs.map(l=>l.openNet));
      g.pending=g.orders.filter(o=>active.has(o.status));
      const affected=`BANKNIFTY ${g.kind || "option"} · ${g.expiry || "Expiry unavailable"} · Paper test ${g.strategy}`;
      if (g.uncertain || g.uncovered) add("protection:"+g.key,g.protection,g.uncertain ? "Contract details, expiry, order state or saved quantities cannot confirm protection. Review the evidence; no protection is assumed." : `${g.uncovered} executed short units have no matching executed protective option. Pending hedge orders do not count.`,affected);
      if (!paper.busy) add("retained:"+g.key,"Paper test left open exposure","The test is not managing this position. Protection-safe recovery closing is unavailable; review orders and saved evidence.",affected);
    }
    if (mode === "live") add("live-data","Broker positions cannot be verified","This app does not read broker positions or margin. Protection and account profit cannot be confirmed here. Review your actual positions in Zerodha; connecting prices does not enable trading.","All broker positions","#kite-connection");
    else if (!current) add("ledger","Paper records unavailable", "A current saved-position snapshot is unavailable. Do not assume the account is flat. Check the local service in Connections.","All paper positions","#kite-connection");
    if(mode === "paper" && paper?.historical_replay?.snapshot && !paper.historical_replay.through_latest_close) add("replay-stale","September positions need updating",`The reconstruction stops at ${when(paper.historical_replay.snapshot.as_of)}. Later entries, exits and rollovers are unknown. ${connected ? "Update September positions in Paper trading." : "Sign in in Connections, then update September positions."}`,"Historical September replay",connected ? "#paper-trading" : "#kite-connection",paper.historical_replay.snapshot.saved_at);
    if (mode === "paper" && (positions.length || paper?.busy) && !pricesFresh) add("prices","Fresh prices unavailable","Position values cannot be treated as current. Restore the connection and wait for fresh spot and option prices.","Open paper positions","#kite-connection");
    for (const o of orders) {
      if (active.has(o.status)) add("order:"+o.id,o.status === "UNKNOWN" ? "Order outcome needs review" : "Incomplete paper execution",`${o.filled} of ${o.intent.quantity} units executed. ${o.reason || "The unfilled remainder is not a position."}`,o.intent.symbol,"#positions-orders",desk.events?.find(e=>e.order_id === o.id)?.at);
      if (o.status === "REJECTED" && positions.some(p=>p.strategy === o.intent.strategy)) add("reject:"+o.id,"Paper order rejected",o.reason || "Review the order and affected exposure before another test.",o.intent.symbol,"#positions-orders",desk.events?.find(e=>e.order_id === o.id && e.event === "REJECTED")?.at);
    }
    const activity=mode === "paper" && current ? (desk.events || []).filter(e=>e.event !== "QUOTE_EVIDENCE").map(e=>({...e,order:orders.find(o=>o.id === e.order_id)})) : [];
    return {mode,connected,pricesFresh,current,groups:[...groups.values()],orders,issues,activity,
      asOf:desk?.as_of,lastPrice:usable.map(q=>q.timestamp).sort().at(-1) || paper?.stream?.last_packet,
      active:mode === "paper" && current && paper?.busy === true,
      protection:mode === "live" || !current ? "Unknown · not verified" : orders.some(o=>o.status === "UNKNOWN") ? "Protection unverified" : !positions.some(p=>p.quantity<0) ? "No open paper shorts" : [...groups.values()].some(g=>g.uncertain) ? "Protection unverified" : [...groups.values()].some(g=>g.uncovered) ? "Missing protection" : "Paper shorts protected",
      paper, drafts:input.state?.strategies || []};
  }

  // Historical reconstruction is deliberately not included in derive(): no
  // execution, order, protection or today's-account total can come from it.
  function replayView(data) {
    const s=data?.snapshot;
    if(!s || s.kind !== "historical_paper_replay" || s.is_execution !== false)return null;
    const basis=p=>p?.basis === "recorded" ? "Recorded price · not a fill" : p?.basis === "estimated" ? "Modelled estimate" : "Price source unavailable";
    const groups=[];
    for(const leg of s.legs || []) {
      const key=leg.option_type+"|"+leg.expiry;
      let group=groups.find(g=>g.key===key);
      if(!group) {group={key,kind:leg.option_type,expiry:leg.expiry,legs:[]};groups.push(group);}
      group.legs.push({...leg,entryBasis:basis(leg.entry_pricing),markBasis:basis(leg.mark_pricing),
        title:`${leg.direction === "SELL" ? "Sell" : "Buy hedge"} · ${finite(leg.strike) ? leg.strike.toLocaleString("en-IN") : "Strike unavailable"} ${leg.option_type === "CE" ? "Call" : "Put"}`});
    }
    return {snapshot:s,groups,shorts:s.legs.filter(l=>l.direction === "SELL").length,hedges:s.legs.filter(l=>l.direction === "BUY").length,
      status:data.through_latest_close ? "Through latest close · not live" : "Out of date · not current positions",note:data.note};
  }
  function freshStreamQuotes(input, now=Date.now()) {
    const quotes=new Map();
    for(const quote of input?.paper?.stream?.quotes || []) {
      if(typeof quote?.symbol !== "string" || !finite(quote.last_price) || quote.last_price <= 0
        || !fresh(quote.timestamp,now) || !fresh(quote.received_at,now)) continue;
      quotes.set(quote.symbol.toUpperCase(),quote);
    }
    return quotes;
  }
  function replaySymbol(leg) {
    const raw=leg?.contract?.symbol || leg?.symbol;
    if(typeof raw !== "string" || !raw) return null;
    return (raw.startsWith("NFO:") ? raw : "NFO:"+raw).toUpperCase();
  }
  function liveReplayLeg(leg, quotes) {
    const entry=finite(leg?.entry_premium) ? leg.entry_premium : null;
    const saved={...leg,entry,mark:finite(leg?.mark_premium) ? leg.mark_premium : null,
      pnl:finite(leg?.net_pnl) ? leg.net_pnl : null,liveMark:false};
    // Only open reconstructed legs can receive a present-market reference.
    // Historical fills and closed legs remain historical evidence.
    if(leg?.status !== "open" || !entry || !finite(leg?.quantity) || leg.quantity <= 0) return saved;
    const quote=quotes.get(replaySymbol(leg));
    if(!quote) return saved;
    const points=leg.direction === "SELL" ? entry-quote.last_price : quote.last_price-entry;
    const entryCharges=finite(leg?.charges) ? leg.charges : 0;
    return {...saved,mark:quote.last_price,pnl:points*leg.quantity-entryCharges,liveMark:true,markAt:quote.timestamp};
  }
  function liveReplayView(data, input, now=Date.now()) {
    const historical=replayView(data);
    if(!historical) return null;
    const snapshot=historical.snapshot;
    const legs=(snapshot.legs || []).map(leg=>liveReplayLeg(leg,freshStreamQuotes(input,now)));
    const openLegs=legs.filter(leg=>leg.status === "open");
    const liveLegs=openLegs.filter(leg=>leg.liveMark);
    const allOpenLegsLive=openLegs.length > 0 && liveLegs.length === openLegs.length;
    const liveAt=liveLegs.map(leg=>leg.markAt).filter(Boolean).sort().at(-1);
    const approximate=snapshot.validation_status !== "validated";
    const liveStatus=liveLegs.length ? `Live marks ${liveLegs.length}/${openLegs.length}` : null;
    return {...historical,legs,openLegs,liveLegs,allOpenLegsLive,liveAt,approximate,liveStatus,
      pnl:allOpenLegsLive ? sumKnown(liveLegs.map(leg=>leg.pnl)) : snapshot.combined?.open_net};
  }
  function deploymentRecords(v, input, mode="paper", now=Date.now()) {
    // Historical and paper records must never populate a live deployment view.
    if (mode !== "paper") return [];
    const records=[];
    for (const deployment of input?.state?.deployments || []) {
      if (deployment?.mode !== "paper_only") continue;
      const executor=deployment.executor || {};
      const runner=deployment.runner || {};
      const linked=executor.linked === true;
      const status=linked
        ? deployment.state === "linked_unverified" ? "Link saved · paper check required"
          : deployment.state === "paper_ready" ? "Paper ready · not running"
          : deployment.state === "paper_running" ? "Paper running"
          : deployment.state === "paused" ? "Paper paused"
          : deployment.state === "blocked" ? "Paper blocked"
          : "Paper setup pending"
        : "Not linked · not running";
      records.push({
        id:"saved-deployment:"+deployment.id, strategy:deployment.strategy_name || "BANKNIFTY strategy",
        execution:"Paper", status,
        valuationAt:deployment.updated_at || deployment.created_at, multiplier:deployment.multiplier,
        legs:[], pnl:null,
        deploymentId:deployment.id, deploymentState:deployment.state, executor, runner,
        note:linked
          ? `Saved paper deployment · ${deployment.strategy_version || "version unavailable"}. Its own Tradetron link is protected on this Windows account; automatic sending remains off.`
          : `Saved paper deployment · ${deployment.strategy_version || "version unavailable"}. Link its own Tradetron executor before it can receive signals.`,
      });
    }
    const historical=liveReplayView(input?.paper?.historical_replay,input,now);
    if (historical) {
      const snapshot=historical.snapshot;
      const configuredLots=[snapshot.config?.sell_lots,snapshot.config?.ce_hedge_lots,snapshot.config?.pe_hedge_lots];
      const multiplier=configuredLots.every(value=>Number.isSafeInteger(value) && value === configuredLots[0]) ? configuredLots[0] : null;
      records.push({
        id:"historical-replay",
        strategy:snapshot.strategy?.name || "BANKNIFTY hourly model",
        execution:"Paper",
        status:historical.liveStatus ? `${historical.liveStatus} · ${historical.approximate ? "replay approximate" : "replay complete"}` : (historical.approximate ? "Replay approximate" : "Replay complete"),
        valuationAt:historical.allOpenLegsLive ? historical.liveAt : snapshot.as_of,
        multiplier,
        legs:historical.legs,
        // Do not publish a partly live total. It would combine a current quote
        // with an old mark and look more certain than it is.
        pnl:historical.pnl,
        note:historical.liveLegs.length ? `Historical reconstruction from 1 Sep 2026. ${historical.liveLegs.length}/${historical.openLegs.length} open legs use fresh Zerodha LTPs; this is a live market reference, not an executed paper order or a broker position.` : "Historical reconstruction from 1 Sep 2026. It is not an executed paper order or a broker position."
      });
    }
    const byStrategy=new Map();
    for (const group of v.groups || []) {
      const key=group.strategy || "paper-order-test";
      if (!byStrategy.has(key)) byStrategy.set(key,{id:"paper:"+key,strategy:group.strategy || "Paper order test",execution:"Paper",valuationAt:v.asOf,legs:[],profits:[],needsReview:false});
      const record=byStrategy.get(key);
      record.legs.push(...group.legs.map(leg=>({...leg,pnl:leg.openNet})));
      record.profits.push(group.pnl);
      record.needsReview ||= group.uncertain || Boolean(group.uncovered);
    }
    for (const record of byStrategy.values()) {
      record.status=record.needsReview ? "Needs review" : "Paper positions";
      record.pnl=sumKnown(record.profits);
      record.note=record.needsReview ? "Open paper quantities need a protection or order-state review." : "Executed paper-order record. It is not a broker position.";
      delete record.profits; delete record.needsReview;
      records.push(record);
    }
    return records;
  }
  function runtimeOptionName(leg) {
    const raw=leg?.contract?.symbol || leg?.symbol;
    if(typeof raw !== "string" || !raw) {
      const strike=finite(leg?.strike) ? leg.strike.toLocaleString("en-IN") : "Strike unavailable";
      return `${strike} ${leg?.option_type || leg?.kind || "option"}`;
    }
    const clean=raw.replace(/^NFO:/,"").toUpperCase();
    const monthly=clean.match(/^(BANKNIFTY)(?:\d{2})?([A-Z]{3})(\d{4,6})(CE|PE)$/);
    return monthly ? `${monthly[1]} ${monthly[2]} ${monthly[3]} ${monthly[4]}` : clean;
  }
  function runtimePricing(legs) {
    const bases=new Set();
    for(const leg of legs || []) {
      for(const pricing of [leg?.entry_pricing,leg?.mark_pricing]) {
        if(pricing?.basis === "recorded") bases.add("Recorded option price");
        else if(pricing?.basis === "estimated") bases.add("Modelled option price");
        else if(pricing?.basis === "mixed") bases.add("Recorded and modelled prices");
      }
      if(leg?.basis === "recorded") bases.add("Recorded option price");
      else if(leg?.basis === "estimated") bases.add("Modelled option price");
      else if(leg?.basis === "mixed") bases.add("Recorded and modelled prices");
    }
    return bases.size ? [...bases].join(" · ") : "Price source unavailable";
  }
  function runtimeData(record,input) {
    const historical=record?.id === "historical-replay";
    const bridge=input?.tradetron || {};
    const linked=bridge.linked === true;
    const delivery={
      verified:{value:"Paper link verified",note:"This desk received a successful response to a paper signal. It cannot read Tradetron’s current runtime values, orders or positions back."},
      unverified:{value:"Paper link not tested",note:"A protected link is saved, but no paper signal has been confirmed by Tradetron yet."},
      needs_relink:{value:"Link needs updating",note:"Tradetron rejected the last request. Replace the protected API link before another signal."},
      disabled:{value:"Paper link unavailable",note:"The protected link cannot send a paper signal in its current state."}
    };
    const directions=["CE","PE"].map(kind=>{
      const legs=(record?.legs || []).filter(leg=>(leg.option_type || leg.kind) === kind);
      // Replay quantities are absolute units, while a paper-ledger short is
      // negative. Prefer the explicit direction whenever it exists.
      const short=legs.find(leg=>leg.direction ? leg.direction === "SELL" : leg.quantity < 0) || null;
      const hedge=legs.find(leg=>leg.direction ? leg.direction === "BUY" : leg.quantity > 0) || null;
      const risk=short && finite(short.risk_reference_spot) && finite(short.stop_spot) && finite(short.target_spot)
        ? {reference:short.risk_reference_spot,stop:short.stop_spot,target:short.target_spot} : null;
      return {kind,legs,short,hedge,risk,pricing:runtimePricing(legs)};
    }).filter(group=>group.legs.length);
    return {
      title:historical ? "Historical replay runtime" : "Paper order runtime",
      summary:historical
        ? "These are reconstructed September inputs and marks. They are not an executed paper order, a Tradetron position or a Zerodha position."
        : "These are saved local paper-order records. Only completed simulated fills count as local paper positions.",
      source:historical ? "Research replay" : "Local paper ledger",
      valuationAt:record?.valuationAt || null,
      pricing:runtimePricing(record?.legs || []),
      bridge:!linked ? {value:"Not linked",note:"No Tradetron runtime values can be sent from this desk."} : (delivery[bridge.delivery] || {value:"Link status unavailable",note:"Refresh Connections before relying on this link."}),
      strategyId:linked && bridge.strategy_id ? `Strategy ${bridge.strategy_id}` : "No strategy linked",
      directions
    };
  }
  function storedLegSide(leg) {
    return leg?.direction === "SELL" || (!leg?.direction && leg?.quantity < 0) ? "short" : leg?.direction === "BUY" || (!leg?.direction && leg?.quantity > 0) ? "hedge" : "unknown";
  }
  function storedPricingBasis(leg) {
    const bases=new Set();
    for(const basis of [leg?.pnl_basis,leg?.basis,leg?.entry_pricing?.basis,leg?.mark_pricing?.basis]) {
      if(["recorded","estimated","mixed"].includes(basis)) bases.add(basis);
    }
    if(bases.has("mixed") || (bases.has("recorded") && bases.has("estimated"))) return "mixed";
    if(bases.has("recorded")) return "recorded";
    if(bases.has("estimated")) return "estimated";
    return "unavailable";
  }
  function deploymentStatistics(record) {
    const historical=record?.id === "historical-replay";
    const directions=runtimeData(record,{}).directions.map(group=>{
      const shorts=group.legs.filter(leg=>storedLegSide(leg) === "short");
      const hedges=group.legs.filter(leg=>storedLegSide(leg) === "hedge");
      const units=items=>items.length && items.every(leg=>finite(leg.quantity)) ? items.reduce((total,leg)=>total+Math.abs(leg.quantity),0) : null;
      const pnl=items=>sumKnown(items.map(leg=>finite(leg.pnl) ? leg.pnl : leg.openNet));
      const shortStrike=group.short?.strike ?? group.short?.contract?.strike;
      const hedgeStrike=group.hedge?.strike ?? group.hedge?.contract?.strike;
      return {kind:group.kind,shortUnits:units(shorts),hedgeUnits:units(hedges),shortPnl:pnl(shorts),hedgePnl:pnl(hedges),
        hedgeDistance:finite(shortStrike) && finite(hedgeStrike) ? Math.abs(hedgeStrike-shortStrike) : null,
        risk:group.risk ? {stopDistance:Math.abs(group.risk.reference-group.risk.stop),targetDistance:Math.abs(group.risk.reference-group.risk.target)} : null};
    });
    const coverage={recorded:0,mixed:0,estimated:0,unavailable:0};
    for(const leg of record?.legs || []) coverage[storedPricingBasis(leg)] += 1;
    const shortUnits=sumKnown(directions.map(group=>group.shortUnits));
    const hedgeUnits=sumKnown(directions.map(group=>group.hedgeUnits));
    return {
      title:historical ? "Historical replay statistics" : "Paper-order statistics",
      summary:historical ? "Statistics for the open reconstructed record only. They are research figures, not a broker statement or executed fills." : "Statistics for saved local paper orders only. They are not broker positions or account figures.",
      source:historical ? "Research replay" : "Local paper ledger",
      pnlLabel:historical ? "Open estimate" : "Open paper P&L",
      pnl:record?.pnl ?? null,
      openLegs:record?.legs?.length ?? 0,
      shortUnits,hedgeUnits,directions,coverage,
      note:"Recorded prices are saved historical quotes or candles, not broker fills. Modelled prices are estimates where exact option data is missing."
    };
  }
  if (typeof module !== "undefined" && module.exports) { module.exports={derive,remainingLots,money,when,replayView,liveReplayView,deploymentRecords,runtimeData,deploymentStatistics}; return; }
  const $=id=>document.getElementById(id), seen=new Map();
  let input={}, mode="paper", lastKey="", selectedGroup=null;
  function node(tag,text,cls) { const n=document.createElement(tag); if(text != null)n.textContent=text; if(cls)n.className=cls; return n; }
  function link(label,hash) { const n=node("a",label,"text-link"); n.href=hash; return n; }
  function text(id,value) { if($(id))$(id).textContent=value; }
  function detail(label,value,note) { const n=node("div"); n.append(node("dt",label),node("dd",value)); if(note)n.append(node("small",note)); return n; }
  function empty(title,note,href,label) { const n=node("div",null,"desk-empty"); n.append(node("h3",title),node("p",note)); if(href)n.append(link(label,href)); return n; }
  function orderRow(o) { const status={COMPLETE:"Fully executed",PARTIAL:"Partly executed",OPEN:"Not filled yet",WAITING:"Waiting for replacement hedge",UNKNOWN:"Outcome needs review",REJECTED:"Rejected",CANCELLED:"Unfilled remainder cancelled"}[o.status] || o.status; const n=node("div",null,"order-row"); n.append(node("strong",`${o.intent.side === "BUY" ? "Buy" : "Sell"} · ${o.intent.symbol}`),node("p",`${status} · ${o.filled}/${o.intent.quantity} units executed · Average ${money(o.average_price)} · Paper execution`),node("p",`${when(o.created)} · ${o.reason || (o.intent.close_only ? "Closing order" : "Opening order")}`),node("code",o.id)); return n; }
  function eventRow(e) {
    const names={SUBMITTED:"Paper order submitted",COMPLETE:"Paper order fully executed",PARTIAL:"Paper order partly executed",REJECTED:"Paper order rejected",CANCELLED:"Unfilled paper quantity cancelled",EXPIRED:"Paper order expired",PAUSED:"Paper order processing paused",RESTART_PAUSE:"Restart paused paper processing for review",RECONCILED:"Saved paper orders checked; interrupted remainders cancelled"};
    const n=node("div",null,"activity-row");
    const replacement=e.order?.key?.includes("replacement") ? " · hedge replacement" : "";
    n.append(node("strong",(names[e.event] || "Paper event: "+e.event)+replacement),node("p",`${when(e.at)} · ${e.order?.intent.symbol || "Paper order lab"}${e.detail?.reason ? " · "+e.detail.reason : ""}`));
    const a=link("Inspect position / orders →","#positions-orders");
    a.addEventListener("click",()=>{selectedGroup=e.order?.intent.strategy || null; lastKey=""; render(input);});n.append(a);return n;
  }
  function deskCell(label,value,note,cls) {
    const cell=node("td",null,cls); cell.dataset.label=label;
    if(value instanceof Node) cell.append(value); else cell.append(node("span",value));
    if(note) cell.append(node("small",note));
    return cell;
  }
  function deskTable(label,headers,rows,cls="") {
    const table=node("table",null,"desk-table "+cls); table.setAttribute("aria-label",label);
    const thead=node("thead"), head=node("tr");
    headers.forEach((header,index)=>head.append(node("th",header,index ? "" : "desk-table-primary")));
    thead.append(head); table.append(thead);
    const body=node("tbody"); rows.forEach(row=>body.append(row)); table.append(body); return table;
  }
  function optionLabel(l) {
    const type=l.kind === "CE" ? "Call" : l.kind === "PE" ? "Put" : "Option";
    return `${l.strike == null ? "Strike unavailable" : l.strike.toLocaleString("en-IN")} ${type}`;
  }
  function zerodhaOptionName(l) {
    const symbol=l?.contract?.symbol || l?.symbol;
    if(typeof symbol !== "string" || !symbol) return optionLabel(l);
    const clean=symbol.replace(/^NFO:/,"").toUpperCase();
    const monthly=clean.match(/^(BANKNIFTY)(?:\d{2})?([A-Z]{3})(\d{4,6})(CE|PE)$/);
    return monthly ? `${monthly[1]} ${monthly[2]} ${monthly[3]} ${monthly[4]}` : clean;
  }
  function signedQuantity(l) {
    if(!finite(l?.quantity))return "Unavailable";
    const quantity=l.direction === "SELL" ? -Math.abs(l.quantity) : l.quantity;
    return quantity.toLocaleString("en-IN");
  }
  function compactPosition(g) {
    const n=node("article",null,"desk-table-panel desk-position-table");
    const head=node("div",null,"desk-table-head");
    head.append(node("h3",`Paper positions · ${g.expiry || "Expiry unavailable"}`),node("span",g.protection,"state-label "+(g.uncovered || g.uncertain ? "warning" : "")));
    n.append(head);
    const rows=g.legs.map(l=>{
      const row=node("tr");
      row.append(
        deskCell("Option name",zerodhaOptionName(l)),
        deskCell("Execution",g.executionType || "Paper",null,"execution"),
        deskCell("Quantity",signedQuantity(l)),
        deskCell("Entry price",money(l.entry)),
        deskCell("LTP",money(l.mark)),
        deskCell("P&L",money(l.openNet),null,finite(l.openNet) && l.openNet >= 0 ? "gain" : "loss")
      ); return row;
    });
    n.append(deskTable("Open paper option legs",["Option name","Execution","Quantity","Entry price","LTP","P&L"],rows,"desk-leg-table"));
    const foot=node("div",null,"desk-table-foot"); foot.append(node("span","Total P&L"),node("strong",money(g.pnl))); n.append(foot); return n;
  }
  function position(g, compact=false) {
    if(compact)return compactPosition(g);
    const n=node("article",null,"position-group");
    n.append(node("h3",`BANKNIFTY ${g.kind || "option"} · ${g.expiry || "Expiry unavailable"}`));
    n.append(node("p",`Paper order test ${g.strategy} · Active strategy version unavailable (not a deployed draft)`));
    n.append(node("span",g.protection,"state-label "+(g.uncovered || g.uncertain ? "warning" : "")));
    n.append(node("p",`Uncovered quantity: ${g.uncertain ? "Unverified" : g.uncovered+" units"} · Combined open P&L ${money(g.pnl)} · Estimated entry charges included; future closing charges excluded.`));
    const legs=node("div",null,"leg-grid");
    for(const l of g.legs) {
      const d=node("dl",null,"leg-detail");
      d.append(detail("Option",`${l.strike == null ? "Strike unavailable" : l.strike.toLocaleString("en-IN")} ${l.kind === "CE" ? "Call" : l.kind === "PE" ? "Put" : "Type unavailable"}`),detail("Executed position",`${l.quantity>0 ? "Buy" : "Sell"} · ${Math.abs(l.quantity)} units`),detail("Entry · paper fill",money(l.entry)),detail("Current price",money(l.mark)),detail("Price source",l.basis === "recorded" ? "Recorded quote · not a fill" : l.basis === "mixed" ? "Includes modelled premium estimate" : "Unavailable"),detail("Valuation time",finite(l.mark) ? when(input.paper?.valuation?.as_of) : "Unavailable"));
      legs.append(d);
    }
    n.append(legs,node("p",`Stop / target: unavailable. This is an order test, not the hourly strategy. Pending orders: ${g.pending.length}. Next action: ${g.pending.length || g.uncovered || g.uncertain ? "review executions and protection" : "review retained paper exposure"}.`));
    const expand=node("details"); expand.dataset.group=g.key;
    expand.append(node("summary","Orders & execution history"));
    g.orders.forEach(o=>expand.append(orderRow(o)));
    for(const e of (input.paper?.desk?.events || []).filter(e=>g.orders.some(o=>o.id === e.order_id) && e.event !== "QUOTE_EVIDENCE")) expand.append(eventRow({...e,order:g.orders.find(o=>o.id === e.order_id)}));
    n.append(expand);return n;
  }
  function replayPositions(data, compact=false, scope="desk") {
    const v=liveReplayView(data,input);
    if(!v)return empty("No historical replay positions","Update the September replay after connecting Zerodha. Historical data never creates paper orders or live positions.","#paper-trading","Open paper order lab →");
    const s=v.snapshot, n=node("section",null,"desk-table-panel replay-table-panel");
    const head=node("div",null,"desk-table-head");
    const status=v.liveStatus ? `${v.liveStatus} · ${v.approximate ? "replay approximate" : "replay complete"}` : v.status;
    head.append(node("h3",scope === "lab" ? "September replay" : "Historical replay positions"),node("span",status,"state-label warning"));
    const valuedAt=v.allOpenLegsLive ? v.liveAt : s.as_of;
    const priceNote=v.liveLegs.length ? `${v.liveLegs.length}/${v.openLegs.length} open legs use fresh Zerodha LTPs. They are live market references, not option fills.` : "Prices are recorded where available and modelled otherwise; they are not option fills.";
    n.append(head,node("p",`Valued at ${when(valuedAt)} · ${s.validation_status === "validated" ? "Checked historical data" : "Approximate reconstruction"}. ${priceNote}`,"essential-note"));
    const rows=[];
    for(const l of v.legs) {
      const row=node("tr");
      row.append(
        deskCell("Option name",zerodhaOptionName(l)),
        deskCell("Quantity",signedQuantity(l)),
        deskCell("Entry price",money(l.entry)),
        deskCell("LTP",money(l.mark),l.liveMark ? "Live" : "Saved mark"),
        deskCell("Open P&L",money(l.pnl),null,finite(l.pnl) && l.pnl >= 0 ? "gain" : "loss")
      ); rows.push(row);
    }
    if(rows.length)n.append(deskTable("Historical replay option legs",["Option name","Quantity","Entry price","LTP","Open P&L"],rows,"desk-leg-table replay-leg-table"));
    else n.append(node("p","The reconstruction has no open legs at this snapshot time.","essential-note"));
    const foot=node("div",null,"desk-table-foot");
    foot.append(node("span",`Closed estimate since 1 Sep ${money(s.combined?.realised_net)}`),node("strong",`Open ${money(v.pnl)}`));n.append(foot);
    return n;
  }

  function paperPositionTable(groups) {
    const n=node("section",null,"desk-table-panel desk-position-table");
    const head=node("div",null,"desk-table-head"); head.append(node("h3","Executed paper positions")); n.append(head);
    const rows=[];
    for(const g of groups) for(const l of g.legs) {
      const row=node("tr");
      row.append(
        deskCell("Option name",zerodhaOptionName(l)),
        deskCell("Quantity",signedQuantity(l)),
        deskCell("Entry price",money(l.entry)),
        deskCell("LTP",money(l.mark)),
        deskCell("Open P&L",money(l.openNet),null,finite(l.openNet) && l.openNet >= 0 ? "gain" : "loss"),
        deskCell("Protection",g.protection)
      ); rows.push(row);
    }
    n.append(deskTable("Executed paper option positions",["Option name","Quantity","Entry price","LTP","Open P&L","Protection"],rows,"desk-leg-table paper-position-table"));
    const total=sumKnown(groups.map(g=>g.pnl));
    const foot=node("div",null,"desk-table-foot");foot.append(node("span","Open paper P&L"),node("strong",money(total)));n.append(foot);
    return n;
  }

  function orderTable(orders) {
    const labels={COMPLETE:"Filled",PARTIAL:"Partly filled",OPEN:"Open",WAITING:"Waiting",UNKNOWN:"Needs review",REJECTED:"Rejected",CANCELLED:"Cancelled"};
    const n=node("div",null,"desk-table-panel order-table-panel"), rows=[];
    for(const order of orders) {
      const row=node("tr"), intent=order.intent || {};
      row.append(
        deskCell("Option name",zerodhaOptionName({symbol:intent.symbol})),
        deskCell("Side",intent.side === "BUY" ? "Buy" : intent.side === "SELL" ? "Sell" : "Unavailable"),
        deskCell("Quantity",Number.isFinite(intent.quantity) ? intent.quantity.toLocaleString("en-IN") : "Unavailable"),
        deskCell("Filled",Number.isFinite(order.filled) ? order.filled.toLocaleString("en-IN") : "Unavailable"),
        deskCell("Status",labels[order.status] || order.status || "Unavailable"),
        deskCell("Average price",money(order.average_price))
      ); rows.push(row);
    }
    n.append(deskTable("Paper order history",["Option name","Side","Quantity","Filled","Status","Average price"],rows,"desk-order-table"));return n;
  }
  function strategyTable(v) {
    const n=node("div",null,"desk-table-panel strategy-table-panel");
    const row=node("tr"), strategy=link("BANKNIFTY hourly model","#strategies-heading");
    row.append(
      deskCell("Strategy",strategy),
      deskCell("Status",v.active ? "Paper test running" : "Stopped"),
      deskCell("Next check","None")
    );
    n.append(deskTable("Strategies shown on the trading desk",["Strategy","Status","Next check"],[row],"desk-strategy-table")); return n;
  }
  function runtimeFact(label,value,note) {
    const n=node("div",null,"runtime-fact");
    n.append(node("span",label),node("strong",value));
    if(note)n.append(node("small",note));
    return n;
  }
  function runtimeDirection(group) {
    const n=node("section",null,"runtime-direction");
    const title=group.kind === "CE" ? "Call side" : "Put side";
    n.append(node("h3",title));
    const facts=node("div",null,"runtime-facts");
    facts.append(
      runtimeFact("Short option",group.short ? runtimeOptionName(group.short) : "No short saved",group.short?.expiry ? `Expiry ${group.short.expiry}` : null),
      runtimeFact("Protective option",group.hedge ? runtimeOptionName(group.hedge) : "No hedge saved",group.hedge?.expiry ? `Expiry ${group.hedge.expiry}` : null),
      runtimeFact("Price source",group.pricing)
    );
    if(group.risk) facts.append(runtimeFact("Spot risk levels",`Reference ${group.risk.reference.toLocaleString("en-IN")} · Stop ${group.risk.stop.toLocaleString("en-IN")} · Target ${group.risk.target.toLocaleString("en-IN")}`,"Fixed from the completed signal-hour close."));
    n.append(facts); return n;
  }
  function historicalRuntimeNote(record) {
    return record?.id === "historical-replay"
      ? "To confirm remote values or executions, open Tradetron’s Runtime Data and deployment pages. This local view deliberately does not assume they match."
      : "Local paper records are separate from Tradetron and Zerodha. A pending or intended hedge never counts as confirmed protection.";
  }
  function showRuntimeData(record) {
    const dialog=$("runtime-data-dialog");
    if(!dialog)return;
    const data=runtimeData(record,input);
    text("runtime-data-title",data.title);
    text("runtime-data-description",data.summary);
    const overview=$("runtime-data-overview");
    overview.replaceChildren(
      runtimeFact("Record source",data.source),
      runtimeFact("Valued through",when(data.valuationAt)),
      runtimeFact("Price basis",data.pricing),
      runtimeFact("Tradetron link",data.bridge.value,`${data.strategyId}. ${data.bridge.note}`)
    );
    const directions=$("runtime-data-directions");
    directions.replaceChildren(...(data.directions.length ? data.directions.map(runtimeDirection) : [node("p","No open option runtime inputs are saved for this record.","essential-note")]));
    text("runtime-data-note",historicalRuntimeNote(record));
    if(!dialog.open)dialog.showModal();
    $("runtime-data-close").focus();
  }
  function statisticDirection(group) {
    const n=node("section",null,"runtime-direction");
    n.append(node("h3",group.kind === "CE" ? "Call side" : "Put side"));
    const facts=node("div",null,"runtime-facts");
    facts.append(
      runtimeFact("Sold quantity",finite(group.shortUnits) ? group.shortUnits.toLocaleString("en-IN") : "Unavailable"),
      runtimeFact("Hedge quantity",finite(group.hedgeUnits) ? group.hedgeUnits.toLocaleString("en-IN") : "Unavailable"),
      runtimeFact("Short P&L",money(group.shortPnl)),
      runtimeFact("Hedge P&L",money(group.hedgePnl))
    );
    if(finite(group.hedgeDistance)) facts.append(runtimeFact("Hedge distance",`${group.hedgeDistance.toLocaleString("en-IN")} spot points`,`Distance between the saved short and hedge strikes.`));
    if(group.risk) facts.append(runtimeFact("Risk distance",`Stop ${group.risk.stopDistance.toLocaleString("en-IN")} · Target ${group.risk.targetDistance.toLocaleString("en-IN")} spot points`,`Measured from the original signal-hour close.`));
    n.append(facts); return n;
  }
  function coverageText(coverage) {
    const labels={recorded:"recorded",mixed:"mixed",estimated:"modelled",unavailable:"unavailable"};
    return Object.entries(coverage).filter(([,count])=>count).map(([basis,count])=>`${count} ${labels[basis]}`).join(" · ") || "No open option legs";
  }
  function showStatistics(record) {
    const dialog=$("deployment-statistics-dialog");
    if(!dialog)return;
    const data=deploymentStatistics(record);
    text("statistics-title",data.title);
    text("statistics-description",data.summary);
    $("statistics-overview").replaceChildren(
      runtimeFact("Record source",data.source),
      runtimeFact(data.pnlLabel,money(data.pnl)),
      runtimeFact("Open option legs",String(data.openLegs)),
      runtimeFact("Position size",`Sold ${finite(data.shortUnits) ? data.shortUnits.toLocaleString("en-IN") : "Unavailable"} · Hedge ${finite(data.hedgeUnits) ? data.hedgeUnits.toLocaleString("en-IN") : "Unavailable"}`)
    );
    $("statistics-directions").replaceChildren(...(data.directions.length ? data.directions.map(statisticDirection) : [node("p","No open option statistics are available for this record.","essential-note")]));
    $("statistics-coverage").replaceChildren(runtimeFact("Open-leg price coverage",coverageText(data.coverage),data.note));
    if(!dialog.open)dialog.showModal();
    $("statistics-close").focus();
  }
  function deploymentMenu(record) {
    const menu=node("details",null,"row-menu");
    const toggle=node("summary","⋮","row-menu-trigger");
    toggle.setAttribute("aria-label",`Controls for ${record.strategy}`);
    const actions=node("div",null,"row-menu-actions");
    actions.setAttribute("role","menu");
    const runtime=node("button","Runtime data","menu-action");
    runtime.type="button";
    runtime.addEventListener("click",()=>showRuntimeData(record));
    const statistics=node("button","Statistics","menu-action");
    statistics.type="button";
    statistics.addEventListener("click",()=>showStatistics(record));
    actions.append(runtime,statistics);
    if(record.deploymentId) {
      const linked=record.executor?.linked === true;
      const linkExecutor=node("button",linked ? "Replace executor link" : "Link paper executor","menu-action");
      linkExecutor.type="button";
      linkExecutor.addEventListener("click",()=>root.dispatchEvent(new CustomEvent("deployment-link",{detail:{id:record.deploymentId}})));
      actions.append(linkExecutor);
      if(linked) {
        const disconnect=node("button","Remove executor link","menu-action");
        disconnect.type="button";
        disconnect.addEventListener("click",()=>root.dispatchEvent(new CustomEvent("deployment-disconnect",{detail:{id:record.deploymentId}})));
        actions.append(disconnect);
      } else if(input.tradetron?.linked === true) {
        const move=node("button","Move legacy link here","menu-action");
        move.type="button";
        move.addEventListener("click",()=>root.dispatchEvent(new CustomEvent("deployment-migrate-legacy",{detail:{id:record.deploymentId}})));
        actions.append(move);
      }
    }
    actions.append(link("View deployment","#deployments"),link("Open positions & orders","#positions-orders"));
    menu.append(toggle,actions); return menu;
  }
  function deploymentSummary(records) {
    if (!records.length) return empty("No recorded deployments","There are no paper deployments to show. Live deployments will appear only after broker-position sync is available.","#kite-connection","Review connection limits →");
    const rows=records.map(record=>{
      const row=node("tr");
      row.append(
        deskCell("Strategy",link(record.strategy,"#deployments")),
        deskCell("Execution",record.execution,null,"execution"),
        deskCell("Status",record.status),
        deskCell("Open legs",String(record.legs.length)),
        deskCell("Open P&L",money(record.pnl),null,finite(record.pnl) && record.pnl >= 0 ? "gain" : "loss"),
        deskCell("Controls",deploymentMenu(record),null,"table-controls")
      );
      return row;
    });
    const n=node("div",null,"desk-table-panel strategy-summary-table");
    n.append(deskTable("Strategy deployment summary",["Strategy","Execution","Status","Open legs","Open P&L","Controls"],rows,"desk-deployment-summary"));
    return n;
  }
  function deploymentBlock(record) {
    const n=node("article",null,"deployment-card");
    const head=node("div",null,"deployment-card-head"), title=node("div");
    title.append(node("h2",record.strategy),node("p",record.note,"essential-note"));
    head.append(title,node("span",record.execution,"deployment-execution"),node("span",record.status,"state-label deployment-status"+(record.status === "Needs review" ? " warning" : "")),deploymentMenu(record));
    const facts=node("dl",null,"deployment-facts");
    facts.append(detail("Valued through",when(record.valuationAt)),detail("Open legs",String(record.legs.length)));
    if(record.multiplier) facts.append(detail("Paper size",`1 × ${record.multiplier} = ${record.multiplier} lots`));
    n.append(head,facts);
    if (record.legs.length) {
      const rows=record.legs.map(leg=>{
        const row=node("tr");
        row.append(
          deskCell("Option name",zerodhaOptionName(leg)),
          deskCell("Quantity",signedQuantity(leg)),
          deskCell("Entry price",money(leg.entry)),
          deskCell("LTP",money(leg.mark),leg.liveMark ? "Live" : "Saved mark"),
          deskCell("P&L",money(leg.pnl),null,finite(leg.pnl) && leg.pnl >= 0 ? "gain" : "loss")
        );
        return row;
      });
      n.append(deskTable(`${record.strategy} option positions`,["Option name","Quantity","Entry price","LTP","P&L"],rows,"desk-leg-table deployment-leg-table"));
      const foot=node("div",null,"desk-table-foot");
      foot.append(node("span","Open P&L"),node("strong",money(record.pnl))); n.append(foot);
    }
    return n;
  }
  function eventName(e) {
    return {SUBMITTED:"Paper order submitted",COMPLETE:"Paper order fully executed",PARTIAL:"Paper order partly executed",REJECTED:"Paper order rejected",CANCELLED:"Unfilled paper quantity cancelled",EXPIRED:"Paper order expired",PAUSED:"Paper order processing paused",RESTART_PAUSE:"Restart paused paper processing for review",RECONCILED:"Saved paper orders checked"}[e.event] || "Paper event: "+e.event;
  }
  function activityTable(events,limit=null) {
    if(!events.length)return null;
    const n=node("div",null,"desk-table-panel activity-table-panel"), rows=[];
    for(const e of events.slice(0,limit || events.length)) {
      const row=node("tr");
      row.append(
        deskCell("Time",when(e.at)),
        deskCell("Activity",eventName(e)),
        deskCell("Option",e.order?.intent.symbol?.replace(/^NFO:/,"") || "Paper order lab")
      ); rows.push(row);
    }
    n.append(deskTable("Recent paper activity",["Time","Activity","Option"],rows,"desk-activity-table")); return n;
  }
  function render(next) {
    input=next;
    const v=derive(input,mode);
    text("desk-execution",mode === "paper" ? "Paper records" : "Live unavailable");
    text("desk-connection",v.connected ? "Connected · prices only" : "Disconnected");
    text("desk-prices",v.pricesFresh ? "Fresh · spot & options" : "Fresh prices unavailable");
    text("desk-trading",v.active ? "Paper test running" : mode === "live" ? "Not running" : "Not running · paper only");
    text("desk-protection",v.protection);
    text("desk-status-note",mode === "live" ? `Live execution, broker positions and account values are not connected. Last price: ${v.lastPrice ? when(v.lastPrice) : "not received"}.` : `Live execution unavailable. Paper records are separate from broker positions. Last price: ${v.lastPrice ? when(v.lastPrice) : "not received"}.`);
    // Polling must not replace a focused button or collapse expanded evidence.
    const deployments=deploymentRecords(v,input,mode);
    const key=JSON.stringify([mode,v.active,v.current,v.protection,v.pricesFresh,v.orders,v.groups.map(g=>[g.key,g.protection,g.pnl,g.legs.map(l=>[l.entry,l.mark,l.basis])]),deployments.map(d=>[d.id,d.status,d.valuationAt,d.multiplier,d.pnl,d.legs.map(l=>[l.mark,l.pnl,l.liveMark])]),v.issues,v.activity,v.drafts,input.paper?.historical_replay,input.live?.account,input.tradetron?.linked,input.tradetron?.delivery,input.tradetron?.strategy_id]);
    if(key === lastKey)return; lastKey=key;
    const expanded=new Set([...document.querySelectorAll("details[data-group][open]")].map(n=>n.dataset.group));
    const focusedGroup=document.activeElement?.tagName === "SUMMARY" ? document.activeElement.closest("details[data-group]")?.dataset.group : null;
    $("desk-attention").hidden=!v.issues.length;
    for(const id of seen.keys())if(!v.issues.some(i=>i.id===id))seen.delete(id);
    $("desk-issues").replaceChildren(...v.issues.map(issue=>{
      if(!seen.has(issue.id))seen.set(issue.id,new Date().toISOString());
      const n=node("article",null,"attention-row");n.append(node("h3",issue.title),node("p",issue.explanation),node("p",`${issue.affected} · Detected ${when(issue.at || seen.get(issue.id))}`,"issue-meta"),link(issue.href === "#kite-connection" ? "Review connection & limits →" : "Inspect position / orders →",issue.href));return n;
    }));
    const liveAccount=input.live?.account || {};
    const metric=[];
    const addMetric=(label,value,note)=>{if(finite(value))metric.push({label,value,note});};
    if(mode === "paper") {
      addMetric("Open paper profit/loss",sumKnown(v.groups.map(g=>g.pnl)),"Estimated from saved paper marks");
    } else {
      addMetric("Realised profit/loss today",liveAccount.realised_pnl,"Confirmed broker account value");
      addMetric("Unrealised profit/loss",liveAccount.unrealised_pnl,"Confirmed broker account value");
      addMetric("Daily loss limit remaining",liveAccount.daily_loss_remaining,"Enforced limit");
      addMetric("Available margin",liveAccount.available_margin,"Confirmed broker account value");
      addMetric("Margin used",liveAccount.margin_used,"Confirmed broker account value");
    }
    $("desk-risk-section").hidden=!metric.length;
    $("desk-risk").replaceChildren(...metric.map(item=>{const n=node("div");n.append(node("span",item.label),node("strong",money(item.value)),node("small",item.note));return n;}));
    text("desk-risk-note",mode === "paper" ? `Saved-paper valuation: ${when(input.paper?.valuation?.as_of)}. Charges, if present, are estimates.` : "Confirmed broker account values only.");
    const positionEmpty=()=>empty(mode === "live" ? "Broker positions unavailable" : v.current ? "No open real-quote paper executions" : "Paper positions unavailable",mode === "live" ? "An empty view does not mean your Zerodha account is flat. Broker positions and order reconciliation are not connected." : "Only executed paper quantities appear here. Historical strategy positions are shown separately.",mode === "live" ? "#kite-connection" : "#paper-trading",mode === "live" ? "Review connection limits →" : "Open paper trading →");
    $("desk-open").replaceChildren(deploymentSummary(deployments));
    $("deployment-list").replaceChildren(...(deployments.length ? deployments.map(deploymentBlock) : [empty("No recorded deployments","Live deployments will appear only after broker-position sync is available.","#kite-connection","Review connection limits →")]));
    const savedDeploymentCount=deployments.filter(record=>record.deploymentId).length;
    const historicalShown=deployments.some(record=>record.id === "historical-replay");
    text("deployments-scope",savedDeploymentCount
      ? `${savedDeploymentCount} saved paper ${savedDeploymentCount === 1 ? "deployment" : "deployments"}${historicalShown ? " · Historical reconstruction shown separately." : ""} · Live execution unavailable.`
      : historicalShown ? "No saved paper deployments · Historical reconstruction shown separately · Live execution unavailable." : "No recorded paper deployments · Live execution unavailable.");
    const ordered=v.groups.slice().sort((a,b)=>Number(b.strategy === selectedGroup)-Number(a.strategy === selectedGroup));
    const historical=mode === "paper" ? replayView(input.paper?.historical_replay) : null;
    const positionTables=[];
    if(historical)positionTables.push(replayPositions(input.paper?.historical_replay,false,"positions"));
    if(ordered.length)positionTables.push(paperPositionTable(ordered));
    if(!positionTables.length)positionTables.push(positionEmpty());
    $("position-groups").replaceChildren(...positionTables);
    $("paper-replay-positions").replaceChildren(replayPositions(input.paper?.historical_replay,false,"lab"));
    for(const d of document.querySelectorAll("details[data-group]")) {d.open=expanded.has(d.dataset.group);if(focusedGroup===d.dataset.group)d.querySelector("summary")?.focus({preventScroll:true});}
    text("positions-scope",mode === "live" ? "Live positions and orders are unavailable until broker-position sync is built." : "Paper records only · historical replay and executed paper orders are labelled separately.");
    $("orders-section").hidden=!v.orders.length;
    $("desk-orders").replaceChildren(...(v.orders.length ? [orderTable(v.orders)] : []));
    if(mode === "paper" && v.current && input.paper?.paper?.fill_count) {
      const total=input.paper.valuation, d=node("div",null,"paper-total");
      const open=sumKnown(v.groups.map(g=>g.pnl));
      d.append(node("h3","Paper results · all saved dates"),node("p",`Closed net ${money(total?.closed_net)} · Open estimate ${money(v.groups.length ? open : 0)} · Combined ${money(finite(total?.closed_net) && (!v.groups.length || finite(open)) ? total.closed_net+(open || 0) : null)}`),node("p",`Dated estimated charges included; not a broker statement. Valuation ${when(total?.as_of)}. Modelled marks never create executions.`));$("position-groups").append(d);
    }
    text("activity-scope",mode === "live" ? "LIVE · Real signals, orders and executions unavailable. No autonomous strategy runner or broker activity feed." : "PAPER · Latest 100 saved events. Simulated orders and recovery only. Historical and artificial signals stay under Research.");
    const activityEmpty=()=>empty(mode === "live" ? "Real execution history unavailable" : "No paper activity recorded","Backtests and artificial checks are not trading activity.","#research","Open Research →");
    const allActivity=activityTable(v.activity);
    $("desk-activity").replaceChildren(...(allActivity ? [allActivity] : [activityEmpty()]));
  }
  function setMode(next) { if(!["live","paper"].includes(next))return;mode=next;render(input); if(next === "live" && root.location.hash === "#paper-trading") root.location.hash="#positions-orders"; }
  $("runtime-data-close")?.addEventListener("click",()=>$("runtime-data-dialog")?.close());
  $("statistics-close")?.addEventListener("click",()=>$("deployment-statistics-dialog")?.close());
  root.TradingDesk={render,setMode};
})(typeof window === "undefined" ? {} : window);
