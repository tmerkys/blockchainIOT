// Defence demo page. Plain JS: one SSE stream in, a few POSTs out.
// The page is Lithuanian; the terminal (log panel) stays English on purpose.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let STATE = null;
let REF = null;
let LAST_SIGNING = null;   // mean signing time of the last channel run
let CONNECTED = false;     // SSE stream open
let PENDING = null;        // action clicked, server has not reported it busy yet
let PREV_BUSY = null;

// ---------- formatting ----------------------------------------------------
// Page numbers in lt-LT ("1 920", "4,7"); terminal numbers in en-US ("1,920").
const num = (v, d = 0) => Number(v).toLocaleString("lt-LT", { maximumFractionDigits: d });
const numEn = (v, d = 0) => Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const ms = (v) => (v == null ? "–" : `${num(v)} ms`);
const ms1 = (v) => (v == null ? "–" : `${num(v, 1)} ms`);
const gas = (v) => (v == null ? "–" : num(v));
function toGwei(wei) {
  try { return Number(BigInt(wei)) / 1e9; } catch { return null; }
}
function gweiWith(fmt, wei) {
  const g = wei == null || wei === "" ? null : toGwei(wei);
  return g == null ? "–" : `${fmt(g, g < 10 ? 2 : 0)} gwei`;
}
const gwei = (wei) => gweiWith(num, wei);
const gweiEn = (wei) => gweiWith(numEn, wei);
const short = (h) => (h ? `${h.slice(0, 6)}…${h.slice(-4)}` : "–");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const reason = (r) => esc(String(r || "").replace(/^execution reverted:\s*/, "").split(":")[0]);
function link(kind, h, text) {
  if (!h) return "–";
  const ex = STATE?.info?.explorer;
  const t = esc(text || short(h));
  return ex ? `<a href="${ex}/${kind}/${esc(h)}" target="_blank" rel="noopener">${t} ↗</a>` : `<span class="mono">${t}</span>`;
}
const tx = (h, text) => link("tx", h, text);
const WHERE = {
  "Raspberry Pi": "Raspberry Pi", "this computer": "šis kompiuteris",
  before: "prieš demonstraciją", live: "gyvai",
  "opened before the demo": "prieš demonstraciją", "opened live": "gyvai",
};
const where = (w) => esc(WHERE[w] || w || "");

function announce(text) {        // one polite screen-reader line per outcome
  const sr = $("#sr");
  sr.textContent = "";
  requestAnimationFrame(() => (sr.textContent = text));
}

// ---------- tabs (WAI-ARIA tabs; ←/→ and 1–3 switch) ------------------------
const TABS = $$(".tabs [role=tab]");
function selectTab(tab, focus = false) {
  TABS.forEach((t) => {
    const on = t === tab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    $(`#${t.getAttribute("aria-controls")}`).classList.toggle("active", on);
  });
  if (focus) tab.focus();
}
TABS.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t));
  t.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (step) { e.preventDefault(); selectTab(TABS[(i + step + TABS.length) % TABS.length], true); }
  });
});
document.addEventListener("keydown", (e) => {
  if (e.altKey || e.ctrlKey || e.metaKey || e.target.closest("select, input, textarea")) return;
  const t = TABS[Number(e.key) - 1];
  if (t) selectTab(t, true);
});

// ---------- terminal ------------------------------------------------------
const logEl = $("#log");
function log(text, cls = "") {
  const stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 24; // don't yank a reader back down
  const line = document.createElement("span");
  if (cls) line.className = cls;
  line.textContent = `[${new Date().toTimeString().slice(0, 8)}] ${text}\n`;
  logEl.appendChild(line);
  while (logEl.childNodes.length > 300) logEl.removeChild(logEl.firstChild);
  if (stick) logEl.scrollTop = logEl.scrollHeight;
}
$("#btnLogToggle").addEventListener("click", (e) => {
  const sec = $(".log");
  const collapsed = sec.classList.toggle("collapsed");
  e.currentTarget.textContent = collapsed ? "show" : "hide";
  e.currentTarget.setAttribute("aria-expanded", !collapsed);
});

