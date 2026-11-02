// Canvas rendering for the cleaner arena world viewer: world-to-canvas projection,
// trash, radar scan, localization estimate, robot, gripper, and burn effects.
// app.js owns the shared viewer state and the WebSocket client; this module only
// reads that state, except for the burn-effect list it animates.
import { radarDetectionWorldPoint, radarSensorPose, visibleScanRange } from './scan_visualization.mjs?v=robot-cleaner-world-19';

const BURN_EFFECT_DURATION_MS = 1200;

export function clampValue(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function burnedCount(world) {
  return Number(world?.score?.burned ?? world?.robot?.incinerated ?? 0);
}

export function createArenaRenderer({ state, canvas }) {
  const ctx = canvas.getContext('2d');
  const robotLogo = new Image();

  robotLogo.addEventListener('load', () => draw());
  robotLogo.src = './logo.svg';

  function localizationPose() {
    const value = state.localization?.pose;
    const position = value?.position;
    const orientation = value?.orientation;
    if (!position || !orientation) {
      return null;
    }
    const x = Number(orientation.x || 0);
    const y = Number(orientation.y || 0);
    const z = Number(orientation.z || 0);
    const w = Number(orientation.w ?? 1);
    return {
      x: Number(position.x || 0),
      y: Number(position.y || 0),
      heading: Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
    };
  }

  function canvasMetrics() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(640, Math.floor(rect.width * dpr));
    const height = Math.max(420, Math.floor(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const pad = Math.round(Math.min(width, height) * 0.075);
    return { width, height, pad, dpr };
  }

  function toCanvas(point, metrics) {
    const size = Math.min(metrics.width - metrics.pad * 2, metrics.height - metrics.pad * 2);
    const x0 = (metrics.width - size) / 2;
    const y0 = (metrics.height - size) / 2;
    return {
      x: x0 + point.x * size,
      y: y0 + point.y * size,
      size,
      x0,
      y0,
    };
  }

  function wrapRadians(value) {
    let result = value;
    while (result > Math.PI) {
      result -= Math.PI * 2;
    }
    while (result < -Math.PI) {
      result += Math.PI * 2;
    }
    return result;
  }

  function addBurnEffect(world, burnedItems = 1) {
    const robot = world?.robot;
    if (!robot) {
      return;
    }
    const count = Math.max(1, Number(burnedItems) || 1);
    const particleCount = Math.min(56, 24 + count * 10);
    const particles = [];
    for (let index = 0; index < particleCount; index += 1) {
      particles.push({
        angle: -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.6,
        speed: 0.26 + Math.random() * 0.72,
        drift: (Math.random() - 0.5) * 0.48,
        size: 0.45 + Math.random() * 1.1,
        delay: Math.random() * 0.18,
        color: Math.random() > 0.45 ? '#ff9f1c' : (Math.random() > 0.45 ? '#ff3f2f' : '#ffd166'),
      });
    }
    state.burnEffects.push({
      x: clampValue(Number(robot.x ?? 0.5), 0, 1),
      y: clampValue(Number(robot.y ?? 0.5), 0, 1),
      startedAt: Date.now(),
      duration: BURN_EFFECT_DURATION_MS,
      particles,
    });
  }

  function projectedRobot() {
    const robot = state.world?.robot;
    if (!robot) {
      return null;
    }
    const projected = { ...robot };
    const ageS = clampValue((performance.now() - state.worldReceivedAt) / 1000, 0, 0.12);
    const vx = Number(robot.vx ?? robot.linear ?? 0);
    const vy = Number(robot.vy ?? 0);
    const wz = Number(robot.wz ?? robot.angular ?? 0);
    const speed = 0.36;
    const turnRate = (150 * Math.PI) / 180;
    let heading = Number(robot.heading ?? 0);
    let worldVx = vx;
    let worldVy = vy;
    if ((robot.velocity_frame || 'base_link') === 'base_link') {
      worldVx = Math.cos(heading) * vx - Math.sin(heading) * vy;
      worldVy = Math.sin(heading) * vx + Math.cos(heading) * vy;
    }
    projected.x = clampValue(Number(robot.x ?? 0) + worldVx * speed * ageS, 0.05, 0.95);
    projected.y = clampValue(Number(robot.y ?? 0) + worldVy * speed * ageS, 0.05, 0.95);
    heading = wrapRadians(heading + wz * turnRate * ageS);
    projected.heading = heading;
    projected.heading_deg = (heading * 180) / Math.PI;
    const reach = 0.08 + 0.18 * Number(robot.arm_extension ?? 0);
    projected.reach_x = clampValue(projected.x + Math.cos(heading) * reach, 0.0, 1.0);
    projected.reach_y = clampValue(projected.y + Math.sin(heading) * reach, 0.0, 1.0);
    return projected;
  }

  function drawGrid(metrics) {
    const origin = toCanvas({ x: 0, y: 0 }, metrics);
    ctx.fillStyle = '#111512';
    ctx.fillRect(origin.x0, origin.y0, origin.size, origin.size);
    ctx.strokeStyle = '#26332c';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 10; i += 1) {
      const p = origin.x0 + (origin.size * i) / 10;
      const q = origin.y0 + (origin.size * i) / 10;
      ctx.beginPath();
      ctx.moveTo(p, origin.y0);
      ctx.lineTo(p, origin.y0 + origin.size);
      ctx.moveTo(origin.x0, q);
      ctx.lineTo(origin.x0 + origin.size, q);
      ctx.stroke();
    }
    ctx.strokeStyle = '#68746b';
    ctx.lineWidth = 2;
    ctx.strokeRect(origin.x0, origin.y0, origin.size, origin.size);
  }

  function drawTrash(metrics) {
    const trash = state.world?.trash || [];
    const scan = state.world?.scan;
    const beamIds = new Set((scan?.beam_detections || scan?.detections || []).map((item) => String(item.id || '')));
    const trackedIds = new Set((scan?.detections || []).map((item) => String(item.id || '')));
    for (const item of trash) {
      const p = toCanvas(item, metrics);
      const radius = Math.max(7, p.size * 0.014);
      const itemId = String(item.id || '');
      const beamVisible = beamIds.has(itemId);
      const tracked = trackedIds.has(itemId);
      const sensorVisible = item.state !== 'loose' || beamVisible;
      ctx.globalAlpha = sensorVisible ? 1 : (tracked ? 0.72 : 0.28);
      ctx.fillStyle = item.state === 'held' ? '#ffd08a' : (item.state === 'payload' ? '#39d99d' : '#d84d5d');
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#1b0d10';
      ctx.lineWidth = 2;
      ctx.stroke();
      if (tracked && !beamVisible) {
        ctx.strokeStyle = '#f3b64b';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius + 5, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawScan(metrics) {
    const scan = state.world?.scan;
    const scanRange = visibleScanRange(scan);
    if (scanRange <= 0) {
      return;
    }
    const detections = scan?.beam_detections || scan?.detections || [];
    const trackedDetections = (scan?.detections || []).filter((item) => !item.fresh);
    const sensorPose = radarSensorPose(scan);
    const r = toCanvas(sensorPose, metrics);
    const fovDeg = clampValue(Number(scan?.fov_deg ?? 360), 1, 360);
    const bearingCenter = Number(scan?.bearing_center_deg ?? 0) * Math.PI / 180;
    const centerAngle = sensorPose.heading + bearingCenter;
    const halfBeam = (fovDeg * Math.PI / 180) / 2;
    const radius = r.size * scanRange;
    ctx.strokeStyle = 'rgba(37, 198, 218, 0.22)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    if (fovDeg >= 359.9) {
      ctx.arc(r.x, r.y, radius, 0, Math.PI * 2);
    } else {
      ctx.moveTo(r.x, r.y);
      ctx.arc(r.x, r.y, radius, centerAngle - halfBeam, centerAngle + halfBeam);
      ctx.closePath();
      ctx.fillStyle = 'rgba(37, 198, 218, 0.07)';
      ctx.fill();
    }
    ctx.stroke();
    for (const detection of detections) {
      const p = toCanvas(radarDetectionWorldPoint(detection, sensorPose), metrics);
      ctx.strokeStyle = '#25c6da';
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.moveTo(r.x, r.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    for (const detection of trackedDetections) {
      const p = toCanvas(radarDetectionWorldPoint(detection, sensorPose), metrics);
      ctx.strokeStyle = 'rgba(243, 182, 75, 0.72)';
      ctx.setLineDash([4, 7]);
      ctx.beginPath();
      ctx.moveTo(r.x, r.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function drawLocalizationEstimate(metrics, robot) {
    const estimate = localizationPose();
    if (!estimate || !robot) {
      return;
    }
    const truth = toCanvas(robot, metrics);
    const point = toCanvas(estimate, metrics);
    const marker = Math.max(14, point.size * 0.024);
    ctx.save();
    ctx.strokeStyle = 'rgba(255, 211, 92, 0.92)';
    ctx.lineWidth = Math.max(2, point.size * 0.003);
    ctx.setLineDash([7, 5]);
    ctx.beginPath();
    ctx.moveTo(truth.x, truth.y);
    ctx.lineTo(point.x, point.y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.translate(point.x, point.y);
    ctx.rotate(estimate.heading);
    ctx.beginPath();
    ctx.arc(0, 0, marker, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(marker * 1.5, 0);
    ctx.stroke();
    ctx.restore();
  }

  function drawLocalPolygon(points) {
    ctx.beginPath();
    points.forEach(([x, y], index) => {
      if (index === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }

  function drawFallbackBody(p, heading) {
    ctx.save();
    ctx.translate(p.x, p.y);
    ctx.rotate(heading);
    ctx.fillStyle = '#d70d30';
    ctx.strokeStyle = '#ff5a70';
    ctx.lineWidth = 2;
    drawLocalPolygon([[22, 0], [-8, -15], [-18, 0], [-8, 15]]);
    ctx.restore();
  }

  function drawLogoBody(p, heading) {
    const width = Math.max(52, p.size * 0.092);
    const height = width * (362 / 505);
    ctx.save();
    ctx.translate(p.x, p.y);
    ctx.rotate(heading + Math.PI / 2);
    ctx.shadowColor = 'rgba(0, 0, 0, 0.42)';
    ctx.shadowBlur = Math.max(8, width * 0.12);
    ctx.shadowOffsetX = width * 0.035;
    ctx.shadowOffsetY = width * 0.035;
    if (robotLogo.complete && robotLogo.naturalWidth > 0) {
      ctx.drawImage(robotLogo, -width / 2, -height / 2, width, height);
    } else {
      ctx.rotate(-(heading + Math.PI / 2));
      drawFallbackBody({ x: 0, y: 0 }, heading);
    }
    ctx.restore();
  }

  function drawGripper(reach, heading, metrics, open) {
    const scale = Math.max(10, reach.size * 0.018);
    const baseGap = scale * 0.18;
    const tipGap = open ? scale * 0.78 : scale * 0.20;
    const length = scale * 1.22;
    const hook = scale * 0.28;

    ctx.save();
    ctx.translate(reach.x, reach.y);
    ctx.rotate(heading);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = open ? '#25c6da' : '#ffd08a';
    ctx.fillStyle = open ? 'rgba(37, 198, 218, 0.16)' : 'rgba(255, 208, 138, 0.22)';
    ctx.lineWidth = Math.max(3, scale * 0.22);

    ctx.beginPath();
    ctx.moveTo(-scale * 0.42, 0);
    ctx.lineTo(scale * 0.08, 0);
    ctx.stroke();

    for (const sign of [-1, 1]) {
      ctx.beginPath();
      ctx.moveTo(0, sign * baseGap);
      ctx.lineTo(length * 0.74, sign * tipGap);
      ctx.lineTo(length, sign * (tipGap - hook));
      ctx.stroke();
    }

    ctx.lineWidth = Math.max(2, scale * 0.11);
    ctx.strokeStyle = 'rgba(8, 6, 7, 0.82)';
    ctx.beginPath();
    ctx.moveTo(scale * 0.18, -baseGap * 1.6);
    ctx.lineTo(scale * 0.18, baseGap * 1.6);
    ctx.stroke();
    ctx.restore();
  }

  function drawBurnEffects(metrics) {
    const now = Date.now();
    const active = [];
    for (const effect of state.burnEffects) {
      const elapsed = now - effect.startedAt;
      const t = elapsed / effect.duration;
      if (t >= 1) {
        continue;
      }
      active.push(effect);
      const origin = toCanvas(effect, metrics);
      const alpha = Math.max(0, 1 - t * 0.82);
      const glowRadius = origin.size * (0.085 + t * 0.20);
      const gradient = ctx.createRadialGradient(origin.x, origin.y, 0, origin.x, origin.y, glowRadius);
      gradient.addColorStop(0, `rgba(255, 225, 118, ${0.88 * alpha})`);
      gradient.addColorStop(0.34, `rgba(255, 99, 32, ${0.58 * alpha})`);
      gradient.addColorStop(0.70, `rgba(255, 45, 35, ${0.20 * alpha})`);
      gradient.addColorStop(1, 'rgba(255, 45, 35, 0)');
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(origin.x, origin.y, glowRadius, 0, Math.PI * 2);
      ctx.fill();

      ctx.save();
      ctx.translate(origin.x, origin.y);
      ctx.globalAlpha = alpha;
      const flameScale = origin.size * (0.035 + Math.sin(now * 0.018) * 0.003);
      const flameOffsets = [-0.52, -0.22, 0.08, 0.38];
      for (const [index, offset] of flameOffsets.entries()) {
        const height = flameScale * (2.45 - index * 0.18);
        const width = flameScale * (0.86 - index * 0.06);
        const x = offset * flameScale * 1.15;
        ctx.fillStyle = index % 2 === 0 ? '#ff4b2f' : '#ff9f1c';
        ctx.beginPath();
        ctx.moveTo(x, -height * (1.25 + t * 0.24));
        ctx.bezierCurveTo(x + width, -height * 0.42, x + width * 0.62, height * 0.52, x, height * 0.72);
        ctx.bezierCurveTo(x - width * 0.72, height * 0.42, x - width, -height * 0.36, x, -height * (1.25 + t * 0.24));
        ctx.fill();
      }
      ctx.globalAlpha = Math.min(1, alpha * 1.15);
      ctx.fillStyle = '#ffe66d';
      ctx.beginPath();
      ctx.moveTo(0, -flameScale * 2.1);
      ctx.bezierCurveTo(flameScale * 0.55, -flameScale * 0.55, flameScale * 0.42, flameScale * 0.62, 0, flameScale * 0.82);
      ctx.bezierCurveTo(-flameScale * 0.42, flameScale * 0.35, -flameScale * 0.55, -flameScale * 0.55, 0, -flameScale * 2.1);
      ctx.fill();
      ctx.restore();

      for (const particle of effect.particles) {
        const local = clampValue((t - particle.delay) / (1 - particle.delay), 0, 1);
        if (local <= 0) {
          continue;
        }
        const flicker = 0.74 + Math.sin((elapsed * 0.035) + particle.angle * 9) * 0.22;
        const distance = origin.size * particle.speed * 0.155 * local;
        const x = origin.x + Math.cos(particle.angle + particle.drift * local) * distance;
        const y = origin.y + Math.sin(particle.angle) * distance - origin.size * 0.035 * local;
        const size = Math.max(4, origin.size * 0.014 * particle.size * (1 - local * 0.56) * flicker);
        ctx.globalAlpha = alpha * (1 - local * 0.25);
        ctx.fillStyle = particle.color;
        ctx.beginPath();
        ctx.moveTo(x, y - size * 1.7);
        ctx.quadraticCurveTo(x + size * 0.95, y - size * 0.2, x, y + size * 1.1);
        ctx.quadraticCurveTo(x - size * 0.95, y - size * 0.2, x, y - size * 1.7);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
    state.burnEffects = active;
  }

  function drawRobot(metrics, robot = projectedRobot()) {
    if (!robot) {
      return;
    }
    const p = toCanvas(robot, metrics);
    const reach = toCanvas({ x: robot.reach_x, y: robot.reach_y }, metrics);
    const heading = Number(robot.heading || 0);

    ctx.strokeStyle = '#25c6da';
    ctx.lineWidth = Math.max(5, p.size * 0.008);
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(reach.x, reach.y);
    ctx.stroke();

    drawLogoBody(p, heading);
    drawGripper(reach, heading, metrics, Boolean(robot.gripper_open));
  }

  function drawEmpty(metrics) {
    ctx.fillStyle = '#111512';
    ctx.fillRect(0, 0, metrics.width, metrics.height);
    ctx.fillStyle = '#a79b9e';
    ctx.font = `${Math.max(15, metrics.width * 0.018)}px Georgia, serif`;
    ctx.textAlign = 'center';
    ctx.fillText('Waiting for world state', metrics.width / 2, metrics.height / 2);
  }

  function draw() {
    const metrics = canvasMetrics();
    ctx.clearRect(0, 0, metrics.width, metrics.height);
    if (!state.world) {
      drawEmpty(metrics);
      return;
    }
    drawGrid(metrics);
    const robot = projectedRobot();
    if (robot) {
      drawScan(metrics);
    }
    drawTrash(metrics);
    drawLocalizationEstimate(metrics, robot);
    drawRobot(metrics, robot);
    drawBurnEffects(metrics);
  }

  function startRenderLoop() {
    if (state.renderFrame) {
      return;
    }
    const render = () => {
      draw();
      state.renderFrame = window.requestAnimationFrame(render);
    };
    state.renderFrame = window.requestAnimationFrame(render);
  }

  return { draw, startRenderLoop, addBurnEffect, localizationPose };
}
