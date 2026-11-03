// A view over server-owned, redacted diagnostics. It never controls the run.
export function formatTrace(event) {
  const stamp = String(event.timestamp || '').replace(/^.*T/, '').replace(/\+00:00$/, 'Z');
  const duration = Number.isFinite(event.duration_ms) ? ` ${event.duration_ms.toFixed(1)}ms` : '';
  const details = Object.entries(event.metadata || {}).map(([k,v]) => `${k}=${Array.isArray(v) ? v.join(',') : v}`).join(' ');
  const line = `[${stamp}] [${event.category}] [${event.severity}] ${event.operation} ${event.status}${duration}`
    + (event.invocation_id ? ` id=${event.invocation_id}` : '') + (details ? ` ${details}` : '');
  if (event.operation !== 'turn' || !['completed','cancelled','error'].includes(event.status)) return line;
  const m=event.metadata || {}, value=(key,unit='')=>m[key] == null ? 'unavailable' : `${m[key]}${unit}`;
  return `${line}\nTURN SUMMARY\nWall-clock: ${Number.isFinite(event.duration_ms) ? event.duration_ms.toFixed(1)+'ms' : 'unavailable'}\n`
    + `Model requests: ${value('model_requests')}\nCumulative input: ${value('input_tokens')}\nGenerated tokens: ${value('output_tokens')}\n`
    + `Current context: ${value('request_context_tokens')} / ${value('context_length')} (${value('context_usage_source')})\n`
    + `Prompt processing: ${value('prefill_ms','ms')} (${value('prefill_samples')} requests reported)\n`
    + `Generation time: ${value('generation_ms','ms')} (${value('generation_samples')} requests reported)\nModel request time: ${value('model_execution_ms','ms')}\n`
    + `Tool execution time: ${value('tool_execution_ms','ms')}\nApproval wait: ${value('approval_wait_ms','ms')} (${value('approval_wait_source')})\nModel retries: ${value('retries')}\nFallbacks: ${value('fallbacks')}\nErrors: ${value('errors')}`;
}

export function matchesTrace(event, filters) {
  return (!filters.category || event.category === filters.category)
    && (!filters.severity || event.severity === filters.severity)
    && (!filters.turn || event.turn_id === filters.turn)
    && (!filters.query || (formatTrace(event) + ' ' + JSON.stringify(event)).toLowerCase().includes(filters.query.toLowerCase()));
}

