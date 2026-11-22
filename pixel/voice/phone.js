// Simulated iPhone (`/phone`, markup in `phone.html`): a lock screen that rings when `/api/calls/stream` sends a
// `call.ringing` from the laptop, the incoming-call screen with Accept / Decline, and the in-call screen that runs the
// ElevenLabs conversation. Every spoken turn and `record_order_status` update goes straight back to the server for
// the laptop screen; the phone itself shows no transcript, like a real call.
const CLIENT_URL = 'https://esm.sh/@elevenlabs/client@1.35.0';
const $ = (id) => document.getElementById(id);
const bc = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('pixel-voice') : null;
const notify = (kind, payload) => { try { bc?.postMessage({ kind, payload }); } catch {} };
const CALLER = 'PIXEL Air Freight';

let pending = null;
let live = null;
let liveCallId = null;
let timer = null;
let ringTimer = null;
let audioCtx = null;
let muted = false;

// The simulated device scales to fit the window (or the presenter's dock); a real phone is the screen itself.
function fit() {
  if (document.documentElement.classList.contains('real')) return;
  const dev = $('device');
  const pad = document.body.classList.contains('embed') ? 8 : 32;
  const scale = Math.min(1, (innerWidth - pad) / dev.offsetWidth, (innerHeight - pad) / dev.offsetHeight);
  dev.style.transform = `scale(${scale})`;
}
addEventListener('resize', fit);
fit();

function updateClock() {
  const now = new Date();
  const time = now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }).replace(/\s?[AP]M$/i, '');
  $('sb-time').textContent = time;
  $('ls-time').textContent = time;
  $('ls-date').textContent = now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
}
updateClock();
setInterval(updateClock, 5000);

function showScreen(name) {
  $('v-lock').hidden = name !== 'lock';
  $('v-ring').hidden = name !== 'ring';
  $('v-call').hidden = name !== 'call';
  // iOS shows no clock in the status bar on the lock screen: the big clock is the time.
  document.body.classList.toggle('sb-hide', name === 'lock');
}
showScreen('lock');

const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

// Browsers (iOS above all) play sound only after a tap: the first touch anywhere unlocks it for the ringtone.
function unlockAudio() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  } catch {}
}
addEventListener('pointerdown', unlockAudio, { passive: true });
addEventListener('keydown', unlockAudio);

// A short marimba-like phrase, repeated while it rings.
const PHRASE = [[0, 1046.5], [0.16, 1318.5], [0.32, 1568], [0.48, 1318.5], [0.64, 1568], [0.8, 2093]];
function playRingTone() {
  try {
    unlockAudio();
    if (!audioCtx) return;
    const t0 = audioCtx.currentTime + 0.02;
    for (const [at, freq] of PHRASE) {
      for (const [mult, level] of [[1, 0.09], [4, 0.015]]) {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq * mult;
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.exponentialRampToValueAtTime(level, t0 + at + 0.008);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.32);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(t0 + at);
        osc.stop(t0 + at + 0.34);
      }
    }
  } catch { /* no sound until the first tap; the screen still rings */ }
}

function startRinging(call) {
  if (live) return;
  pending = call;
  $('r-name').textContent = CALLER;
  $('r-sub').textContent = 'mobile';
  $('btn-accept').disabled = false;
  $('btn-decline').disabled = false;
  showScreen('ring');
  clearInterval(ringTimer);
  playRingTone();
  ringTimer = setInterval(playRingTone, 1800);
  try { navigator.vibrate?.([400, 300, 400]); } catch {}
}

function stopRinging() {
  clearInterval(ringTimer);
  ringTimer = null;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) throw new Error(data.error || `Server answered ${res.status}`);
  return data;
}

