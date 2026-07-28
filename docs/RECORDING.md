# Recording, scanning, colour and export

A re-scope, not an increment. Realtime streaming stops being the point;
capturing something and then producing a *file* becomes the point. Three of
the requirements contradict decisions already taken, and those are worth
stating before any of it gets built.

## What changes, and what it costs

### 1. Colour and texture force the Godot client

WebXR has no passthrough camera access. None — it is not a gap that gets
filled by a flag, the API does not exist. Every colour and texture
requirement here is therefore reachable **only** through the Godot client and
Meta's Passthrough Camera API.

This reverses [ADR-009](DECISIONS.md#adr-009-two-capture-clients-webxr-preferred),
which preferred WebXR on the strength of its depth rate. That reasoning still
holds for geometry-only realtime streaming; it does not survive a colour
requirement.

It also re-opens [R-02](RISKS.md#r-02--camera-data-policy): camera imagery is
Device User Data under Meta's Developer Data Use Policy. Processing on-device
and exporting only the finished textured model is the *good* shape for that —
better than the streaming design, since no camera frame ever leaves the
headset.

**Consequence:** the Godot APK becomes required rather than optional, and the
export-config blocker in `quest-app/README.md` becomes a real blocker rather
than an inconvenience.

### 2. Dropping realtime is what makes on-device processing possible

This is the part that makes the whole idea work. The host currently spends
~60 ms per frame fusing depth on a desktop CPU. Doing that on a Quest, live,
would be hopeless. Doing it **after the session, with no frame budget**, is an
entirely different problem: record raw frames to storage during capture, then
process at whatever speed the hardware manages while the user waits behind a
progress bar.

Removing the deadline is worth more here than any optimisation.

### 3. Drawing the mesh on the lenses reverses the founding constraint

The original brief was explicit that the 3D must *not* appear on the lenses.
Scan mode needs the opposite: you cannot scan a room well without seeing which
surfaces have been captured and which have not. Coverage feedback is the
single largest determinant of scan quality, because the wearer is the scanner.

This is a sound reversal, but it is a reversal, and it changes the app from
"a sensor that renders nothing" into "a scanner with a viewfinder". The
thermal and power arguments for rendering nothing no longer apply during scan
mode.

## The one question that changes the plan

**Does processing have to run on the Quest, or just not in realtime?**

| | On the Quest | On the PC, offline |
| --- | --- | --- |
| Standalone, no PC needed | yes | no |
| Reuses the existing host pipeline | no — reimplement fusion, meshing and texturing in GDScript/C++ | yes, nearly unchanged |
| Processing time for a room | minutes, thermally limited | seconds to a minute |
| Quality ceiling | mobile SoC | desktop GPU |
| Work to first result | weeks | days |

**Recommendation: record on the Quest, process on the PC, at least first.**
The recorder is the same either way — capture depth, colour and poses to
storage — so nothing is wasted if on-device processing follows later. What
changes is only where the recording gets consumed, and the existing host
already consumes exactly that data.

If the answer is "it must be standalone", say so and the plan below reorders:
the fusion core moves into a GDExtension and the timeline roughly triples.

## Formats

### Static scan → glTF 2.0 (`.glb`)

Textured mesh, one file, opens in Blender, Unreal, Unity, Windows 3D Viewer,
and the web viewer with no conversion. This is the easy one and there is no
reason to invent anything.

### 4D sequence → not glTF

glTF cannot represent geometry whose *topology* changes over time. Morph
targets require a fixed vertex count and correspondence between frames, which
a reconstruction does not have — each frame's marching cubes output is its own
mesh.

| Option | Blender | Web viewer | Notes |
| --- | --- | --- | --- |
| **Alembic `.abc`** | native import | no | the industry standard for time-varying geometry; what VFX actually uses |
| **USD `.usdz`** | native import | no | modern alternative, better metadata, larger tooling surface |
| glTF sequence (one file per frame) | importable via addon | yes | crude but universal; large |
| **Custom chunked stream** | no | yes | what the viewer already speaks — `MeshChunkUpdate` with timestamps |

**Recommendation: two outputs, not one compromise format.** Alembic for
Blender and anything downstream of it; the existing chunk stream, written to a
file with its timestamps, for the viewer. They are different consumers with
genuinely different needs, and a single format that serves both would serve
neither well.

The viewer already applies versioned chunk updates. A recorded session is that
same message stream with timestamps — so "watch it as a video" is a timeline
scrubber over a file the viewer can already decode, not a new renderer.

## Fast versus quality

One dial, several settings behind it:

| | Fast | Quality |
| --- | --- | --- |
| Voxel size | 4 cm | 1.5 cm |
| Depth frames kept | every 3rd | every frame |
| Texture | vertex colours | projected atlas |
| Carving | on | on, plus a second pass |
| Room processing time | seconds | minutes |

Fast mode should be the default and should be genuinely usable, because it is
what people will judge the thing by. Quality mode is for the export you
actually keep.

## Texturing

Vertex colours are nearly free — project each vertex into the nearest colour
frame and accumulate. They look acceptable at 1.5 cm voxels and are what Fast
mode should use.

A real texture atlas is a different job: UV-unwrap the mesh, then for each
texel pick the best camera view (most face-on, sharpest, best exposed) and
blend across seams. This is the single most expensive piece of work in this
document and the one most likely to look bad on the first attempt. It should
be scheduled on its own, after everything else works with vertex colours.

## Plan

Ordered so each step produces something usable on its own.

### S1 — Recorder *(~4 days)*
Write depth, poses, colour frames and the room profile to a session file on
the headset. No processing. Add a session browser and a way to pull files off
over USB or the network.

**Done when:** a recorded session replays through the existing host and
produces the same reconstruction a live stream would.

### S2 — Scan mode with live coverage *(~1 week)*
Render the accumulating mesh on the lenses during scanning, tinted by
confidence, with unscanned regions visibly absent. Fast/quality toggle.

**Done when:** the wearer can tell, without taking the headset off, which parts
of the room still need scanning.

**Note:** this needs an on-device *preview* mesh, which is a coarse version of
the fusion work — 8 cm voxels, points or blocks rather than marching cubes. It
is a viewfinder, not the output.

### S3 — Static export *(~4 days)*
Offline processing of a recorded session into a textured `.glb`, with vertex
colours. Export from the host; open in Blender.

**Done when:** a scanned room opens in Blender with recognisable colour.

### S4 — 4D sequence *(~1 week)*
Keyframe the volume at intervals, mesh each, write both a chunk-stream file
and an Alembic export.

**Done when:** the viewer scrubs a timeline and Blender imports the same
sequence.

### S5 — Texture atlas *(~1.5 weeks, optional)*
UV unwrap, per-texel view selection, seam blending.

**Done when:** the exported model is recognisably photographic rather than
recognisably voxel-coloured.

### S6 — On-device processing *(only if required)*
Move fusion and meshing into a GDExtension so no PC is needed. Add ~2–3 weeks
and expect the quality ceiling to drop.

## What survives from the existing build

More than might be obvious:

- The TSDF volume, free-space carving, contradiction-aware fusion and
  time-boxed mesher are exactly what offline processing needs. They stop being
  latency-critical, which only makes them easier.
- The wire format already carries depth, poses and the room profile. A
  recording is that stream written to a file.
- The viewer already applies chunk updates and disposes them correctly. A
  timeline is an addition, not a rewrite.
- The synthetic Quest still drives the whole pipeline without hardware.

What does not survive: the argument for streaming at all, and the "renders
nothing" constraint during scan mode.
