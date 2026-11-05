// Persona sidebar (Phase 01 prompt stitching).
//
// Shows Mabel's current mood and how the last turn was composed, and lets an
// admin edit her identity modules (identity/core.md, functions/*.md,
// moods/*.md), switch stitching off / shadow / on, and pin a mood for a while.
// Module text is validated server-side by the same parser that serves it.

import uiModule from './ui.js?v=20260916largetoolscroll1';
import { mdToHtml } from './markdown.js';

const esc = uiModule.esc;
const toast = (msg) => uiModule.showToast(msg);
const API = `${window.location.origin}/api/persona`;
const MODE_HELP = {
  off: 'Off — the preset system prompt is used as-is.',
  shadow: 'Shadow — the stitched prompt is composed and logged, but the preset prompt is served.',
  on: 'On — identity replaces the preset prompt; a short mode + mood note rides along each turn.',
};
let data = null;
let research = null;      // /api/persona/research
let inbox = null;         // /api/persona/inbox
let inboxTimer = null;
const LOOP_LABELS = { archivist: 'Vault tidying (Archivist)', scout: 'Research leads (Scout)',
  dreamer: 'Things you keep talking about (Dreamer)', sentinel: 'Changes (Sentinel)' };
let openArticle = null;   // { file, html }
let pollTimer = null;
const STEP_LABELS = { candidates: 'looking at what your notes link to', choice: 'choosing what to research',
  evidence: 'searching and reading sources', draft: 'writing the article', quotes: 'checking her quotes',
  write: 'verifying and saving' };

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
    conversation_warmup: 'woken up by chatting', conversation_lively: 'a lively conversation',
    background_busy: 'busy with background work',
    manual: `pinned until ${when(m.manual_until)}` }[m.source] || m.source;
  const a = data.activity || {};
  const r = a.rules || {};
  const gap = a.gap_hours == null ? 'no conversation recorded yet'
    : a.gap_hours < 1 ? 'last talked just now' : `last talked ${a.gap_hours} h ago`;
  const activity = `Recent chat ≈${a.conversation_tokens || 0} tokens (warm-up ${r.warm_up}, lively ${r.lively}) · background ≈${a.background_tokens || 0} (busy ${r.busy_background}) · ${esc(gap)}`;
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
    <div style="font-size:12px;margin-top:4px;opacity:0.85">${activity}</div>
    <div style="font-size:12px;margin-top:6px;opacity:0.85">${lastLine}</div>
    ${pin}
    <div style="margin-top:8px"><button type="button" class="theme-io-btn" data-persona-action="diagnostics" style="height:24px;font-size:11px">Copy diagnostics</button></div>`);
}

function whisperCard() {
  const w = data.whisper;
  let current = '<div class="admin-toggle-sub">No whisper right now.</div>';
  if (w) {
    const status = data.whisper_servable ? 'can come up in casual chat'
      : `came up ${when(w.last_discussed_at)}; quiet for ${data.whisper_cooldown_hours} h`;
    current = `<div style="font-size:13px"><strong>${esc(w.topic)}</strong> <span style="opacity:0.65;font-size:11px">${esc(w.origin === 'user_seeded' ? 'seeded by you' : `her own (${w.source || 'auto'})`)} · ${esc(status)} · expires ${esc(new Date(w.expires_at).toLocaleDateString())}</span></div>
      <div style="font-size:12px;margin-top:4px;font-style:italic">${esc(w.thought)}</div>
      ${w.reference ? `<div class="admin-toggle-sub">Ref: ${esc(w.reference)}</div>` : ''}
      ${data.can_manage ? '<div style="display:flex;justify-content:flex-end;margin-top:4px"><button type="button" class="theme-io-btn" data-persona-action="retire-whisper" style="height:24px;font-size:11px">Retire</button></div>' : ''}`;
  }
  const field = 'width:100%;box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid color-mix(in srgb, var(--fg) 20%, transparent);border-radius:6px;padding:5px;font-size:12px';
  const form = data.can_manage ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px">${w ? 'Replace with a new thought' : 'Seed a thought'}</summary>
      <input id="persona-whisper-topic" maxlength="80" placeholder="Topic, e.g. Serial Experiments Lain" style="${field};margin-top:6px">
      <textarea id="persona-whisper-thought" maxlength="400" rows="3" placeholder="In her words: I've been thinking about how Lain portrays artificial identity…" style="${field};margin-top:4px"></textarea>
      <input id="persona-whisper-ref" maxlength="300" placeholder="Optional reference (note or URL)" style="${field};margin-top:4px">
      <div style="display:flex;gap:6px;align-items:center;justify-content:flex-end;margin-top:4px">
        <span style="font-size:12px">keep for</span>
        <select id="persona-whisper-days" class="memory-edit-cat-select"><option value="3">3 days</option><option value="7">7 days</option><option value="14" selected>14 days</option><option value="30">30 days</option></select>
        <button type="button" class="theme-io-btn" data-persona-action="seed-whisper" style="height:26px;font-size:12px">Save</button>
      </div></details>` : '';
  return card('Curiosity whisper', `<div class="admin-toggle-sub" style="margin-top:0">One continuing thought she may mention in casual chat if it naturally connects. After it comes up it stays quiet for a while. Her own research can set it automatically, but never replaces one you seeded.</div>${current}${form}`);
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

