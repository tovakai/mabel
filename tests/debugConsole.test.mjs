import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
let chromium;
try { ({ chromium } = await import('playwright')); } catch { /* optional browser test dependency */ }

const source=await readFile(new URL('../static/js/debugConsole.js',import.meta.url),'utf8');
const css=await readFile(new URL('../static/css/debugConsole.css',import.meta.url),'utf8');

test('console replays, filters, copies, exports and follows the active theme',{skip:!chromium},async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();
    await page.setContent(`<style>${css}</style><div class="chat-input-right"></div>`);
    await page.evaluate(()=>{
      document.documentElement.style.setProperty('--bg','rgb(240, 240, 240)');
      document.documentElement.style.setProperty('--fg','rgb(20, 20, 20)');
      document.documentElement.style.setProperty('--border','rgb(80, 80, 80)');
      window.sessionModule={getCurrentSessionId:()=> 's1',getSessions:()=>[{id:'s1',name:'One'}]};
      window.savedClipboard=''; Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.savedClipboard=text;}}});
      window.traceRows=[
        {seq:1,timestamp:'2026-10-29T01:00:00.000+00:00',turn_id:'turn_'+ 'a'.repeat(32),category:'MODEL',severity:'INFO',operation:'model_request',status:'started',metadata:{model:'gyro'}},
        {seq:2,timestamp:'2026-10-29T01:00:01.000+00:00',turn_id:'turn_'+ 'a'.repeat(32),category:'TOOL',severity:'ERROR',operation:'read_file',status:'error',metadata:{result_bytes:3}},
      ];
      window.fetch=async url=>{
        const params=new URL(url,'http://test/').searchParams, after=Number(params.get('after') || 0), turn=params.get('turn');
        return {ok:true,json:async()=>({events:window.traceRows.filter(row=>row.seq>after && (!turn || row.turn_id===turn))})};
      };
      window.exportBlob=null; URL.createObjectURL=blob=>{window.exportBlob=blob;return 'blob:test';}; URL.revokeObjectURL=()=>{};
      HTMLAnchorElement.prototype.click=function(){window.exportName=this.download;};
    });
    await page.addScriptTag({content:source.replaceAll('export function','function')});
    await page.getByRole('button',{name:'Open Debug Console'}).click();
    await page.getByText(/model_request started/).waitFor();
    assert.equal(await page.locator('.debug-output details').count(),2);
    await page.locator('[data-field="category"]').selectOption('TOOL');
    assert.equal(await page.locator('.debug-output details').count(),1);
    await page.getByRole('button',{name:'Copy visible'}).click();
    assert.match(await page.evaluate(()=>window.savedClipboard),/read_file error/);
    await page.getByRole('button',{name:'Copy complete turn'}).click();
    await page.waitForFunction(()=>window.savedClipboard.includes('model_request started'));
    await page.getByRole('button',{name:'Export .jsonl'}).click();
    await page.waitForFunction(()=>window.exportBlob!==null);
    assert.equal(await page.evaluate(()=>window.exportName),'mabel-debug.jsonl');
    assert.equal((await page.evaluate(async()=>await window.exportBlob.text())).trim().split('\n').length,2);
    const before=await page.locator('#debug-console').evaluate(el=>getComputedStyle(el).backgroundColor);
    await page.evaluate(()=>document.documentElement.style.setProperty('--bg','rgb(25, 25, 25)'));
    const after=await page.locator('#debug-console').evaluate(el=>getComputedStyle(el).backgroundColor);
    assert.notEqual(before,after);
    await page.getByRole('button',{name:'Full screen'}).click();
    assert.equal(await page.locator('#debug-console').evaluate(el=>el.classList.contains('expanded')),true);
    await page.getByRole('button',{name:'Clear visible'}).click();
    assert.equal(await page.locator('.debug-output details').count(),0);
    await page.getByRole('button',{name:'Close Debug Console'}).click();
    assert.equal(await page.locator('#debug-console').isHidden(),true);
  } finally { await browser.close(); }
});
