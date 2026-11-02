// Payload helpers for cleaner arena WebSocket and simulator responses.

export function parsePayload(message) {
  return message.payload_json || message.payload_text || message.payload_b64 || null;
}

export function unwrapPayload(value) {
  let current = value;
  for (let index = 0; index < 8; index += 1) {
    if (!current || typeof current !== 'object') {
      return current;
    }
    if ('payload_json' in current) {
      current = current.payload_json;
      continue;
    }
    if ('payload' in current) {
      current = current.payload;
      continue;
    }
    if ('result' in current) {
      current = current.result;
      continue;
    }
    return current;
  }
  return current;
}

export function extractWorldState(value) {
  let current = value;
  for (let index = 0; index < 6; index += 1) {
    if (!current || typeof current !== 'object') {
      return null;
    }
    if (current.kind === 'robot_cleaner.world') {
      return current;
    }
    if (current.state?.kind === 'robot_cleaner.world') {
      return current.state;
    }
    if (current.world?.kind === 'robot_cleaner.world') {
      return current.world;
    }
    if (current.world?.state?.kind === 'robot_cleaner.world') {
      return current.world.state;
    }
    if ('result' in current) {
      current = current.result;
      continue;
    }
    return null;
  }
  return null;
}
