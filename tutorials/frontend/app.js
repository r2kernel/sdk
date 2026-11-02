import { worldStateIsStale } from './world_state_order.mjs';
import { decodePoseEstimateBase64 } from './localization_codec.mjs?v=robot-cleaner-localization-1';
import { actionStateFor, actionStepsFor, createActionStatusView } from './action_status.mjs?v=robot-cleaner-world-23';
import { burnedCount, createArenaRenderer } from './arena_renderer.mjs?v=robot-cleaner-world-23';
import { extractWorldState, parsePayload, unwrapPayload } from './payloads.mjs?v=robot-cleaner-world-23';

const APP_TOPIC = 'robot_cleaner/app/status';
const LOCALIZATION_TOPIC = 'pose_estimate';
const RADAR_TARGET = 'app.cleaner.radar';
const TELEOP_TARGET = 'app.cleaner.teleop';
const LEARNED_RADAR_TARGET = 'app.cleaner.learned_radar';
const configuredAppTarget = document.querySelector('meta[name="r2kernel-app-target"]')?.content?.trim();
const APP_TARGET = new URLSearchParams(window.location.search).get('app') || configuredAppTarget || RADAR_TARGET;

const state = {
  controlSocket: null,
  eventSocket: null,
  controlReady: false,
  eventReady: false,
  worldReady: false,
  requestSeq: 1,
  pending: new Map(),
  subscribedTopics: new Set(),
  worldPollTimer: null,
  worldPollInFlight: false,
  simBase: '',
  world: null,
  worldReceivedAt: 0,
  app: null,
  localization: null,
  renderFrame: null,
  teleopKeys: new Set(),
  teleopButtons: new Set(),
  teleopTimer: null,
  teleopInFlight: false,
  actionInFlight: false,
  lastTeleopVector: { vx: 0, vy: 0, wz: 0 },
  lastKeyboardNoticeAt: 0,
  actionStatus: { action: 'idle', state: 'idle', steps: [] },
  burnEffects: [],
  lastBurnedCount: null,
};

const elements = {
  arena: document.getElementById('arena'),
  wsBase: document.getElementById('ws-base'),
  connectBtn: document.getElementById('connect-btn'),
  disconnectBtn: document.getElementById('disconnect-btn'),
  connectionState: document.getElementById('connection-state'),
  runtimeLabel: document.getElementById('runtime-label'),
  remainingCount: document.getElementById('remaining-count'),
  payloadCount: document.getElementById('payload-count'),
  burnedCount: document.getElementById('burned-count'),
  localizationError: document.getElementById('localization-error'),
  worldView: document.getElementById('world-view'),
  logView: document.getElementById('log-view'),
  actionName: document.getElementById('action-name'),
  actionState: document.getElementById('action-state'),
  actionSteps: document.getElementById('action-steps'),
  appStartBtn: document.getElementById('app-start-btn'),
  appStopBtn: document.getElementById('app-stop-btn'),
  policyModel: document.getElementById('policy-model'),
  policyObservation: document.getElementById('policy-observation'),
  policyProposed: document.getElementById('policy-proposed'),
  policyApplied: document.getElementById('policy-applied'),
  resetBtn: document.getElementById('reset-btn'),
  spawnBtn: document.getElementById('spawn-btn'),
  farTargetBtn: document.getElementById('far-target-btn'),
  commandBtns: Array.from(document.querySelectorAll('[data-command]')),
};

const { draw, startRenderLoop, addBurnEffect, localizationPose } = createArenaRenderer({ state, canvas: elements.arena });

const MOTION_COMMANDS = new Set(['forward', 'backward', 'left', 'right', 'rotate_left', 'rotate_right']);
const TELEOP_ENABLED = elements.commandBtns.length > 0;
const WORLD_POLL_INTERVAL_MS = 100;
const MAX_LOG_LINES = 80;
const { setActionStatus, renderActionStatus, updateActionFromApp } = createActionStatusView({
  state,
  elements,
  motionCommands: MOTION_COMMANDS,
});

