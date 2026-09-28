const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');

// Load the pure UI reducers with the project's existing TypeScript compiler.
// This process-local loader avoids adding a browser or a test-runner dependency.
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  });
  module._compile(outputText, filename);
};
const { groupIntoTurns, turnIO, turnHasOutput, deriveActiveStage, extractNodeInfo, harnessOutputPresentation } =
  require('../src/pages/monitor/FlowSection/helpers.ts');

function event(seq, runId, node, data, type = 'flow_event') {
  return { id: String(seq), _seq: seq, runId, type,
    time: new Date(1789363300000 + seq * 1000).toISOString(),
    summary: '', detail: { node, data } };
}
function input(seq, runId, type = 'voice', route = 'harness_only') {
  return event(seq, runId, 'sensing_input', { type, route, message: `input ${runId}` }, 'flow_enter');
}
function reply(seq, runId, text = `answer ${runId}`) {
  return event(seq, runId, 'harness_response', { text, run_id: runId });
}

test('persisted Harness response closes the turn and restores output without live SSE or TTS', () => {
  const events = [input(1, 'a'), event(2, 'a', 'sensing_input', { route: 'harness_only' }, 'flow_exit'), reply(3, 'a')];
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.path, 'harness');
  assert.equal(turn.status, 'done');
  assert.equal(turn.endTime, events[2].time);
  assert.equal(turnIO(turn).output, 'answer a');
  assert.equal(turnHasOutput(turn), true);
  assert.equal(deriveActiveStage(events), 'agent_response');
  assert.ok(extractNodeInfo(events).agent_response.some((text) => text.includes('answer a')));
});

test('accepted input stays active until its own nonempty response', () => {
  const [turn] = groupIntoTurns([input(1, 'a'), event(2, 'a', 'sensing_input', {}, 'flow_exit'), reply(3, 'a', '  ')]);
  assert.equal(turn.path, 'harness');
  assert.equal(turn.status, 'active');
  assert.equal(turnHasOutput(turn), false);
});

test('interleaved replies attach to their original runs, leaving unanswered inputs active', () => {
  const events = [input(1, 'a'), input(2, 'b', 'voice_followup'), input(3, 'c'), reply(4, 'b'), reply(5, 'a')];
  const turns = groupIntoTurns(events);
  assert.equal(turns.length, 3);
  for (const turn of turns) {
    assert.equal(turn.path, 'harness');
    assert.equal(turn.status, turn.runId === 'c' ? 'active' : 'done');
    if (turn.runId !== 'c') assert.equal(turnIO(turn).output, `answer ${turn.runId}`);
  }
});

test('an orphan response never closes or supplies output to another input', () => {
  const turns = groupIntoTurns([input(1, 'a'), reply(2, 'unknown')]);
  const turn = turns.find((item) => item.runId === 'a');
  assert.equal(turn.status, 'active');
  assert.ok(!turnIO(turn).output);
  assert.equal(turns.length, 2);
});

test('main-agent routing stays unchanged and delegated Harness final also ends its own run', () => {
  const events = [input(1, 'main', 'voice', undefined), event(2, 'main', 'chat_send', { message: 'hello' }), reply(3, 'main')];
  delete events[0].detail.data.route;
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.path, 'agent');
  assert.equal(turn.status, 'done');
  assert.equal(turnIO(turn).output, 'answer main');
});

test('a Harness final preserves an existing lifecycle error', () => {
  const failure = event(2, 'a', 'lifecycle_error', {});
  failure.type = 'lifecycle';
  failure.phase = 'error';
  const [turn] = groupIntoTurns([input(1, 'a'), failure, reply(3, 'a')]);
  assert.equal(turn.status, 'error');
  assert.equal(turnIO(turn).output, 'answer a');
});

function historyMessage(source = 'harness') {
  return '[skills: input-branching]\n[external-context] ' + JSON.stringify({ source, agent_name: 'Coder' }) +
    '\nHistory only. NO_REPLY.\n[HANDLED] ' + JSON.stringify('Open Chrome\nand search') +
    '\n[REPLY] ' + JSON.stringify('A tab is open.');
}

