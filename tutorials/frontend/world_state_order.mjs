export function worldStateIsStale(current, next) {
  if (!current || !next) {
    return false;
  }
  const currentTimestamp = Number(current.ts);
  const nextTimestamp = Number(next.ts);
  if (Number.isFinite(currentTimestamp) && Number.isFinite(nextTimestamp)) {
    if (nextTimestamp !== currentTimestamp) {
      return nextTimestamp < currentTimestamp;
    }
  }
  const currentSequence = Number(current.seq);
  const nextSequence = Number(next.seq);
  return Number.isFinite(currentSequence)
    && Number.isFinite(nextSequence)
    && nextSequence < currentSequence;
}