function researchCard() {
  if (!research) return card('Research', '<div class="admin-toggle-sub">Loading…</div>');
  const st = research.status || {};
  const sess = st.session;
  let now = '';
  if (st.running && sess) {
    now = `<div style="font-size:12px;margin:4px 0"><strong>Researching…</strong> ${esc(STEP_LABELS[sess.next_step] || sess.next_step || '')}${sess.subject ? ` · <em>${esc(sess.subject)}</em>` : ''}</div>
      ${sess.why ? `<div class="admin-toggle-sub">Why: ${esc(sess.why)}</div>` : ''}`;
  } else if (st.error) {
    now = `<div style="font-size:12px;color:var(--red);margin:4px 0">Last session stopped: ${esc(st.error)}</div>`;
  } else if (sess && sess.result) {
    const r = sess.result;
    now = `<div style="font-size:12px;margin:4px 0">Finished: <strong>${esc(sess.subject || '')}</strong> (${esc(r.status)}${r.whisper_outcome === 'set' || r.whisper_outcome === 'replaced_automatic' ? ', set her whisper' : ''})</div>`;
    const verdicts = (sess.dottie_decisions || []).map(d => `${d.decision === 'reject' ? 'rejected' : d.decision === 'keep' ? 'kept' : 'picked'} “${esc(d.title)}”${d.reason ? ` (${esc(d.reason)})` : ''}`);
    if (verdicts.length) now += `<div class="admin-toggle-sub">Dottie's suggestions: ${verdicts.join('; ')}</div>`;
  }
  const button = research.can_manage
    ? `<button type="button" class="theme-io-btn" data-persona-action="research-run" ${st.running ? 'disabled' : ''} style="height:26px;font-size:12px">${st.running ? 'Researching…' : 'Go research something'}</button>
       <span class="admin-toggle-sub" style="margin-left:6px">She picks the topic. Takes a few minutes and occupies her model.</span>`
    : '';
  const articles = research.articles.length ? research.articles.map(a => {
    const open = openArticle && openArticle.file === a.file;
    return `<div style="margin:6px 0">
      <a href="#" data-persona-action="open-article" data-file="${esc(a.file)}" style="font-size:13px;font-weight:600">${esc(a.title)}</a>
      <span class="admin-toggle-sub" style="margin-left:4px">${esc(a.status || '')} · ${esc((a.created || '').slice(0, 10))} · ${esc(String(a.chosen_from || '').replace(/_/g, ' '))}</span>
      ${a.question ? `<div class="admin-toggle-sub" style="margin:2px 0 0">${esc(a.question)}</div>` : ''}
      ${open ? `<div class="persona-article" style="margin-top:6px;padding:8px 10px;border:1px solid color-mix(in srgb, var(--fg) 15%, transparent);border-radius:6px;font-size:13px;max-height:420px;overflow-y:auto">${openArticle.html}</div>` : ''}
    </div>`;
  }).join('') : '<div class="admin-toggle-sub">No articles yet.</div>';
  const questions = research.open_questions.length
    ? '<ul style="margin:4px 0 0 18px;padding:0;font-size:12px">' + research.open_questions.map(q =>
      `<li>${esc(q.question)}${q.cooling ? ` <span style="opacity:0.6">(cooling off until ${esc(new Date(q.eligible_after).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }))})</span>` : ''}</li>`).join('') + '</ul>'
    : '<div class="admin-toggle-sub">None yet.</div>';
  return card('Research', `${button}${now}
    <div style="font-size:12px;font-weight:600;margin-top:10px">Her articles</div>${articles}
    <div style="font-size:12px;font-weight:600;margin-top:10px">Questions she left herself</div>${questions}
    ${research.spawned.length ? `<div class="admin-toggle-sub" style="margin-top:6px">Links she wants to follow: ${research.spawned.map(esc).join(', ')}</div>` : ''}`);
}

