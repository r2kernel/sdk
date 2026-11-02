import assert from 'node:assert/strict';
import test from 'node:test';

import { decodePoseEstimateBase64 } from '../frontend/localization_codec.mjs';

// Captured from the running tutorial UI event log (pose_estimate / component.localizer).
const CAPTURED_LOCALIZER_POSE = 'AwAAAG1hcEAAAAAYAAAABAAAAKnlND4EAAAAcpf+PgQAAAAAAAAAIAAAAAQAAAAAAAAABAAAAAAAAAAEAAAApIJMuwQAAACu/38/lAAAAJAAAAAXt9E5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAF7fROQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIA/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAPwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFJJnToEAAAAH4VrPxMAAABjb21wb25lbnQubG9jYWxpemVy';

test('decodes a captured typed PoseEstimate into the tutorial pose shape', () => {
  const estimate = decodePoseEstimateBase64(CAPTURED_LOCALIZER_POSE);

  assert.equal(estimate.frame_id, 'map');
  assert.deepEqual(estimate.pose.position, {
    x: 0.17665733397006989,
    y: 0.49724918603897095,
    z: 0,
  });
  assert.deepEqual(estimate.pose.orientation, {
    x: 0,
    y: 0,
    z: -0.00312057975679636,
    w: 0.9999951124191284,
  });
  assert.equal(estimate.covariance.values.length, 36);
  assert.equal(estimate.covariance.values[0], 0.00039999998989515007);
  assert.equal(estimate.covariance.values[35], 0.0012000000569969416);
  assert.equal(estimate.quality, 0.9200000166893005);
  assert.equal(estimate.source, 'component.localizer');
});

test('accepts absent optional quality and source fields', () => {
  const bytes = decodeBase64(CAPTURED_LOCALIZER_POSE);
  const frameId = readField(bytes, 0);
  const pose = readField(bytes, frameId.nextOffset);
  const covariance = readField(bytes, pose.nextOffset);
  const optionalFieldsOffset = covariance.nextOffset;
  const payloadWithoutOptionals = new Uint8Array(optionalFieldsOffset + 8);
  payloadWithoutOptionals.set(bytes.subarray(0, optionalFieldsOffset));
  const view = new DataView(payloadWithoutOptionals.buffer);
  view.setUint32(optionalFieldsOffset, 0xffff_ffff, true);
  view.setUint32(optionalFieldsOffset + 4, 0xffff_ffff, true);

  const estimate = decodePoseEstimateBase64(Buffer.from(payloadWithoutOptionals).toString('base64'));
  assert.equal(Object.hasOwn(estimate, 'quality'), false);
  assert.equal(Object.hasOwn(estimate, 'source'), false);
});

test('rejects malformed base64, truncated payloads, and trailing bytes', () => {
  assert.throws(() => decodePoseEstimateBase64('%%%='), /standard base64/);

  const bytes = decodeBase64(CAPTURED_LOCALIZER_POSE);
  assert.throws(() => decodeBase64ToEstimate(bytes.subarray(0, bytes.length - 1)), /length|prefix/);
  assert.throws(() => decodeBase64ToEstimate(appendByte(bytes)), /trailing byte/);
});

test('rejects forged compound and fixed-array lengths', () => {
  const bytes = decodeBase64(CAPTURED_LOCALIZER_POSE);
  const offsets = readPoseEstimateOffsets(bytes);

  const forgedPoseLength = bytes.slice();
  new DataView(forgedPoseLength.buffer).setUint32(offsets.pose.headerOffset, 0xffff, true);
  assert.throws(() => decodeBase64ToEstimate(forgedPoseLength), /exceeds the available payload/);

  const forgedCovarianceLength = bytes.slice();
  new DataView(forgedCovarianceLength.buffer).setUint32(offsets.covarianceValues.headerOffset, 140, true);
  assert.throws(() => decodeBase64ToEstimate(forgedCovarianceLength), /36 float32|trailing byte/);

  const nonFiniteCovariance = bytes.slice();
  new DataView(nonFiniteCovariance.buffer).setFloat32(offsets.covarianceValues.dataOffset, Number.NaN, true);
  assert.throws(() => decodeBase64ToEstimate(nonFiniteCovariance), /covariance.values\[0\] must be finite/);
});

