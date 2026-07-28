# RQ4D host

Depth frames and poses in, chunked mesh deltas out.

## Run

```bash
pip install numpy scikit-image websockets pytest

# reconstruction server
python -m rq4d_host.server --port 8787

# synthetic Quest, in another shell — no headset needed
python tools/synth_quest.py --url ws://127.0.0.1:8787 --hz 15
```

## Benchmark

Runs the real pipeline against synthetic depth with transport excluded, so the
numbers isolate fusion and meshing:

```bash
python tools/bench.py --duration 26 --hz 15
```

It also asserts the carving property — after the virtual chair moves, its
original location must clear. A pipeline that only accumulates will pass every
throughput target and still fail this.

## Tests

```bash
python -m pytest host/tests -v
```

## Where the time goes

Measured in this container at 160x160 depth, 2 cm voxels, 32³ chunks:

| Stage | p50 | Notes |
| --- | --- | --- |
| Surface integration | ~30 ms | ray-band, scales with observed pixels |
| Projective carve | ~48 ms | every 2nd frame, scales with visible surface area |
| Mesh tick | ~9 ms | time-boxed at 8 ms, holds under load |
| Achieved rate | 13.4 Hz | against a 15 Hz target |

The mesh tick is the number that matters most for responsiveness, and it is
bounded by construction rather than by how much the scene changed.

Fusion is CPU numpy and is the current ceiling. The path to 30 Hz is a GPU
backend (Open3D `VoxelBlockGrid`) or a native core — deliberately deferred,
because the layout above is what a GPU port would need anyway, and it was worth
finding the algorithmic bugs at a speed where they were debuggable.

## Layout

| File | Role |
| --- | --- |
| `wire.py` | Binary protocol codec — reference for all other languages |
| `volume.py` | Chunked TSDF: ray-band integration, projective carving |
| `mesher.py` | Time-boxed, priority-ordered marching cubes |
| `pipeline.py` | Bounded queues, fusion thread, per-client publish state |
| `metrics.py` | Latency and rate instrumentation |
| `server.py` | WebSocket hub: one producer, N viewers |
