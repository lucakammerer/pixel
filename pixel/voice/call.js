// One browser voice call with the ElevenLabs agent, started from a session page's Call panel (`ui/routes.py`
// `_calls_panel`). The server signs the session and says what to ask (`voice/calls.py` `start`); the agent's client
// tools post what it heard straight to the server, so the bid or the answer is in the thread while the call goes on.
const CLIENT_URL = 'https://esm.sh/@elevenlabs/client@1.35.0';
const panel = document.getElementById('voice-calls');
const status = document.getElementById('call-status');
const log = document.getElementById('call-log');
let live = null;

const say = (text, bad = false) => {
  status.textContent = text;
  status.className = bad ? 'notice bad' : 'notice';
  status.hidden = false;
};
const line = (role, text) => {
  const row = document.createElement('div');
  row.className = 'pad';
  row.innerHTML = `<b></b> <span></span>`;
  row.querySelector('b').textContent = role === 'agent' ? 'PIXEL:' : 'Them:';
  row.querySelector('span').textContent = text;
  log.append(row);
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
  const buttons = panel.querySelectorAll('[data-call-email]');
  buttons.forEach((b) => { b.disabled = true; });
  log.replaceChildren();
  window.pixelCallLive = true; // the session page does not reload while a call is going on
  let callId = null;
  try {
    say('Starting the call…');
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
        say('Saved to the request. PIXEL reads it like a mailed answer.');
        return 'Saved.';
      } catch (error) {
        say(`Not saved: ${error.message}`, true);
        return `Not saved: ${error.message}`;
      }
    };
    await new Promise((resolve, reject) => {
      Conversation.startSession({
        signedUrl: started.signed_url,
        dynamicVariables: started.dynamic_variables,
        clientTools: { record_carrier_offer: save, record_customer_details: save },
        onConnect: ({ conversationId }) => {
          say(`On the call with ${button.dataset.callName}.`);
          post(`/api/calls/${callId}/connected`, { conversation_id: conversationId }).catch(() => {});
        },
        onMessage: ({ source, message }) => line(source === 'ai' ? 'agent' : 'user', message),
        onError: (message) => say(String(message), true),
        onDisconnect: () => resolve(),
      }).then((conversation) => {
        live = conversation;
        const hangUp = document.getElementById('call-hang-up');
        hangUp.hidden = false;
        hangUp.onclick = () => conversation.endSession();
      }, reject);
    });
    await post(`/api/calls/${callId}/ended`, {});
    say('Call ended. Fetching the transcript…');
    const kept = await keepTranscript(callId);
    say(kept?.summary || 'Call ended.');
  } catch (error) {
    const message = error.name === 'NotAllowedError' ? 'Microphone access was blocked. Allow it and try again.' : error.message;
    say(message, true);
    if (callId) post(`/api/calls/${callId}/ended`, { error: message }).catch(() => {});
  } finally {
    live = null;
    window.pixelCallLive = false;
    document.getElementById('call-hang-up').hidden = true;
    buttons.forEach((b) => { b.disabled = false; });
  }
}

panel?.querySelectorAll('[data-call-email]').forEach((button) => button.addEventListener('click', () => call(button)));
