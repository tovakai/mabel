// Persona sidebar (Phase 01 prompt stitching).
//
// Shows Mabel's current mood and how the last turn was composed, and lets an
// admin edit her identity modules (identity/core.md, functions/*.md,
// moods/*.md), switch stitching off / shadow / on, and pin a mood for a while.
// Module text is validated server-side by the same parser that serves it.

import uiModule from './ui.js?v=20260916largetoolscroll1';

const esc = uiModule.esc;
const toast = (msg) => uiModule.showToast(msg);
const API = `${window.location.origin}/api/persona`;
const MODE_HELP = {
  off: 'Off — the preset system prompt is used as-is.',
  shadow: 'Shadow — the stitched prompt is composed and logged, but the preset prompt is served.',
  on: 'On — identity replaces the preset prompt; a short mode + mood note rides along each turn.',
};
let data = null;

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function card(title, inner) {
  return `<div class="admin-card" style="margin-bottom:10px">
    <h2 style="margin:0 0 6px;padding:0;line-height:1.2;font-size:14px">${esc(title)}</h2>${inner}</div>`;
}

function statusCard() {
  const m = data.mood;
  const source = { time_of_day: 'from the time of day', hysteresis: 'holding briefly before changing',
    manual: `pinned until ${when(m.manual_until)}` }[m.source] || m.source;
  const schedule = m.schedule.map(s =>
    `<span style="${s.mood === m.scheduled ? 'font-weight:600' : 'opacity:0.65'}">${esc(s.from)} ${esc(s.mood)}</span>`,
  ).join(' · ');
  const last = data.last_composition;
  const lastLine = last
    ? `Last turn: <strong>${esc(last.function)}</strong> (${esc(last.function_reason)}), mood ${esc(last.mood)}, ${esc(last.mode)}${last.fallback ? `, fallback: ${esc(last.fallback)}` : ''}`
    : 'No turn composed since the last restart.';
  let pin = '';
  if (data.can_manage) {
    const moods = data.modules.filter(x => x.kind === 'mood').map(x =>
      `<option value="${esc(x.name)}"${x.name === m.current ? ' selected' : ''}>${esc(x.name)}</option>`).join('');
    pin = `<div style="display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap">
      <span style="font-size:12px">Pin mood</span>
      <select id="persona-pin-mood" class="memory-edit-cat-select">${moods}</select>
      <span style="font-size:12px">for</span>
      <select id="persona-pin-hours" class="memory-edit-cat-select">
        <option value="1">1 h</option><option value="2" selected>2 h</option><option value="4">4 h</option><option value="8">8 h</option>
      </select>
      <button type="button" class="theme-io-btn" data-persona-action="pin" style="height:26px;font-size:12px">Pin</button>
      ${m.source === 'manual' ? '<button type="button" class="theme-io-btn" data-persona-action="unpin" style="height:26px;font-size:12px">Back to schedule</button>' : ''}
    </div>`;
  }
  return card('Right now', `
    <div style="font-size:13px">Mood: <strong>${esc(m.current)}</strong> <span style="opacity:0.65">(${esc(source)}, since ${esc(when(m.since))})</span></div>
    <div style="font-size:12px;opacity:0.8;margin-top:2px">Energy ${Math.round(m.energy * 100)}% · local time ${esc(m.local_time)}</div>
    <div style="font-size:12px;margin-top:6px">Night-owl schedule: ${schedule}</div>
    <div style="font-size:12px;margin-top:6px;opacity:0.85">${lastLine}</div>
    ${pin}
    <div style="margin-top:8px"><button type="button" class="theme-io-btn" data-persona-action="diagnostics" style="height:24px;font-size:11px">Copy diagnostics</button></div>`);
}

function modeCard() {
  const buttons = ['off', 'shadow', 'on'].map(mode =>
    `<button type="button" class="theme-io-btn" data-persona-mode="${mode}" ${data.can_manage ? '' : 'disabled'}
      style="height:26px;font-size:12px;${mode === data.mode ? 'outline:2px solid var(--accent, var(--red));' : 'opacity:0.7;'}">${mode}</button>`,
  ).join('');
  return card('Prompt stitching', `<div style="display:flex;gap:6px">${buttons}</div>
    <div class="admin-toggle-sub" style="margin-top:6px">${esc(MODE_HELP[data.mode] || '')}</div>
    <div class="admin-toggle-sub">Only applies to the M.A.B.E.L. preset. Tool permissions and approvals are never affected.</div>`);
}

