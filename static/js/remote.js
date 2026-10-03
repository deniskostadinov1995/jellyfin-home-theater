/* ═══════════════════════════════════════════════════════════════════════
   Jellyfin Home Theater — Phone Remote Logic
   Sends commands to TV via WebSocket, displays playback state
═══════════════════════════════════════════════════════════════════════ */

// ── Socket Setup ──────────────────────────────────────────────────────────
// Both transports, in this order. The server runs Socket.IO in threading mode
// (see app.py for why), which serves WebSockets but will not accept one as the
// *opening* transport — so pinning 'websocket' alone left the socket unable to
// connect at all, retrying forever, and the remote dead. Starting on polling
// connects immediately and Socket.IO then upgrades itself to a WebSocket, so
// this costs nothing: measured round-trip is 2-3 ms on a LAN.
const socket = io({ transports: ['polling', 'websocket'] });

socket.on('connect', () => {
  setConnStatus(true);
});
socket.on('disconnect', () => {
  setConnStatus(false);
});
socket.on('state', (state) => {
  updateState(state);
});

function setConnStatus(on) {
  const dot  = document.getElementById('conn-dot');
  const text = document.getElementById('conn-text');
  dot.className  = on ? 'dot-on' : 'dot-off';
  text.textContent = on ? 'Connected to TV' : 'Disconnected';
}

// ── Send Command ───────────────────────────────────────────────────────────
function send(action, value) {
  const payload = { action };
  if (value !== undefined) payload.value = value;
  socket.emit('command', payload);
  // Haptic feedback on mobile
  if (navigator.vibrate) navigator.vibrate(30);
}

// ── State Display ──────────────────────────────────────────────────────────
let lastState = {};

function updateState(state) {
  lastState = state;

  // Now playing card
  const title    = state.title    || 'Not Playing';
  const subtitle = state.subtitle || '';
  document.getElementById('np-title').textContent    = title;
  document.getElementById('np-subtitle').textContent = subtitle;

  // Thumb — with nothing playing, drop the src entirely and hide the element.
  // src='' makes the browser re-request the page itself, which comes back as
  // HTML and renders as a broken-image icon.
  const thumb = document.getElementById('np-thumb');
  if (state.thumb) {
    thumb.src = `/img?path=${encodeURIComponent(state.thumb)}&w=120&h=120`;
    thumb.hidden = false;
  } else {
    thumb.removeAttribute('src');
    thumb.hidden = true;
  }

  // State icon
  document.getElementById('np-state-icon').textContent = state.playing ? '▶' : '⏸';

  // Progress
  const pos = state.position || 0;
  const dur = state.duration || 0;
  const pct = dur ? (pos / dur * 100) : 0;
  document.getElementById('np-progress-fill').style.width = pct + '%';
  document.getElementById('np-time').textContent = dur ? `${formatTime(pos)} / ${formatTime(dur)}` : '—';

  // Volume
  const vol = state.volume ?? 100;
  document.getElementById('vol-value').textContent  = vol;
  document.getElementById('vol-number').textContent = vol;
  document.getElementById('vol-slider').value = vol;

  // Seek bar
  if (dur) {
    const seekBar = document.getElementById('seek-bar');
    if (!seekBar._dragging) seekBar.value = Math.round(pct);
    document.getElementById('seek-current').textContent = formatTime(pos);
    document.getElementById('seek-total').textContent   = formatTime(dur);
  }
}

