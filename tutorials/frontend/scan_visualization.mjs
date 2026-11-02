export function visibleScanRange(scan) {
  if (!scan || scan.mode !== 'radar') {
    return 0;
  }
  const range = Number(scan.max_range);
  return Number.isFinite(range) && range > 0 ? range : 0;
}

export function radarSensorPose(scan) {
  const pose = scan?.sensor_pose;
  if (!pose || ![pose.x, pose.y, pose.heading].every(Number.isFinite)) {
    throw new Error('radar scan requires a finite world-frame sensor_pose');
  }
  return { x: pose.x, y: pose.y, heading: pose.heading };
}

export function radarDetectionWorldPoint(detection, sensorPose) {
  if (!Number.isFinite(detection?.bearing_deg) || !Number.isFinite(detection?.distance) || detection.distance < 0) {
    throw new Error('radar detection requires a finite bearing and nonnegative distance');
  }
  const angle = sensorPose.heading + detection.bearing_deg * Math.PI / 180;
  return {
    x: sensorPose.x + Math.cos(angle) * detection.distance,
    y: sensorPose.y + Math.sin(angle) * detection.distance,
  };
}
