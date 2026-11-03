// Opt-in local Strata / Gyro-S control in the chat composer.
// Browser requests always go through Mabel, never directly to Strata.
import { showError, showToast } from './ui.js?v=20260916largetoolscroll1';

const STATUS_URL = '/api/strata/status';
const button = document.getElementById('strata-toggle-btn');
const label = document.getElementById('strata-toggle-label');
let state = 'offline';
let pending = false;
let enabled = false;

function render() {
  if (!button || !label) return;
  button.hidden = !enabled;
  if (!enabled) return;

  const captions = {
    ready: 'Suspend Gyro-S',
    suspended: 'Resume Gyro-S',
    offline: 'Start Gyro-S',
    starting: 'Starting Gyro-S…',
    unavailable: 'Strata unavailable',
    unauthorized: 'Strata authentication failed',
    'wrong-model': 'Different Strata model active',
    'unexpected-service': 'Port 8080 is not Gyro-S',
  };
  const caption = captions[state] || 'Strata unavailable';
  const interactive = ['ready', 'suspended', 'offline'].includes(state) && !pending;
  button.dataset.state = pending ? 'starting' : state;
  button.disabled = !interactive;
  button.title = caption;
  button.setAttribute('aria-label', caption);
  button.setAttribute('aria-pressed', String(state === 'ready'));
  label.textContent = caption;
}

async function refresh() {
  if (pending) return;
  try {
    const response = await fetch(STATUS_URL, { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 403 || response.status === 401) {
      enabled = false;
    } else if (!response.ok) {
      throw new Error('Status request failed');
    } else {
      const data = await response.json();
      enabled = data.enabled === true;
      state = data.state || 'unavailable';
    }
  } catch (_) {
    // A temporary backend interruption should not claim the model is stopped.
    if (enabled) state = 'unavailable';
  }
  render();
}

async function toggle() {
  if (!enabled || pending || !['ready', 'offline', 'suspended'].includes(state)) return;
  const action = state === 'ready' ? 'suspend' : 'start';
  pending = true;
  render();
  try {
    const response = await fetch('/api/strata/' + action, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || 'Strata action failed');
    state = result.state || 'unavailable';
    showToast(state === 'ready' ? 'Gyro-S ready' :
      state === 'suspended' ? 'Gyro-S suspended, GPU memory released' :
      state === 'starting' ? 'Starting Strata with Gyro-S…' : 'Strata: ' + state);
  } catch (error) {
    showError(error.message || 'Could not control Strata');
  } finally {
    pending = false;
    render();
    await refresh();
  }
}

if (button && label) {
  button.addEventListener('click', toggle);
  refresh();
  setInterval(() => {
    if (!document.hidden) refresh();
  }, 7000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
}
