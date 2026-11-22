// Live view of the agent's order status calls, on the audience's screen.
// The page shows no call button: an order's status dot (`[data-order-call]`) or the key C starts a call, which rings
// `/phone` on a second device (P in the call shows that phone screen here instead). The view follows the transcript,
// the status steps and the location, ETA and flight the agent hears, as they come in.

const STEPS = [
  { key: 'booked', label: 'Booked' },
  { key: 'picked_up', label: 'Picked up' },
  { key: 'at_origin', label: 'Origin hub' },
  { key: 'in_transit', label: 'In transit' },
  { key: 'customs', label: 'Customs' },
  { key: 'arrived', label: 'Arrived' },
  { key: 'delivered', label: 'Delivered' },
];
const STEP_INDEX = Object.fromEntries(STEPS.map((s, i) => [s.key, i]));
STEP_INDEX.booking = 0;
STEP_INDEX.confirmed = 0;
STEP_INDEX.out_for_delivery = 5;

const STYLES = `
.oc-backdrop{position:fixed;inset:0;background:rgba(20,22,28,.48);backdrop-filter:blur(4px);z-index:200;display:grid;place-items:center;padding:20px}
.oc-backdrop[hidden]{display:none!important}
.oc-wrap{display:flex;gap:18px;align-items:stretch;width:100%;max-width:1060px;max-height:90vh}
.oc-modal{flex:1 1 auto;min-width:0;background:#fff;border:1px solid var(--line,#ebe6e1);border-radius:18px;box-shadow:0 24px 64px rgba(20,22,28,.22);display:flex;flex-direction:column;overflow:hidden}
.oc-phone-dock{width:340px;flex:none;border-radius:24px;overflow:hidden;box-shadow:0 24px 64px rgba(20,22,28,.35);background:#0b0d12;display:flex;flex-direction:column}
.oc-phone-dock[hidden]{display:none!important}
.oc-phone-dock iframe{width:100%;height:100%;min-height:600px;border:0}
.oc-head{display:flex;align-items:center;gap:14px;padding:18px 22px;border-bottom:1px solid var(--line,#ebe6e1);background:var(--sunk,#f5f2ef)}
.oc-pulse{width:11px;height:11px;border-radius:50%;background:var(--warn,#b25b00);flex:none;position:relative}
.oc-pulse.live{background:var(--ok,#17804f)}.oc-pulse.live::after,.oc-pulse.ring::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid currentColor;color:inherit;animation:oc-ping 1.5s ease-out infinite}
.oc-pulse.ring{color:var(--accent,#d95f3b);background:var(--accent,#d95f3b)}.oc-pulse.live{color:var(--ok,#17804f)}.oc-pulse.done{background:var(--line-2,#d9d2cb)}
@keyframes oc-ping{from{transform:scale(.9);opacity:.8}to{transform:scale(1.9);opacity:0}}
.oc-title{font-size:17px;font-weight:600;color:var(--ink,#14161c);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.oc-sub{font-size:13px;color:var(--muted,#8a827b);margin-top:2px}
.oc-head-actions{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:nowrap;flex:none}
.oc-chip{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:500;letter-spacing:.02em;background:#fff;border:1px solid var(--line,#ebe6e1);color:var(--ink-2,#4a4540)}
.oc-chip svg{width:12px;height:12px}
.oc-x{all:unset;cursor:pointer;width:30px;height:30px;border-radius:8px;display:grid;place-items:center;color:var(--muted,#8a827b);font-size:20px;line-height:1}
.oc-x:hover{background:rgba(0,0,0,.05);color:var(--ink,#14161c)}
.oc-pulse{cursor:default}
.oc-body{padding:20px 22px;overflow-y:auto;display:flex;flex-direction:column;gap:18px}
/* Stepper */
.oc-stepper{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;padding:14px 16px;background:var(--bg,#faf9f7);border:1px solid var(--line,#ebe6e1);border-radius:12px}
.oc-step{display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;font-size:11px;color:var(--muted,#8a827b);position:relative}
.oc-step i{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-style:normal;font-size:11px;font-weight:600;background:#fff;border:2px solid var(--line-2,#d9d2cb);color:var(--muted,#8a827b);transition:all .35s}
.oc-step.done i{background:var(--ok,#17804f);border-color:var(--ok,#17804f);color:#fff}
.oc-step.now i{background:var(--accent,#d95f3b);border-color:var(--accent,#d95f3b);color:#fff;transform:scale(1.16);box-shadow:0 0 0 5px rgba(217,95,59,.2)}
.oc-step.now{color:var(--ink,#14161c);font-weight:600}
.oc-alert{padding:11px 14px;border-radius:10px;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;font-size:13px;font-weight:500}
.oc-alert[hidden]{display:none}
/* Extracted cards */
.oc-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.oc-card{padding:12px 14px;border-radius:12px;border:1px solid var(--line,#ebe6e1);background:#fff;transition:border-color .3s,background .3s,transform .25s}
.oc-card.updated{border-color:var(--ok,#17804f);background:#f0fdf4;animation:oc-pop .6s ease}
@keyframes oc-pop{0%{transform:scale(.97)}50%{transform:scale(1.02)}100%{transform:scale(1)}}
.oc-card .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted,#8a827b)}
.oc-card .v{font-size:15px;font-weight:600;color:var(--ink,#14161c);margin-top:4px;overflow-wrap:anywhere}
.oc-notes{padding:12px 14px;border-radius:10px;background:var(--sunk,#f5f2ef);font-size:13px;color:var(--ink-2,#4a4540)}
.oc-notes[hidden]{display:none}
/* Transcript */
.oc-chat-box{border:1px solid var(--line,#ebe6e1);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.oc-chat-head{padding:9px 14px;background:var(--sunk,#f5f2ef);border-bottom:1px solid var(--line,#ebe6e1);font-size:12px;font-weight:500;color:var(--ink-2,#4a4540);display:flex;justify-content:space-between}
.oc-chat{padding:14px;display:flex;flex-direction:column;gap:8px;min-height:140px;max-height:240px;overflow-y:auto;background:#fff}
.oc-empty{color:var(--muted,#8a827b);font-size:13px;text-align:center;margin:auto}
.oc-msg{max-width:78%;padding:8px 12px;border-radius:14px;font-size:13px;line-height:1.45;animation:oc-fade .2s ease}
@keyframes oc-fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.oc-msg.agent{align-self:flex-start;background:var(--sunk,#f5f2ef);color:var(--ink,#14161c)}
.oc-msg.user{align-self:flex-end;background:var(--accent,#d95f3b);color:#fff}
.oc-msg b{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.05em;opacity:.7;margin-bottom:2px}
.oc-row-flash td{animation:oc-row 2.2s ease}
@keyframes oc-row{0%{background:#dcfce7}100%{background:transparent}}
@media (max-width:900px){.oc-wrap{flex-direction:column}.oc-phone-dock{width:100%;height:460px}.oc-cards{grid-template-columns:repeat(2,1fr)}.oc-stepper{grid-template-columns:repeat(4,1fr)}}
`;