const KEY_COMMANDS = {
  w: 'forward',
  arrowup: 'forward',
  s: 'backward',
  arrowdown: 'backward',
  a: 'left',
  arrowleft: 'left',
  d: 'right',
  arrowright: 'right',
  q: 'rotate_left',
  e: 'rotate_right',
  h: 'extend',
  j: 'shrink',
  k: 'open',
  l: 'close',
  ' ': 'burn',
};

const CODE_COMMANDS = {
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

function suggestedWsBase() {
  if (window.location.protocol === 'file:') {
    return 'ws://127.0.0.1:8001';
  }
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.hostname}:8001`;
}

function suggestedSimBase() {
  if (window.location.protocol === 'file:') {
    return 'http://127.0.0.1:8092';
  }
  return `${window.location.protocol}//${window.location.hostname}:8092`;
}

function queryOptions() {
  return new URLSearchParams(window.location.search);
}

function queryEnabled(params, name) {
  const value = (params.get(name) || '').trim().toLowerCase();
  return value === '1' || value === 'true' || value === 'yes' || value === 'on';
}

function queryDisabled(params, name) {
  if (!params.has(name)) {
    return false;
  }
  const value = (params.get(name) || '').trim().toLowerCase();
  return value === '0' || value === 'false' || value === 'no' || value === 'off';
}

function delay(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

async function waitFor(predicate, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) {
      return true;
    }
    await delay(50);
  }
  return false;
}

function wsUrl(path) {
  return `${elements.wsBase.value.trim().replace(/\/$/, '')}${path}`;
}

function simUrl(path) {
  return `${state.simBase.replace(/\/$/, '')}${path}`;
}

function nextRequestId(prefix) {
  const requestId = `${prefix}-${state.requestSeq}`;
  state.requestSeq += 1;
  return requestId;
}

function logLine(label, payload) {
  const rendered = typeof payload === 'string' ? payload : JSON.stringify(payload);
  const line = `[${new Date().toLocaleTimeString()}] ${label} ${rendered}`;
  const previous = elements.logView.textContent
    ? elements.logView.textContent.split('\n', MAX_LOG_LINES - 1)
    : [];
  elements.logView.textContent = [line, ...previous].slice(0, MAX_LOG_LINES).join('\n');
}

function isEditableTarget(target) {
  if (!target || target === document.body || target === document.documentElement) {
    return false;
  }
  const tag = String(target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(target.isContentEditable);
}

function commandFromKeyboardEvent(event) {
  if (isEditableTarget(event.target)) {
    return null;
  }
  return CODE_COMMANDS[event.code] || KEY_COMMANDS[String(event.key || '').toLowerCase()] || null;
}

function runtimeConnected() {
  return state.eventReady && (!TELEOP_ENABLED || state.controlReady);
}

function showKeyboardConnectionNotice() {
  const now = Date.now();
  if (now - state.lastKeyboardNoticeAt < 1800) {
    return;
  }
  state.lastKeyboardNoticeAt = now;
  const message = 'runtime websocket is not connected';
  setActionStatus({
    action: 'teleop',
    actionState: 'waiting',
    steps: [{ name: 'runtime', state: 'waiting', detail: message }],
    message,
  });
  logLine('teleop.keyboard', message);
}

function setConnectionState() {
  const ready = runtimeConnected();
  const label = ready ? 'runtime online' : (state.worldReady ? 'world online' : 'starting');
  elements.connectionState.textContent = label;
  elements.runtimeLabel.textContent = ready ? 'online' : (state.worldReady ? 'world only' : 'offline');
  if (ready) {
    focusArena();
  }
}

function closeSockets() {
  if (state.controlSocket) {
    state.controlSocket.close();
    state.controlSocket = null;
  }
  if (state.eventSocket) {
    state.eventSocket.close();
    state.eventSocket = null;
  }
  for (const [, pending] of state.pending) {
    pending.reject(new Error('socket closed'));
  }
  state.pending.clear();
  state.subscribedTopics.clear();
  state.controlReady = false;
  state.eventReady = false;
  if (state.teleopTimer) {
    window.clearInterval(state.teleopTimer);
    state.teleopTimer = null;
  }
  state.teleopKeys.clear();
  state.teleopButtons.clear();
  setTeleopButtonState();
  setConnectionState();
}

function subscribeTopic(topic) {
  if (!state.eventSocket || state.eventSocket.readyState !== WebSocket.OPEN || state.subscribedTopics.has(topic)) {
    return;
  }
  state.subscribedTopics.add(topic);
  state.eventSocket.send(JSON.stringify({ kind: 'subscribe', topic }));
}

function sendControl(kind, body) {
  if (!state.controlSocket || state.controlSocket.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error('control websocket is not connected'));
  }
  const requestId = nextRequestId(kind);
  const message = { kind, request_id: requestId, ...body };
  return new Promise((resolve, reject) => {
    state.pending.set(requestId, { resolve, reject });
    state.controlSocket.send(JSON.stringify(message));
  });
}

