"""WebSocket server: one capture producer, N viewers.

Transport is deliberately the dumbest thing that works on a LAN. WebRTC comes
in M4, when browser clients need to work off-LAN; until then a single
WebSocket keeps the whole path inspectable, and `adb reverse` gives the
headset a USB-speed link that takes Wi-Fi variance out of every measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .pipeline import ClientState, PipelineConfig, Session
from .volume import VolumeConfig
from .wire import (
    PROTOCOL_VERSION,
    DepthFrame,
    MsgType,
    PoseFrame,
    decode,
    encode,
    encode_json,
)

log = logging.getLogger("rq4d")


class Hub:
    def __init__(self, session: Session, publish_hz: float = 20.0):
        self.session = session
        self.viewers: dict[ServerConnection, ClientState] = {}
        self.producer: ServerConnection | None = None
        self.publish_interval = 1.0 / publish_hz

    # -- connection handling ------------------------------------------------

    async def handle(self, ws: ServerConnection) -> None:
        peer = f"{ws.remote_address[0]}:{ws.remote_address[1]}"
        role = "viewer"
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    raw = raw.encode()
                frame = decode(raw)

                if frame.type == MsgType.HELLO:
                    role = await self._hello(ws, frame, peer)
                elif frame.type == MsgType.ROOM_PROFILE:
                    self.session.set_room_profile(frame.json())
                    log.info("room profile received (%s)", peer)
                    await self._broadcast_profile()
                elif frame.type == MsgType.DEPTH_FRAME:
                    self.session.submit_depth(DepthFrame.unpack(frame.payload), frame.timestamp_ns)
                elif frame.type == MsgType.POSE_FRAME:
                    self.session.submit_pose(PoseFrame.unpack(frame.payload))
                elif frame.type == MsgType.CONTROL:
                    await self._control(frame.json(), peer)
                elif frame.type == MsgType.HEARTBEAT:
                    pass
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("connection error (%s)", peer)
        finally:
            self.viewers.pop(ws, None)
            if self.producer is ws:
                self.producer = None
            log.info("disconnected %s (%s)", role, peer)

    async def _hello(self, ws: ServerConnection, frame, peer: str) -> str:
        info = frame.json()
        role = info.get("role", "producer" if "depth" in info else "viewer")

        if role == "producer":
            self.producer = ws
            # Relate the device clock to ours once, so frame ages are real
            # rather than an artefact of two unrelated monotonic origins.
            if "clock_ns" in info:
                self.session.set_clock_offset(time.monotonic_ns() - int(info["clock_ns"]))
            log.info("producer connected: %s (%s)", info.get("device", "?"), peer)
        else:
            self.viewers[ws] = ClientState(ident=peer)
            log.info("viewer connected (%s)", peer)

        await ws.send(
            encode_json(
                MsgType.HELLO_ACK,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "role": role,
                    "clock_ns": time.monotonic_ns(),
                    "accepted": True,
                },
                time.monotonic_ns(),
            )
        )
        if role == "viewer" and self.session.room_profile:
            await ws.send(
                encode_json(
                    MsgType.ROOM_PROFILE, self.session.room_profile, time.monotonic_ns()
                )
            )
        return role

    async def _control(self, cmd: dict, peer: str) -> None:
        log.info("control from %s: %s", peer, cmd)
        if cmd.get("command") == "Recalibrate" and self.producer is not None:
            await self.producer.send(
                encode_json(MsgType.CONTROL, cmd, time.monotonic_ns())
            )

    async def _broadcast_profile(self) -> None:
        if not self.session.room_profile:
            return
        msg = encode_json(MsgType.ROOM_PROFILE, self.session.room_profile, time.monotonic_ns())
        await self._send_all(msg)
        for state in self.viewers.values():
            state.sent.clear()
            state.needs_keyframe = True

    async def _send_all(self, payload: bytes) -> None:
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send(payload)
            except websockets.ConnectionClosed:
                dead.append(ws)
        for ws in dead:
            self.viewers.pop(ws, None)

    # -- publishing ---------------------------------------------------------

    async def publish_loop(self) -> None:
        """Fan mesh deltas out to viewers.

        Chunk versions are tracked per viewer, so a late joiner gets the whole
        volume while established viewers keep receiving only what changed.
        """
        while True:
            await asyncio.sleep(self.publish_interval)
            chunks = self.session.drain_chunks()

            for ws, state in list(self.viewers.items()):
                try:
                    if state.needs_keyframe:
                        for chunk in self.session.keyframe_chunks(state):
                            await ws.send(
                                encode(
                                    MsgType.MESH_CHUNK_UPDATE,
                                    chunk.pack(),
                                    time.monotonic_ns(),
                                )
                            )
                            state.sent[chunk.key] = chunk.version
                            self.session.metrics.counters.chunks_sent += 1
                    for chunk in chunks:
                        if state.sent.get(chunk.key, -1) >= chunk.version:
                            continue
                        payload = chunk.pack()
                        await ws.send(
                            encode(MsgType.MESH_CHUNK_UPDATE, payload, time.monotonic_ns())
                        )
                        state.sent[chunk.key] = chunk.version
                        self.session.metrics.counters.chunks_sent += 1
                        self.session.metrics.counters.bytes_sent += len(payload)
                except websockets.ConnectionClosed:
                    self.viewers.pop(ws, None)

    async def stats_loop(self, interval: float = 2.0) -> None:
        while True:
            await asyncio.sleep(interval)
            status = self.session.status()
            with contextlib.suppress(Exception):
                await self._send_all(
                    encode_json(MsgType.STATS, status, time.monotonic_ns())
                )
            if self.session.metrics.counters.frames_integrated:
                log.info(
                    "chunks=%d backlog=%d viewers=%d\n%s",
                    status["chunks"],
                    status["backlog"],
                    len(self.viewers),
                    self.session.metrics.render(),
                )


async def run(args) -> None:
    vol = VolumeConfig(voxel_size=args.voxel, chunk_voxels=args.chunk)
    cfg = PipelineConfig(volume=vol)
    cfg.mesh.budget_ms = args.budget

    session = Session(cfg)
    session.start()
    hub = Hub(session, publish_hz=args.publish_hz)

    async with serve(hub.handle, args.host, args.port, max_size=None):
        log.info("listening on ws://%s:%d", args.host, args.port)
        await asyncio.gather(hub.publish_loop(), hub.stats_loop())


def main() -> None:
    p = argparse.ArgumentParser(description="RQ4D reconstruction host")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--voxel", type=float, default=0.02)
    p.add_argument("--chunk", type=int, default=32)
    p.add_argument("--budget", type=float, default=8.0)
    p.add_argument("--publish-hz", type=float, default=20.0)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
