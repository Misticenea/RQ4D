// Scene setup, room-profile rendering, headset gizmo, and camera controls.

import * as THREE from '../vendor/three.module.js';

export function createScene(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x0b0f14, 1);

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x0b0f14, 12, 30);

  const camera = new THREE.PerspectiveCamera(55, 1, 0.05, 200);
  camera.position.set(4.5, 3.2, 6.5);

  // Three lights, no shadows: the geometry is noisy reconstruction, and
  // shadow acne on it reads as reconstruction error rather than as lighting.
  scene.add(new THREE.HemisphereLight(0xbfd4ea, 0x1a2029, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.4);
  key.position.set(4, 8, 5);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x8fb3d9, 0.5);
  fill.position.set(-5, 3, -4);
  scene.add(fill);

  const grid = new THREE.GridHelper(20, 40, 0x2a3644, 0x1a222c);
  grid.name = 'grid';
  scene.add(grid);

  scene.add(new THREE.AxesHelper(0.5));

  return { renderer, scene, camera };
}

// Minimal orbit controls. three.js ships OrbitControls only in examples/jsm,
// which would mean vendoring a second file for about sixty lines of maths.
export class Orbit {
  constructor(camera, element, target = new THREE.Vector3(3, 1.2, 2.5)) {
    this.camera = camera;
    this.element = element;
    this.target = target;
    this.spherical = new THREE.Spherical();
    this.spherical.setFromVector3(camera.position.clone().sub(target));
    this._dragging = false;
    this._panning = false;
    this._last = { x: 0, y: 0 };

    element.addEventListener('contextmenu', (e) => e.preventDefault());
    element.addEventListener('pointerdown', (e) => {
      this._dragging = true;
      this._panning = e.button === 2 || e.shiftKey;
      this._last = { x: e.clientX, y: e.clientY };
      element.setPointerCapture(e.pointerId);
    });
    element.addEventListener('pointerup', (e) => {
      this._dragging = false;
      element.releasePointerCapture(e.pointerId);
    });
    element.addEventListener('pointermove', (e) => {
      if (!this._dragging) return;
      const dx = e.clientX - this._last.x;
      const dy = e.clientY - this._last.y;
      this._last = { x: e.clientX, y: e.clientY };
      if (this._panning) this._pan(dx, dy);
      else this._rotate(dx, dy);
    });
    element.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.spherical.radius = THREE.MathUtils.clamp(
        this.spherical.radius * (1 + Math.sign(e.deltaY) * 0.12), 0.4, 60
      );
      this.update();
    }, { passive: false });

    this.update();
  }

  _rotate(dx, dy) {
    this.spherical.theta -= dx * 0.006;
    this.spherical.phi = THREE.MathUtils.clamp(
      this.spherical.phi - dy * 0.006, 0.05, Math.PI - 0.05
    );
    this.update();
  }

  _pan(dx, dy) {
    const scale = this.spherical.radius * 0.0016;
    const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrix, 0);
    const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrix, 1);
    this.target.addScaledVector(right, -dx * scale).addScaledVector(up, dy * scale);
    this.update();
  }

  frame(box) {
    if (box.isEmpty()) return;
    box.getCenter(this.target);
    this.spherical.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 0.9, 1.5);
    this.update();
  }

  update() {
    this.camera.position.setFromSpherical(this.spherical).add(this.target);
    this.camera.lookAt(this.target);
  }
}

// The room profile is drawn the moment it arrives, before any live geometry.
// A viewer that sits blank while the reconstruction converges looks broken;
// showing the room outline immediately makes the wait legible.
export class RoomView {
  constructor(scene) {
    this.group = new THREE.Group();
    this.group.name = 'room';
    scene.add(this.group);
    this.bounds = new THREE.Box3();
  }

  setProfile(profile) {
    this.clear();
    if (!profile) return;

    if (Array.isArray(profile.bounds) && profile.bounds.length === 2) {
      const [lo, hi] = profile.bounds;
      this.bounds = new THREE.Box3(
        new THREE.Vector3(lo[0], lo[1], lo[2]),
        new THREE.Vector3(hi[0], hi[1], hi[2])
      );
      const helper = new THREE.Box3Helper(this.bounds, 0x2563eb);
      helper.material.transparent = true;
      helper.material.opacity = 0.4;
      this.group.add(helper);
    }

    const planeMat = new THREE.MeshBasicMaterial({
      color: 0x1d4ed8, transparent: true, opacity: 0.12, side: THREE.DoubleSide,
    });
    const volumeMat = new THREE.MeshBasicMaterial({
      color: 0x0ea5e9, wireframe: true, transparent: true, opacity: 0.5,
    });

    for (const entry of profile.planes ?? []) this._addEntity(entry, planeMat);
    for (const entry of profile.volumes ?? []) this._addEntity(entry, volumeMat);
  }

  // Entities arrive either as an lo/hi box (the synthetic room) or as a
  // position plus orientation (the headset). Accept both rather than forcing
  // one shape on the capture side.
  _addEntity(entry, material) {
    let mesh = null;
    if (Array.isArray(entry.lo) && Array.isArray(entry.hi)) {
      const lo = new THREE.Vector3(...entry.lo);
      const hi = new THREE.Vector3(...entry.hi);
      const size = hi.clone().sub(lo);
      mesh = new THREE.Mesh(
        new THREE.BoxGeometry(Math.max(size.x, 0.01), Math.max(size.y, 0.01), Math.max(size.z, 0.01)),
        material
      );
      mesh.position.copy(lo).add(hi).multiplyScalar(0.5);
    } else if (Array.isArray(entry.position)) {
      mesh = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.4, 0.4), material);
      mesh.position.set(...entry.position);
      if (Array.isArray(entry.basis) && entry.basis.length === 4) {
        mesh.quaternion.set(...entry.basis);
      }
    }
    if (!mesh) return;
    mesh.userData.label = entry.label ?? '';
    this.group.add(mesh);
  }

  setVisible(visible) {
    this.group.visible = visible;
  }

  clear() {
    for (const child of [...this.group.children]) {
      this.group.remove(child);
      child.geometry?.dispose?.();
    }
  }
}

// Where the wearer is and what they are looking at — the single most useful
// cue for understanding why a region of the room is or is not filling in.
export class HeadsetGizmo {
  constructor(scene) {
    this.group = new THREE.Group();
    this.group.name = 'headset';

    const body = new THREE.Mesh(
      new THREE.BoxGeometry(0.18, 0.09, 0.11),
      new THREE.MeshStandardMaterial({ color: 0xf97316, roughness: 0.5 })
    );
    this.group.add(body);

    const frustum = new THREE.Mesh(
      new THREE.ConeGeometry(0.55, 1.1, 4, 1, true),
      new THREE.MeshBasicMaterial({
        color: 0xf97316, transparent: true, opacity: 0.16, side: THREE.DoubleSide,
      })
    );
    frustum.rotation.x = Math.PI / 2;
    frustum.rotation.y = Math.PI / 4;
    frustum.position.z = -0.55;
    this.group.add(frustum);

    this.group.visible = false;
    scene.add(this.group);
  }

  update(pose) {
    this.group.visible = true;
    this.group.position.set(...pose.position);
    this.group.quaternion.set(...pose.quaternion);
  }
}