// Refresh only the Research card, so polling never disturbs text being edited elsewhere.
function evidenceList(item) {
  return '<ul style="margin:4px 0 0 16px;padding:0;font-size:11px;opacity:0.85">' + item.evidence.slice(0, 6).map(e => {
    const ref = String(e.ref || '');
    const label = e.title || ref.replace(/^chat:[^/]+\//, 'message ').replace(/#L(\d+)$/, ' (line $1)');
    const link = /^https?:\/\//.test(ref) ? `<a href="${esc(ref)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label);
    const extra = e.excerpt || e.context;
    return `<li>${link}${extra ? `<div style="opacity:0.75">${esc(String(extra).slice(0, 220))}</div>` : ''}</li>`;
  }).join('') + '</ul>';
}

function inboxCard() {
  if (!inbox) return card("Dottie's inbox", '<div class="admin-toggle-sub">Loading…</div>');
  const groups = {};
  inbox.items.forEach(item => { (groups[item.loop] = groups[item.loop] || []).push(item); });
  const runs = Object.entries(inbox.last_runs || {}).map(([name, r]) => `${name} ${when(r.at)}`).join(' · ');
  const button = inbox.can_manage
    ? `<button type="button" class="theme-io-btn" data-persona-action="inbox-run" ${inbox.running ? 'disabled' : ''} style="height:26px;font-size:12px">${inbox.running ? 'Dottie is looking around…' : 'Let Dottie look around'}</button>
       <span class="admin-toggle-sub" style="margin-left:6px">Runs on Dottie's machine; never slows Mabel.</span>` : '';
  const body = Object.keys(groups).length ? Object.entries(groups).map(([loop, items]) => `
    <div style="font-size:12px;font-weight:600;margin-top:10px">${esc(LOOP_LABELS[loop] || loop)}</div>
    ${items.map(item => `<details style="margin:4px 0">
      <summary style="cursor:pointer;font-size:12px">${esc(item.title)} <span style="opacity:0.55;font-size:11px">${Math.round(item.confidence * 100)}% · ${esc(item.verification.replace('_', ' '))}</span></summary>
      ${item.summary ? `<div class="admin-toggle-sub" style="margin:2px 0">${esc(item.summary)}</div>` : ''}
      ${item.payload && item.payload.curiosity ? `<div style="font-size:12px;font-style:italic;margin:2px 0">${esc(item.payload.curiosity)}</div>` : ''}
      ${evidenceList(item)}
      ${inbox.can_manage ? `<div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px">
        <button type="button" class="theme-io-btn" data-persona-action="inbox-accept" data-item="${esc(item.id)}" style="height:24px;font-size:11px">Keep</button>
        <button type="button" class="theme-io-btn" data-persona-action="inbox-reject" data-item="${esc(item.id)}" style="height:24px;font-size:11px">Reject</button></div>` : ''}
    </details>`).join('')}`).join('') : '<div class="admin-toggle-sub">Nothing waiting.</div>';
  const decided = (inbox.decided || []).length ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px">Decided (${inbox.decided.length})</summary>
    <ul style="margin:4px 0 0 16px;padding:0;font-size:11px">${inbox.decided.map(i => `<li>${esc(i.status)} by ${esc(i.decided_by || '?')}: ${esc(i.title)}${i.decision_reason ? ` <span style="opacity:0.6">(${esc(i.decision_reason)})</span>` : ''}</li>`).join('')}</ul></details>` : '';
  return card("Dottie's inbox", `<div class="admin-toggle-sub" style="margin-top:0">Suggestions from Dottie's background loops. Rejecting keeps the item (and where it came from) so it isn't suggested again. Nothing here changes your notes.</div>
    ${button}${runs ? `<div class="admin-toggle-sub">Last runs: ${esc(runs)}</div>` : ''}${body}${decided}`);
}

function renderInbox() {
  const slot = document.getElementById('persona-inbox');
  if (slot) slot.innerHTML = inboxCard();
}

async function loadInbox() {
  clearTimeout(inboxTimer);
  try { inbox = await api('/inbox'); } catch (err) {
    inbox = { can_manage: false, running: false, items: [], decided: [], last_runs: {}, error: err.message };
  }
  renderInbox();
  const modal = document.getElementById('persona-modal');
  if (inbox.running && modal && !modal.classList.contains('hidden')) inboxTimer = setTimeout(loadInbox, 4000);
}

function renderResearch() {
  const slot = document.getElementById('persona-research');
  if (slot) slot.innerHTML = researchCard();
  else render();
}

async function loadResearch() {
  clearTimeout(pollTimer);
  try {
    research = await api('/research');
  } catch (err) {
    research = { can_manage: false, status: { error: err.message }, articles: [], open_questions: [], spawned: [] };
  }
  renderResearch();
  const modal = document.getElementById('persona-modal');
  if (research.status?.running && modal && !modal.classList.contains('hidden')) {
    pollTimer = setTimeout(loadResearch, 5000);
  }
}

function render() {
  const body = document.getElementById('persona-body');
  if (!body) return;
  if (!data) { body.innerHTML = '<div class="admin-toggle-sub" style="padding:12px">Loading…</div>'; return; }
  const open = new Set([...body.querySelectorAll('details[open] textarea')].map(t => t.dataset.personaText));
  body.innerHTML = statusCard() + `<div id="persona-research">${researchCard()}</div>`
    + `<div id="persona-inbox">${inboxCard()}</div>` + whisperCard() + modeCard() + modulesCard();
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
  if (target.dataset.personaAction === 'open-article') {
    event.preventDefault();
    const file = target.dataset.file;
    if (openArticle && openArticle.file === file) { openArticle = null; renderResearch(); return; }
    try {
      const art = await api(`/research/article?file=${encodeURIComponent(file)}`);
      openArticle = { file, html: mdToHtml(art.markdown) };
    } catch (err) { toast(`Persona: ${err.message}`); }
    renderResearch();
    return;
  }
  const inboxAction = target.dataset.personaAction;
  if (inboxAction === 'inbox-run' || inboxAction === 'inbox-accept' || inboxAction === 'inbox-reject') {
    try {
      if (inboxAction === 'inbox-run') {
        await api('/inbox/run', { method: 'POST' });
        toast('Dottie is looking around');
      } else {
        const accept = inboxAction === 'inbox-accept';
        const reason = accept ? '' : (window.prompt('Why reject it? (optional)') ?? null);
        if (reason === null) return;
        await api(`/inbox/${encodeURIComponent(target.dataset.item)}/decide`, { method: 'POST',
          body: JSON.stringify({ status: accept ? 'accepted' : 'rejected', reason }) });
      }
    } catch (err) { toast(`Persona: ${err.message}`); }
    await loadInbox();
    return;
  }
  if (target.dataset.personaAction === 'research-run') {
    try {
      await api('/research/run', { method: 'POST' });
      toast('Mabel is off researching something');
    } catch (err) { toast(`Persona: ${err.message}`); }
    await loadResearch();
    return;
  }
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
    } else if (target.dataset.personaAction === 'seed-whisper') {
      const value = (id) => document.getElementById(id)?.value || '';
      await api('/whisper', { method: 'PUT', body: JSON.stringify({
        topic: value('persona-whisper-topic'), thought: value('persona-whisper-thought'),
        reference: value('persona-whisper-ref'), days: Number(value('persona-whisper-days') || 14) }) });
      toast('Whisper saved');
    } else if (target.dataset.personaAction === 'retire-whisper') {
      await api('/whisper', { method: 'DELETE' });
      toast('Whisper retired');
    } else if (target.dataset.personaAction === 'unpin') {
      await api('/mood-override', { method: 'DELETE' });
      toast('Mood follows the schedule again');
    } else if (target.dataset.personaAction === 'diagnostics') {
      const diag = { mode: data.mode, mood: data.mood, activity: data.activity, last_composition: data.last_composition,
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
  loadResearch();
  loadInbox();
}

function closePersonaModal() {
  clearTimeout(pollTimer);
  clearTimeout(inboxTimer);
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