function injectUi() {
  if (document.getElementById('oc-backdrop')) return;
  const style = document.createElement('style');
  style.textContent = STYLES;
  document.head.append(style);
  const el = document.createElement('div');
  el.id = 'oc-backdrop';
  el.className = 'oc-backdrop';
  el.hidden = true;
  el.innerHTML = `
    <div class="oc-wrap">
      <div class="oc-modal" role="dialog" aria-modal="true" aria-labelledby="oc-title">
        <div class="oc-head">
          <span class="oc-pulse ring" id="oc-pulse"></span>
          <div>
            <div class="oc-title" id="oc-title">Carrier status call</div>
            <div class="oc-sub" id="oc-sub">Dialing…</div>
          </div>
          <div class="oc-head-actions">
            <span class="oc-chip" id="oc-trigger"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 8v4l3 2"/><circle cx="12" cy="12" r="9"/></svg><span id="oc-trigger-text">Started by PIXEL</span></span>
            <button type="button" class="oc-x" id="oc-close" aria-label="Close">×</button>
          </div>
        </div>
        <div class="oc-body">
          <div class="oc-stepper" id="oc-stepper"></div>
          <div class="oc-alert" id="oc-alert" hidden></div>
          <div class="oc-cards">
            <div class="oc-card" id="oc-c-status"><div class="k">Order Status</div><div class="v" id="oc-v-status">—</div></div>
            <div class="oc-card" id="oc-c-location"><div class="k">Current Location</div><div class="v" id="oc-v-location">—</div></div>
            <div class="oc-card" id="oc-c-eta"><div class="k">Expected Arrival</div><div class="v" id="oc-v-eta">—</div></div>
            <div class="oc-card" id="oc-c-flight"><div class="k">Flight / AWB</div><div class="v" id="oc-v-flight">—</div></div>
          </div>
          <div class="oc-notes" id="oc-notes" hidden></div>
          <div class="oc-chat-box">
            <div class="oc-chat-head"><span>Live transcript</span><span id="oc-state-badge">Dialing…</span></div>
            <div class="oc-chat" id="oc-chat"><div class="oc-empty">Waiting for the carrier to pick up…</div></div>
          </div>
        </div>
      </div>
      <div class="oc-phone-dock" id="oc-phone-dock" hidden>
        <iframe id="oc-phone-frame" allow="microphone; autoplay" title="Phone"></iframe>
      </div>
    </div>`;
  document.body.append(el);
}

