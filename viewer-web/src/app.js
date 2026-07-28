// RQ4D viewer — connects to the reconstruction host and renders the live mesh.

import * as THREE from '../vendor/three.module.js';
import { Msg, TRACKING_LABELS, decodeFrame, decodeMeshChunk, decodePoseFrame, encodeJson } from 'rq4d/wire';
import { ChunkManager } from './chunks.js';
import { HeadsetGizmo, Orbit, RoomView, createScene } from './scene.js';

const canvas = document.getElementById('view');
const { renderer, scene, camera } = createScene(canvas);
const orbit = new Orbit(camera, canvas);
const chunks = new ChunkManager(scene);
const room = new RoomView(scene);
const headset = new HeadsetGizmo(scene);

const ui = {
  link: document.getElementById('link'),
  linkDot: document.getElementById('link-dot'),
  stats: document.getElementById('stats'),
  hostStats: document.getElementById('host-stats'),
  toast: document.getElementById('toast'),
};

let socket = null;
let reconnectTimer = null;
let framed = false;
let poseArrivals = [];
let chunkArrivals = [];
let hostStatus = null;

const HOST_KEY = 'rq4d.host';

// Served by the host itself, so its address is normally just where we came
// from — no configuration and no IP to type twice. The single-file build
// breaks that assumption: opened from disk there is no origin to infer, so
// fall back to an explicit address, remembered between sessions.
function hostUrl() {
  const override = new URLSearchParams(location.search).get('host');
  if (override) return normaliseHost(override);
  if (location.protocol === 'http:' || location.protocol === 'https:') {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws`;
  }
  const saved = localStorage.getItem(HOST_KEY);
  return saved ? normaliseHost(saved) : '';
}

// Accept whatever a person reasonably types: a bare IP, an IP and port, or a
// full URL of either scheme. Reading an address off a headset through
// passthrough is error-prone enough without demanding exact syntax.
function normaliseHost(input) {
  let value = input.trim();
  if (!value) return '';
  value = value.replace(/^https?:\/\//, 'ws://').replace(/^wss:\/\//, 'wss://');
  if (!/^wss?:\/\//.test(value)) value = `ws://${value}`;
  const url = new URL(value);
  if (!url.port) url.port = '8787';
  if (url.pathname === '/' || url.pathname === '') url.pathname = '/ws';
  return url.toString();
}

function connect() {
  clearTimeout(reconnectTimer);
  const url = hostUrl();
  if (!url) {
    setLink('offline', 'enter host address');
    document.getElementById('connect-bar').classList.add('visible');
    return;
  }
  document.getElementById('connect-bar').classList.remove('visible');
  setLink('connecting', url);

  socket = new WebSocket(url);
  socket.binaryType = 'arraybuffer';

  socket.onopen = () => {
    // `location.host` is empty for a file:// page, so show the address we
    // actually dialled rather than a blank label.
    setLink('connected', url.replace(/^wss?:\/\//, '').replace(/\/ws$/, ''));
    socket.send(encodeJson(Msg.HELLO, {
      protocol_version: '0.1',
      role: 'viewer',
      client: 'rq4d-web',
    }, Date.now() * 1e6));
  };

  socket.onmessage = (event) => handleFrame(event.data);

  socket.onclose = () => {
    setLink('offline', 'reconnecting…');
    headset.group.visible = false;
    reconnectTimer = setTimeout(connect, 2000);
  };

  socket.onerror = () => socket.close();
}

function handleFrame(data) {
  const frame = decodeFrame(data);
  if (!frame) return;

  switch (frame.type) {
    case Msg.ROOM_PROFILE: {
      // A new profile means recalibration: the coordinate frame may have
      // moved, so cached chunks are no longer trustworthy. A clean reset is
      // more predictable than trying to reconcile two baselines.
      chunks.clear();
      room.setProfile(frame.json);
      framed = false;
      // Default the cut just under the ceiling, so the first thing shown is
      // the inside of the room rather than the outside of a closed box.
      if (!room.bounds.isEmpty()) {
        const floor = frame.json?.floor_height ?? room.bounds.min.y;
        clipInput.min = floor.toFixed(2);
        clipInput.max = room.bounds.max.y.toFixed(2);
        clipInput.step = '0.05';
        clipInput.value = Math.max(floor + 0.1, room.bounds.max.y - 0.75).toFixed(2);
        applyClip();
      }
      toast('Room profile received');
      break;
    }
    case Msg.MESH_CHUNK_UPDATE: {
      if (chunks.apply(decodeMeshChunk(frame.payload))) {
        chunkArrivals.push(performance.now());
      }
      if (!framed && chunks.chunks.size > 8) {
        orbit.frame(room.bounds.isEmpty() ? sceneBounds() : room.bounds.clone());
        framed = true;
      }
      break;
    }
    case Msg.CHUNK_REMOVED: {
      if (frame.json?.key) chunks.remove(frame.json.key.join(','));
      break;
    }
    case Msg.POSE_FRAME: {
      const pose = decodePoseFrame(frame.payload);
      headset.update(pose.head);
      poseArrivals.push(performance.now());
      ui.linkDot.dataset.tracking = TRACKING_LABELS[pose.tracking] ?? 'lost';
      break;
    }
    case Msg.STATS: {
      hostStatus = frame.json;
      break;
    }
    default:
      break;
  }
}

function sceneBounds() {
  const box = new THREE.Box3();
  box.setFromObject(chunks.group);
  return box;
}

function setLink(state, detail) {
  ui.linkDot.dataset.state = state;
  ui.link.textContent = detail;
}

let toastTimer = null;
function toast(message) {
  ui.toast.textContent = message;
  ui.toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ui.toast.classList.remove('visible'), 2600);
}

// -- controls ---------------------------------------------------------------

for (const button of document.querySelectorAll('[data-mode]')) {
  button.addEventListener('click', () => {
    for (const other of document.querySelectorAll('[data-mode]')) {
      other.classList.toggle('active', other === button);
    }
    chunks.setMode(button.dataset.mode);
    document.getElementById('legend').classList.toggle(
      'visible', button.dataset.mode === 'age'
    );
  });
}

document.getElementById('toggle-room').addEventListener('click', (e) => {
  const visible = !room.group.visible;
  room.setVisible(visible);
  e.currentTarget.classList.toggle('active', visible);
});

document.getElementById('toggle-grid').addEventListener('click', (e) => {
  const grid = scene.getObjectByName('grid');
  grid.visible = !grid.visible;
  e.currentTarget.classList.toggle('active', grid.visible);
});

document.getElementById('recalibrate').addEventListener('click', () => {
  if (socket?.readyState !== WebSocket.OPEN) return;
  socket.send(encodeJson(Msg.CONTROL, { command: 'Recalibrate' }, Date.now() * 1e6));
  toast('Recalibration requested');
});

const clipInput = document.getElementById('clip');
const clipValue = document.getElementById('clip-value');

function applyClip() {
  const height = parseFloat(clipInput.value);
  chunks.setClipHeight(height >= parseFloat(clipInput.max) ? Infinity : height);
  clipValue.textContent = height >= parseFloat(clipInput.max) ? 'off' : `${height.toFixed(1)} m`;
}
clipInput.addEventListener('input', applyClip);

document.getElementById('refit').addEventListener('click', () => {
  const box = room.bounds.isEmpty() ? sceneBounds() : room.bounds.clone();
  orbit.frame(box);
});

const hostInput = document.getElementById('host-input');
function submitHost() {
  const value = hostInput.value.trim();
  if (!value) return;
  localStorage.setItem(HOST_KEY, value);
  if (socket) { socket.onclose = null; socket.close(); }
  connect();
}
document.getElementById('host-go').addEventListener('click', submitHost);
hostInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitHost(); });
hostInput.value = localStorage.getItem(HOST_KEY) ?? '';

