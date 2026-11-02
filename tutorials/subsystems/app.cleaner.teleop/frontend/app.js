const state = {
  apiBase: '',
  keys: new Set(),
  buttons: new Set(),
  timer: null,
  inFlight: false,
  actionInFlight: false,
  requestSeq: 1,
  lastVector: { vx: 0, vy: 0, wz: 0 },
};

const elements = {
  connectionState: document.getElementById('connection-state'),
  actionName: document.getElementById('action-name'),
  actionState: document.getElementById('action-state'),
  actionSteps: document.getElementById('action-steps'),
  logView: document.getElementById('log-view'),
  commandBtns: Array.from(document.querySelectorAll('[data-command]')),
};

const MOTION_COMMANDS = new Set(['forward', 'backward', 'left', 'right', 'rotate_left', 'rotate_right']);
const KEY_COMMANDS = {
  KeyW: 'forward',
  ArrowUp: 'forward',
  KeyS: 'backward',
  ArrowDown: 'backward',
  KeyA: 'left',
  ArrowLeft: 'left',
  KeyD: 'right',
  ArrowRight: 'right',
  KeyQ: 'rotate_left',
  KeyE: 'rotate_right',
  KeyH: 'extend',
  KeyJ: 'shrink',
  KeyK: 'open',
  KeyL: 'close',
  Space: 'burn',
};

function contextPath() {
  const params = new URLSearchParams(window.location.search);
  return params.get('context') || '';
}

function apiUrl(path) {
  const base = state.apiBase.replace(/\/$/, '');
  const suffix = String(path || '').replace(/^\//, '');
  return `${base}/${suffix}`;
}

function logLine(label, payload) {
  const rendered = typeof payload === 'string' ? payload : JSON.stringify(payload);
  const line = `[${new Date().toLocaleTimeString()}] ${label} ${rendered}`;
  const previous = elements.logView.textContent ? elements.logView.textContent.split('\n', 59) : [];
  elements.logView.textContent = [line, ...previous].slice(0, 60).join('\n');
}

function setConnection(label, stateName = '') {
  elements.connectionState.textContent = label;
  if (stateName) {
    elements.connectionState.dataset.state = stateName;
  } else {
    delete elements.connectionState.dataset.state;
  }
}

function setActionStatus({ action, actionState, steps = [], message = '' }) {
  elements.actionName.textContent = action || 'idle';
  elements.actionState.textContent = actionState || 'idle';
  elements.actionSteps.textContent = '';
  const renderedSteps = steps.length ? steps : [{ name: 'ready', state: actionState || 'idle', detail: message }];
  for (const step of renderedSteps) {
    const item = document.createElement('div');
    item.className = 'action-step';
    item.dataset.state = step.state || 'idle';
    const label = document.createElement('span');
    label.textContent = step.name || 'step';
    const value = document.createElement('strong');
    value.textContent = step.state || 'idle';
    if (step.detail !== undefined && step.detail !== null) {
      value.title = typeof step.detail === 'string' ? step.detail : JSON.stringify(step.detail);
    }
    item.append(label, value);
    elements.actionSteps.append(item);
  }
}

function unwrapPayload(value) {
  let current = value;
  for (let index = 0; index < 8; index += 1) {
    if (!current || typeof current !== 'object') {
      return current;
    }
    if ('payload_json' in current) {
      current = current.payload_json;
      continue;
    }
    if ('payload' in current) {
      current = current.payload;
      continue;
    }
    if ('result' in current) {
      current = current.result;
      continue;
    }
    return current;
  }
  return current;
}

function actionStepsFor(action, result, response) {
  const payload = result && typeof result === 'object' ? result : {};
  if (Array.isArray(payload.steps)) {
    return payload.steps.map((step) => ({
      name: String(step.name || 'step'),
      state: String(step.state || 'done'),
      detail: step.detail,
    }));
  }
  if (response?.ok === false || payload.ok === false || payload.success === false || payload.error) {
    return [{
      name: 'command',
      state: 'failed',
      detail: payload.error || response?.error || payload,
    }];
  }
  const ok = response?.ok !== false && payload.ok !== false && payload.success !== false && !payload.error;
  if (action === 'extend' || action === 'shrink' || action === 'stow') {
    return [{ name: 'arm', state: ok ? 'done' : 'failed', detail: payload }];
  }
  if (action === 'open' || action === 'close') {
    return [{ name: 'gripper', state: ok ? 'done' : 'failed', detail: payload }];
  }
  if (action === 'burn') {
    return [{ name: 'burn', state: ok ? 'done' : 'failed', detail: payload }];
  }
  return [{ name: action || 'action', state: ok ? 'done' : 'failed', detail: payload }];
}

function actionStateFor(action, result, response) {
  const payload = result && typeof result === 'object' ? result : {};
  if (response?.ok === false || payload.ok === false || payload.success === false || payload.error) {
    return 'failed';
  }
  if (action === 'burn' && Array.isArray(payload.burned) && payload.burned.length === 0) {
    return 'blocked';
  }
  return 'done';
}

function activeCommands() {
  return new Set([...state.keys, ...state.buttons]);
}

function motionVectorFromCommands(commands) {
  let vx = 0;
  let vy = 0;
  let wz = 0;
  if (commands.has('forward')) vx += 1;
  if (commands.has('backward')) vx -= 1;
  if (commands.has('left')) vy -= 1;
  if (commands.has('right')) vy += 1;
  if (commands.has('rotate_left')) wz -= 1;
  if (commands.has('rotate_right')) wz += 1;
  const magnitude = Math.hypot(vx, vy);
  if (magnitude > 1) {
    vx /= magnitude;
    vy /= magnitude;
  }
  return { vx, vy, wz };
}

function setButtonState() {
  for (const btn of elements.commandBtns) {
    const command = btn.dataset.command;
    btn.classList.toggle('teleop-held', state.buttons.has(command) || state.keys.has(command));
  }
}

async function loadContext() {
  const path = contextPath();
  if (!path) {
    throw new Error('missing app frontend context');
  }
  const response = await window.fetch(path, { headers: { accept: 'application/json' } });
  if (!response.ok) {
    throw new Error(`context failed: ${response.status}`);
  }
  const context = await response.json();
  if (!context?.endpoints?.api) {
    throw new Error('app frontend context has no API endpoint');
  }
  state.apiBase = context.endpoints.api;
  setConnection('connected');
  logLine('context', { api: state.apiBase, instance: context.instance_id });
}

async function callCommand(params) {
  if (!state.apiBase) {
    throw new Error('app API is not connected');
  }
  const response = await window.fetch(apiUrl('command'), {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      request_id: `teleop-ui-${state.requestSeq++}`,
      ...params,
    }),
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(value.error || `command failed: ${response.status}`);
  }
  return value;
}

