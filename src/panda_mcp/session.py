"""In-memory store for captures and snapshots.

A capture runs a background thread that polls ``device.recv()`` and appends
``CanFrame``s until stopped or until an optional duration/frame cap is hit.
Everything is kept in RAM; use the export tools to persist.
"""
from __future__ import annotations

import threading
import time
import uuid

from .device import PandaDevice
from .model import CanFrame


class Capture:
    def __init__(self, session_id: str, device: PandaDevice,
                 bus: int | None, arb_ids: set[int] | None,
                 duration_s: float | None, max_frames: int | None):
        self.id = session_id
        self.device = device
        self.bus = bus
        self.arb_ids = arb_ids
        self.duration_s = duration_s
        self.max_frames = max_frames
        self.frames: list[CanFrame] = []
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                batch = self.device.recv()
            except Exception:
                break
            now = time.monotonic() - self._t0
            if batch:
                with self._lock:
                    for arb_id, data, bus in batch:
                        if self.bus is not None and bus != self.bus:
                            continue
                        if self.arb_ids is not None and arb_id not in self.arb_ids:
                            continue
                        self.frames.append(CanFrame(now, arb_id, bytes(data), bus))
                    if self.max_frames and len(self.frames) >= self.max_frames:
                        break
            if self.duration_s and now >= self.duration_s:
                break
            time.sleep(0.002)
        self.stopped_at = time.time()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self.stopped_at is None:
            self.stopped_at = time.time()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def snapshot_frames(self) -> list[CanFrame]:
        with self._lock:
            return list(self.frames)

    def meta(self) -> dict:
        frames = self.snapshot_frames()
        ids = sorted({f.arb_id for f in frames})
        return {
            "session_id": self.id,
            "running": self.running,
            "frame_count": len(frames),
            "unique_ids": len(ids),
            "ids": [f"0x{i:X}" for i in ids],
            "bus_filter": self.bus,
            "arb_id_filter": sorted(self.arb_ids) if self.arb_ids else None,
            "duration_s": round((self.stopped_at or time.time()) - self.started_at, 3),
        }


class Store:
    def __init__(self):
        self.captures: dict[str, Capture] = {}
        # snapshot_id -> {(bus, arb_id): data_bytes}
        self.snapshots: dict[str, dict[tuple[int, int], bytes]] = {}

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def add_capture(self, cap: Capture) -> None:
        self.captures[cap.id] = cap

    def get_capture(self, session_id: str) -> Capture:
        if session_id not in self.captures:
            raise KeyError(f"unknown session_id: {session_id}")
        return self.captures[session_id]

    def delete_capture(self, session_id: str) -> None:
        cap = self.captures.pop(session_id, None)
        if cap and cap.running:
            cap.stop()


store = Store()
