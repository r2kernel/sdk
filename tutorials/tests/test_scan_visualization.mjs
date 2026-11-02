import assert from 'node:assert/strict';
import test from 'node:test';

import { radarDetectionWorldPoint, radarSensorPose, visibleScanRange } from '../frontend/scan_visualization.mjs';


test('uses the runtime scan range for the visible sensor radius', () => {
  assert.equal(visibleScanRange({ mode: 'radar', max_range: 0.30 }), 0.30);
});

test('hides the sensor radius before a scan is active', () => {
  assert.equal(visibleScanRange({ mode: 'none', detections: [] }), 0);
  assert.equal(visibleScanRange(null), 0);
});

test('hides internal collection and verification scans', () => {
  assert.equal(visibleScanRange({ mode: 'nearest', max_range: 0.70 }), 0);
  assert.equal(visibleScanRange({ mode: 'front', max_range: 0.28 }), 0);
});

test('renders radar beams from the declared sensor pose rather than the body origin', () => {
  const sensorPose = radarSensorPose({
    mode: 'radar',
    sensor_pose: { x: 0.58, y: 0.53, heading: Math.PI / 2 },
  });
  const point = radarDetectionWorldPoint({ bearing_deg: 0, distance: 0.2 }, sensorPose);
  assert.equal(sensorPose.x, 0.58);
  assert.ok(Math.abs(point.x - 0.58) < 1e-12);
  assert.ok(Math.abs(point.y - 0.73) < 1e-12);
});

test('rejects a radar scan without an explicit sensor pose', () => {
  assert.throws(() => radarSensorPose({ mode: 'radar' }), /sensor_pose/);
});