function formatTime(ms) {
  if (!ms || isNaN(ms)) return '0:00';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const ss = String(s % 60).padStart(2, '0');
  const mm = String(m % 60).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

// ── Button Wiring ──────────────────────────────────────────────────────────
// Guard against touchstart + click double-fire (common on mobile browsers).
// Strategy: touchstart fires first and sets a timestamp; the subsequent click
// event is suppressed if it arrives within 400ms of a touch.
let _lastTouchTs = 0;

document.querySelectorAll('[data-action]').forEach(btn => {
  btn.addEventListener('touchstart', (e) => {
    e.preventDefault();          // stops the ghost click from being generated
    _lastTouchTs = Date.now();
    handleButtonAction(btn.dataset.action);
  }, { passive: false });

  btn.addEventListener('click', (e) => {
    // If a touch happened recently this click is the ghost — ignore it
    if (Date.now() - _lastTouchTs < 400) return;
    handleButtonAction(btn.dataset.action);
  });
});

function handleButtonAction(action) {
  switch (action) {
    // Navigation
    case 'up':    case 'down': case 'left': case 'right':
    case 'select': case 'back': case 'home': case 'search':
      send(action);
      break;

    // Playback
    case 'play':   case 'pause': case 'stop':
    case 'toggle_play': case 'previous': case 'next':
    case 'seek_back': case 'seek_fwd':
      send(action);
      break;

    // Subs / aspect / fullscreen
    case 'toggle_subs': case 'toggle_audio': case 'aspect': case 'fullscreen':
      send(action);
      break;

    // Volume
    case 'vol_up':   send('vol_up');   break;
    case 'vol_down': send('vol_down'); break;
    case 'mute':     send('mute');     break;
  }
}

// ── Seek Bar ───────────────────────────────────────────────────────────────
const seekBar = document.getElementById('seek-bar');
seekBar._dragging = false;
seekBar._lastSent = null;

// mouseup/touchend and change can both fire for one drag — dedupe on value
function sendSeek() {
  const val = parseInt(seekBar.value);
  if (val === seekBar._lastSent) return;
  seekBar._lastSent = val;
  send('seek', val);
  // allow re-seeking to the same spot after the state next updates the bar
  setTimeout(() => { seekBar._lastSent = null; }, 500);
}

seekBar.addEventListener('touchstart', () => { seekBar._dragging = true; });
seekBar.addEventListener('mousedown',  () => { seekBar._dragging = true; });
seekBar.addEventListener('touchend',   () => { seekBar._dragging = false; sendSeek(); });
seekBar.addEventListener('mouseup',    () => { seekBar._dragging = false; sendSeek(); });
seekBar.addEventListener('change',     () => { if (!seekBar._dragging) sendSeek(); });

// ── Volume Slider ──────────────────────────────────────────────────────────
const volSlider = document.getElementById('vol-slider');
let volTimer;
volSlider.addEventListener('input', () => {
  const v = parseInt(volSlider.value);
  document.getElementById('vol-number').textContent = v;
  document.getElementById('vol-value').textContent  = v;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => send('volume', v), 80);
});

// ── Tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => {
      c.classList.remove('active');
      c.classList.add('hidden');
    });

    tab.classList.add('active');
    const content = document.getElementById(`tab-${tab.dataset.tab}`);
    if (content) {
      content.classList.remove('hidden');
      content.classList.add('active');
    }
  });
});

// ── Long press D-pad for repeat ────────────────────────────────────────────
// NOTE: The general touchstart listener above already sends the first action.
// Here we only add the HOLD-repeat behaviour — we do NOT re-send on touchstart
// to avoid a double action on the initial press.
document.querySelectorAll('.dpad-btn').forEach(btn => {
  let pressTimer;
  let repeatInterval;
  const action = btn.dataset.action;

  if (!['up','down','left','right'].includes(action)) return;

  btn.addEventListener('touchstart', (e) => {
    e.preventDefault();
    _lastTouchTs = Date.now();
    // First action is already sent by the general listener above.
    // Start repeat only after a 500ms hold — do NOT send immediately here.
    pressTimer = setTimeout(() => {
      repeatInterval = setInterval(() => send(action), 180);
    }, 500);
  }, { passive: false });

  btn.addEventListener('touchend', () => {
    clearTimeout(pressTimer);
    clearInterval(repeatInterval);
  });
  btn.addEventListener('touchcancel', () => {
    clearTimeout(pressTimer);
    clearInterval(repeatInterval);
  });
});

// ── Keyboard support (for desktop testing) ────────────────────────────────
document.addEventListener('keydown', (e) => {
  const map = {
    'ArrowUp':    'up',
    'ArrowDown':  'down',
    'ArrowLeft':  'left',
    'ArrowRight': 'right',
    'Enter':      'select',
    'Backspace':  'back',
    ' ':          'toggle_play',
  };
  if (map[e.key]) {
    e.preventDefault();
    send(map[e.key]);
  }
});

// ── Lights (WLED) ──────────────────────────────────────────────────────────
// SignalRGB drives the strips by streaming over UDP, which puts WLED into
// realtime mode — that is the "another device is controlling the lights"
// notice the WLED app shows. While it lasts WLED ignores its own on/off, so a
// plain On/Off button appears to do nothing. The modes below use WLED's live
// override to take the strips back first, which is what makes Off mean off
// without having to go and quit SignalRGB.
let lightsState = null;

