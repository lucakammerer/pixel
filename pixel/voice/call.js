// One browser voice call with the ElevenLabs agent, started from a session page's Call panel (`ui/routes.py`
// `_calls_panel`). The server signs the session and says what to ask (`voice/calls.py` `start`); the agent's client
// tools post what it heard straight to the server, so the bid or the answer is in the thread while the call goes on.
const CLIENT_URL = 'https://esm.sh/@elevenlabs/client@1.35.0';
const panel = document.getElementById('voice-calls');
const $ = (id) => document.getElementById(id);
let live = null;
let timer = null;

const say = (text, tone = '') => {
  const status = $('call-status');
  status.textContent = text;
  status.className = `vc-status ${tone}`;
  status.hidden = !text;
};
const bubble = (role, text) => {
  const log = $('call-log');
  const msg = document.createElement('div');
  msg.className = `vc-msg ${role}`;
  msg.textContent = text;
  log.hidden = false;
  log.append(msg);
  log.scrollTop = log.scrollHeight;
};
const liveBar = (name, state) => {
  const bar = $('call-live');
  bar.hidden = !state;
  bar.classList.toggle('idle', state !== 'connected');
  $('call-with').textContent = state === 'connected' ? `On the call with ${name}` : `Calling ${name}…`;
};
const startTimer = () => {
  const began = Date.now();
  timer = setInterval(() => {
    const s = Math.floor((Date.now() - began) / 1000);
    $('call-timer').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }, 1000);
};

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok && response.status !== 202) throw new Error(data.error || `Server answered ${response.status}`);
  return { status: response.status, data };
}

// ElevenLabs needs a few seconds after the call to finish the transcript and summary.
async function keepTranscript(callId) {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 4000));
    try {
      const { status: code, data } = await post(`/api/calls/${callId}/transcript`);
      if (code === 200) return data;
    } catch { return null; }
  }
  return null;
}

async function call(button) {
  if (live) return;
  const name = button.dataset.callName;
  const row = button.closest('.vc-row');
  const buttons = panel.querySelectorAll('[data-call-email]');
  buttons.forEach((b) => { b.disabled = true; });
  row.classList.add('on');
  $('call-log').replaceChildren();
  $('call-log').hidden = true;
  $('call-timer').textContent = '0:00';
  say('');
  liveBar(name, 'calling');
  window.pixelCallLive = true; // the session page does not reload while a call is going on
  let callId = null;
  let saved = false;
  try {
    const client = import(CLIENT_URL).catch(() => { throw new Error('The voice client could not be loaded.'); });
    const started = (await post('/api/calls', {
      session_id: Number(panel.dataset.session), email: button.dataset.callEmail,
    })).data;
    callId = started.call_id;
    const lib = await client;
    const Conversation = lib.VoiceConversation ?? lib.Conversation;
    const save = async (fields) => {
      try {
        await post(`/api/calls/${callId}/answer`, fields);
        saved = true;
        say('✓ Saved to the request. PIXEL reads it like an emailed reply.', 'ok');
        return 'Saved.';
      } catch (error) {
        say(`Not saved: ${error.message}`, 'bad');
        return `Not saved: ${error.message}`;
      }
    };
    $('call-hang-up').onclick = () => { live?.endSession(); };
    await new Promise((resolve, reject) => {
      Conversation.startSession({
        signedUrl: started.signed_url,
        dynamicVariables: started.dynamic_variables,
        clientTools: { record_carrier_offer: save, record_customer_details: save },
        onConnect: ({ conversationId }) => {
          liveBar(name, 'connected');
          startTimer();
          post(`/api/calls/${callId}/connected`, { conversation_id: conversationId }).catch(() => {});
        },
        onMessage: ({ source, message }) => bubble(source === 'ai' ? 'agent' : 'user', message),
        onError: (message) => say(String(message), 'bad'),
        onDisconnect: () => resolve(),
      }).then((conversation) => { live = conversation; }, reject);
    });
    await post(`/api/calls/${callId}/ended`, {});
    liveBar(name, null);
    if (!saved) say('Call ended without an answer. Fetching the summary…');
    const kept = await keepTranscript(callId);
    if (!saved) say(kept?.summary || 'Call ended without an answer.');
  } catch (error) {
    const message = error.name === 'NotAllowedError' ? 'Microphone access was blocked. Allow it and try again.' : error.message;
    liveBar(name, null);
    say(message, 'bad');
    if (callId) post(`/api/calls/${callId}/ended`, { error: message }).catch(() => {});
  } finally {
    clearInterval(timer);
    live = null;
    window.pixelCallLive = false;
    row.classList.remove('on');
    buttons.forEach((b) => { b.disabled = false; });
    // A saved answer changes the request: show it once the call is over.
    if (saved) setTimeout(() => location.reload(), 2500);
  }
}

panel?.querySelectorAll('[data-call-email]').forEach((button) => button.addEventListener('click', () => call(button)));