let activeCallId = null;
let activeOrder = null;
let stream = null;
let callTimer = null;
let pollTimer = null;
let seenTurns = new Set();
let lastUpdateSig = '';
const bc = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('pixel-voice') : null;

function renderStepper(statusKey) {
  const box = document.getElementById('oc-stepper');
  const alertEl = document.getElementById('oc-alert');
  const isIssue = statusKey === 'delayed' || statusKey === 'exception';
  alertEl.hidden = !isIssue;
  if (isIssue) {
    alertEl.textContent = statusKey === 'delayed'
      ? 'Shipment delayed — updated live from carrier phone call.'
      : 'Shipment exception reported on carrier phone call.';
  }
  const idx = STEP_INDEX[statusKey] ?? 0;
  box.innerHTML = STEPS.map((step, i) => {
    const cls = !isIssue && i < idx ? 'done' : !isIssue && i === idx ? 'now' : '';
    const mark = cls === 'done' ? '✓' : String(i + 1);
    return `<div class="oc-step ${cls}"><i>${mark}</i><span>${step.label}</span></div>`;
  }).join('');
}

function setCard(id, value, highlight = false) {
  const card = document.getElementById(`oc-c-${id}`);
  const val = document.getElementById(`oc-v-${id}`);
  if (!card || !val) return;
  val.textContent = value || '—';
  if (highlight && value) {
    card.classList.remove('updated');
    void card.offsetWidth;
    card.classList.add('updated');
  }
}