async function sendVelocity({ force = false } = {}) {
  if (state.inFlight || state.actionInFlight) {
    return;
  }
  const vector = motionVectorFromCommands(activeCommands());
  const changed =
    vector.vx !== state.lastVector.vx ||
    vector.vy !== state.lastVector.vy ||
    vector.wz !== state.lastVector.wz;
  if (!force && !changed && vector.vx === 0 && vector.vy === 0 && vector.wz === 0) {
    return;
  }
  state.lastVector = vector;
  state.inFlight = true;
  try {
    await callCommand({
      action: 'drive',
      vx: vector.vx,
      vy: vector.vy,
      wz: vector.wz,
    });
  } catch (error) {
    logLine('drive', error.message || String(error));
  } finally {
    state.inFlight = false;
  }
}

function startMotionLoop() {
  setButtonState();
  sendVelocity({ force: true });
  if (state.timer) {
    return;
  }
  state.timer = window.setInterval(() => {
    sendVelocity({ force: true });
  }, 100);
}

async function stopMotion() {
  if (state.timer) {
    window.clearInterval(state.timer);
    state.timer = null;
  }
  state.lastVector = { vx: 0, vy: 0, wz: 0 };
  setButtonState();
  try {
    await callCommand({ action: 'stop' });
  } catch (error) {
    logLine('stop', error.message || String(error));
  }
}

function stopMotionIfIdle() {
  setButtonState();
  if (activeCommands().size === 0) {
    run(stopMotion);
  }
}

async function teleopCommand(action) {
  if (MOTION_COMMANDS.has(action)) {
    await callCommand({ action });
    return;
  }
  state.actionInFlight = true;
  state.keys.clear();
  state.buttons.clear();
  setButtonState();
  setActionStatus({ action, actionState: 'running', steps: [{ name: action, state: 'running' }] });
  try {
    if (state.timer) {
      await stopMotion();
    }
    const response = await callCommand({ action });
    const payload = unwrapPayload(response);
    const result = payload && typeof payload === 'object' && 'result' in payload ? unwrapPayload(payload.result) : payload;
    const nextState = actionStateFor(action, result, response);
    setActionStatus({
      action,
      actionState: nextState,
      steps: actionStepsFor(action, result, response),
      message: result?.error || '',
    });
    logLine(action, response);
  } catch (error) {
    setActionStatus({
      action,
      actionState: 'failed',
      steps: [{ name: action, state: 'failed', detail: error.message || String(error) }],
      message: error.message || String(error),
    });
    throw error;
  } finally {
    state.actionInFlight = false;
  }
}

function commandFromKeyboardEvent(event) {
  const tag = String(event.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || event.target?.isContentEditable) {
    return null;
  }
  return KEY_COMMANDS[event.code] || null;
}

async function run(action) {
  try {
    await action();
  } catch (error) {
    logLine('error', error.message || String(error));
  }
}

for (const btn of elements.commandBtns) {
  const command = btn.dataset.command;
  if (MOTION_COMMANDS.has(command)) {
    btn.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      state.buttons.add(command);
      startMotionLoop();
    });
    for (const eventName of ['pointerup', 'pointercancel', 'pointerleave']) {
      btn.addEventListener(eventName, () => {
        state.buttons.delete(command);
        stopMotionIfIdle();
      });
    }
    btn.addEventListener('click', (event) => event.preventDefault());
  } else {
    btn.addEventListener('click', () => run(() => teleopCommand(command)));
  }
}

window.addEventListener('keydown', (event) => {
  const command = commandFromKeyboardEvent(event);
  if (!command) {
    return;
  }
  event.preventDefault();
  if (MOTION_COMMANDS.has(command)) {
    if (event.repeat && state.keys.has(command)) {
      return;
    }
    state.keys.add(command);
    startMotionLoop();
    return;
  }
  if (!event.repeat) {
    run(() => teleopCommand(command));
  }
});

window.addEventListener('keyup', (event) => {
  const command = commandFromKeyboardEvent(event);
  if (!MOTION_COMMANDS.has(command)) {
    return;
  }
  event.preventDefault();
  state.keys.delete(command);
  stopMotionIfIdle();
});

window.addEventListener('blur', () => {
  state.keys.clear();
  state.buttons.clear();
  run(stopMotion);
});

setActionStatus({ action: 'idle', actionState: 'idle' });
loadContext().catch((error) => {
  setConnection('error', 'error');
  setActionStatus({
    action: 'connect',
    actionState: 'failed',
    steps: [{ name: 'context', state: 'failed', detail: error.message || String(error) }],
  });
  logLine('context', error.message || String(error));
});