function applyWorldState(value) {
  const world = extractWorldState(value);
  if (!world) {
    return false;
  }
  if (worldStateIsStale(state.world, world)) {
    return false;
  }
  const nextBurned = burnedCount(world);
  if (state.lastBurnedCount !== null && nextBurned > state.lastBurnedCount) {
    addBurnEffect(world, nextBurned - state.lastBurnedCount);
  }
  state.world = world;
  state.worldReceivedAt = performance.now();
  state.lastBurnedCount = nextBurned;
  updateTelemetry();
  return true;
}

function handleEventMessage(message) {
  if (message.kind !== 'event') {
    logLine('events', message);
    return;
  }
  const topic = message.topic || '';
  const payload = parsePayload(message);
  if (topic === APP_TOPIC && payload) {
    state.app = payload;
    updateActionFromApp(payload);
    updateTelemetry();
    return;
  }
  if (topic === LOCALIZATION_TOPIC) {
    try {
      state.localization = decodePoseEstimateBase64(message.payload_b64);
    } catch (error) {
      state.localization = null;
      logLine('localization.decode', {
        publisher: message.publisher || null,
        error: error.message || String(error),
      });
    }
    updateTelemetry();
    return;
  }
  logLine('event', { topic, payload });
}

async function connectSockets() {
  closeSockets();
  const eventSocket = new WebSocket(wsUrl('/api/v1/ws/events'));
  const controlSocket = new WebSocket(wsUrl('/api/v1/ws/control'));
  state.eventSocket = eventSocket;
  state.controlSocket = controlSocket;

  eventSocket.addEventListener('open', () => {
    state.eventReady = true;
    setConnectionState();
    subscribeTopic(APP_TOPIC);
    subscribeTopic(LOCALIZATION_TOPIC);
    logLine('events', 'connected');
  });
  controlSocket.addEventListener('open', () => {
    state.controlReady = true;
    setConnectionState();
    logLine('control', 'connected');
    void syncAppStatus().catch((error) => logLine('app.status', error.message || String(error)));
  });

  eventSocket.addEventListener('message', (event) => handleEventMessage(JSON.parse(event.data)));
  controlSocket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    const pending = state.pending.get(message.request_id);
    if (pending) {
      state.pending.delete(message.request_id);
      pending.resolve(message);
      return;
    }
    logLine('control', message);
  });

  eventSocket.addEventListener('close', () => {
    state.eventReady = false;
    setConnectionState();
  });
  controlSocket.addEventListener('close', () => {
    state.controlReady = false;
    setConnectionState();
  });
  eventSocket.addEventListener('error', () => logLine('events', 'error'));
  controlSocket.addEventListener('error', () => logLine('control', 'error'));
}

async function callProcedure(target, procedure, params = {}, options = {}) {
  const timeoutMs = Number(options.timeoutMs ?? 1200);
  const response = await sendControl('procedure.call', {
    target,
    procedure,
    params,
    timeout_ms: timeoutMs,
  });
  applyWorldState(response);
  if (!options.quiet) {
    logLine(`${target}.${procedure}`, response);
  }
  return response;
}

