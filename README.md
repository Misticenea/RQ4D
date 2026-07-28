# RQ4D — Realtime Quest 4D Capture

Turn a Meta Quest 3 / 3S into a **room scanner** that streams a live, evolving 3D
model of the physical space to a **web viewer** or a **desktop app**.

Three things define this project:

1. **The reconstruction is realtime.** Not a scan-then-export workflow — geometry
   updates continuously as the wearer moves and as the room changes.
2. **Nothing is rendered on the lenses.** The headset is a sensor, not a display.
   The wearer sees plain passthrough plus a small status panel. The 3D model
   exists only on the receiving client.
3. **The app calibrates on launch.** Before streaming, it captures a baseline
   model of the room — bounds, floor, walls, furniture, semantic labels — and
   anchors the whole session to a stable, room-fixed origin.

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/PLAN.md](docs/PLAN.md) | Milestones M0–M6, acceptance criteria, effort estimates |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data flow, coordinate frames, latency budget |
| [docs/CALIBRATION.md](docs/CALIBRATION.md) | Launch calibration flow, room profile spec, recalibration triggers |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Wire format, message types, compression, bandwidth math |
| [docs/DECISIONS.md](docs/DECISIONS.md) | ADRs with rationale, plus open questions needing a call |
| [docs/RISKS.md](docs/RISKS.md) | Platform limits, policy constraints, mitigations |

## Planned layout

```
quest-app/        Unity 6 capture client (the headset app)
host/             Reconstruction service — depth frames in, mesh deltas out
viewer-web/       three.js viewer (browser)
viewer-desktop/   Tauri shell wrapping viewer-web (PC app)
protocol/         Schema + generated bindings, single source of truth
tools/            Session recorder/replayer, synthetic datasets, benchmarks
docs/             This plan
```

## Hardware requirement

Quest 3 or Quest 3S. Quest 2, Pro, and 1 have no Depth API and no passthrough
camera access — they cannot run this.

## Status

Planning. No code yet.

## License

GPL-3.0 — see [LICENSE](LICENSE).
