import assert from 'node:assert/strict';
import test from 'node:test';

import { worldStateIsStale } from '../frontend/world_state_order.mjs';


test('rejects a delayed world snapshot with an older timestamp', () => {
  const current = { seq: 102, ts: 2_000_000_000 };
  const delayed = { seq: 101, ts: 1_900_000_000 };

  assert.equal(worldStateIsStale(current, delayed), true);
});

test('accepts a simulator restart when its timestamp is newer', () => {
  const current = { seq: 50_000, ts: 2_000_000_000 };
  const restarted = { seq: 1, ts: 2_100_000_000 };

  assert.equal(worldStateIsStale(current, restarted), false);
});

test('uses sequence ordering when timestamps are unavailable', () => {
  assert.equal(worldStateIsStale({ seq: 8 }, { seq: 7 }), true);
  assert.equal(worldStateIsStale({ seq: 8 }, { seq: 9 }), false);
});