async function syncAppStatus() {
  const target = TELEOP_ENABLED ? TELEOP_TARGET : APP_TARGET;
  const response = await callProcedure(target, 'status', {}, { timeoutMs: 1200, quiet: true });
  const payload = unwrapPayload(response);
  if (!payload || payload.kind !== 'robot_cleaner.app') {
    throw new Error(`unexpected ${target}.status response`);
  }
  state.app = payload;
  updateActionFromApp(payload);
  updateTelemetry();
}

async function simulatorCall(operation, params = {}) {
  const response = await window.fetch(simUrl(`/${operation}`), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    throw new Error(`simulator ${operation} failed: ${response.status}`);
  }
  const value = await response.json();
  applyWorldState(value);
  logLine(`sim.${operation}`, value);
  return value;
}

async function simulatorState() {
  const response = await window.fetch(simUrl('/state'));
  if (!response.ok) {
    throw new Error(`simulator state failed: ${response.status}`);
  }
  const value = await response.json();
  state.worldReady = true;
  applyWorldState(value);
  setConnectionState();
  return value;
}

function startWorldPolling() {
  if (state.worldPollTimer) {
    return;
  }
  state.worldPollTimer = window.setInterval(() => {
    if (state.worldPollInFlight) {
      return;
    }
    state.worldPollInFlight = true;
    simulatorState()
      .catch((error) => logLine('sim.error', error.message || String(error)))
      .finally(() => {
        state.worldPollInFlight = false;
      });
  }, WORLD_POLL_INTERVAL_MS);
}

async function syncWorldState() {
  await simulatorState();
}

async function showWorld(options = {}) {
  try {
    await syncWorldState();
    if (options.reset) {
      await resetWorld();
    }
    startWorldPolling();
  } catch (error) {
    logLine('sim.error', error.message || String(error));
  }
}

function activeTeleopCommands() {
  return new Set([...state.teleopKeys, ...state.teleopButtons]);
}

function motionVectorFromCommands(commands) {
  let vx = 0;
  let vy = 0;
  let wz = 0;
  if (commands.has('forward')) {
    vx += 1;
  }
  if (commands.has('backward')) {
    vx -= 1;
  }
  if (commands.has('left')) {
    vy -= 1;
  }
  if (commands.has('right')) {
    vy += 1;
  }
  if (commands.has('rotate_left')) {
    wz -= 1;
  }
  if (commands.has('rotate_right')) {
    wz += 1;
  }
  const magnitude = Math.hypot(vx, vy);
  if (magnitude > 1) {
    vx /= magnitude;
    vy /= magnitude;
  }
  return { vx, vy, wz };
}

function setTeleopButtonState() {
  for (const btn of elements.commandBtns) {
    const command = btn.dataset.command;
    btn.classList.toggle('teleop-held', state.teleopButtons.has(command) || state.teleopKeys.has(command));
  }
}

async function sendTeleopVelocity({ force = false } = {}) {
  if (state.teleopInFlight || state.actionInFlight) {
    return;
  }
  if (!runtimeConnected()) {
    showKeyboardConnectionNotice();
    return;
  }
  const vector = motionVectorFromCommands(activeTeleopCommands());
  const changed =
    vector.vx !== state.lastTeleopVector.vx ||
    vector.vy !== state.lastTeleopVector.vy ||
    vector.wz !== state.lastTeleopVector.wz;
  if (!force && !changed && vector.vx === 0 && vector.vy === 0 && vector.wz === 0) {
    return;
  }
  state.lastTeleopVector = vector;
  state.teleopInFlight = true;
  try {
    await callProcedure(
      TELEOP_TARGET,
      'command',
      {
        action: 'drive',
        vx: vector.vx,
        vy: vector.vy,
        wz: vector.wz,
      },
      { timeoutMs: 700, quiet: true },
    );
  } catch (error) {
    logLine('teleop.command', error.message || String(error));
  } finally {
    state.teleopInFlight = false;
  }
}

