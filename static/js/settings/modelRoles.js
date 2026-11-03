// Role policies extend the existing Settings panel and endpoint/model pickers.
export async function initModelRoles({ fetchEndpoints, fillEndpoints, fillModels, onEndpointsRefresh }) {
  const panel = document.querySelector('[data-settings-panel="ai"]');
  if (!panel || panel.querySelector('[data-model-role-routing]')) return;
  const marker = document.createElement('div');
  marker.dataset.modelRoleRouting = 'true';
  panel.appendChild(marker);
  try {
    const [response, initialEndpoints] = await Promise.all([
      fetch('/api/model-roles', { credentials: 'same-origin' }), fetchEndpoints(),
    ]);
    if (!response.ok) throw new Error('Could not load model roles');
    const data = await response.json();
    let endpoints = initialEndpoints;
    const rolePickers = [];
    let cachedModels = [];
    try {
      const models = await fetch('/api/embeddings/models', { credentials: 'same-origin' });
      if (models.ok) cachedModels = (await models.json()).filter(m => m.downloaded);
    } catch (_) { /* CPU cache is optional. No downloads are started here. */ }
    const anchors = {
      default: 'set-defaultModelSelect', utility: 'set-utilityModelSelect',
      research: 'set-researchModel', vision: 'set-vlModelSelect',
      image: 'set-imgModelSelect', teacher: 'set-teacherModelSelect',
      tts: 'set-ttsModelSelect', stt: 'set-sttModelSelect',
    };
    for (const role of data.roles) {
      let host = document.getElementById(anchors[role.role])?.closest('.admin-card');
      const hasPrimary = Boolean(host);
      if (!host) {
        host = document.createElement('div');
        host.className = 'admin-card';
        const title = document.createElement('h2');
        title.textContent = role.label;
        host.appendChild(title);
        marker.appendChild(host);
      }
      const section = document.createElement('details');
      section.dataset.role = role.role;
      const summary = document.createElement('summary');
      summary.textContent = 'Model routing and optional fallback';
      section.appendChild(summary);
      host.appendChild(section);
      function row(label, control) {
        const wrap = document.createElement('div');
        wrap.className = 'settings-row';
        const text = document.createElement('label');
        text.className = 'settings-label'; text.textContent = label;
        control.classList.add('settings-select');
        wrap.append(text, control); section.appendChild(wrap);
        return control;
      }
      function input(label, value, type = 'text') {
        const node = document.createElement('input'); node.type = type;
        node.value = value ?? ''; return row(label, node);
      }
      function picker(label, selected) {
        const ep = row(label + ' endpoint', document.createElement('select'));
        const model = row(label + ' model', document.createElement('select'));
        // Shared fill helpers only *retain* blank options. They do not create
        // them, so a new picker without this placeholder silently selects the
        // first endpoint/model, including for an optional fallback.
        for (const [select, text] of [[ep, label === 'Primary' ? 'Choose endpoint' : 'None'],
                                      [model, 'Choose model']]) {
          const blank = document.createElement('option');
          blank.value = '';
          blank.textContent = text;
          select.appendChild(blank);
        }
        function models(value) {
          const entry = endpoints.find(e => e.id === ep.value);
          fillModels(model, [...new Set([...(entry?.models || []), ...(entry?.pinned_models || [])])],
            value, true);
        }
        function refresh() {
          // This module used to keep its original endpoint snapshot forever.
          // The legacy research picker subscribes to registry refreshes.
          if (ep.disabled) return; // Local fastembed uses a separate model list.
          const endpointId = ep.value;
          const modelId = model.value;
          fillEndpoints(ep, endpoints, endpointId, true);
          models(ep.value === endpointId ? modelId : '');
        }
        fillEndpoints(ep, endpoints, selected?.endpoint_id || '', true);
        models(selected?.model || '');
        ep.addEventListener('change', () => models(''));
        const result = { ep, model, refresh };
        rolePickers.push(result);
        return result;
      }
      const primary = hasPrimary ? null : picker('Primary', role.primary);
      if (hasPrimary) {
        const current = document.createElement('p');
        current.textContent = 'Primary uses the existing selection above. Saving routing does not change it.';
        section.appendChild(current);
      }
      const fallback = picker('Fallback (optional)', role.policy.fallback || role.effective_fallback);
      const adapter = row('Primary adapter', document.createElement('select'));
      const adapters = role.role === 'embedding'
        ? ['openai_embeddings', 'ollama_embed', 'fastembed_cached']
        : role.role === 'reranking' ? ['rerank', 'tei_rerank']
        : role.role === 'image' ? ['existing', 'openai_image']
        : role.role === 'tts' ? ['existing', 'openai_tts']
        : role.role === 'stt' ? ['existing', 'openai_stt'] : ['existing', 'openai_tools'];
      for (const name of adapters) adapter.add(new Option(name.replaceAll('_', ' '), name));
      adapter.value = adapters.includes(role.primary?.adapter) ? role.primary.adapter : adapters[0];
      const fallbackAdapter = row('Fallback adapter', document.createElement('select'));
      for (const name of adapters) fallbackAdapter.add(new Option(name.replaceAll('_', ' '), name));
      fallbackAdapter.value = role.policy.fallback?.adapter || role.effective_fallback?.adapter || adapter.value;
      const caps = input('Capabilities (explicit)', (role.primary?.capabilities?.length
        ? role.primary.capabilities : role.role === 'research' ? ['chat', 'tools'] : [role.required_capability]).join(','));
      const fallbackCaps = input('Fallback capabilities', (role.policy.fallback?.capabilities?.length
        ? role.policy.fallback.capabilities : role.role === 'research' ? ['chat', 'tools'] : [role.required_capability]).join(','));
      const primaryContext = input('Primary context tokens', role.primary?.max_context || 16384, 'number');
      const fallbackContext = input('Fallback context tokens', role.policy.fallback?.max_context || 16384, 'number');
      const timeout = input('Timeout seconds', role.policy.timeout, 'number');
      const retries = input('Inference retries (0–2)', role.policy.retries, 'number');
      const cooldown = input('Recovery probe interval', role.policy.cooldown, 'number');
      let identity, revision, dimension, fallbackIdentity, fallbackRevision, fallbackDimension;
      if (role.role === 'embedding') {
        identity = input('Primary embedding space', role.primary?.space_id);
        revision = input('Primary revision', role.primary?.revision);
        dimension = input('Primary dimensions', role.primary?.dimensions || '', 'number');
        fallbackIdentity = input('Fallback embedding space', role.policy.fallback?.space_id);
        fallbackRevision = input('Fallback revision', role.policy.fallback?.revision);
        fallbackDimension = input('Fallback dimensions', role.policy.fallback?.dimensions || '', 'number');
      }
      let limits;
      if (role.role === 'research') {
        limits = row('Research limits (JSON)', document.createElement('textarea'));
        limits.value = JSON.stringify(role.policy.limits || {
          max_calls: 24, max_concurrency: 3, max_hops: 2, max_chunks: 32,
          max_bytes: 24000, max_seconds: 90,
        });
      }
      if (role.role === 'embedding') {
        function cpuPicker(picker, selectedAdapter, current) {
          if (!picker) return;
          const update = () => {
            picker.ep.disabled = selectedAdapter.value === 'fastembed_cached';
            if (picker.ep.disabled) {
              picker.ep.value = '';
              fillModels(picker.model, cachedModels.map(m => m.model), current || picker.model.value, true);
            } else {
              picker.refresh();
            }
          };
          selectedAdapter.addEventListener('change', update);
          update();
        }
        cpuPicker(primary, adapter, role.primary?.model);
        cpuPicker(fallback, fallbackAdapter, role.policy.fallback?.model);
      }
      const status = document.createElement('p');
      status.setAttribute('role', 'status');
      const diag = role.diagnostics || {};
      status.textContent = diag.fallback_active
        ? 'Fallback active: ' + diag.model
        : diag.model ? 'Last request: ' + diag.model : 'Not probed; no model is loaded by opening Settings.';
      section.appendChild(status);
      const save = document.createElement('button');
      save.type = 'button'; save.className = 'settings-fallback-add'; save.textContent = 'Save routing';
      section.appendChild(save);
      save.addEventListener('click', async () => {
        save.disabled = true;
        try {
          const capabilities = caps.value.split(',').map(s => s.trim()).filter(Boolean);
          const assignment = {
            endpoint_id: primary ? primary.ep.value : role.primary?.endpoint_id || '',
            model: primary ? primary.model.value : role.primary?.model || '',
            adapter: adapter.value, capabilities, max_context: Number(primaryContext.value),
          };
          const fb = { endpoint_id: fallback.ep.value, model: fallback.model.value,
            adapter: fallbackAdapter.value, max_context: Number(fallbackContext.value),
            capabilities: fallbackCaps.value.split(',').map(s => s.trim()).filter(Boolean) };
          if (identity) {
            Object.assign(assignment, { space_id: identity.value, revision: revision.value, dimensions: Number(dimension.value) });
            Object.assign(fb, { space_id: fallbackIdentity.value, revision: fallbackRevision.value, dimensions: Number(fallbackDimension.value) });
          }
          const body = { fallback: fb.model ? fb : null, timeout: Number(timeout.value),
            retries: Number(retries.value), cooldown: Number(cooldown.value) };
          if (limits) body.limits = JSON.parse(limits.value);
          // Resolve the currently visible picker rather than a stale snapshot.
          // Primary fields are posted only for cards this extension owns.
          if (primary) body.primary = assignment.model ? assignment : null;
          else if (assignment.model) body.primary_metadata = { ...assignment };
          const result = await fetch('/api/model-roles/' + encodeURIComponent(role.role), {
            method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const payload = await result.json();
          if (!result.ok) throw new Error(payload.detail || 'Could not save routing');
          status.textContent = 'Saved. Fallback is optional; inference changes never replay executed tools.';
        } catch (error) { status.textContent = error.message; }
        finally { save.disabled = false; }
      });
    }
    // Subscribe to the exact same endpoint-refresh event as the legacy AI
    // selectors. Preserve unsaved choices while updating available models.
    if (typeof onEndpointsRefresh === 'function') {
      onEndpointsRefresh(nextEndpoints => {
        endpoints = Array.isArray(nextEndpoints) ? nextEndpoints : [];
        for (const picker of rolePickers) picker.refresh();
      });
    }
  } catch (error) {
    marker.textContent = error.message;
  }
}
