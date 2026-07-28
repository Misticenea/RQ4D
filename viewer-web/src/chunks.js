// Chunk manager: applies mesh deltas to the scene.
//
// The host sends only chunks whose geometry actually changed, versioned per
// chunk. This keeps the matching client-side state: replace on a newer
// version, ignore anything stale or duplicated, and dispose properly so a
// long session does not leak GPU buffers a chunk at a time.

import * as THREE from '../vendor/three.module.js';

// Age buckets for the freshness overlay. Discrete rather than a continuous
// gradient so materials are shared: hundreds of chunks each owning a material
// is the difference between one draw call per chunk and a stalled frame.
const AGE_BUCKETS = [
  { maxAge: 1.0, color: 0x4ade80 },
  { maxAge: 3.0, color: 0x86efac },
  { maxAge: 8.0, color: 0xfde047 },
  { maxAge: 20.0, color: 0xfb923c },
  { maxAge: Infinity, color: 0x64748b },
];

export class ChunkManager {
  constructor(scene) {
    this.scene = scene;
    this.chunks = new Map(); // "x,y,z" -> { mesh, version, updatedAt }
    this.group = new THREE.Group();
    this.group.name = 'chunks';
    scene.add(this.group);

    this.solidMaterial = new THREE.MeshStandardMaterial({
      color: 0xc8d4e0,
      roughness: 0.85,
      metalness: 0.0,
      flatShading: false,
      side: THREE.DoubleSide,
    });
    this.ageMaterials = AGE_BUCKETS.map(
      (b) => new THREE.MeshStandardMaterial({
        color: b.color, roughness: 0.9, metalness: 0.0, side: THREE.DoubleSide,
      })
    );
    this.wireMaterial = new THREE.MeshBasicMaterial({
      color: 0x38bdf8, wireframe: true, transparent: true, opacity: 0.35,
    });

    this.mode = 'solid';
    this.triangleCount = 0;
    this.clipHeight = Infinity;
  }

  // A reconstructed room is a closed box, so viewed from outside it is just a
  // box — the ceiling hides everything worth looking at. Hiding chunks above
  // a height is the same trick every floorplan viewer uses, and it costs one
  // visibility flag per chunk rather than a clipping plane per material.
  setClipHeight(height) {
    this.clipHeight = height;
    for (const entry of this.chunks.values()) {
      entry.mesh.visible = entry.mesh.position.y <= this.clipHeight;
    }
  }

  apply(update) {
    const key = update.key.join(',');
    const existing = this.chunks.get(key);
    if (existing && existing.version >= update.version) return false;

    if (existing) {
      this.group.remove(existing.mesh);
      existing.mesh.geometry.dispose();
      this.triangleCount -= existing.triangles;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(update.vertices, 3));
    if (update.normals.length === update.vertices.length) {
      geometry.setAttribute('normal', new THREE.BufferAttribute(update.normals, 3));
    } else {
      geometry.computeVertexNormals();
    }
    geometry.setIndex(new THREE.BufferAttribute(update.indices, 1));
    geometry.computeBoundingSphere();

    const mesh = new THREE.Mesh(geometry, this._materialFor(0));
    // Vertices arrive in chunk-local metres; the origin places the chunk in
    // the anchor frame. Keeping them local is what lets a chunk be replaced
    // without touching anything around it.
    mesh.position.set(update.origin[0], update.origin[1], update.origin[2]);
    mesh.frustumCulled = true;
    mesh.visible = mesh.position.y <= this.clipHeight;
    this.group.add(mesh);

    const triangles = update.indices.length / 3;
    this.triangleCount += triangles;
    this.chunks.set(key, {
      mesh, version: update.version, updatedAt: performance.now() / 1000, triangles,
    });
    return true;
  }

  remove(key) {
    const entry = this.chunks.get(key);
    if (!entry) return;
    this.group.remove(entry.mesh);
    entry.mesh.geometry.dispose();
    this.triangleCount -= entry.triangles;
    this.chunks.delete(key);
  }

  clear() {
    for (const key of [...this.chunks.keys()]) this.remove(key);
  }

  setMode(mode) {
    this.mode = mode;
    this.refreshMaterials(true);
  }

  // Freshness tinting has to be re-evaluated over time, not just on update:
  // a chunk going stale is exactly the signal worth seeing, and it happens
  // when nothing arrives.
  refreshMaterials(force = false) {
    if (this.mode !== 'age' && !force) return;
    const now = performance.now() / 1000;
    for (const entry of this.chunks.values()) {
      entry.mesh.material = this._materialFor(now - entry.updatedAt);
    }
  }

  _materialFor(age) {
    if (this.mode === 'wire') return this.wireMaterial;
    if (this.mode !== 'age') return this.solidMaterial;
    for (let i = 0; i < AGE_BUCKETS.length; i++) {
      if (age <= AGE_BUCKETS[i].maxAge) return this.ageMaterials[i];
    }
    return this.ageMaterials[this.ageMaterials.length - 1];
  }

  stats() {
    return { chunks: this.chunks.size, triangles: this.triangleCount };
  }
}