test('history sync displays the exchange without instruction or metadata wrappers', () => {
  const id = 'device-chat-context-test';
  const events = [event(1, id, 'chat_input', { message: historyMessage().slice(0, 140) }),
    event(2, id, 'chat_send', { message: historyMessage() }),
    event(3, id, 'lifecycle_end', {})];
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.type, 'history_sync');
  assert.equal(turn.status, 'done');
  assert.equal(turnIO(turn).input, 'Open Chrome\nand search');
  assert.equal(turnIO(turn).output, 'A tab is open.');
});

test('history parsing is source-neutral and does not close a pending sync', () => {
  const id = 'device-chat-context-other';
  const [turn] = groupIntoTurns([event(1, id, 'chat_input', { message: historyMessage('another-device') })]);
  assert.equal(turn.type, 'history_sync');
  assert.equal(turn.status, 'active');
  assert.equal(turnIO(turn).output, 'A tab is open.');
});

test('normal turns and malformed context envelopes do not masquerade as history sync', () => {
  for (const [id, message] of [['device-chat-user', historyMessage()],
    ['device-chat-context-broken', historyMessage().slice(0, 150)]]) {
    const [turn] = groupIntoTurns([event(1, id, 'chat_input', { message })]);
    assert.ok(turn);
    assert.notEqual(turn.type, 'history_sync');
  }
});


test('legacy shared-ID realtime history keeps its existing combined display', () => {
  const id = 'device-chat-context-realtime';
  const turns = groupIntoTurns([
    input(1, id, 'voice_agent_handled', 'external_history'),
    event(2, id, 'chat_input', { message: historyMessage('realtime').slice(0, 140) }),
    event(3, id, 'chat_send', { message: historyMessage('realtime') }),
    event(4, id, 'lifecycle_end', {}),
  ]);
  assert.equal(turns.length, 1);
  const [turn] = turns;
  assert.equal(turn.type, 'history_sync');
  assert.equal(turn.status, 'done');
  assert.equal(turnIO(turn).input, 'Open Chrome\nand search');
  assert.equal(turnIO(turn).output, 'A tab is open.');
});

test('realtime voice remains separate when its history sync arrives and completes', () => {
  const voice = 'device-realtime-separated';
  const sync = 'device-chat-context-separated';
  const voiceEvents = [input(1, voice, 'voice_agent_handled', 'realtime'),
    event(2, voice, 'realtime_response', { input: 'What time is it?', text: 'Seven.', history_run_id: sync })];
  for (const terminal of [[], [event(5, sync, 'lifecycle_end', {})], [{ ...event(5, sync, 'lifecycle_error', {}), type: 'lifecycle', phase: 'error', error: 'failed' }]]) {
    const turns = groupIntoTurns([...voiceEvents,
      event(3, sync, 'chat_input', { message: historyMessage('realtime') }),
      event(4, sync, 'chat_send', { message: historyMessage('realtime') }), ...terminal]);
    assert.equal(turns.length, 2);
    const original = turns.find(t => t.runId === voice);
    const history = turns.find(t => t.runId === sync);
    assert.equal(original.type, 'voice_agent_handled');
    assert.equal(original.path, 'realtime');
    assert.equal(original.status, 'done');
    assert.equal(turnIO(original).input, 'What time is it?');
    assert.equal(turnIO(original).output, 'Seven.');
    assert.equal(history.type, 'history_sync');
    assert.equal(history.status, terminal.length ? terminal[0].detail.node === 'lifecycle_error' ? 'error' : 'done' : 'active');
  }
});

test('ordinary voice and followup are not relabeled by neighboring history sync', () => {
  for (const type of ['voice', 'voice_command', 'voice_followup']) {
    const id = 'device-voice-' + type;
    const source = input(1, id, type, 'agent');
    source.summary = `[${type}] hello`;
    const turns = groupIntoTurns([source,
      event(2, id, 'lifecycle_end', {}),
      event(3, 'device-chat-context-adjacent', 'chat_input', { message: historyMessage('realtime') }),
      event(4, 'device-chat-context-adjacent', 'lifecycle_end', {})]);
    assert.equal(turns.length, 2);
    assert.equal(turns.find(t => t.runId === id).type, type);
  }
});

