// RQ4D capture client — WebXR. The headset is a sensor, not a display.
//
// Same contract as the Godot app: emit RoomProfile, PoseFrame and DepthFrame,
// and the host cannot tell which producer it is talking to. Nothing of the
// reconstruction is drawn on the lenses; the wearer sees passthrough plus a
// DOM overlay with status.
//
// The reason this exists alongside the Godot client is depth rate. Godot's
// CPU readback is documented as a 1-2 second operation; WebXR's is only valid
// *inside* the animation frame callback, which is a per-frame contract by
// construction. Measured on device, WebXR depth is realtime.

import { Msg, encodeJson } from 'rq4d/wire';
import { checkAlignmentMatrix, encodeDepthFrame, encodePoseFrame, nowNs } from './encode.js';

const HOST_KEY = 'rq4d.capture.host';
const POSE_HZ = 30;

const ui = {
  status: document.getElementById('status'),
  detail: document.getElementById('detail'),
  stats: document.getElementById('stats'),
  warn: document.getElementById('warn'),
  enter: document.getElementById('enter'),
  host: document.getElementById('host'),
  setup: document.getElementById('setup'),
};

const state = {
  session: null,
  refSpace: null,
  socket: null,
  anchor: null,
  anchorSpace: null,
  profileSent: false,
  targetDepthHz: 15,
  lastDepthAt: 0,
  lastPoseAt: 0,
  depthArrivals: [],
  framesSent: 0,
  bytesSent: 0,
  depthSize: null,
  alignmentWarned: false,
};

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------