test('rejects invalid UTF-8 and non-finite pose coordinates but preserves finite values', () => {
  const bytes = decodeBase64(CAPTURED_LOCALIZER_POSE);
  const offsets = readPoseEstimateOffsets(bytes);

  const invalidFrame = bytes.slice();
  invalidFrame[offsets.frameId.dataOffset] = 0xff;
  assert.throws(() => decodeBase64ToEstimate(invalidFrame), /valid UTF-8/);

  const nonFinitePosition = bytes.slice();
  new DataView(nonFinitePosition.buffer).setFloat32(offsets.positionX.dataOffset, Number.NaN, true);
  assert.throws(() => decodeBase64ToEstimate(nonFinitePosition), /position.x must be finite/);

  const finitePositionOutsideTheArena = bytes.slice();
  new DataView(finitePositionOutsideTheArena.buffer).setFloat32(offsets.positionX.dataOffset, 2, true);
  const decoded = decodeBase64ToEstimate(finitePositionOutsideTheArena);
  assert.equal(decoded.pose.position.x, 2);
});

test('rejects malformed scalar lengths and non-finite quaternion values', () => {
  const bytes = decodeBase64(CAPTURED_LOCALIZER_POSE);
  const offsets = readPoseEstimateOffsets(bytes);

  const shortPosition = bytes.slice();
  new DataView(shortPosition.buffer).setUint32(offsets.positionX.headerOffset, 3, true);
  assert.throws(() => decodeBase64ToEstimate(shortPosition), /exactly 4 bytes/);

  for (const nonFiniteValue of [Number.NaN, Number.POSITIVE_INFINITY]) {
    const nonFiniteQuaternion = bytes.slice();
    new DataView(nonFiniteQuaternion.buffer).setFloat32(offsets.orientationW.dataOffset, nonFiniteValue, true);
    assert.throws(() => decodeBase64ToEstimate(nonFiniteQuaternion), /orientation.w must be finite/);
  }
});

function decodeBase64(value) {
  return new Uint8Array(Buffer.from(value, 'base64'));
}

function decodeBase64ToEstimate(bytes) {
  return decodePoseEstimateBase64(Buffer.from(bytes).toString('base64'));
}

function appendByte(bytes) {
  const appended = new Uint8Array(bytes.length + 1);
  appended.set(bytes);
  return appended;
}

function readPoseEstimateOffsets(bytes) {
  const frameId = readField(bytes, 0);
  const pose = readField(bytes, frameId.nextOffset);
  const covariance = readField(bytes, pose.nextOffset);
  const position = readField(pose.bytes, 0);
  const orientation = readField(pose.bytes, position.nextOffset);
  const positionX = readField(position.bytes, 0);
  const orientationW = readField(orientation.bytes, 24);
  const covarianceValues = readField(covariance.bytes, 0);

  return {
    frameId,
    pose,
    covariance,
    positionX: {
      headerOffset: pose.dataOffset + position.dataOffset + positionX.headerOffset,
      dataOffset: pose.dataOffset + position.dataOffset + positionX.dataOffset,
    },
    orientationW: {
      dataOffset: pose.dataOffset + orientation.dataOffset + orientationW.dataOffset,
    },
    covarianceValues: {
      headerOffset: covariance.dataOffset + covarianceValues.headerOffset,
      dataOffset: covariance.dataOffset + covarianceValues.dataOffset,
    },
  };
}

function readField(bytes, headerOffset) {
  const length = new DataView(bytes.buffer, bytes.byteOffset + headerOffset, 4).getUint32(0, true);
  const dataOffset = headerOffset + 4;
  return {
    headerOffset,
    dataOffset,
    bytes: bytes.subarray(dataOffset, dataOffset + length),
    nextOffset: dataOffset + length,
  };
}