const SAY = {
  tick: (e) => `meter tick${e.kwh ? `: ${numEn(e.kwh, 4)} kWh` : ""}`,
  signed: (e) => `voucher signed: nonce ${e.nonce}, cumulative ${gweiEn(e.amount)}, ${numEn(e.signing_ms, 1)} ms`,
  sent: (e) => `POST /voucher → gateway ${e.gateway}`,
  settled: (e) => `settled: block ${e.blockNumber}, gas ${numEn(e.gasUsed)}, ${numEn(e.latency_ms)} ms, tx ${short(e.txHash)}`,
  "ch.progress": (e) => (e.signed % 50 === 0 ? `${e.signed}/${e.ticks} vouchers signed off-chain` : null),
  "ch.signed": (e) => `all ${e.ticks} signed, ${numEn(e.signing_ms_total)} ms of signing in total`,
  "ch.closing": () => "sending the last voucher only: close()",
  "ch.closed": (e) => `close() mined: gas ${numEn(e.gasUsed)}, tx ${short(e.txHash)}`,
  "ch.opened": (e) => `channel opened: ${short(e.channel)}`,
  "pay.money": (e) => `payout: ${gweiEn(e.amount)} from ${short(e.funder)}'s deposit to merchant ${short(e.merchant)}`,
  "ch.money": (e) => `payout: ${gweiEn(e.paid)} from ${short(e.funder)}'s deposit to merchant ${short(e.merchant)}, ${gweiEn(e.refund)} refunded`,
  "atk.start": (e) => `attack: gateway signs in the device slot, wants ${gweiEn(e.target)}`,
  "atk.api": (e) => `gateway API answered HTTP ${e.status}: ${e.reason}`,
  "atk.estimate": (e) => `estimate_gas ${e.refused ? "refused" : "ACCEPTED"}: ${e.reason}; ignoring it`,
  "atk.forced": (e) => `forced tx sent: ${short(e.txHash)}`,
  "atk.mined": (e) => `mined, status ${e.status}: ${e.reason}`,
  "pi.hello": (e) => `connected to ${e.host} (${e.machine}, ${e.system}), device ${e.device}`,
  log: (e) => e.text,
  error: (e) => `ERROR: ${e.message}`,
  ready: (e) => `mode ready: ${e.mode}`,
};
function describe(ev) {
  const fn = SAY[ev.type];
  const text = fn && fn(ev);
  if (!text) return null;
  let who = "device", cls = "";
  if (ev.source === "pi") { who = "pi"; cls = "pi"; }
  else if (ev.flow === "attack") { who = "gateway"; cls = "gw"; }
  else if (ev.flow === "mode" || ev.flow === "state") { who = "system"; cls = "sys"; }
  else if (ev.type === "ch.money" || ev.type === "pay.money") { who = "chain"; cls = "sys"; }
  if (ev.type === "error") cls = "err";
  return { text: `${who.padEnd(8)}${text}`, cls };
}

// ---------- state / header ------------------------------------------------
const BADGE = { pi: "GYVAI · Raspberry Pi", laptop: "GYVAI · įrenginys kompiuteryje" };
const MODE_NAME = { pi: "Raspberry Pi", laptop: "Kompiuteris" };
const ACTIONS = ["#btnPay", "#btnChannel", "#btnAttack", "#btnOpen"];

const GAS_FLOOR = 2000000000000n; // ~4 settlements' fees on Base Sepolia
function big(v) { try { return BigInt(v); } catch { return null; } }
function lowFunds(info) {          // the EV owner cannot fund another demo channel
  const have = big(info.funderBalance), need = big(info.demoDeposit);
  return have != null && need != null && have < need;
}
function lowGas(info) {            // the gateway cannot pay for transactions much longer
  const have = big(info.gatewayBalance);
  return have != null && have < GAS_FLOOR;
}