function hostUrl() {
  const override = new URLSearchParams(location.search).get('host');
  const saved = override ?? localStorage.getItem(HOST_KEY);
  if (saved) {
    let value = saved.trim().replace(/^https?:\/\//, 'ws://');
    if (!/^wss?:\/\//.test(value)) value = `ws://${value}`;
    const url = new URL(value);
    if (!url.port) url.port = '8787';
    if (url.pathname === '/' || url.pathname === '') url.pathname = '/ws';
    return url.toString();
  }
  // Served by the host itself in the normal case, so its address is simply
  // where this page came from.
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

function connect() {
  const url = hostUrl();
  state.socket = new WebSocket(url);
  state.socket.binaryType = 'arraybuffer';

  state.socket.onopen = () => {
    setStatus('connected', url.replace(/^wss?:\/\//, ''));
    send(encodeJson(Msg.HELLO, {
      protocol_version: '0.1',
      role: 'producer',
      device: navigator.userAgent.includes('Quest') ? 'quest-webxr' : 'webxr',
      client: 'rq4d-webxr',
      clock_ns: Math.round(performance.now() * 1e6),
    }, nowNs()));
    if (state.profileSent) state.profileSent = false;  // resend on reconnect
  };

  state.socket.onclose = () => {
    setStatus('offline', 'reconnecting…');
    setTimeout(connect, 2000);
  };
  state.socket.onerror = () => state.socket.close();
  state.socket.onmessage = (event) => {
    // Control only; the producer ignores mesh traffic.
    const view = new DataView(event.data);
    if (view.byteLength >= 16 && view.getUint8(4) === Msg.CONTROL) {
      const body = JSON.parse(new TextDecoder().decode(event.data.slice(16)));
      if (body.command === 'Recalibrate') state.profileSent = false;
      if (body.command === 'SetQuality' && body.quality?.depth_hz) {
        state.targetDepthHz = body.quality.depth_hz;
      }
    }
  };
}

function send(buffer) {
  const socket = state.socket;
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  // Drop rather than queue. A depth frame that could not go out in time
  // describes a moment the wearer has already left, and bufferedAmount growing
  // is exactly the signal that the link cannot keep up.
  if (socket.bufferedAmount > 4 * 1024 * 1024) return false;
  socket.send(buffer);
  state.bytesSent += buffer.byteLength;
  return true;
}

// ---------------------------------------------------------------------------
// calibration — planes, meshes, and a persistent anchor as the origin
// ---------------------------------------------------------------------------

async function buildRoomProfile(frameData, refSpace) {
  const planes = [];
  const volumes = [];
  let floor = Infinity;
  let ceiling = -Infinity;
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];

  const record = (label, pose, extents) => {
    const p = pose.transform.position;
    for (let i = 0; i < 3; i++) {
      const v = [p.x, p.y, p.z][i];
      lo[i] = Math.min(lo[i], v);
      hi[i] = Math.max(hi[i], v);
    }
    const q = pose.transform.orientation;
    return {
      label,
      position: [p.x, p.y, p.z],
      basis: [q.x, q.y, q.z, q.w],
      extents,
    };
  };

  for (const plane of frameData.detectedPlanes ?? []) {
    const pose = frameData.getPose(plane.planeSpace, refSpace);
    if (!pose) continue;
    const label = plane.semanticLabel ?? plane.orientation ?? 'plane';
    planes.push(record(label, pose, null));
    if (label === 'floor') floor = Math.min(floor, pose.transform.position.y);
    if (label === 'ceiling') ceiling = Math.max(ceiling, pose.transform.position.y);
  }

  for (const mesh of frameData.detectedMeshes ?? []) {
    const pose = frameData.getPose(mesh.meshSpace, refSpace);
    if (!pose) continue;
    volumes.push(record(mesh.semanticLabel ?? 'mesh', pose, null));
  }

  if (!Number.isFinite(floor)) floor = lo[1];
  if (!Number.isFinite(ceiling)) ceiling = Math.max(floor + 2.4, hi[1]);
  if (!Number.isFinite(lo[0])) {
    // Nothing detected — fall back to a generous box around the wearer so the
    // host still has bounds to size its volume with.
    lo[0] = -4; lo[2] = -4; hi[0] = 4; hi[2] = 4;
    floor = -1.6; ceiling = 1.4;
  }

  return {
    profile_id: `webxr-${Date.now()}`,
    bounds: [
      [lo[0] - 1.0, floor - 0.2, lo[2] - 1.0],
      [hi[0] + 1.0, ceiling + 0.2, hi[2] + 1.0],
    ],
    floor_height: floor,
    ceiling_height: ceiling,
    planes,
    volumes,
    device: { model: 'webxr', engine: navigator.userAgent },
    coverage_pct: 0,
  };
}

// A persistent anchor is the world origin — ADR-003. Tracking space drifts and
// jumps on relocalisation, and data anchored to it shears over a long session,
// invisibly until it is badly wrong.
async function establishAnchor(session, frameData, refSpace) {
  if (!('createAnchor' in frameData)) return null;
  const saved = localStorage.getItem('rq4d.anchor');
  if (saved && session.restorePersistentAnchor) {
    try {
      const anchor = await session.restorePersistentAnchor(saved);
      return anchor;
    } catch (err) {
      console.warn('anchor restore failed, creating a new one', err);
    }
  }
  try {
    const anchor = await frameData.createAnchor(new XRRigidTransform(), refSpace);
    if (anchor.requestPersistentHandle) {
      localStorage.setItem('rq4d.anchor', await anchor.requestPersistentHandle());
    }
    return anchor;
  } catch (err) {
    console.warn('anchor creation failed; poses will be in tracking space', err);
    return null;
  }
}

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------

async function start() {
  if (!navigator.xr) return fail('WebXR unavailable in this browser');
  if (!(await navigator.xr.isSessionSupported('immersive-ar'))) {
    return fail('immersive-ar not supported — needs a Quest 3 or 3S');
  }

  let session;
  try {
    session = await navigator.xr.requestSession('immersive-ar', {
      requiredFeatures: ['depth-sensing', 'local-floor'],
      optionalFeatures: ['anchors', 'plane-detection', 'mesh-detection', 'dom-overlay'],
      depthSensing: {
        usagePreference: ['cpu-optimized'],
        dataFormatPreference: ['float32', 'luminance-alpha'],
      },
      domOverlay: { root: document.getElementById('hud') },
    });
  } catch (err) {
    return fail(`session refused: ${err.message}`);
  }

  state.session = session;
  ui.setup.hidden = true;
  setStatus('starting', 'entering session…');

  session.addEventListener('end', () => {
    state.session = null;
    ui.setup.hidden = false;
    setStatus('offline', 'session ended');
  });

  // A canvas is required for the frame loop even though nothing is drawn into
  // it. Rendering no content is the point: the GPU stays idle and the power
  // budget goes to the sensors and the radio.
  const canvas = document.createElement('canvas');
  const gl = canvas.getContext('webgl2', { xrCompatible: true });
  await gl.makeXRCompatible();
  session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });

  state.refSpace = await session.requestReferenceSpace('local-floor');
  session.requestAnimationFrame(onFrame);
}

function onFrame(_time, frameData) {
  const session = state.session;
  if (!session) return;
  session.requestAnimationFrame(onFrame);

  const pose = frameData.getViewerPose(state.refSpace);
  if (!pose) return;

  const now = performance.now();

  if (!state.profileSent) {
    state.profileSent = true;  // set first: the await must not re-enter
    (async () => {
      state.anchor = state.anchor ?? await establishAnchor(session, frameData, state.refSpace);
      const profile = await buildRoomProfile(frameData, state.refSpace);
      send(encodeJson(Msg.ROOM_PROFILE, profile, nowNs()));
      setStatus('streaming', `${profile.planes.length} planes, ${profile.volumes.length} meshes`);
    })().catch((err) => {
      state.profileSent = false;
      console.error('calibration failed', err);
    });
  }

  if (now - state.lastPoseAt >= 1000 / POSE_HZ) {
    state.lastPoseAt = now;
    send(encodePoseFrame(pose));
  }

  if (now - state.lastDepthAt >= 1000 / state.targetDepthHz) {
    state.lastDepthAt = now;
    // One view only. The two eyes overlap heavily, so the second roughly
    // doubles the bitrate for very little extra coverage.
    const view = pose.views[0];
    const depth = frameData.getDepthInformation?.(view);
    if (depth) {
      const drift = checkAlignmentMatrix(depth);
      if (drift > 0.01 && !state.alignmentWarned) {
        state.alignmentWarned = true;
        ui.warn.textContent =
          `depth buffer is not view-aligned (max deviation ${drift.toFixed(3)}) — ` +
          `geometry will be offset until normDepthBufferFromNormView is applied`;
        ui.warn.hidden = false;
      }
      state.depthSize = `${depth.width}x${depth.height}`;
      if (send(encodeDepthFrame(depth, view, 0))) {
        state.framesSent += 1;
        state.depthArrivals.push(now);
      }
    }
  }

  if (now % 500 < 20) updateStats(now);
}

// ---------------------------------------------------------------------------
// ui
// ---------------------------------------------------------------------------

function setStatus(kind, detail) {
  ui.status.dataset.kind = kind;
  ui.status.textContent = kind;
  ui.detail.textContent = detail ?? '';
}

function fail(message) {
  setStatus('error', message);
  ui.enter.disabled = true;
}

function updateStats(now) {
  state.depthArrivals = state.depthArrivals.filter((t) => t > now - 4000);
  const hz = state.depthArrivals.length / 4;
  ui.stats.textContent = [
    `depth ${hz.toFixed(1)} Hz of ${state.targetDepthHz}`,
    state.depthSize ? `res ${state.depthSize}` : 'res —',
    `sent ${(state.bytesSent / 1e6).toFixed(1)} MB`,
    state.anchor ? 'anchored' : 'no anchor — poses will drift',
  ].join('\n');
}

ui.enter.addEventListener('click', start);
ui.host.addEventListener('change', () => {
  localStorage.setItem(HOST_KEY, ui.host.value.trim());
  if (state.socket) { state.socket.onclose = null; state.socket.close(); }
  connect();
});
ui.host.value = localStorage.getItem(HOST_KEY) ?? '';

connect();
