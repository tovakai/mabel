import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../static/js/contextMetrics.js', import.meta.url), 'utf8');
const { contextOccupancy } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('context occupancy never uses cumulative multi-request input as window usage', () => {
  const metrics = { input_tokens:233650, request_context_tokens:19562, context_length:32768,
                    context_percent:59.7, usage_source:'real', context_usage_source:'provider_last_request' };
  assert.deepEqual(contextOccupancy(metrics), { used:19562, capacity:32768, estimated:false });
  assert.deepEqual(contextOccupancy({ input_tokens:233650, context_length:32768, context_percent:100 }),
                   { used:0, capacity:32768, estimated:true });
  assert.deepEqual(contextOccupancy({ context_length:32768, context_percent:50 }),
                   { used:16384, capacity:32768, estimated:true });
});