// -- loop -------------------------------------------------------------------

function resize() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width === width && canvas.height === height) return;
  renderer.setSize(width, height, false);
  camera.aspect = width / Math.max(height, 1);
  camera.updateProjectionMatrix();
}

let lastRefresh = 0;
function render(now) {
  resize();

  if (now - lastRefresh > 500) {
    lastRefresh = now;
    chunks.refreshMaterials();
    updateStats(now);
  }

  renderer.render(scene, camera);
  requestAnimationFrame(render);
}

function updateStats(now) {
  const cutoff = now - 4000;
  chunkArrivals = chunkArrivals.filter((t) => t > cutoff);
  const chunkHz = chunkArrivals.length / 4;
  const s = chunks.stats();
  // Rate, not age. The stats panel only refreshes with the render loop, and
  // under a software renderer that can be slower than any sensible age
  // threshold — which made a healthy pose stream read as "no pose".
  poseArrivals = poseArrivals.filter((t) => t > cutoff);
  const poseHz = poseArrivals.length / 4;

  ui.stats.innerHTML = [
    ['chunks', s.chunks],
    ['triangles', s.triangles.toLocaleString()],
    ['updates', `${chunkHz.toFixed(1)}/s`],
    ['headset', poseHz > 0 ? `${poseHz.toFixed(1)} Hz` : 'no pose'],
  ].map(([k, v]) => `<span class="k">${k}</span><span class="v">${v}</span>`).join('');

  if (!hostStatus) {
    ui.hostStats.textContent = '';
    return;
  }
  const stages = hostStatus.stages ?? {};
  const rates = hostStatus.rates ?? {};
  const counters = hostStatus.counters ?? {};
  const rows = [
    ['depth in', `${(rates.depth_hz ?? 0).toFixed(1)} Hz`],
    ['integrate', fmtMs(stages.integrate)],
    ['mesh tick', fmtMs(stages.mesh_tick)],
    ['backlog', hostStatus.backlog ?? 0],
    ['volume', `${hostStatus.volume_mb ?? 0} MB`],
  ];
  if (counters.frames_dropped_backpressure) {
    rows.push(['dropped', counters.frames_dropped_backpressure]);
  }
  ui.hostStats.innerHTML = rows
    .map(([k, v]) => `<span class="k">${k}</span><span class="v">${v}</span>`)
    .join('');
}

function fmtMs(stage) {
  if (!stage || !stage.n) return '—';
  return `${stage.p50} / ${stage.p95} ms`;
}

connect();
requestAnimationFrame(render);
