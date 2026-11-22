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

// Browsers (iOS above all) play sound only after a tap: the first touch anywhere unlocks the ringtone. It plays through
// an <audio> element rather than Web Audio, because iOS mutes Web Audio when the ring/silent switch is on.
const RING_RATE = 22050;
const RING_LOOP = 2.6; // seconds per repeat: the phrase twice, then a pause
const PHRASE = [[0, 1046.5], [0.16, 1318.5], [0.32, 1568], [0.48, 1318.5], [0.64, 1568], [0.8, 2093]];

// Renders a marimba-like phrase to a looping WAV, so no audio file has to ship with the page.
function ringtoneUrl() {
  const n = Math.round(RING_RATE * RING_LOOP);
  const pcm = new Float32Array(n);
  for (const offset of [0, 1.05]) {
    for (const [at, freq] of PHRASE) {
      const start = Math.round((offset + at) * RING_RATE);
      const len = Math.round(0.45 * RING_RATE);
      for (let i = 0; i < len && start + i < n; i++) {
        const t = i / RING_RATE;
        const attack = Math.min(1, t / 0.004);
        const v = Math.sin(2 * Math.PI * freq * t) * Math.exp(-t * 9)
          + 0.25 * Math.sin(2 * Math.PI * freq * 4 * t) * Math.exp(-t * 30)
          + 0.08 * Math.sin(2 * Math.PI * freq * 9.9 * t) * Math.exp(-t * 60);
        pcm[start + i] += 0.42 * attack * v;
      }
    }
  }
  const buf = new ArrayBuffer(44 + n * 2);
  const dv = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, RING_RATE, true); dv.setUint32(28, RING_RATE * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); str(36, 'data'); dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) dv.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
  return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
}

const ringtone = new Audio(ringtoneUrl());
ringtone.loop = true;
ringtone.preload = 'auto';
ringtone.setAttribute('playsinline', '');
let ringtoneUnlocked = false;

function unlockAudio() {
  if (ringtoneUnlocked || ringTimer) return;
  // Playing it once (muted) inside a tap is what lets iOS start it later from an SSE event.
  ringtone.muted = true;
  ringtone.play().then(() => {
    ringtoneUnlocked = true;
    if (!ringTimer) { ringtone.pause(); ringtone.currentTime = 0; }
    ringtone.muted = false;
  }).catch(() => { ringtone.muted = false; });
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx && !audioCtx) audioCtx = new Ctx();
    if (audioCtx?.state === 'suspended') audioCtx.resume().catch(() => {});
  } catch {}
}
addEventListener('pointerdown', unlockAudio, { passive: true });
addEventListener('keydown', unlockAudio);

// Web Audio fallback for the one repeat if the <audio> element is refused.
function beepFallback() {
  try {
    if (!audioCtx || audioCtx.state !== 'running') return;
    const t0 = audioCtx.currentTime + 0.02;
    for (const [at, freq] of PHRASE) {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + at);
      gain.gain.exponentialRampToValueAtTime(0.09, t0 + at + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.32);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t0 + at);
      osc.stop(t0 + at + 0.34);
    }
  } catch {}
}

function playRingtone() {
  ringtone.muted = false;
  ringtone.currentTime = 0;
  ringtone.play().catch(() => {
    beepFallback();
    ringTimer = setInterval(beepFallback, RING_LOOP * 1000);
  });
}

function stopRingtone() {
  ringtone.pause();
  ringtone.currentTime = 0;
}

// Keep the screen on from the moment it rings until a minute after the call ends, so the page (and the call with it)
// survives the phone going into a pocket: iOS suspends the page as soon as the screen auto-locks.
const AWAKE_AFTER_CALL_MS = 60_000;
let wakeLock = null;
let wantAwake = false;
let releaseTimer = null;

async function acquireWakeLock() {
  if (!wantAwake || wakeLock || document.visibilityState !== 'visible' || !navigator.wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (err) { console.warn('wake lock refused', err); }
}

function stayAwake() {
  clearTimeout(releaseTimer);
  releaseTimer = null;
  wantAwake = true;
  acquireWakeLock();
}

function stayAwakeForAMinute() {
  clearTimeout(releaseTimer);
  releaseTimer = setTimeout(() => {
    wantAwake = false;
    releaseTimer = null;
    wakeLock?.release().catch(() => {});
    wakeLock = null;
  }, AWAKE_AFTER_CALL_MS);
}

// The OS drops a wake lock whenever the page is hidden; take it back when the page returns.
document.addEventListener('visibilitychange', acquireWakeLock);

function startRinging(call) {
  if (live) return;
  pending = call;
  $('r-name').textContent = CALLER;
  $('r-sub').textContent = 'mobile';
  $('btn-accept').disabled = false;
  $('btn-decline').disabled = false;
  showScreen('ring');
  stayAwake();
  clearInterval(ringTimer);
  ringTimer = -1; // ringing; replaced by an interval id only if the Web Audio fallback is used
  playRingtone();
  try { navigator.vibrate?.([800, 400, 800, 400, 800, 400, 800, 400, 800, 400, 800, 400, 800]); } catch {}
}

function stopRinging(wasAnswered = false) {
  if (ringTimer > 0) clearInterval(ringTimer);
  ringTimer = null;
  stopRingtone();
  try { navigator.vibrate?.(0); } catch {}
  if (!wasAnswered) stayAwakeForAMinute();
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
  stopRinging(true);
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
    stayAwakeForAMinute();
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