function startTeleopLoop() {
  setTeleopButtonState();
  sendTeleopVelocity({ force: true });
  if (state.teleopTimer) {
    return;
  }
  state.teleopTimer = window.setInterval(() => {
    sendTeleopVelocity({ force: true });
  }, 100);
}

async function stopTeleopMotion() {
  if (state.teleopTimer) {
    window.clearInterval(state.teleopTimer);
    state.teleopTimer = null;
  }
  state.lastTeleopVector = { vx: 0, vy: 0, wz: 0 };
  setTeleopButtonState();
  if (!runtimeConnected()) {
    showKeyboardConnectionNotice();
    return;
  }
  try {
    await callProcedure(TELEOP_TARGET, 'command', { action: 'stop' }, { timeoutMs: 1000, quiet: true });
    await syncWorldState();
  } catch (error) {
    logLine('teleop.stop', error.message || String(error));
  }
}

function stopTeleopLoopIfIdle() {
  setTeleopButtonState();
  if (activeTeleopCommands().size === 0) {
    run(stopTeleopMotion);
  }
}

function focusArena() {
  if (document.activeElement && isEditableTarget(document.activeElement)) {
    return;
  }
  elements.arena.focus({ preventScroll: true });
}

async function teleopCommand(action) {
  if (MOTION_COMMANDS.has(action)) {
    await callProcedure(
      TELEOP_TARGET,
      'command',
      { action },
      { timeoutMs: 1000 },
    );
    await syncWorldState();
    return;
  }
  const timeoutMs = 2500;
  state.actionInFlight = true;
  state.teleopKeys.clear();
  state.teleopButtons.clear();
  setTeleopButtonState();
  setActionStatus({ action, actionState: 'running', steps: [{ name: action, state: 'running' }] });
  try {
    if (state.teleopTimer) {
      await stopTeleopMotion();
    }
    const response = await callProcedure(TELEOP_TARGET, 'command', { action }, { timeoutMs });
    const payload = unwrapPayload(response);
    const result = payload && typeof payload === 'object' && 'result' in payload ? unwrapPayload(payload.result) : payload;
    const nextState = actionStateFor(action, result, response);
    setActionStatus({
      action,
      actionState: nextState,
      steps: actionStepsFor(action, result, response),
      message: result?.error || '',
    });
    await syncWorldState();
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

async function resetWorld() {
  state.burnEffects = [];
  state.lastBurnedCount = null;
  await simulatorCall('reset', { seed: 7, count: 9 });
}

async function spawnTrash() {
  await simulatorCall('spawn_trash', {});
}

async function resetWithFarTarget() {
  state.burnEffects = [];
  state.lastBurnedCount = null;
  if (runtimeConnected()) {
    await callProcedure(APP_TARGET, 'stop', {}, { timeoutMs: 1500, quiet: true });
  }
  await simulatorCall('reset', { seed: 701, count: 1, scenario: 'far_target' });
  if (runtimeConnected()) {
    await callProcedure(APP_TARGET, 'start', {}, { timeoutMs: 1500, quiet: true });
  }
  setActionStatus({
    action: 'far-target scenario',
    actionState: 'running',
    steps: [{ name: 'radar', state: 'searching', detail: 'target starts outside the 0.30 m radar range' }],
  });
}

function updateTelemetry() {
  if (state.world) {
    elements.worldView.textContent = JSON.stringify(
      {
        seq: state.world.seq,
        robot: state.world.robot,
        score: state.world.score,
        scan: state.world.scan,
        localization: state.localization,
        application: state.app,
      },
      null,
      2,
    );
    elements.remainingCount.textContent = String(state.world.score?.remaining ?? 0);
    elements.payloadCount.textContent = String(state.world.score?.payload ?? 0);
    elements.burnedCount.textContent = String(state.world.score?.burned ?? 0);
    const estimate = localizationPose();
    const truth = state.world.robot;
    if (estimate && truth) {
      const error = Math.hypot(estimate.x - Number(truth.x || 0), estimate.y - Number(truth.y || 0));
      elements.localizationError.textContent = `${(error * 100).toFixed(1)} cm`;
    } else {
      elements.localizationError.textContent = 'acquiring';
    }
  }
}

async function run(action) {
  try {
    await action();
  } catch (error) {
    logLine('error', error.message || String(error));
  }
}

elements.connectBtn.addEventListener('click', () => run(connectSockets));
elements.disconnectBtn.addEventListener('click', closeSockets);
elements.resetBtn.addEventListener('click', () => run(resetWorld));
elements.spawnBtn.addEventListener('click', () => run(spawnTrash));
elements.farTargetBtn.addEventListener('click', () => run(resetWithFarTarget));
elements.appStartBtn.addEventListener('click', () => run(() => callProcedure(APP_TARGET, 'start', {}, { timeoutMs: 1500 })));
elements.appStopBtn.addEventListener('click', () => run(() => callProcedure(APP_TARGET, 'stop', {}, { timeoutMs: 1500 })));
elements.arena.addEventListener('pointerdown', focusArena);
for (const btn of elements.commandBtns) {
  const command = btn.dataset.command;
  if (MOTION_COMMANDS.has(command)) {
    btn.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      state.teleopButtons.add(command);
      startTeleopLoop();
    });
    for (const eventName of ['pointerup', 'pointercancel', 'pointerleave']) {
      btn.addEventListener(eventName, () => {
        state.teleopButtons.delete(command);
        stopTeleopLoopIfIdle();
      });
    }
    btn.addEventListener('click', (event) => event.preventDefault());
  } else {
    btn.addEventListener('click', () => run(() => teleopCommand(command)));
  }
}

