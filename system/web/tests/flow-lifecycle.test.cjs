const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');

// Load the TS reducers with the project's TypeScript compiler (no extra test dependency).
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  });
  module._compile(outputText, filename);
};
const { groupIntoTurns, extractNodeInfo } =
  require('../src/pages/monitor/FlowSection/helpers.ts');

function event(seq, runId, node, data, type = 'flow_event') {
  return { id: String(seq), _seq: seq, runId, type,
    time: new Date(1789363300000 + seq * 1000).toISOString(),
    summary: '', detail: { node, data } };
}
function input(seq, runId, type = 'voice', route = 'harness_only') {
  return event(seq, runId, 'sensing_input', { type, route, message: `input ${runId}` }, 'flow_enter');
}


for (const live of [false, true]) {
  test(`${live ? 'live' : 'persisted'} cancelled lifecycle closes voice command with its error`, () => {
    const failure = event(3, 'a', 'lifecycle_error', { error: 'Hermes cancelled' });
    if (live) Object.assign(failure, { type: 'lifecycle', phase: 'error', error: 'Hermes cancelled' });
    const events = [input(1, 'a', 'voice_command', 'agent'), event(2, 'a', 'lifecycle_start', {}), failure];
    const [turn] = groupIntoTurns(events);
    assert.equal(turn.status, 'error');
    assert.equal(turn.endTime, failure.time);
    assert.ok(extractNodeInfo(turn.events).agent_response.includes('❌ Hermes cancelled'));
  });
}

test('persisted interleaved failure closes only its own run', () => {
  const failure = event(4, 'a', 'lifecycle_error', { error: 'Hermes cancelled' });
  const turns = groupIntoTurns([input(1, 'a', 'voice_command', 'agent'), event(2, 'a', 'lifecycle_start', {}), input(3, 'b', 'voice_command', 'agent'), failure]);
  assert.equal(turns.find(turn => turn.runId === 'a').status, 'error');
  assert.equal(turns.find(turn => turn.runId === 'a').endTime, failure.time);
  assert.equal(turns.find(turn => turn.runId === 'b').status, 'active');
});

test('successful persisted end stays done, end with nested error fails', () => {
  for (const error of ['', 'provider failed']) {
    const end = event(2, 'a', 'lifecycle_end', { error });
    const [turn] = groupIntoTurns([input(1, 'a', 'voice_command', 'agent'), end]);
    assert.equal(turn.status, error ? 'error' : 'done');
    assert.equal(turn.endTime, end.time);
  }
});

test('nonterminal tool error does not end a running turn', () => {
  const [turn] = groupIntoTurns([input(1, 'a', 'voice_command', 'agent'), event(2, 'a', 'tool_call', { error: 'retrying' })]);
  assert.equal(turn.status, 'active');
  assert.equal(turn.endTime, undefined);
});

test('persisted recovered error retains terminal status without exposing original error', () => {
  const failure = event(2, 'a', 'lifecycle_error', { error: '', recovered: true, original_error: 'incomplete turn' });
  const [turn] = groupIntoTurns([input(1, 'a', 'voice_command', 'agent'), failure]);
  assert.equal(turn.status, 'error');
  assert.ok(!extractNodeInfo(turn.events).agent_response.some(text => text.includes('incomplete turn')));
});

test('successful turn retains its later TTS dispatch end time', () => {
  const tts = event(3, 'a', 'tts_send', { text: 'hello' });
  const [turn] = groupIntoTurns([input(1, 'a', 'voice_command', 'agent'), event(2, 'a', 'lifecycle_end', {}), tts]);
  assert.equal(turn.status, 'done');
  assert.equal(turn.endTime, tts.time);
});
