const OPTIONAL_FIELD_ABSENT_LENGTH = 0xffff_ffff;
const FLOAT32_LENGTH = 4;
const COVARIANCE_VALUE_COUNT = 36;
const STANDARD_BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const UTF8_DECODER = new TextDecoder('utf-8', { fatal: true });

export function decodePoseEstimateBase64(payloadBase64) {
  if (typeof payloadBase64 !== 'string' || payloadBase64.length === 0
    || payloadBase64.length % 4 !== 0 || !STANDARD_BASE64.test(payloadBase64)) {
    throw new Error('PoseEstimate payload_b64 is not valid standard base64');
  }

  let binary;
  try {
    binary = atob(payloadBase64);
  } catch {
    throw new Error('PoseEstimate payload_b64 could not be decoded');
  }

  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return decodePoseEstimate(bytes);
}

function decodePoseEstimate(bytes) {
  return readCompound(bytes, [
    { name: 'frame_id', decode: decodeString },
    { name: 'pose', decode: decodePose },
    { name: 'covariance', decode: decodeCovariance },
    { name: 'quality', optional: true, decode: decodeQuality },
    { name: 'source', optional: true, decode: decodeString },
  ], 'PoseEstimate');
}

function decodePose(bytes, path) {
  return readCompound(bytes, [
    { name: 'position', decode: decodePosition },
    { name: 'orientation', decode: decodeQuaternion },
  ], path);
}

function decodePosition(bytes, path) {
  return readCompound(bytes, [
    { name: 'x', decode: decodeFloat32 },
    { name: 'y', decode: decodeFloat32 },
    { name: 'z', decode: decodeFloat32 },
  ], path);
}

function decodeQuaternion(bytes, path) {
  return readCompound(bytes, [
    { name: 'x', decode: decodeFloat32 },
    { name: 'y', decode: decodeFloat32 },
    { name: 'z', decode: decodeFloat32 },
    { name: 'w', decode: decodeFloat32 },
  ], path);
}

function decodeCovariance(bytes, path) {
  return readCompound(bytes, [
    {
      name: 'values',
      decode: (valueBytes, fieldPath) => decodeFixedFloat32Array(
        valueBytes,
        COVARIANCE_VALUE_COUNT,
        fieldPath,
      ),
    },
  ], path);
}

function decodeQuality(bytes, path) {
  return decodeFloat32(bytes, path);
}

function readCompound(bytes, fields, path) {
  let offset = 0;
  const value = {};

  for (const field of fields) {
    const fieldPath = `${path}.${field.name}`;
    const result = readLengthPrefixedField(bytes, offset, fieldPath, field.optional === true);
    offset = result.offset;
    if (result.bytes !== undefined) {
      value[field.name] = field.decode(result.bytes, fieldPath);
    }
  }

  if (offset !== bytes.byteLength) {
    throw new Error(`${path} has ${bytes.byteLength - offset} trailing byte(s)`);
  }
  return value;
}

function readLengthPrefixedField(bytes, offset, path, optional) {
  if (bytes.byteLength - offset < FLOAT32_LENGTH) {
    throw new Error(`${path} is missing its 4-byte length prefix`);
  }

  const length = new DataView(bytes.buffer, bytes.byteOffset + offset, FLOAT32_LENGTH)
    .getUint32(0, true);
  const dataOffset = offset + FLOAT32_LENGTH;
  if (length === OPTIONAL_FIELD_ABSENT_LENGTH) {
    if (!optional) {
      throw new Error(`${path} is required but marked absent`);
    }
    return { bytes: undefined, offset: dataOffset };
  }
  if (length > bytes.byteLength - dataOffset) {
    throw new Error(`${path} length ${length} exceeds the available payload`);
  }
  return {
    bytes: bytes.subarray(dataOffset, dataOffset + length),
    offset: dataOffset + length,
  };
}

function decodeString(bytes, path) {
  try {
    return UTF8_DECODER.decode(bytes);
  } catch {
    throw new Error(`${path} is not valid UTF-8`);
  }
}

function decodeFloat32(bytes, path) {
  if (bytes.byteLength !== FLOAT32_LENGTH) {
    throw new Error(`${path} must contain exactly 4 bytes`);
  }
  const value = new DataView(bytes.buffer, bytes.byteOffset, FLOAT32_LENGTH).getFloat32(0, true);
  if (!Number.isFinite(value)) {
    throw new Error(`${path} must be finite`);
  }
  return value;
}

function decodeFixedFloat32Array(bytes, count, path) {
  const expectedLength = count * FLOAT32_LENGTH;
  if (bytes.byteLength !== expectedLength) {
    throw new Error(`${path} must contain exactly ${count} float32 values`);
  }

  const values = [];
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let index = 0; index < count; index += 1) {
    const value = view.getFloat32(index * FLOAT32_LENGTH, true);
    if (!Number.isFinite(value)) {
      throw new Error(`${path}[${index}] must be finite`);
    }
    values.push(value);
  }
  return values;
}
