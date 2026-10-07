import assert from 'node:assert/strict';
import test from 'node:test';
import { downloadPercent, installationActivityAge, isNewUpdate, isTerminalProgress } from '../src/pages/monitor/otaProgress.ts';
const progress = { target: 'hal', run_id: 'new', updated_at: 100, phase: 'downloading', downloaded_bytes: 42, total_bytes: 100 };
test('only measurable downloads show percentages', () => {
  assert.equal(downloadPercent(progress), 42);
  for (const patch of [{ total_bytes: 0 }, { total_bytes: undefined }, { downloaded_bytes: -1 }, { downloaded_bytes: 101 }, { total_bytes: NaN }, { phase: 'installing' }, { phase: 'completed' }]) {
    assert.equal(downloadPercent({ ...progress, ...patch }), null);
  }
});
test('terminal outcomes include failure and interruption, not rollback', () => {
  for (const phase of ['failed', 'completed', 'interrupted']) assert.equal(isTerminalProgress({ ...progress, phase }), true);
  assert.equal(isTerminalProgress({ ...progress, phase: 'rolling_back' }), false);
});
test('old persisted status cannot acknowledge a newly requested update', () => {
  assert.equal(isNewUpdate(progress, 'new'), false);
  assert.equal(isNewUpdate({ ...progress, updated_at: 1 }, 'old'), true);
  assert.equal(isNewUpdate(progress, 'old'), true);
});

test('installation activity is optional and ignored outside installing', () => {
  const installing = { ...progress, phase: 'installing' };
  for (const activity_at of [undefined, 0, -1, NaN, Infinity]) {
    assert.equal(installationActivityAge({ ...installing, activity_at }, 200), null);
  }
  for (const phase of ['downloading', 'restarting', 'checking', 'completed', 'failed', 'interrupted']) {
    assert.equal(installationActivityAge({ ...installing, phase, activity_at: 100 }, 200), null);
  }
});
test('installation activity ages without inventing progress and clamps clock skew', () => {
  const installing = { ...progress, phase: 'installing', activity_at: 100 };
  for (const [now, label] of [[90, 'just now'], [100, 'just now'], [109, '9s ago'], [192, '1m 32s ago'], [3700, '1h 0m ago']]) {
    assert.equal(installationActivityAge(installing, now), `Last activity ${label}`);
  }
  assert.equal(installationActivityAge(installing, NaN), null);
  assert.equal(downloadPercent(installing), null);
});