async function wledSend(payload) {
  try {
    await fetch('/api/wled/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (navigator.vibrate) navigator.vibrate(30);
  } catch (_) {}
}

async function wledSetMode(mode) {
  try {
    await fetch('/api/wled/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (navigator.vibrate) navigator.vibrate(30);
  } catch (_) {}
  refreshLights();
}

function renderLights(st) {
  lightsState = st;
  document.querySelectorAll('[data-wled-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.wledMode === st.mode);
  });

  const reachable = (st.devices || []).filter(d => !d.error);
  const dead = (st.devices || []).filter(d => d.error);
  const bits = [];
  if (st.mode === 'ambilight') {
    bits.push(st.anyLive
      ? 'SignalRGB is driving the lights.'
      : 'Waiting for SignalRGB — start it to see the ambilight.');
  } else if (st.mode === 'manual') {
    bits.push('Ignoring SignalRGB; brightness and on/off apply here.');
  } else if (st.mode === 'off') {
    bits.push('Off, and staying off while SignalRGB streams.');
  } else if (reachable.length) {
    bits.push('Controllers disagree — pick a mode to bring them together.');
  }
  if (dead.length) {
    bits.push(`${dead.length} controller${dead.length > 1 ? 's' : ''} unreachable.`);
  }
  document.getElementById('lights-status').textContent = bits.join(' ');

  if (typeof st.bri === 'number' && st.bri > 0) {
    document.getElementById('wled-bri').value = st.bri;
  }

  // Dim-on-playback: state plus what it is currently doing.
  const autoBtn = document.getElementById('wled-auto-toggle');
  autoBtn.textContent = st.autoDim ? 'On' : 'Off';
  autoBtn.classList.toggle('active', !!st.autoDim);
  const sceneWords = { playing: 'dimmed for playback',
                       paused:  'up a little, paused',
                       idle:    'back to normal' };
  document.getElementById('wled-auto-meta').textContent =
    st.autoDim ? (sceneWords[st.autoScene] || 'waiting for playback') : '';

  // Per-controller rows, so one strip can be toggled without the other.
  const wrap = document.getElementById('wled-devices');
  wrap.innerHTML = '';
  (st.devices || []).forEach(d => {
    const row = document.createElement('div');
    row.className = 'wled-device';
    if (d.error) {
      row.innerHTML = `<span class="wled-dev-name">${d.name}</span>` +
                      `<span class="wled-dev-meta">unreachable</span>`;
    } else {
      // Strips report their LED count; ceiling lights report a level.
      const meta = d.kind === 'ceiling'
        ? (d.on ? `${Math.round((d.bri || 0) / 255 * 100)}%` : 'off')
        : `${d.leds || '?'} LEDs${d.live ? ' · live' : ''}`;
      row.innerHTML =
        `<span class="wled-dev-name">${d.name}</span>` +
        `<span class="wled-dev-meta">${meta}</span>`;
      const btn = document.createElement('button');
      btn.className = 'pill-btn tiny' + (d.on ? ' active' : '');
      btn.textContent = d.on ? 'On' : 'Off';
      btn.onclick = async () => {
        await wledSend({ host: d.host, on: !d.on });
        refreshLights();
      };
      row.appendChild(btn);
    }
    wrap.appendChild(row);
  });
}

async function refreshLights() {
  try {
    const st = await (await fetch('/api/wled/state')).json();
    const configured = !!st.configured;
    document.getElementById('lights-unconfigured').classList.toggle('hidden', configured);
    document.getElementById('lights-controls').classList.toggle('hidden', !configured);
    if (configured) renderLights(st);
    return configured;
  } catch (_) {
    document.getElementById('lights-unconfigured').classList.remove('hidden');
    return false;
  }
}

async function initLights() {
  if (!await refreshLights()) return;

  document.querySelectorAll('[data-wled-mode]').forEach(btn => {
    btn.addEventListener('click', () => wledSetMode(btn.dataset.wledMode));
  });

  // Changing brightness implies you want a plain light rather than the
  // ambilight, so take the strips back first — otherwise the stream would
  // overwrite the new brightness immediately and the slider would look broken.
  const applyBri = async (value) => {
    if (lightsState && lightsState.mode === 'ambilight') await wledSetMode('manual');
    wledSend({ on: true, bri: value });
  };

  let briTimer;
  document.getElementById('wled-bri').addEventListener('input', (e) => {
    clearTimeout(briTimer);
    const value = parseInt(e.target.value, 10);
    briTimer = setTimeout(() => applyBri(value), 150);
  });
  document.querySelectorAll('[data-wled-bri]').forEach(btn => {
    btn.addEventListener('click', () => {
      const value = parseInt(btn.dataset.wledBri, 10);
      document.getElementById('wled-bri').value = value;
      applyBri(value);
    });
  });

  document.getElementById('wled-auto-toggle').addEventListener('click', async () => {
    const next = !(lightsState && lightsState.autoDim);
    try {
      await fetch('/api/wled/auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (navigator.vibrate) navigator.vibrate(30);
    } catch (_) {}
    refreshLights();
  });

  // Re-read while the Lights tab is open, so the state stays honest if
  // SignalRGB starts or stops behind our back.
  setInterval(() => {
    const tab = document.getElementById('tab-lights');
    if (tab && !tab.classList.contains('hidden')) refreshLights();
  }, 5000);
}
initLights();

// ── Ping TV to keep connection alive ─────────────────────────────────────
setInterval(() => {
  if (socket.connected) socket.emit('ping_check');
}, 10000);