function renderState(st) {
  STATE = st;
  const info = st.info || {};
  const badge = $("#badge");
  badge.className = "badge";
  if (!CONNECTED) { badge.classList.add("failed"); badge.textContent = "Neprisijungta"; }
  else if (st.status === "starting") badge.textContent = "Paleidžiama…";
  else if (st.status === "failed") { badge.classList.add("failed"); badge.textContent = "Nepavyko"; }
  else { badge.classList.add("live"); badge.textContent = BADGE[info.mode] || info.mode; }
  $("#modeSelect").value = info.mode || "pi";

  const banner = $("#banner"), msg = $("#bannerText"), retry = $("#btnRetry");
  retry.hidden = true;
  if (!CONNECTED) {
    banner.className = "banner";
    msg.textContent = "Nėra ryšio su demo serveriu. Jungiamasi iš naujo…";
  } else if (st.status === "failed") {
    banner.className = "banner";
    msg.textContent = `Režimas nepasileido: ${st.error || "nežinoma klaida"}`;
    retry.hidden = false;
  } else if (lowFunds(info)) {
    banner.className = "banner warn";
    msg.textContent = `EV savininko balanso (${gwei(info.funderBalance)}) neužteks naujam kanalui (reikia ${gwei(info.demoDeposit)}). Papildykite ${short(info.funder)} iš faucet.`;
  } else if (lowGas(info)) {
    banner.className = "banner warn";
    msg.textContent = `Gateway balansas mažas (${gwei(info.gatewayBalance)}): gali neužtekti gas. Papildykite ${short(info.gateway)} iš faucet.`;
  } else banner.className = "banner hidden";

  const f = [];
  if (info.chainName) f.push(`<b>${esc(info.chainName)}</b>`);
  if (info.device) f.push(`įrenginys ${link("address", info.device)}${info.deviceWhere ? ` (${where(info.deviceWhere)})` : ""}`);
  if (info.funderBalance && info.funder !== info.gateway) f.push(`EV savininkas <b>${gwei(info.funderBalance)}</b>`);
  if (info.gatewayBalance) f.push(`gateway <b>${gwei(info.gatewayBalance)}</b>`);
  $("#facts").innerHTML = f.join(" · ") || "&nbsp;";

  const blocked = !CONNECTED || !!PENDING || !!st.busy || st.status !== "ready";
  ACTIONS.forEach((s) => ($(s).disabled = blocked));
  const needsNewChannel = !info.demoChannel || info.demoChannel.closed || Number(info.demoChannel.nonce) > 0;
  if (!blocked && lowFunds(info)) {
    $("#btnOpen").disabled = true;
    if (needsNewChannel) $("#btnChannel").disabled = true;
  }
  $("#modeSelect").disabled = !CONNECTED || !!PENDING || !!st.busy;
  $("#btnOpenCost").textContent = info.demoDeposit ? `· ${gwei(info.demoDeposit)}` : "";
  $("#logTitle").textContent = info.mode === "pi" ? "device & gateway log · green = Raspberry Pi over SSH" : "device & gateway log";

  // An action ended without its final event (server restart, lost stream): stop its spinners.
  if (PREV_BUSY && !st.busy) markStale(PREV_BUSY);
  PREV_BUSY = st.busy;

  renderChannelLine(info.demoChannel);
  renderRef();
}
function rerender() { if (STATE) renderState(STATE); }