async function acceptCall() {
  if (!pending || live) return;
  const call = pending;
  stopRinging();
  $('btn-accept').disabled = true;
  $('btn-decline').disabled = true;
  $('c-name').textContent = CALLER;
  $('c-timer').textContent = 'connecting…';
  setMuted(false);
  showScreen('call');

  liveCallId = call.call_id;
  let ending = 'Call Ended';
  try {
    const clientPromise = import(CLIENT_URL).catch(() => { throw new Error('Voice client could not load.'); });
    const started = await post(`/api/calls/${call.call_id}/accept`, {});
    const lib = await clientPromise;
    const Conversation = lib.VoiceConversation ?? lib.Conversation;

    const handleTool = async (fields) => {
      try {
        const updated = await post(`/api/calls/${call.call_id}/answer`, fields);
        const label = updated.status_label || updated.status || 'Updated';
        notify('shipment.updated', updated);
        return `Saved status ${label}.`;
      } catch (err) {
        return `Not saved: ${err.message}`;
      }
    };

    await new Promise((resolve, reject) => {
      Conversation.startSession({
        signedUrl: started.signed_url,
        dynamicVariables: started.dynamic_variables,
        clientTools: {
          record_order_status: handleTool,
          record_carrier_offer: handleTool,
          record_customer_details: handleTool,
        },
        onConnect: ({ conversationId }) => {
          const began = Date.now();
          $('c-timer').textContent = fmt(0);
          timer = setInterval(() => { $('c-timer').textContent = fmt(Math.floor((Date.now() - began) / 1000)); }, 1000);
          notify('call.connected', { call_id: call.call_id, conversation_id: conversationId });
          post(`/api/calls/${call.call_id}/connected`, { conversation_id: conversationId }).catch(() => {});
        },
        onMessage: ({ source, message }) => {
          const role = source === 'ai' ? 'agent' : 'user';
          notify('call.turn', { call_id: call.call_id, role, message });
          post(`/api/calls/${call.call_id}/turn`, { role, message }).catch(() => {});
        },
        onError: (msg) => console.warn('call error', msg),
        onDisconnect: () => resolve(),
      }).then((conv) => { live = conv; if (muted) conv.setMicMuted?.(true); }, reject);
    });
    notify('call.ended', { call_id: call.call_id });
    await post(`/api/calls/${call.call_id}/ended`, {}).catch(() => {});
    setTimeout(() => post(`/api/calls/${call.call_id}/transcript`, {}).catch(() => {}), 4500);
  } catch (err) {
    const msg = err.name === 'NotAllowedError'
      ? 'Microphone blocked. Allow mic access (HTTPS required on mobile).'
      : err.message;
    console.warn(msg);
    ending = 'Call Failed';
    post(`/api/calls/${call.call_id}/ended`, { error: msg }).catch(() => {});
  } finally {
    clearInterval(timer);
    timer = null;
    live = null;
    liveCallId = null;
    pending = null;
    $('c-timer').textContent = ending;
    await new Promise((r) => setTimeout(r, 1600));
    showScreen('lock');
  }
}

async function declineCall() {
  if (!pending) return;
  const callId = pending.call_id;
  stopRinging();
  pending = null;
  showScreen('lock');
  post(`/api/calls/${callId}/ended`, { error: 'Declined on phone' }).catch(() => {});
}

function setMuted(on) {
  muted = on;
  $('k-mute').classList.toggle('on', on);
  try { live?.setMicMuted?.(on); } catch {}
}

$('btn-accept').addEventListener('click', acceptCall);
$('btn-decline').addEventListener('click', declineCall);
$('btn-end').addEventListener('click', () => { $('c-timer').textContent = 'Call Ended'; live?.endSession(); });
$('k-mute').querySelector('button').addEventListener('click', () => setMuted(!muted));
for (const key of document.querySelectorAll('.key[data-toggle]:not(#k-mute)')) {
  key.querySelector('button').addEventListener('click', () => key.classList.toggle('on'));
}

// Connect SSE to receive incoming calls triggered from the laptop screen.
const es = new EventSource('/api/calls/stream');
es.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  if (ev.kind === 'call.ringing') {
    startRinging({ call_id: ev.id, order: ev.payload.order, variables: ev.payload.variables });
  } else if (ev.kind === 'call.started' && pending && ev.payload.call_id === pending.call_id && liveCallId !== pending.call_id) {
    // Picked up on another screen
    stopRinging();
    pending = null;
    showScreen('lock');
  } else if (ev.kind === 'call.ended' && pending && ev.payload.call_id === pending.call_id && liveCallId !== pending.call_id) {
    stopRinging();
    pending = null;
    showScreen('lock');
  }
};

// Check if a call is already ringing right now (e.g. opened via /phone?call_id=...).
const initialId = document.body.dataset.initialCall;
fetch(initialId ? `/api/calls/ringing?call_id=${encodeURIComponent(initialId)}` : '/api/calls/ringing')
  .then((r) => r.json())
  .then((data) => { if (data && data.call_id) startRinging(data); })
  .catch(() => {});
