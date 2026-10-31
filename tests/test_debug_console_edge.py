"""Exercise the console in an existing Windows Edge without downloading a browser."""
from pathlib import Path
import os
import subprocess
import pytest


def test_debug_console_in_existing_edge(tmp_path):
    edge=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
    if os.name!='nt' or not edge.exists(): pytest.skip('Windows Edge unavailable')
    root=Path(__file__).resolve().parents[1]
    source=(root/'static/js/debugConsole.js').read_text(encoding='utf-8').replace('export function','function')
    css=(root/'static/css/debugConsole.css').read_text(encoding='utf-8')
    setup='''<script>
    document.documentElement.style.setProperty('--bg','rgb(240, 240, 240)');
    document.documentElement.style.setProperty('--fg','rgb(20, 20, 20)');
    document.documentElement.style.setProperty('--border','rgb(80, 80, 80)');
    window.sessionModule={getCurrentSessionId:()=> 's1',getSessions:()=>[{id:'s1',name:'One'}]};
    window.savedClipboard=''; Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.savedClipboard=text;}}});
    window.traceRows=[{seq:1,timestamp:'2026-10-29T01:00:00.000+00:00',turn_id:'turn_'+ 'a'.repeat(32),category:'MODEL',severity:'INFO',operation:'model_request',status:'started',metadata:{model:'gyro'}},
      {seq:2,timestamp:'2026-10-29T01:00:01.000+00:00',turn_id:'turn_'+ 'a'.repeat(32),category:'TOOL',severity:'ERROR',operation:'read_file',status:'error',metadata:{result_bytes:3}}];
    window.fetch=async url=>{const p=new URL(url,'http://test/').searchParams,after=Number(p.get('after')||0),turn=p.get('turn');return {ok:true,json:async()=>({events:window.traceRows.filter(r=>r.seq>after&&(!turn||r.turn_id===turn))})};};
    window.exportBlob=null; URL.createObjectURL=blob=>{window.exportBlob=blob;return 'blob:test';};URL.revokeObjectURL=()=>{};
    HTMLAnchorElement.prototype.click=function(){window.exportName=this.download;};
    </script>'''
    check='''<script>
    window.addEventListener('DOMContentLoaded',async()=>{
      function assert(ok,msg){if(!ok) throw new Error(msg);}
      try {
        document.querySelector('.debug-console-toggle').click();
        await new Promise(r=>setTimeout(r,100));
        const panel=document.querySelector('#debug-console'), output=panel.querySelector('.debug-output');
        assert(output.querySelectorAll('details').length===2,'replay');
        const category=panel.querySelector('[data-field="category"]');category.value='TOOL';category.dispatchEvent(new Event('change'));
        assert(output.querySelectorAll('details').length===1,'filter');
        panel.querySelector('[data-action="copy"]').click();await new Promise(r=>setTimeout(r,20));
        assert(window.savedClipboard.includes('read_file error'),'copy visible');
        panel.querySelector('[data-action="turn"]').click();await new Promise(r=>setTimeout(r,20));
        assert(window.savedClipboard.includes('model_request started'),'copy complete turn');
        panel.querySelector('[data-action="jsonl"]').click();await new Promise(r=>setTimeout(r,20));
        assert(window.exportName==='mabel-debug.jsonl' && (await window.exportBlob.text()).trim().split('\\n').length===2,'export');
        const search=panel.querySelector('[data-field="query"]');search.value='read_file';search.dispatchEvent(new Event('input'));
        assert(output.querySelectorAll('details').length===1,'search');search.value='';search.dispatchEvent(new Event('input'));
        const severity=panel.querySelector('[data-field="severity"]');severity.value='ERROR';severity.dispatchEvent(new Event('change'));
        assert(output.querySelectorAll('details').length===1,'severity');severity.value='';severity.dispatchEvent(new Event('change'));
        const textNode=output.querySelector('summary').firstChild,range=document.createRange();range.selectNodeContents(textNode);
        const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
        panel.querySelector('[data-action="selection"]').click();await new Promise(r=>setTimeout(r,20));
        assert(window.savedClipboard.includes('read_file error'),'copy selection');selection.removeAllRanges();
        panel.querySelector('[data-action="pause"]').click();
        window.traceRows.push({seq:3,timestamp:'2026-10-29T01:00:02.000+00:00',turn_id:'turn_'+ 'a'.repeat(32),category:'TOOL',severity:'INFO',operation:'glob',status:'completed',metadata:{}});
        await new Promise(r=>setTimeout(r,1200));assert(output.querySelectorAll('details').length===1,'pause');
        panel.querySelector('[data-action="pause"]').click();assert(output.querySelectorAll('details').length===2,'resume');
        const before=getComputedStyle(panel).backgroundColor;
        document.documentElement.style.setProperty('--bg','rgb(25, 25, 25)');
        assert(before!==getComputedStyle(panel).backgroundColor,'theme');
        panel.querySelector('[data-action="expand"]').click();assert(panel.classList.contains('expanded'),'expand');
        panel.querySelector('[data-action="clear"]').click();assert(output.querySelectorAll('details').length===0,'clear');
        panel.querySelector('[data-action="close"]').click();assert(panel.hidden,'close');
        document.querySelector('.debug-console-toggle').click();await new Promise(r=>setTimeout(r,100));
        assert(output.querySelectorAll('details').length===2,'reconnect without duplicates');
        const turn=panel.querySelector('[data-field="turn"]');turn.value='turn_'+ 'a'.repeat(32);turn.dispatchEvent(new Event('change'));
        await new Promise(r=>setTimeout(r,100));assert(output.querySelectorAll('details').length===2,'turn replay');
        document.body.dataset.testResult='pass';
      } catch(error) { document.body.dataset.testResult='fail'; document.body.dataset.testError=error.message; }
    });</script>'''
    page=tmp_path/'console-test.html'
    page.write_text('<!doctype html><style>'+css+'</style><body><div class="chat-input-right"></div>'+setup+'<script>'+source+'</script>'+check+'</body>',encoding='utf-8')
    result=subprocess.run([str(edge),'--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
                           '--user-data-dir='+str(tmp_path/'profile'),'--virtual-time-budget=5000','--dump-dom',page.as_uri()],
                          capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
    assert result.returncode==0, result.stderr[-1000:]
    assert 'data-test-result="pass"' in result.stdout, result.stdout[-2500:]