function moduleEditor(mod) {
  const key = `${mod.kind}/${mod.name}`;
  const label = mod.kind === 'core' ? 'Identity (core)' : mod.name;
  const meta = `v${mod.version} · ${mod.sha}${mod.mood_influence ? ` · mood influence ${esc(mod.mood_influence)}` : ''}`;
  const rows = mod.kind === 'core' ? 18 : 9;
  return `<details style="margin:4px 0">
    <summary style="cursor:pointer;font-size:13px"><strong>${esc(label)}</strong> <span style="opacity:0.6;font-size:11px">${meta}</span></summary>
    <textarea data-persona-text="${esc(key)}" rows="${rows}" spellcheck="true" ${data.can_manage ? '' : 'readonly'}
      style="width:100%;box-sizing:border-box;margin-top:6px;font-family:var(--mono, monospace);font-size:12px;background:var(--bg);color:var(--fg);border:1px solid color-mix(in srgb, var(--fg) 20%, transparent);border-radius:6px;padding:6px">${esc(mod.text)}</textarea>
    ${data.can_manage ? `<div style="display:flex;justify-content:flex-end;margin-top:4px">
      <button type="button" class="theme-io-btn" data-persona-save="${esc(key)}" style="height:26px;font-size:12px">Save</button></div>` : ''}
  </details>`;
}

function modulesCard() {
  const group = (kind, title, hint) => {
    const mods = data.modules.filter(m => m.kind === kind);
    return `<div style="margin-top:8px"><div style="font-size:12px;font-weight:600">${esc(title)}</div>
      <div class="admin-toggle-sub" style="margin:0 0 2px">${esc(hint)}</div>
      ${mods.length ? mods.map(moduleEditor).join('') : '<div class="admin-toggle-sub">None loaded.</div>'}</div>`;
  };
  const errors = (data.errors || []).map(e =>
    `<div style="font-size:12px;color:var(--red)">${esc(e.path.split('/').slice(-2).join('/'))}: ${esc(e.error)}${e.serving ? ` (still serving ${esc(e.serving)})` : ''}</div>`,
  ).join('');
  return card('Modules', `
    <div class="admin-toggle-sub">Stored in <code>${esc(data.module_root)}</code> (your vault; synced, never committed). Edits take effect on the next message. Bump <code>version</code> when you change one.</div>
    ${errors}
    ${group('core', 'Identity', 'Who she is. Replaces the preset system prompt when stitching is on.')}
    ${group('function', 'Modes', 'Chosen per turn from what you asked. Prose is only served for the social mode; technical modes get a one-line mood.')}
    ${group('mood', 'Moods', 'Picked from the night-owl schedule above, or pinned.')}`);
}

function render() {
  const body = document.getElementById('persona-body');
  if (!body) return;
  if (!data) { body.innerHTML = '<div class="admin-toggle-sub" style="padding:12px">Loading…</div>'; return; }
  const open = new Set([...body.querySelectorAll('details[open] textarea')].map(t => t.dataset.personaText));
  body.innerHTML = statusCard() + modeCard() + modulesCard();
  body.querySelectorAll('details textarea').forEach(t => {
    if (open.has(t.dataset.personaText)) t.closest('details').open = true;
  });
}

async function load() {
  try {
    data = await api('');
  } catch (err) {
    data = null;
    const body = document.getElementById('persona-body');
    if (body) body.innerHTML = `<div class="admin-toggle-sub" style="padding:12px">Could not load persona: ${esc(err.message)}</div>`;
    return;
  }
  render();
}