window.addEventListener('keydown', (event) => {
  if (!TELEOP_ENABLED) {
    return;
  }
  const command = commandFromKeyboardEvent(event);
  if (!command) {
    return;
  }
  event.preventDefault();
  if (MOTION_COMMANDS.has(command)) {
    if (event.repeat && state.teleopKeys.has(command)) {
      return;
    }
    state.teleopKeys.add(command);
    startTeleopLoop();
    return;
  }
  if (!event.repeat) {
    run(() => teleopCommand(command));
  }
});

window.addEventListener('keyup', (event) => {
  if (!TELEOP_ENABLED) {
    return;
  }
  const command = commandFromKeyboardEvent(event);
  if (!MOTION_COMMANDS.has(command)) {
    return;
  }
  event.preventDefault();
  state.teleopKeys.delete(command);
  stopTeleopLoopIfIdle();
});

window.addEventListener('blur', () => {
  if (!TELEOP_ENABLED) {
    return;
  }
  state.teleopKeys.clear();
  state.teleopButtons.clear();
  run(stopTeleopMotion);
});

window.addEventListener('resize', () => draw());

if (!elements.wsBase.value.trim() || elements.wsBase.value.trim() === 'ws://127.0.0.1:8001') {
  elements.wsBase.value = suggestedWsBase();
}
state.simBase = suggestedSimBase();

async function applyQueryOptions() {
  const params = queryOptions();
  const wsBase = params.get('ws');
  if (wsBase) {
    elements.wsBase.value = wsBase;
  }
  state.simBase = params.get('sim') || state.simBase;

  const shouldShowWorld = !params.has('world') || queryEnabled(params, 'world');
  if (shouldShowWorld) {
    await showWorld({ reset: queryEnabled(params, 'reset') });
  }

  if (!queryDisabled(params, 'connect')) {
    await connectSockets();
  }

  if (!queryEnabled(params, 'connect')) {
    return;
  }

  await waitFor(() => runtimeConnected(), 5000);
  await syncWorldState();
  startWorldPolling();

  if (queryEnabled(params, 'reset')) {
    await resetWorld();
  }
  await waitFor(() => Boolean(state.world), 5000);

  const commands = (params.get('commands') || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  if (TELEOP_ENABLED) {
    for (const command of commands) {
      await teleopCommand(command);
      await delay(160);
    }
  }
}

renderActionStatus();
startRenderLoop();
focusArena();
run(applyQueryOptions);
