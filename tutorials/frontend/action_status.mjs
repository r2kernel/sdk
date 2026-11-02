// Action status panel for the cleaner arena viewer: derives step lists from app and
// teleop results and renders them, including the learned-radar policy readout.
import { unwrapPayload } from './payloads.mjs?v=robot-cleaner-world-23';

export function actionStepsFor(action, result, response) {
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

export function actionStateFor(action, result, response) {
  const payload = result && typeof result === 'object' ? result : {};
  if (response?.ok === false || payload.ok === false || payload.success === false || payload.error) {
    return 'failed';
  }
  if (action === 'collect' && payload.collected === false) {
    return 'blocked';
  }
  if (action === 'burn' && Array.isArray(payload.burned) && payload.burned.length === 0) {
    return 'blocked';
  }
  return 'done';
}

export function createActionStatusView({ state, elements, motionCommands }) {
  function setActionStatus({ action, actionState, steps = [], message = '' }) {
    state.actionStatus = {
      action: action || 'idle',
      state: actionState || 'idle',
      steps,
      message,
    };
    renderActionStatus();
  }

  function renderActionStatus() {
    const current = state.actionStatus;
    elements.actionName.textContent = current.action || 'idle';
    elements.actionState.textContent = current.state || 'idle';
    elements.actionSteps.textContent = '';
    const steps = current.steps && current.steps.length
      ? current.steps
      : [{ name: 'ready', state: current.state || 'idle', detail: current.message || '' }];
    for (const step of steps) {
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

  function updateActionFromApp(payload) {
    if (!payload || typeof payload !== 'object') {
      return;
    }
    const actionText = String(payload.last_action || '');
    if (actionText.startsWith('learned_radar.')) {
      const action = actionText.replace(/^learned_radar\./, '');
      const result = unwrapPayload(payload.last_result || {});
      let actionState = payload.active ? 'running' : 'idle';
      if (action === 'explore') {
        actionState = 'searching';
      } else if (action === 'approach') {
        actionState = 'tracking';
      } else if (action === 'fault' || action === 'error') {
        actionState = 'failed';
      } else if (action.endsWith('_done')) {
        actionState = 'done';
      } else if (action === 'stop') {
        actionState = 'stopped';
      }
      const observation = result?.observation || {};
      const proposed = result?.proposed || {};
      const appliedTwist = result?.applied?.applied_twist || {};
      const appliedLinear = appliedTwist?.linear?.x;
      const appliedAngular = appliedTwist?.angular?.z;
      if (elements.policyModel) {
        elements.policyModel.textContent = `${result?.policy || 'behavior_cloning_mlp'} ${result?.model_sha256 || ''}`.trim();
        elements.policyObservation.textContent = observation.detected
          ? `${observation.target_id || 'target'} ${Number(observation.distance || 0).toFixed(2)} m`
          : 'no target · active exploration';
        elements.policyProposed.textContent = proposed.linear === undefined
          ? '—'
          : `v ${Number(proposed.linear).toFixed(2)} · ω ${Number(proposed.angular).toFixed(2)}`;
        elements.policyApplied.textContent = appliedLinear === undefined
          ? String(result?.applied?.reason || 'waiting')
          : `v ${Number(appliedLinear).toFixed(2)} · ω ${Number(appliedAngular).toFixed(2)}`;
      }
      const steps = Array.isArray(result?.steps)
        ? result.steps
        : [
          { name: 'observe', state: observation.detected ? 'done' : 'searching', detail: observation },
          { name: 'infer', state: 'done', detail: proposed },
          { name: 'component', state: result?.applied?.accepted === false ? 'failed' : 'done', detail: result?.applied },
        ];
      setActionStatus({ action, actionState, steps, message: result?.error || '' });
      return;
    }
    if (actionText.startsWith('radar.')) {
      const action = actionText.replace(/^radar\./, '');
      const result = unwrapPayload(payload.last_result || {});
      let actionState = payload.active ? 'running' : 'idle';
      if (action === 'no_target' || action === 'target_lost') {
        actionState = 'waiting';
      } else if (action === 'fault' || action === 'error' || action.endsWith('_failed')) {
        actionState = 'failed';
      } else if (action.endsWith('_done')) {
        actionState = 'done';
      } else if (action === 'stop') {
        actionState = 'stopped';
      }
      const steps = Array.isArray(result?.steps)
        ? result.steps
        : [{ name: action || 'radar', state: actionState, detail: result }];
      setActionStatus({
        action,
        actionState,
        steps,
        message: result?.error || '',
      });
      return;
    }
    if (!actionText.startsWith('teleop.')) {
      return;
    }
    const action = actionText.replace(/^teleop\./, '').replace(/\.start$/, '');
    if (motionCommands.has(action) || action === 'drive' || action === 'velocity' || action === 'stop') {
      return;
    }
    const last = payload.last_result || {};
    const result = unwrapPayload(last);
    if (actionText.endsWith('.start')) {
      setActionStatus({
        action,
        actionState: 'running',
        steps: [{ name: action, state: 'running' }],
      });
      return;
    }
    const actionResult = result && typeof result === 'object' && 'result' in result ? unwrapPayload(result.result) : result;
    const nextState = actionStateFor(action, actionResult, payload);
    setActionStatus({
      action,
      actionState: nextState,
      steps: actionStepsFor(action, actionResult, payload),
      message: actionResult?.error || '',
    });
  }

  return { setActionStatus, renderActionStatus, updateActionFromApp };
}