async function onClick(event) {
  const target = event.target.closest('[data-persona-mode], [data-persona-save], [data-persona-action]');
  if (!target || target.disabled) return;
  try {
    if (target.dataset.personaMode) {
      await api('/mode', { method: 'PUT', body: JSON.stringify({ mode: target.dataset.personaMode }) });
      toast(`Prompt stitching: ${target.dataset.personaMode}`);
    } else if (target.dataset.personaSave) {
      const key = target.dataset.personaSave;
      const text = document.querySelector(`textarea[data-persona-text="${CSS.escape(key)}"]`)?.value ?? '';
      const result = await api(`/modules/${key}`, { method: 'PUT', body: JSON.stringify({ text }) });
      toast(`Saved ${result.ref}`);
    } else if (target.dataset.personaAction === 'pin') {
      const mood = document.getElementById('persona-pin-mood')?.value;
      const hours = Number(document.getElementById('persona-pin-hours')?.value || 2);
      await api('/mood-override', { method: 'POST', body: JSON.stringify({ mood, hours }) });
      toast(`Mood pinned: ${mood}`);
    } else if (target.dataset.personaAction === 'unpin') {
      await api('/mood-override', { method: 'DELETE' });
      toast('Mood follows the schedule again');
    } else if (target.dataset.personaAction === 'diagnostics') {
      const diag = { mode: data.mode, mood: data.mood, last_composition: data.last_composition,
        modules: data.modules.map(m => m.ref), errors: data.errors };
      await navigator.clipboard.writeText(JSON.stringify(diag, null, 2));
      toast('Diagnostics copied');
      return;
    }
  } catch (err) {
    toast(`Persona: ${err.message}`);
    return;
  }
  await load();
}

export function openPersonaModal() {
  const modal = document.getElementById('persona-modal');
  if (!modal) return;
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  render();
  load();
}

function closePersonaModal() {
  const modal = document.getElementById('persona-modal');
  if (modal) modal.classList.add('hidden');
}


// The M.A.B.E.L. preset's system prompt is the fallback for when stitching is
// off; her identity lives in Persona. Lock the field (with an escape hatch for
// editing the fallback) whenever the preset name is Mabel.
let presetUnlocked = false;

function syncPresetLock() {
  const name = document.getElementById('custom-character-name');
  const prompt = document.getElementById('custom-system-prompt');
  if (!name || !prompt) return;
  const isMabel = name.value.toLowerCase().replace(/[^a-z]/g, '') === 'mabel';
  const locked = isMabel && !presetUnlocked;
  prompt.readOnly = locked;
  prompt.style.opacity = locked ? '0.6' : '';
  document.getElementById('char-expand-btn')?.toggleAttribute('disabled', locked);
  let note = document.getElementById('persona-preset-note');
  if (!isMabel) { note?.remove(); presetUnlocked = false; return; }
  if (!note) {
    note = document.createElement('div');
    note.id = 'persona-preset-note';
    note.className = 'admin-toggle-sub';
    note.style.margin = '2px 0 6px';
    prompt.closest('.char-prompt-wrap')?.before(note);
  }
  note.innerHTML = locked
    ? 'Mabel\'s identity is managed in <a href="#" data-persona-open>Persona</a>. This prompt is only the fallback when stitching is off. <a href="#" data-persona-unlock>Edit the fallback anyway</a>'
    : 'Editing the fallback prompt (used only when stitching is off). Her identity is in <a href="#" data-persona-open>Persona</a>.';
}

function initPresetLock() {
  document.addEventListener('input', (event) => {
    if (event.target.id === 'custom-character-name') syncPresetLock();
  });
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-persona-unlock]')) {
      event.preventDefault();
      presetUnlocked = true;
      syncPresetLock();
    } else if (event.target.closest('[data-persona-open]')) {
      event.preventDefault();
      openPersonaModal();
    }
  });
  // Values are filled programmatically when the prompt window opens or a
  // template is picked; re-check whenever that window's visibility changes.
  const modal = document.getElementById('custom-preset-modal');
  if (modal) new MutationObserver(() => { presetUnlocked = false; syncPresetLock(); })
    .observe(modal, { attributes: true, attributeFilter: ['class', 'style'] });
  document.getElementById('custom-character-name')?.addEventListener('change', syncPresetLock);
  document.getElementById('char-template-select')?.addEventListener('change', () => setTimeout(syncPresetLock, 0));
  syncPresetLock();
}

function init() {
  document.getElementById('tool-persona-btn')?.addEventListener('click', openPersonaModal);
  document.getElementById('close-persona-modal')?.addEventListener('click', closePersonaModal);
  document.getElementById('persona-body')?.addEventListener('click', onClick);
  document.getElementById('persona-modal')?.addEventListener('click', (event) => {
    if (event.target.id === 'persona-modal') closePersonaModal();
  });
  initPresetLock();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { openPersonaModal };
