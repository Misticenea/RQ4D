// Frame encoding for the WebXR capture client.
//
// Kept apart from the session and the DOM so it can be tested directly: this
// is the half that has to agree byte-for-byte with host/rq4d_host/wire.py,
// and a layout mismatch here produces geometry that decodes into plausible
// nonsense rather than an error. See host/tests/test_webxr_encode.py.

import { Msg } from 'rq4d/wire';

export const nowNs = () => Math.round(performance.now() * 1e6);

function frame(type, payloadLength, build) {
  const buffer = new ArrayBuffer(16 + payloadLength);
  const view = new DataView(buffer);
  view.setUint32(0, payloadLength, true);
  view.setUint8(4, type);
  view.setUint8(5, 0);
  view.setUint16(6, 0, true);
  view.setBigUint64(8, BigInt(nowNs()), true);
  build(view, new Uint8Array(buffer), 16);
  return buffer;
}

function writePose(view, offset, position, orientation) {
  view.setFloat32(offset, position.x, true);
  view.setFloat32(offset + 4, position.y, true);
  view.setFloat32(offset + 8, position.z, true);
  view.setFloat32(offset + 12, orientation.x, true);
  view.setFloat32(offset + 16, orientation.y, true);
  view.setFloat32(offset + 20, orientation.z, true);
  view.setFloat32(offset + 24, orientation.w, true);
  return offset + 28;
}

// OpenXR-style half-angles, recovered from the view's projection matrix.
// Quest frusta are asymmetric; assuming symmetry puts every reconstructed
// point on a slight diagonal offset that reads as tracking drift.
function fovFromProjection(p) {
  const sx = p[0], sy = p[5], ox = p[8], oy = p[9];
  return [
    Math.atan((ox - 1) / sx),
    Math.atan((ox + 1) / sx),
    Math.atan((oy + 1) / sy),
    Math.atan((oy - 1) / sy),
  ];
}

function encodePoseFrame(viewerPose) {
  const views = viewerPose.views;
  const length = 1 + 28 + views.length * (28 + 16);
  return frame(Msg.POSE_FRAME, length, (view) => {
    let off = 16;
    view.setUint8(off, 0);  // tracked
    off += 1;
    off = writePose(view, off, viewerPose.transform.position, viewerPose.transform.orientation);
    for (const v of views) {
      off = writePose(view, off, v.transform.position, v.transform.orientation);
      for (const angle of fovFromProjection(v.projectionMatrix)) {
        view.setFloat32(off, angle, true);
        off += 4;
      }
    }
  });
}

// Depth payload — identical layout to the Godot client and wire.py:
//   0 view_index | 1 encoding | 2 hands_removed | 3 pad
//   4 width u16  | 6 height u16
//   8 depth_scale f32 | 12 near f32 | 16 far f32
//  20 pose (7 f32)    | 48 fov (4 f32)
//  64 matrix (16 f32) | 128 depth blob
const ENC_RAW16 = 0;
const ENC_RAW_F32 = 2;

function encodeDepthFrame(depth, xrView, viewIndex) {
  const bytes = new Uint8Array(depth.data);
  const isFloat = bytes.byteLength === depth.width * depth.height * 4;
  const length = 128 + bytes.byteLength;

  return frame(Msg.DEPTH_FRAME, length, (view, u8, base) => {
    let off = base;
    view.setUint8(off, viewIndex);
    view.setUint8(off + 1, isFloat ? ENC_RAW_F32 : ENC_RAW16);
    view.setUint8(off + 2, 0);
    view.setUint8(off + 3, 0);
    view.setUint16(off + 4, depth.width, true);
    view.setUint16(off + 6, depth.height, true);
    // WebXR hands back metric depth directly: raw * rawValueToMeters. No
    // clip-space linearisation, unlike the Godot path.
    view.setFloat32(off + 8, depth.rawValueToMeters, true);
    view.setFloat32(off + 12, 0.2, true);
    view.setFloat32(off + 16, 5.0, true);
    off += 20;
    off = writePose(view, off, xrView.transform.position, xrView.transform.orientation);
    for (const angle of fovFromProjection(xrView.projectionMatrix)) {
      view.setFloat32(off, angle, true);
      off += 4;
    }
    // Matrix slot: unused on this path, see checkAlignment().
    for (let i = 0; i < 16; i++) {
      view.setFloat32(off, 0, true);
      off += 4;
    }
    u8.set(bytes, off);
  });
}

// The depth buffer is not required to cover the view rectangle exactly, and
// `normDepthBufferFromNormView` describes the mapping. Building rays as if it
// were identity is only correct when it is, so measure the deviation rather
// than assume — a wrong mapping yields geometry that looks plausible and sits
// in the wrong place, which is the hardest kind of error to notice.
export function checkAlignmentMatrix(depth) {
  const transform = depth?.normDepthBufferFromNormView;
  if (!transform) return 0;
  const m = transform.matrix;
  const identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  let drift = 0;
  for (let i = 0; i < 16; i++) drift = Math.max(drift, Math.abs(m[i] - identity[i]));
  return drift;
}

export { frame, writePose, fovFromProjection, encodePoseFrame,
         encodeDepthFrame, ENC_RAW16, ENC_RAW_F32 };