test('wake classification labels realtime input without changing its routing or history', () => {
  const id = 'device-realtime-followup';
  const turns = groupIntoTurns([
    input(1, id, 'voice_agent_handled', 'realtime'),
    event(2, id, 'realtime_response', { input: 'And tomorrow?', text: 'Sunny.', voice_turn_type: 'voice_followup' }),
    event(3, 'device-chat-context-followup', 'chat_input', { message: historyMessage('realtime') }),
  ]);
  const original = turns.find(turn => turn.runId === id);
  assert.equal(original.voiceTurnType, 'voice_followup');
  assert.equal(original.type, 'voice_agent_handled');
  assert.equal(original.path, 'realtime');
  assert.equal(turnIO(original).output, 'Sunny.');
  assert.equal(turns.find(turn => turn.type === 'history_sync').voiceTurnType, undefined);
});

test('wake classification accepts only input metadata from its own run', () => {
  for (const classification of ['voice', 'voice_command', 'voice_followup', 'garbage', '', null]) {
    const source = input(1, 'classified', 'voice', 'agent');
    source.detail.data.voice_turn_type = classification;
    const turns = groupIntoTurns([
      source,
      input(2, 'neighbor', 'voice', 'agent'),
      event(3, 'neighbor', 'tts_send', { voice_turn_type: 'voice_followup' }),
    ]);
    const classified = turns.find(turn => turn.runId === 'classified');
    assert.equal(classified.voiceTurnType,
      ['voice', 'voice_command', 'voice_followup'].includes(classification) ? classification : undefined);
    assert.equal(classified.type, 'voice');
    assert.equal(turns.find(turn => turn.runId === 'neighbor').voiceTurnType, undefined);
  }
});

const { turnDisplayType, turnMatchesSearch, migrateTurnTypeFilters } =
  require('../src/pages/monitor/FlowSection/helpers.ts');

function classifiedVoice(classification, realtime = true) {
  const id = `voice-${classification}-${realtime}`;
  const source = input(1, id, realtime ? 'voice_agent_handled' : 'voice', realtime ? 'realtime' : 'agent');
  source.detail.data.voice_turn_type = classification;
  return groupIntoTurns([source, ...(realtime ? [event(2, id, 'realtime_response', {
    input: 'Hello Lamp, can you hear me?', text: 'I can!', voice_turn_type: classification,
  })] : [])])[0];
}

test('visible handled subtypes preserve raw routing and remain searchable', () => {
  for (const [kind, expected] of [
    ['voice_command', 'voice_command_handled'], ['voice_followup', 'voice_followup_handled'],
    ['voice', 'voice_agent_handled'],
  ]) {
    const turn = classifiedVoice(kind);
    assert.equal(turnDisplayType(turn), expected);
    assert.equal(turnMatchesSearch(turn, expected.toUpperCase()), true);
    assert.equal(turnMatchesSearch(turn, 'voice_agent_handled'), true);
    assert.equal(turnMatchesSearch(turn, 'not a matching question'), false);
    assert.equal(turn.type, 'voice_agent_handled');
    assert.equal(turn.path, 'realtime');
  }
  assert.equal(turnDisplayType(classifiedVoice('voice_command', false)), 'voice_command');
  assert.equal(turnDisplayType(classifiedVoice('voice_followup', false)), 'voice_followup');
  assert.equal(turnDisplayType(classifiedVoice(undefined)), 'voice_agent_handled');
  const [history] = groupIntoTurns([event(1, 'device-chat-context-display-test', 'chat_input', { message: historyMessage('realtime') })]);
  assert.equal(turnDisplayType(history), 'history_sync');
});

test('saved exclusions expand to new visible subtypes', () => {
  assert.deepEqual([...migrateTurnTypeFilters(['voice_agent_handled'])],
    ['voice_agent_handled', 'voice_command_handled', 'voice_followup_handled']);
  assert.deepEqual([...migrateTurnTypeFilters(['voice'])], ['voice', 'voice_command', 'voice_followup']);
  assert.deepEqual([...migrateTurnTypeFilters(['__dropped'])], ['__dropped']);
});

