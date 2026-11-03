const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('@playwright/test');

test('Settings preserves existing primary controls and saves independent optional fallback', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<section data-settings-panel="ai"><div class="admin-card"><select id="set-defaultModelSelect"><option selected>Gyro-S</option></select></div></section>');
    const source = fs.readFileSync('static/js/settings/modelRoles.js', 'utf8');
    await page.addScriptTag({ content: source.replace('export async function', 'async function') });
    await page.evaluate(async () => {
      window.requests = [];
      window.fetch = async (url, options = {}) => {
        window.requests.push({ url, options });
        if (options.method === 'PUT') return { ok: true, json: async () => ({ ok: true }) };
        if (url === '/api/embeddings/models') return { ok: true, json: async () => [] };
        return { ok: true, json: async () => ({ roles: ['default', 'research', 'embedding', 'reranking'].map(role => ({
          role, label: role, required_capability: role === 'embedding' ? 'embedding' : role === 'reranking' ? 'reranking' : 'chat',
          primary: { endpoint_id: 'local', model: role === 'default' ? 'Gyro-S' : '', capabilities: [] },
          policy: { timeout: 30, retries: 0, cooldown: 30 }, diagnostics: {},
        })) }) };
      };
      await initModelRoles({
        fetchEndpoints: async () => [{ id: 'local', models: ['Gyro-S', 'Fallback'] }],
        fillEndpoints: (select, entries, selected) => {
          select.add(new Option('None', ''));
          entries.forEach(ep => select.add(new Option(ep.id, ep.id)));
          select.value = selected;
        },
        fillModels: (select, models, selected) => {
          select.replaceChildren(new Option('None', ''));
          models.forEach(model => select.add(new Option(model, model)));
          select.value = selected;
        },
      });
    });
    assert.equal(await page.locator('[data-role]').count(), 4);
    assert.equal(await page.locator('#set-defaultModelSelect').inputValue(), 'Gyro-S');
    const section = page.locator('[data-role="default"]');
    await section.locator('summary').click();
    const selects = section.locator('select');
    await selects.nth(0).selectOption('local');
    await selects.nth(1).selectOption('Fallback');
    await section.getByRole('button', { name: 'Save routing' }).click();
    await section.getByRole('status').filter({ hasText: 'Saved.' }).waitFor();
    const body = await page.evaluate(() => JSON.parse(window.requests.find(r => r.options.method === 'PUT').options.body));
    assert.equal(body.fallback.model, 'Fallback');
    assert.equal(body.primary, undefined);
    assert.equal(body.primary_metadata.model, 'Gyro-S');
    await selects.nth(0).selectOption('');
    await section.getByRole('button', { name: 'Save routing' }).click();
    await page.waitForFunction(() => window.requests.filter(r => r.options.method === 'PUT').length === 2);
    const disabled = await page.evaluate(() => JSON.parse(window.requests.filter(r => r.options.method === 'PUT').at(-1).options.body));
    assert.equal(disabled.fallback, null);
    assert.equal(await page.locator('#set-defaultModelSelect').inputValue(), 'Gyro-S');
  } finally { await browser.close(); }
});
