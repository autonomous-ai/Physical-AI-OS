import assert from 'node:assert/strict';
import test from 'node:test';
import { downloadPercent, isNewUpdate, isTerminalProgress } from '../src/pages/monitor/otaProgress.ts';
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
