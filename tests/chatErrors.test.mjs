import assert from 'node:assert/strict';
import test from 'node:test';
import { classifyChatStreamError, redactSecrets } from '../static/js/chatErrors.js';

const coded = (code, message) => JSON.stringify({ detail: { error: code, message } });

test('expired approval keeps chat mode and explains how to retry', () => {
  const info = classifyChatStreamError(409, coded('TOOL_APPROVAL_EXPIRED',
    'This tool approval is invalid, expired, or belongs to another thread.'));
  assert.equal(info.category, 'approval');
  assert.equal(info.switchToChat, false);
  assert.match(info.text, /expired/);
  assert.match(info.text, /fresh approval prompt/);
  assert.doesNotMatch(info.text, /doesn't support agent tools/);
});

test('authorization denial is not a capability error', () => {
  const info = classifyChatStreamError(403, coded('TOOL_APPROVAL_NOT_ALLOWED',
    'Tool approvals require an interactive session.'));
  assert.equal(info.category, 'authorization');
  assert.equal(info.switchToChat, false);
});

test('only an explicit capability code switches modes', () => {
  const info = classifyChatStreamError(400, coded('MODEL_TOOLS_UNSUPPORTED', 'x'));
  assert.equal(info.category, 'tool_capability');
  assert.equal(info.switchToChat, true);
  // Wording that merely mentions tools no longer triggers it.
  for (const body of ['{"detail": "Tool budget exceeded"}', 'auto tool choice failed', '{"detail": {"message": "tool error"}}']) {
    assert.equal(classifyChatStreamError(500, body).switchToChat, false, body);
  }
});

test('ordinary server errors keep useful detail without secrets', () => {
  const info = classifyChatStreamError(500, '{"detail": "Upstream failed with Bearer abcdef123456 sk-ABCDEFGH12345678"}');
  assert.equal(info.category, 'server');
  assert.equal(info.switchToChat, false);
  assert.match(info.text, /^Error 500: Upstream failed/);
  assert.doesNotMatch(info.text, /abcdef123456|sk-ABCDEFGH/);
  assert.equal(classifyChatStreamError(502, '<html>Bad gateway</html>').text, 'Error 502');
  assert.equal(redactSecrets('api_key=xyz123 ok'), 'api_key=[redacted] ok');
});

// Execute the real chat.js error branch with a fake DOM: the mode toggle and
// stored mode must stay on Chat for everything except a capability error.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const chatSource = readFileSync(new URL('../static/js/chat.js', import.meta.url), 'utf8');

async function runErrorBranch(status, body) {
  const start = chatSource.indexOf("        let errBody = '';");
  const end = chatSource.indexOf('        enableResearchBtn();', start);
  assert.ok(start !== -1 && end > start, 'error branch must exist in chat.js');
  const branch = chatSource.slice(start, end);
  const classes = { agent: new Set(['active']), chat: new Set(), toggle: new Set() };
  const button = (name) => ({
    classList: { add: (c) => classes[name].add(c), remove: (c) => classes[name].delete(c) },
    closest: () => ({ classList: { add: (c) => classes.toggle.add(c) } }),
  });
  const stored = { mode: 'agent' };
  const shown = [];
  const sandbox = {
    res: { status, text: async () => body },
    classifyChatStreamError,
    document: { getElementById: (id) => (id === 'mode-agent-btn' ? button('agent') : id === 'mode-chat-btn' ? button('chat') : null) },
    Storage: { KEYS: { TOGGLES: 't' }, getJSON: () => ({ ...stored }), setJSON: (_k, v) => Object.assign(stored, v) },
    holder: { querySelector: () => ({}) },
    typewriterInto: (_el, text) => shown.push(text),
  };
  await vm.runInNewContext(`(async () => {\n${branch}\n typewriterInto(holder.querySelector('.body'), errText);\n})()`, sandbox);
  return { stored, classes, shown };
}

test('chat.js keeps Chat/Agent mode untouched for approval, authorization and server errors', async () => {
  for (const [status, body] of [
    [409, coded('TOOL_APPROVAL_EXPIRED', 'This tool approval is invalid, expired, or belongs to another thread.')],
    [403, coded('TOOL_APPROVAL_NOT_ALLOWED', 'Tool approvals require an interactive session.')],
    [500, '{"detail": "tool runner crashed (auto retry failed)"}'],
  ]) {
    const { stored, classes, shown } = await runErrorBranch(status, body);
    assert.equal(stored.mode, 'agent', body);
    assert.ok(classes.agent.has('active') && !classes.chat.has('active'), body);
    assert.doesNotMatch(shown[0], /doesn't support agent tools/, body);
  }
});

test('chat.js switches mode only for an explicit capability error', async () => {
  const { stored, classes, shown } = await runErrorBranch(400, coded('MODEL_TOOLS_UNSUPPORTED', 'no tools'));
  assert.equal(stored.mode, 'chat');
  assert.ok(classes.chat.has('active'));
  assert.match(shown[0], /doesn't support agent tools/);
});