// One action at a time: buttons lock on click, before the server's busy event.
async function act(name, url, onFail) {
  PENDING = name;
  rerender();
  let error = null;
  try {
    const r = await fetch(url, { method: "POST" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      error = r.status === 409 ? `busy: ${body.detail || "another action is running"}` : body.detail || `HTTP ${r.status}`;
    }
  } catch {
    error = "demo server unreachable";
  }
  PENDING = null;
  if (error) {
    log(`ERROR: ${error}`, "err");
    onFail?.(error);
  }
  rerender();
  return !error;
}

$("#modeSelect").addEventListener("change", (e) => {
  const key = e.target.value;
  const ok = confirm(`Perjungti į „${MODE_NAME[key]}“? Gateway bus paleistas iš naujo, gali būti atidaryti nauji kanalai.`);
  if (!ok) { e.target.value = STATE?.info?.mode || "pi"; return; }
  act("mode", `/api/mode/${key}`);
});
$("#btnRetry").addEventListener("click", () => act("mode", `/api/mode/${STATE?.info?.mode || $("#modeSelect").value}`));

// ---------- 1. one payment ------------------------------------------------
const SEG = [
  ["signing_ms", "parašas", "--s1"],
  ["network_ms", "tinklas", "--s2"],
  ["build_ms", "gateway", "--s3"],
  ["inclusion_ms", "laukimas bloko", "--s4"],
  ["other_ms", "kita", "--s5"],
];
function step(name, cls, text) {
  const li = $(`#pipeline li[data-step="${name}"]`);
  li.className = cls || "";
  if (text !== undefined) $("em", li).textContent = text;
}
function resetPipeline() {
  $$("#pipeline li").forEach((li) => { li.className = ""; $("em", li).textContent = ""; });
  $("#latTotal").textContent = "…";
  $("#latBar").innerHTML = "";
  $("#latLegend").innerHTML = "";
  $("#payFacts").innerHTML = "";
  $("#payMoney").innerHTML = "";
}
function renderLatency(ev) {
  const total = ev.latency_ms;
  $("#latTotal").textContent = ms(total);
  if (!total) return;
  const known = ["signing_ms", "network_ms", "build_ms", "inclusion_ms"].reduce((a, k) => a + Math.max(0, ev[k] || 0), 0);
  const v = { ...ev, network_ms: Math.max(0, ev.network_ms || 0), other_ms: Math.max(0, total - known) };
  const sum = SEG.reduce((a, [k]) => a + Math.max(0, v[k] || 0), 0) || 1;
  $("#latBar").innerHTML = SEG.map(([k, , c]) =>
    `<div style="width:${(100 * Math.max(0, v[k] || 0)) / sum}%;background:var(${c})"></div>`).join("");
  $("#latLegend").innerHTML = SEG.filter(([k]) => (v[k] || 0) >= 1).map(([k, label, c]) =>
    `<span><i style="background:var(${c})"></i>${label} <b>${ms(v[k])}</b></span>`).join("");
}
function payFailed(message) {
  $$("#pipeline li.active").forEach((li) => (li.className = "fail"));
  $("#latTotal").textContent = "klaida";
  $("#payFacts").textContent = message;
  announce(`Mokėjimas nepavyko: ${message}`);
}
function onPayment(ev) {
  switch (ev.type) {
    case "tick": resetPipeline(); step("tick", "done", ev.kwh ? `${num(ev.kwh, 3)} kWh` : "✓"); step("signed", "active"); break;
    case "signed": step("signed", "done", ms1(ev.signing_ms)); step("gateway", "active"); break;
    case "sent": step("broadcast", "active"); step("settled", "active"); break;
    case "settled":
      step("gateway", "done", ev.network_ms != null && ev.network_ms < 1 ? "< 1 ms" : ms(ev.network_ms));
      step("broadcast", "done", ms(ev.build_ms));
      step("settled", "done", ms(ev.inclusion_ms));
      renderLatency(ev);
      $("#payFacts").innerHTML = [
        `gas <b>${gas(ev.gasUsed)}</b>`,
        `gas mokestis <b>${gwei(ev.fee_wei)}</b> (moka gateway)`,
        `blokas <b>${esc(ev.blockNumber)}</b>`,
        tx(ev.txHash, "BaseScan"),
      ].join('<span class="sep" aria-hidden="true">|</span>');
      announce(`Įrašyta į bloką per ${ms(ev.latency_ms)}`);
      break;
    case "pay.money": {
      const owner = ev.funderIsGateway ? "Gateway užstatas" : "EV savininko užstatas";
      $("#payMoney").innerHTML = `${owner} ${link("address", ev.funder)} <span class="arrow" aria-hidden="true">→</span>`
        + ` prekybininkui ${link("address", ev.merchant)}: <b>${gwei(ev.amount)}</b>`
        + `<span class="sep" aria-hidden="true">|</span>užstate liko ${gwei(ev.left)}`;
      break;
    }
    case "error": payFailed(ev.message); break;
  }
}
$("#btnPay").addEventListener("click", () => {
  resetPipeline();
  step("tick", "active");
  act("payment", "/api/pay", payFailed);
});

function renderRef() {
  const pick = REF?.latency?.find((r) => r.chain === "base-sepolia" && r.transport === "wifi" && r.config === "controlled")
    || REF?.latency?.find((r) => r.chain === "base-sepolia");
  $("#latRef").textContent = pick
    ? `Tyrimo rezultatas (Base, ${pick.transport === "eth" ? "Ethernet" : "WiFi"}, N = ${pick.n}): ${ms(pick.mean)} ± ${ms(pick.ci95)}`
    : "";
}

// ---------- 2. channel ----------------------------------------------------
function renderChannelLine(ch) {
  const el = $("#chLine");
  if (!ch) { el.textContent = "Kanalo dar nėra: jis bus atidarytas paspaudus „200 mokėjimų“."; return; }
  const state = ch.closed ? "uždarytas, kitas paleidimas atidarys naują"
    : Number(ch.nonce) > 0 ? "jau naudotas, kitas paleidimas atidarys naują"
    : `atidarytas ${where(ch.where)}`;
  el.innerHTML = `Kanalas ${link("address", ch.channel)} · užstatas <b>${gwei(ch.deposit)}</b> · ${state}`;
}
function resetChannel() {
  $("#cSigned").textContent = "0"; $("#cTx").textContent = "0";
  $("#chProgress").style.width = "0";
  $("#chProgress").parentElement.setAttribute("aria-valuenow", "0");
  $("#chResult").classList.add("hidden");
  $("#chMoney").classList.add("hidden");
  $("#chError").textContent = "";
}

// Who paid whom, each with its BaseScan page: the EV owner's deposit, the merchant, the gateway.
function delta(before, after) {
  const d = big(after) - big(before);
  return `${d >= 0n ? "+" : "−"}${gwei((d < 0n ? -d : d).toString())}`;
}
function renderMoney(ev) {
  const owner = ev.funderIsGateway ? "Gateway (įnešė užstatą)" : "EV savininkas";
  $("#chMoney").innerHTML = `
    <div class="who">
      <small>${owner}</small>${link("address", ev.funder)}
      <b>užstatas ${gwei(ev.deposit)}</b>
      <span>grąžinta ${gwei(ev.refund)}${ev.openTx ? ` · ${tx(ev.openTx, "open()")}` : ""}</span>
      <em class="out">−${gwei(ev.paid)}</em>
    </div>
    <div class="flow" aria-hidden="true"><span>${gwei(ev.paid)}</span>→</div>
    <div class="who">
      <small>Prekybininkas</small>${link("address", ev.merchant)}
      <b>${gwei(ev.merchant_before)} → ${gwei(ev.merchant_after)}</b>
      <span>gavo per close()</span>
      <em class="in">${delta(ev.merchant_before, ev.merchant_after)}</em>
    </div>
    <div class="who gw">
      <small>Gateway · tik gas</small>${link("address", ev.gateway)}
      <b>${gwei(ev.gateway_before)} → ${gwei(ev.gateway_after)}</b>
      <span>${ev.funderIsGateway ? "įskaitant grąžintą užstatą" : "moka už transakcijas"}</span>
      <em>${delta(ev.gateway_before, ev.gateway_after)}</em>
    </div>`;
  $("#chMoney").classList.remove("hidden");
  announce(`Prekybininkas gavo ${gwei(ev.paid)}`);
}
function channelFailed(message) {
  $("#chResult").classList.add("hidden");
  $("#chError").textContent = `Nepavyko: ${message}`;
  announce(`Kanalas nepavyko: ${message}`);
}
function onChannel(ev) {
  switch (ev.type) {
    case "ch.progress":
      $("#cSigned").textContent = ev.signed;
      $("#chProgress").style.width = `${(100 * ev.signed) / ev.ticks}%`;
      $("#chProgress").parentElement.setAttribute("aria-valuenow", Math.round((100 * ev.signed) / ev.ticks));
      break;
    case "ch.signed": LAST_SIGNING = ev.signing_ms_mean; break;
    case "ch.closed": {
      $("#cTx").textContent = "1";
      const settle = STATE?.info?.settleGas || 75029;
      const without = settle * ev.ticks;
      $("#cmpA").textContent = `${ev.ticks} × settle() = ${gas(without)} gas`;
      $("#cmpB").textContent = `1 × close() = ${gas(ev.gasUsed)} gas`;
      $("#cmpBarB").style.width = "0";
      $("#chResult").classList.remove("hidden");
      requestAnimationFrame(() => requestAnimationFrame(() => ($("#cmpBarB").style.width = `${(100 * ev.gasUsed) / without}%`)));
      $("#chBig").textContent = `${num(without / ev.gasUsed)}× mažiau gas`;
      const parts = [];
      if (LAST_SIGNING != null) parts.push(`parašas <b>${ms1(LAST_SIGNING)}</b> / čekis`);
      parts.push(tx(ev.txHash, "close() BaseScan"));
      $("#chFacts").innerHTML = parts.join('<span class="sep" aria-hidden="true">|</span>');
      announce(`${ev.ticks} mokėjimų, viena transakcija, ${num(without / ev.gasUsed)} kartų mažiau gas`);
      break;
    }
    case "ch.money": renderMoney(ev); break;
    case "ch.opened": renderChannelLine(ev); resetChannel(); announce("Naujas kanalas atidarytas"); break;
    case "error": channelFailed(ev.message); break;
  }
}
$("#btnChannel").addEventListener("click", () => { resetChannel(); act("channel", "/api/channel/run?ticks=200", channelFailed); });

// "Naujas kanalas" locks a deposit: the first click arms it, a second within 4 s confirms.
let armTimer = null;
$("#btnOpen").addEventListener("click", (e) => {
  const b = e.currentTarget;
  if (!b.classList.contains("armed")) {
    b.classList.add("armed");
    $("#btnOpenLabel").textContent = "Patvirtinti";
    armTimer = setTimeout(disarm, 4000);
    return;
  }
  disarm();
  act("channel", "/api/channel/open", channelFailed);
});
function disarm() {
  clearTimeout(armTimer);
  $("#btnOpen").classList.remove("armed");
  $("#btnOpenLabel").textContent = "Naujas kanalas";
}

// ---------- 3. attack -----------------------------------------------------
function atk(k, cls, html) {
  const li = $(`#atkSteps li[data-k="${k}"]`);
  li.className = cls;
  if (html !== undefined) $("span", li).innerHTML = html;
}
function resetAttack() {
  $$("#atkSteps li").forEach((li) => { li.className = ""; $("span", li).innerHTML = ""; });
  $("#atkOut").classList.add("hidden");
  atk("start", "active");
}
function attackFailed(message) {
  const active = $$("#atkSteps li.active");
  (active.length ? active : [$("#atkSteps li")]).forEach((li) => { li.className = "bad"; $("span", li).textContent = message; });
  announce(`Ataka nutrūko: ${message}`);
}
function bal(label, before, after) {
  const same = before === after;
  return `<div class="bal"><small>${label}</small><b>${gwei(before)} → ${gwei(after)}</b>${same ? "<em>nepakito</em>" : ""}</div>`;
}
function onAttack(ev) {
  switch (ev.type) {
    case "atk.start":
      atk("start", "atk", `<span class="tag">suklastota</span>nori <b>${gwei(ev.target)}</b>`);
      atk("api", "active");
      break;
    case "atk.api":
      if (ev.submitted) atk("api", "bad", `<span class="tag">PRALEISTA</span>`);
      else if (ev.status == null) atk("api", "bad", `API nepasiekiama: ${esc(ev.reason)}`);
      else atk("api", "ok", `<span class="tag">atmesta</span>HTTP ${esc(ev.status)} · ${reason(ev.reason)}`);
      atk("forced", "active", "mazgas įspėja, kad bus atmesta…");
      break;
    case "atk.estimate":
      atk("forced", "active", ev.refused ? "mazgas įspėja, kad bus atmesta → ignoruoja" : "mazgas nieko neįspėja");
      break;
    case "atk.forced":
      atk("forced", "atk", `<span class="tag">išsiųsta</span>įspėjimas ignoruotas · ${tx(ev.txHash)}`);
      atk("mined", "active", "laukiama bloko…");
      break;
    case "atk.mined":
      atk("mined", ev.blocked ? "ok" : "bad",
        `<span class="tag">${ev.status === 0 ? "atmesta" : "ĮVYKDYTA"}</span>${reason(ev.reason)} · blokas ${esc(ev.blockNumber)} · ${tx(ev.txHash)}`);
      $("#atkBalances").innerHTML = bal("Kontrakto balansas (užstatai)", ev.escrow_before, ev.escrow_after)
        + bal("Prekybininko balansas", ev.merchant_before, ev.merchant_after);
      $("#atkVerdict").className = `verdict${ev.blocked ? "" : " bad"}`;
      $("#atkVerdict").textContent = ev.blocked ? "✓ Be įrenginio parašo lėšos nepajudėjo" : "✗ LĖŠOS PAJUDĖJO";
      $("#atkOut").classList.remove("hidden");
      break;
    case "error": attackFailed(ev.message); break;
  }
}
$("#btnAttack").addEventListener("click", () => { resetAttack(); act("attack", "/api/attack", attackFailed); });

function markStale(flow) {
  const sel = { payment: "#pipeline li.active", channel: null, attack: "#atkSteps li.active" }[flow];
  if (!sel || !$$(sel).length) return;
  if (flow === "payment") payFailed("veiksmas nutrūko");
  if (flow === "attack") attackFailed("veiksmas nutrūko");
}

// ---------- event stream --------------------------------------------------
function handle(ev) {
  if (ev.type === "state") { renderState(ev.state); return; }
  if (ev.type === "busy") { if (STATE) renderState({ ...STATE, busy: ev.busy }); return; }
  const d = describe(ev);
  if (d) log(d.text, d.cls);
  if (ev.flow === "payment") onPayment(ev);
  else if (ev.flow === "channel") onChannel(ev);
  else if (ev.flow === "attack") onAttack(ev);
}
function connect() {
  const es = new EventSource("/api/events");   // reconnects by itself; the server re-sends state
  es.onopen = () => {
    if (!CONNECTED) log("connected to the demo server", "sys");
    CONNECTED = true;
    rerender();
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    handle(ev);
  };
  es.onerror = () => {
    if (CONNECTED) log("lost connection to the demo server; retrying…", "err");
    CONNECTED = false;
    rerender();
  };
}
// ?snapshot=1&tab=channel renders recent history once, without the live stream
// (for screenshots: a headless browser never finishes loading an SSE page).
async function snapshot(params) {
  CONNECTED = true;
  renderState(await (await fetch("/api/state")).json());
  for (const ev of await (await fetch("/api/history")).json()) if (ev.type !== "state" && ev.type !== "busy") handle(ev);
  const tab = TABS.find((t) => t.dataset.tab === params.get("tab"));
  if (tab) selectTab(tab);
}
(async () => {
  try { REF = await (await fetch("/api/reference")).json(); } catch { REF = null; }
  renderRef();
  const params = new URLSearchParams(location.search);
  if (params.has("snapshot")) snapshot(params); else connect();
})();