test('rendered badges preserve realtime handled labels and distinguish Harness output from TTS', () => {
  const Module = require('node:module');
  const load = Module._load;
  require.extensions['.tsx'] = (module, filename) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
      fileName: filename,
    });
    module._compile(outputText, filename);
  };
  // Browser URL configuration does not affect server-rendered badge labels.
  Module._load = function(name, ...args) {
    if (name === '@/lib/api') return { hwUrl: path => path };
    if (name === '@/lib/useTheme') return { useTheme: () => ['dark', () => {}, 'lm-dark'] };
    return load.call(this, name, ...args);
  };
  try {
    const React = require('react');
    const { renderToStaticMarkup } = require('react-dom/server');
    const { TurnBadge } = require('../src/pages/monitor/FlowSection/TurnBadge.tsx');
    for (const kind of ['voice_command', 'voice_followup']) {
      const html = renderToStaticMarkup(React.createElement(TurnBadge, { turn: classifiedVoice(kind) }));
      assert.match(html, new RegExp(`>${kind}_handled</span>`));
      assert.match(html, /data-turn-type="voice_agent_handled"/);
      assert.ok(html.includes('>Realtime</span>'));
    }
    for (const reference of [false, true]) {
      const [turn] = groupIntoTurns([input(1, 'group-member'),
        event(2, 'group-member', 'harness_response', { text: reference ? 'See the shared reply for these requests.' : 'Clouds are black.', result_reference: reference, result_run_id: 'original' })]);
      const html = renderToStaticMarkup(React.createElement(TurnBadge, { turn }));
      assert.ok(html.includes(reference ? 'Shared result</span>' : 'Harness</span>'));
      assert.doesNotMatch(html, / TTS<\/span>/);
      if (reference) assert.ok(html.includes('title="Shared reply in run original"'));
    }
  } finally {
    Module._load = load;
  }
});


test('Harness group output stays DONE with distinct answer and shared-reference labels', () => {
  const reference = 'See the shared reply for these requests.';
  const turns = groupIntoTurns([
    input(1, 'original'), input(2, 'followup'),
    event(3, 'original', 'harness_response', { text: 'Clouds are black.', result_reference: false, result_run_id: 'original' }),
    event(4, 'followup', 'harness_response', { text: reference, result_reference: true, result_run_id: 'original' }),
  ]);
  const original = turns.find(turn => turn.runId === 'original');
  const followup = turns.find(turn => turn.runId === 'followup');
  assert.equal(original.status, 'done');
  assert.equal(followup.status, 'done');
  assert.equal(turnIO(original).output, 'Clouds are black.');
  assert.equal(turnIO(followup).output, reference);
  assert.equal(turnHasOutput(followup), true);
  assert.deepEqual(harnessOutputPresentation(original, turnIO(original).output), { label: 'Harness', resultRunId: undefined });
  assert.deepEqual(harnessOutputPresentation(followup, turnIO(followup).output), { label: 'Shared result', resultRunId: 'original' });
});

test('Harness output attribution supports live chat and old flat flow records', () => {
  for (const record of [
    { ...event(2, 'a', '', {}), type: 'chat_response', state: 'final', detail: { source: 'harness', message: 'Shared.', result_reference: 'true', result_run_id: 'owner' } },
    { ...event(2, 'a', 'harness_response', {}), detail: { node: 'harness_response', text: 'Shared.', result_reference: true, result_run_id: 'owner' } },
  ]) {
    const turn = { runId: 'a', events: [record] };
    assert.deepEqual(harnessOutputPresentation(turn, 'Shared.'), { label: 'Shared result', resultRunId: 'owner' });
  }
});

test('Harness attribution does not relabel a different output, run, or realtime reply', () => {
  const record = reply(2, 'a', 'Answer.');
  assert.equal(harnessOutputPresentation({ runId: 'b', events: [record] }, 'Answer.'), null);
  assert.equal(harnessOutputPresentation({ runId: 'a', events: [record] }, 'Other output.'), null);
  assert.equal(harnessOutputPresentation({ runId: 'a', events: [event(2, 'a', 'realtime_response', { text: 'Answer.' })] }, 'Answer.'), null);
  assert.equal(harnessOutputPresentation({ events: [record] }, 'Answer.'), null);
});