export function initDebugConsole() {
  const controls=document.querySelector('.chat-input-right');
  if (!controls || document.getElementById('debug-console')) return;
  const button=document.createElement('button');
  button.type='button'; button.className='debug-console-toggle'; button.title='Debug Console';
  button.setAttribute('aria-label','Open Debug Console'); button.textContent='>_'; controls.prepend(button);
  const panel=document.createElement('section');
  panel.id='debug-console'; panel.className='debug-console'; panel.hidden=true;
  panel.setAttribute('aria-label','Debug Console');
  panel.innerHTML=`<header><strong>Debug Console</strong><span class="debug-status" role="status"></span>
    <button type="button" data-action="pause">Pause</button><button type="button" data-action="expand">Full screen</button>
    <button type="button" data-action="close" aria-label="Close Debug Console">×</button></header>
    <div class="debug-controls"><label>Conversation <select data-field="session"></select></label>
    <label>Turn <select data-field="turn"><option value="">All turns</option></select></label>
    <label>Category <select data-field="category"><option value="">All categories</option><option>MODEL</option><option>TOOL</option><option>RAG</option><option>RUNTIME</option><option>AUTH</option><option>MEMORY</option></select></label>
    <label>Severity <select data-field="severity"><option value="">All levels</option><option>DEBUG</option><option>INFO</option><option>WARN</option><option>ERROR</option></select></label>
    <label>Search <input data-field="query" type="search" autocomplete="off"></label></div>
    <div class="debug-controls"><button type="button" data-action="clear">Clear visible</button>
    <button type="button" data-action="copy">Copy visible</button><button type="button" data-action="selection">Copy selection</button>
    <button type="button" data-action="turn">Copy complete turn</button><button type="button" data-action="log">Export .log</button>
    <button type="button" data-action="jsonl">Export .jsonl</button></div>
    <p class="debug-note">Arguments and results show structure and sizes; private text and credentials are not stored.</p>
    <div class="debug-output" tabindex="0" aria-label="Diagnostic events"></div>`;
  document.body.appendChild(panel);
  const output=panel.querySelector('.debug-output'), status=panel.querySelector('.debug-status');
  const field=name=>panel.querySelector(`[data-field="${name}"]`);
  const action=name=>panel.querySelector(`[data-action="${name}"]`);
  const state={ session:'', cursor:0, events:[], turnEvents:null, clear:0, paused:false, fetching:false, epoch:0, pendingRender:false };
  const filters=()=>({category:field('category').value,severity:field('severity').value,
    turn:field('turn').value,query:field('query').value.trim()});

  function sessions(preferred) {
    const current=preferred || window.sessionModule?.getCurrentSessionId?.() || '';
    const entries=window.sessionModule?.getSessions?.() || [];
    const select=field('session'); select.replaceChildren();
    const list=Array.isArray(entries) ? entries : [];
    for (const item of list) {
      const id=String(item.id || item.session_id || ''); if (!id) continue;
      const option=document.createElement('option'); option.value=id;
      option.textContent=String(item.name || item.title || id); select.appendChild(option);
    }
    if (current && ![...select.options].some(option=>option.value===current)) {
      const option=document.createElement('option'); option.value=current; option.textContent=current; select.appendChild(option);
    }
    select.value=current;
  }
  function chooseSession(id) {
    state.epoch++; state.session=id || ''; state.cursor=0; state.clear=0; state.events=[]; state.turnEvents=null;
    field('turn').replaceChildren(new Option('All turns','')); render();
    if (state.session) void poll();
  }
  function updateTurns(events) {
    const select=field('turn'), selected=select.value;
    for (const event of events) if (event.turn_id && ![...select.options].some(o=>o.value===event.turn_id)) {
      const option=document.createElement('option'); option.value=event.turn_id;
      option.textContent=`${event.timestamp || ''} ${event.turn_id.slice(-8)}`; select.appendChild(option);
    }
    select.value=selected;
  }
  function render() {
    if (state.paused) return;
    const selection=window.getSelection();
    if (selection && !selection.isCollapsed && output.contains(selection.anchorNode)) { state.pendingRender=true; return; }
    state.pendingRender=false;
    const nearBottom=output.scrollHeight-output.scrollTop-output.clientHeight<40;
    const previousTop=output.scrollTop;
    const open=new Set([...output.querySelectorAll('details[open]')].map(node=>node.dataset.seq));
    const source=field('turn').value && state.turnEvents ? state.turnEvents : state.events;
    const matches=source.filter(event=>event.seq>state.clear && matchesTrace(event,filters()));
    const visible=matches.slice(-500);
    output.replaceChildren();
    if (matches.length>visible.length) {
      const note=document.createElement('p'); note.textContent=`Showing the latest ${visible.length}; full retained trace is available via export.`; output.appendChild(note);
    }
    for (const event of visible) {
      const details=document.createElement('details'), summary=document.createElement('summary'), pre=document.createElement('pre');
      details.dataset.seq=String(event.seq); details.open=open.has(details.dataset.seq);
      summary.textContent=formatTrace(event); pre.textContent=JSON.stringify(event,null,2);
      details.append(summary,pre); output.appendChild(details);
    }
    if (nearBottom) output.scrollTop=output.scrollHeight;
    else output.scrollTop=previousTop;
  }
  async function page(after,turn='') {
    const params=new URLSearchParams({after:String(after),limit:'1000'}); if (turn) params.set('turn',turn);
    const response=await fetch(`/api/chat/debug/${encodeURIComponent(state.session)}?${params}`,{credentials:'same-origin'});
    if (!response.ok) throw new Error(`Trace unavailable (${response.status})`);
    return response.json();
  }
  async function poll() {
    if (!state.session || state.fetching || panel.hidden) return;
    state.fetching=true; const epoch=state.epoch;
    try {
      let changed=false;
      for (let batch=0;batch<3;batch++) {
        const result=await page(state.cursor); if (epoch!==state.epoch) return;
        const fresh=(result.events || []).filter(event=>event.seq>state.cursor);
        if (!fresh.length) break;
        changed=true;
        state.cursor=fresh[fresh.length-1].seq; state.events.push(...fresh);
        if (state.events.length>5000) state.events.splice(0,state.events.length-5000);
        if (state.turnEvents && field('turn').value) {
          const combined=new Map(state.turnEvents.map(event=>[event.seq,event]));
          for (const event of fresh) if (event.turn_id===field('turn').value) combined.set(event.seq,event);
          state.turnEvents=[...combined.values()].sort((a,b)=>a.seq-b.seq).slice(-5000);
        }
        updateTurns(fresh); if (fresh.length<1000) break;
      }
      status.textContent=state.paused ? 'Paused' : 'Live'; if (changed) render();
    } catch (error) { if (epoch===state.epoch) status.textContent=error.message; }
    finally { state.fetching=false; }
  }
  async function allEvents(turn='') {
    const result=[]; let cursor=0; const epoch=state.epoch;
    while (true) {
      const response=await page(cursor,turn), rows=response.events || [];
      if (epoch!==state.epoch) throw new Error('Conversation changed during export');
      if (!rows.length) return result;
      result.push(...rows); cursor=rows[rows.length-1].seq;
      if (rows.length<1000) return result;
    }
  }
  async function copy(text) { await navigator.clipboard.writeText(text); status.textContent='Copied'; }
  function download(text,extension) {
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
    const link=document.createElement('a'); link.href=url; link.download=`mabel-debug.${extension}`; link.click(); window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  button.addEventListener('click',()=>{ panel.hidden=!panel.hidden; if (!panel.hidden) { sessions(); chooseSession(field('session').value); } });
  action('close').addEventListener('click',()=>{panel.hidden=true;});
  action('expand').addEventListener('click',()=>{panel.classList.toggle('expanded'); action('expand').textContent=panel.classList.contains('expanded')?'Dock':'Full screen';});
  action('pause').addEventListener('click',()=>{state.paused=!state.paused; action('pause').textContent=state.paused?'Live':'Pause'; if (!state.paused) render();});
  action('clear').addEventListener('click',()=>{state.clear=state.cursor; render();});
  action('selection').addEventListener('click',()=>{void copy(String(window.getSelection() || '')).catch(error=>{status.textContent=error.message;});});
  action('copy').addEventListener('click',()=>{const source=field('turn').value && state.turnEvents ? state.turnEvents : state.events; void copy(source.filter(e=>e.seq>state.clear && matchesTrace(e,filters())).slice(-500).map(formatTrace).join('\n')).catch(error=>{status.textContent=error.message;});});
  action('turn').addEventListener('click',async()=>{try {const turn=field('turn').value || state.events.at(-1)?.turn_id; if (!turn) return; await copy((await allEvents(turn)).map(formatTrace).join('\n'));} catch(error){status.textContent=error.message;}});
  for (const extension of ['log','jsonl']) action(extension).addEventListener('click',async()=>{try {const rows=await allEvents(); download(rows.map(extension==='jsonl'?event=>JSON.stringify(event):formatTrace).join('\n')+'\n',extension);} catch(error){status.textContent=error.message;}});
  field('session').addEventListener('change',()=>chooseSession(field('session').value));
  field('turn').addEventListener('change',async()=>{
    const turn=field('turn').value, epoch=state.epoch; state.turnEvents=null; render();
    if (!turn) return;
    try {
      const rows=await allEvents(turn);
      if (epoch===state.epoch && field('turn').value===turn) {
        const combined=new Map(rows.map(event=>[event.seq,event]));
        for (const event of state.events) if (event.turn_id===turn) combined.set(event.seq,event);
        state.turnEvents=[...combined.values()].sort((a,b)=>a.seq-b.seq).slice(-5000); render();
      }
    } catch(error) { status.textContent=error.message; }
  });
  for (const name of ['category','severity','query']) field(name).addEventListener(name==='query'?'input':'change',render);
  window.addEventListener('odysseus:session-changed',()=>{if (!panel.hidden) {sessions(); chooseSession(field('session').value);}});
  document.addEventListener('selectionchange',()=>{if (state.pendingRender && !panel.hidden && window.getSelection()?.isCollapsed) render();});
  window.setInterval(()=>{void poll();},1000);
}

if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',initDebugConsole,{once:true});
else initDebugConsole();