function addTurn(role, text) {
  const key = `${role}:${text}`;
  if (seenTurns.has(key)) return;
  seenTurns.add(key);
  const chat = document.getElementById('oc-chat');
  const empty = chat.querySelector('.oc-empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = `oc-msg ${role}`;
  div.innerHTML = `<b>${role === 'agent' ? 'PIXEL' : esc(activeOrder?.carrier || 'Carrier')}</b>`;
  const span = document.createElement('span');
  span.textContent = text;
  div.append(span);
  chat.append(div);
  chat.scrollTop = chat.scrollHeight;
}

function applyShipmentUpdate(p) {
  if (!p || !p.status) return;
  const sig = JSON.stringify([p.status, p.location, p.eta, p.flight_number, p.notes]);
  if (sig === lastUpdateSig) return;
  lastUpdateSig = sig;
  try { updateUnderlyingPage(p); } catch {}
  if (!activeCallId || (p.call_id && p.call_id !== activeCallId)) return;
  renderStepper(p.status);
  setCard('status', p.status_label || p.status, true);
  if (p.location) setCard('location', p.location, true);
  if (p.eta) setCard('eta', p.eta, true);
  if (p.flight_number) setCard('flight', p.flight_number, true);
  const notesEl = document.getElementById('oc-notes');
  if (p.notes) {
    notesEl.textContent = `Carrier remarks: ${p.notes}`;
    notesEl.hidden = false;
  }
  document.getElementById('oc-sub').textContent = `✓ Updated ${p.shipment_id || activeOrder?.id || ''} to "${p.status_label || p.status}" in CRM`;
}

function markLive() {
  const pulse = document.getElementById('oc-pulse');
  if (!pulse || pulse.classList.contains('done')) return;
  pulse.className = 'oc-pulse live';
  if (!lastUpdateSig && activeOrder) {
    document.getElementById('oc-sub').textContent = `Connected to ${activeOrder.carrier} operations · asking for the latest status…`;
  }
  if (!callTimer) {
    document.getElementById('oc-state-badge').textContent = 'Live call · 0:00';
    const began = Date.now();
    callTimer = setInterval(() => {
      const s = Math.floor((Date.now() - began) / 1000);
      document.getElementById('oc-state-badge').textContent = `Live call · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }, 1000);
  }
}

function markEnded(error = '') {
  clearInterval(callTimer);
  callTimer = null;
  clearInterval(pollTimer);
  pollTimer = null;
  const pulse = document.getElementById('oc-pulse');
  if (pulse) pulse.className = 'oc-pulse done';
  const badge = document.getElementById('oc-state-badge');
  if (badge) badge.textContent = error ? `Ended (${error})` : 'Call completed';
  window.pixelCallLive = false;
  setAgentCell(activeOrder?.id, 'done');
}

function handleEvent(ev) {
  if (!ev || !ev.kind) return;
  if (ev.kind === 'shipment.updated') {
    applyShipmentUpdate(ev.payload);
    return;
  }
  if (!activeCallId) return;
  const cid = ev.payload?.call_id ?? ev.id;
  if (cid !== activeCallId) return;

  if (ev.kind === 'call.started' || ev.kind === 'call.connected') {
    markLive();
  } else if (ev.kind === 'call.turn') {
    markLive();
    addTurn(ev.payload.role, ev.payload.message);
  } else if (ev.kind === 'call.answer' && ev.payload?.fields?.status) {
    applyShipmentUpdate({ call_id: cid, shipment_id: ev.payload.shipment_id, ...ev.payload.fields });
  } else if (ev.kind === 'call.ended') {
    markEnded(ev.payload?.error || '');
  }
}

function startStatePoll(callId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (activeCallId !== callId) { clearInterval(pollTimer); return; }
    try {
      const res = await fetch(`/api/calls/${callId}/state`);
      if (!res.ok) return;
      const st = await res.json();
      if (st.started) markLive();
      (st.turns || []).forEach((t) => addTurn(t.role, t.message));
      if (st.latest_update) applyShipmentUpdate(st.latest_update);
      if (st.ended) markEnded(st.error || '');
    } catch {}
  }, 1200);
}

function updateUnderlyingPage(update) {
  // Update CRM shipments table row if present
  const row = document.querySelector(`tr[data-shipment-id="${CSS.escape(update.shipment_id)}"]`);
  if (row) {
    const statusCell = row.querySelector('[data-shipment-status]');
    if (statusCell) {
      const cls = ['delayed', 'exception', 'failed'].includes(update.status) ? 'bad' : 'ok';
      const meta = [update.location, update.eta].filter(Boolean).join(' · ');
      statusCell.innerHTML = `<span class="st ${cls}">${update.status_label || update.status}</span>`
        + (meta ? `<div class="line2">${meta}</div>` : '');
    }
    row.classList.remove('oc-row-flash');
    void row.offsetWidth;
    row.classList.add('oc-row-flash');
  }
  // Update session page sidebar status if present
  const sessionStatus = document.querySelector(`[data-session-shipment-status="${CSS.escape(update.shipment_id)}"]`);
  if (sessionStatus) {
    sessionStatus.textContent = update.status_label || update.status;
  }
  const sessionMeta = document.querySelector(`[data-session-shipment-meta="${CSS.escape(update.shipment_id)}"]`);
  if (sessionMeta) {
    const parts = [update.location, update.eta, update.flight_number].filter(Boolean);
    if (parts.length) sessionMeta.textContent = parts.join(' · ');
  }
}

function ensureStream() {
  if (stream) return;
  stream = new EventSource('/api/calls/stream');
  stream.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  };
  if (bc) {
    bc.onmessage = (e) => handleEvent(e.data);
  }
}

async function startOrderCall(shipmentId) {
  injectUi();
  ensureStream();
  window.pixelCallLive = true;
  clearInterval(callTimer);
  callTimer = null;
  clearInterval(pollTimer);
  pollTimer = null;
  seenTurns = new Set();
  lastUpdateSig = '';

  const res = await fetch('/api/calls/order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shipment_id: shipmentId }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    window.pixelCallLive = false;
    alert(data.error || `Could not start call (${res.status})`);
    return;
  }

  activeCallId = data.call_id;
  activeOrder = data.order;
  startStatePoll(activeCallId);
  document.getElementById('oc-title').innerHTML =
    `<span class="mono">${esc(data.order.id)}</span> · ${esc(data.order.origin)} → ${esc(data.order.destination)} · ${esc(data.order.carrier)}`;
  document.getElementById('oc-sub').textContent = `Dialing ${data.order.carrier} operations desk…`;
  document.getElementById('oc-trigger-text').textContent = `Started by PIXEL · ${triggerReason(data.order.status)}`;
  document.getElementById('oc-pulse').className = 'oc-pulse ring';
  document.getElementById('oc-state-badge').textContent = 'Dialing…';
  document.getElementById('oc-notes').hidden = true;
  document.getElementById('oc-chat').innerHTML = '<div class="oc-empty">Waiting for the carrier to pick up…</div>';
  setAgentCell(data.order.id, 'calling');

  renderStepper(data.order.status);
  setCard('status', data.order.status_label || data.order.status);
  setCard('location', 'Asking carrier…');
  setCard('eta', 'Asking carrier…');
  setCard('flight', data.order.booking_reference || '—');

  const dock = document.getElementById('oc-phone-dock');
  dock.hidden = true;
  document.getElementById('oc-phone-frame').src = 'about:blank';

  document.getElementById('oc-backdrop').hidden = false;
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// Why the agent placed this call, as the audience reads it in the modal.
function triggerReason(status) {
  if (status === 'in_transit') return 'no flight update for 6 h';
  if (status === 'customs') return 'customs hold > 4 h';
  if (status === 'booked' || status === 'confirmed') return 'pickup window opening';
  return 'scheduled check';
}

// The order's "Agent watching" cell (`ui/routes.py` `agent_watch`) follows the call.
function setAgentCell(shipmentId, state) {
  if (!shipmentId) return;
  document.querySelectorAll(`[data-agent-watch="${CSS.escape(shipmentId)}"]`).forEach((cell) => {
    const label = cell.querySelector('.ag-label');
    const sub = cell.parentElement?.querySelector('[data-agent-sub]');
    cell.classList.toggle('calling', state === 'calling');
    if (state === 'calling') {
      if (label) label.textContent = 'Calling carrier…';
      if (sub) sub.textContent = 'Live call in progress';
    } else {
      if (label) label.textContent = 'Agent watching';
      if (sub) sub.textContent = 'Checked just now · next in 4 h';
    }
  });
}

function closeModal() {
  document.getElementById('oc-backdrop').hidden = true;
  document.getElementById('oc-phone-frame').src = 'about:blank';
  document.getElementById('oc-phone-dock').hidden = true;
  if (activeOrder && callTimer) setAgentCell(activeOrder.id, 'done');
  clearInterval(callTimer);
  callTimer = null;
  clearInterval(pollTimer);
  pollTimer = null;
  window.pixelCallLive = false;
  activeCallId = null;
}

// Presenter only: shows the phone screen next to the call, for when no second device answers it.
function togglePhone() {
  if (!activeCallId) return;
  const dock = document.getElementById('oc-phone-dock');
  const frame = document.getElementById('oc-phone-frame');
  if (dock.hidden) {
    frame.src = `/phone?call_id=${activeCallId}&embed=1`;
    dock.hidden = false;
  } else {
    frame.src = 'about:blank';
    dock.hidden = true;
  }
}

let hovered = null;

function bindEvents() {
  // Hidden triggers, nothing on screen says so: the status dot of an order (`data-order-call`), or the key C over an
  // order's row (or anywhere, for the first open order). In a call, P shows the phone screen, Esc closes.
  document.addEventListener('mouseover', (e) => {
    hovered = e.target.closest('tr[data-shipment-id], .vc-row') || hovered;
  });
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-order-call]');
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      startOrderCall(btn.dataset.orderCall);
      return;
    }
    if (e.target.closest('#oc-close')) closeModal();
  });
  document.addEventListener('dblclick', (e) => {
    if (e.target.id === 'oc-pulse') togglePhone();
  });
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest?.('input,textarea,select,[contenteditable]')) return;
    const open = !document.getElementById('oc-backdrop')?.hidden && document.getElementById('oc-backdrop');
    const key = e.key.toLowerCase();
    if (open) {
      if (key === 'escape') closeModal();
      else if (key === 'p') togglePhone();
      return;
    }
    if (key !== 'c') return;
    const target = hovered?.querySelector('.ag-watch:not(.idle) [data-order-call]')
      || document.querySelector('.ag-watch:not(.idle) [data-order-call]');
    if (!target) return;
    e.preventDefault();
    startOrderCall(target.dataset.orderCall);
  });
}

bindEvents();
ensureStream();
