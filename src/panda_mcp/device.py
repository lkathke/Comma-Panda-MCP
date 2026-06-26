"""Panda hardware abstraction with a built-in mock for hardware-free development.

The real device is driven through the ``pandacan`` package (``from panda import
Panda``). When that package or the hardware is unavailable, ``MockPanda`` produces
a deterministic synthetic CAN stream that exercises every analysis tool:

  * 0x1A0  20 Hz  rolling counter in nibble + CRC8 (J1850) over bytes 0..6
  * 0x2B0  10 Hz  static payload (baseline noise)
  * 0x3C0  50 Hz  a "steering angle" int16 that sweeps, for find_value
  * 0x0E1   1 Hz  a single toggle bit driven by ``mock_set_signal("blinker")``

This lets the diff, find_value, find_toggle and CRC tools be tested end-to-end
without a car or a Panda plugged in.
"""
from __future__ import annotations

import threading
import time

# Safety mode constants mirrored from panda so callers don't need the package.
SAFETY_SILENT = 0            # listen only, cannot transmit
SAFETY_ALLOUTPUT = 17        # unlocked: can transmit arbitrary frames on any bus
SAFETY_VOLKSWAGEN_MQB = 15   # old MQB (Golf 7, Passat B8) — torque-based
SAFETY_VOLKSWAGEN_MEB = 34   # MEB + MQB Evo (Golf 8, Cupra Leon, ID.3/ID.4)
                             # curvature-based, HCA_03 with check_relay

# Safety mode param flags (passed as second arg to set_safety_mode)
FLAG_VW_LONG_CONTROL = 1     # enable longitudinal control (ACC_18 TX)
FLAG_VW_BENCH_TEST   = 2     # skip controls_allowed for HCA_03 — stationary testing only
                             # REQUIRES ALLOW_DEBUG firmware (firmware/panda_h7.bin.signed)
                             # Use: set_safety_mode(SAFETY_VOLKSWAGEN_MEB, param=FLAG_VW_BENCH_TEST)

# Modes that are allowed to transmit (for mock enforcement)
_TX_MODES = {SAFETY_ALLOUTPUT, SAFETY_VOLKSWAGEN_MQB, SAFETY_VOLKSWAGEN_MEB}

SAFETY_MODES = {
    "silent":          SAFETY_SILENT,
    "alloutput":       SAFETY_ALLOUTPUT,
    "volkswagen_mqb":  SAFETY_VOLKSWAGEN_MQB,
    "volkswagen_meb":  SAFETY_VOLKSWAGEN_MEB,
}


class DeviceError(RuntimeError):
    pass


class MockPanda:
    """Synthetic Panda. Same surface as the bits of ``panda.Panda`` we use."""

    def __init__(self):
        self._t0 = time.monotonic()
        self._safety = SAFETY_SILENT
        self._signals = {"blinker": 0}
        self._sent: list[tuple[int, bytes, int]] = []
        self._speeds = {0: 500, 1: 500, 2: 500}
        # phase offset per id so frames don't all land on the same tick
        self._last_emit: dict[int, float] = {}

    # --- introspection helpers unique to the mock ---
    def mock_set_signal(self, name: str, value: int) -> None:
        self._signals[name] = value

    @property
    def sent_frames(self) -> list[tuple[int, bytes, int]]:
        return list(self._sent)

    # --- panda-compatible surface ---
    def get_version(self) -> str:
        return "mock-1.0"

    def get_type(self) -> bytes:
        return b"\x07"  # red panda hw type byte

    def health(self) -> dict:
        return {
            "safety_mode": self._safety,
            "can_send_errs": 0,
            "can_rx_errs": 0,
            "faults": 0,
            "voltage": 12000,
        }

    def set_safety_mode(self, mode: int, param: int = 0) -> None:
        self._safety = mode

    def set_can_speed_kbps(self, bus: int, speed: int) -> None:
        self._speeds[bus] = speed

    def can_send(self, arb_id: int, data: bytes, bus: int) -> None:
        if self._safety not in _TX_MODES:
            raise DeviceError(
                "cannot send: safety mode does not allow TX "
                "(use alloutput, volkswagen_meb, or volkswagen_mqb_evo)"
            )
        self._sent.append((arb_id, bytes(data), bus))

    def can_send_many(self, msgs) -> None:
        for arb_id, _, data, bus in msgs:
            self.can_send(arb_id, data, bus)

    def can_recv(self):
        """Return frames that are 'due' since the last poll, as (id, data, bus)."""
        now = time.monotonic()
        t = now - self._t0
        out = []
        out += self._due(0x1A0, 0.05, t, now, self._frame_1a0)
        out += self._due(0x2B0, 0.10, t, now, lambda _t: bytes([0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 0, 0]))
        out += self._due(0x3C0, 0.02, t, now, self._frame_3c0)
        out += self._due(0x0E1, 1.00, t, now, self._frame_0e1)
        return out

    def _due(self, arb_id, period, t, now, builder):
        last = self._last_emit.get(arb_id, 0.0)
        if now - last < period:
            return []
        self._last_emit[arb_id] = now
        return [(arb_id, builder(t), 0)]

    def _frame_1a0(self, t: float) -> bytes:
        counter = int(t * 20) & 0x0F
        payload = bytes([counter << 4, 0x12, 0x34, 0x56, 0x78, 0x00, 0x00])
        return payload + bytes([crc8_j1850(payload)])

    def _frame_3c0(self, t: float) -> bytes:
        import math
        angle = int(2000 * math.sin(t)) & 0xFFFF
        return bytes([angle & 0xFF, (angle >> 8) & 0xFF, 0, 0, 0, 0, 0, 0])

    def _frame_0e1(self, t: float) -> bytes:
        bit = 0x01 if self._signals.get("blinker") else 0x00
        return bytes([bit, 0, 0, 0, 0, 0, 0, 0])

    # flashing is a no-op on the mock
    def flash(self, *a, **k) -> None:
        pass

    def recover(self, *a, **k) -> bool:
        return True

    def close(self) -> None:
        pass


def crc8_j1850(data: bytes) -> int:
    """SAE J1850 CRC-8 (poly 0x1D, init 0xFF, xorout 0xFF). Common in automotive."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


class PandaDevice:
    """Process-wide singleton wrapping either a real or mock Panda."""

    def __init__(self):
        self._lock = threading.Lock()
        self._panda = None
        self._is_mock = False

    @property
    def connected(self) -> bool:
        return self._panda is not None

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    @property
    def raw(self):
        if self._panda is None:
            raise DeviceError("no device connected (call device_connect)")
        return self._panda

    def connect(self, serial: str | None = None, mock: bool = False) -> dict:
        with self._lock:
            if self._panda is not None:
                self.close()
            if mock:
                self._panda = MockPanda()
                self._is_mock = True
            else:
                try:
                    from panda import Panda  # type: ignore
                except ImportError as e:
                    raise DeviceError(
                        "pandacan not installed; pass mock=true or `pip install pandacan`"
                    ) from e
                self._panda = Panda(serial=serial)
                self._is_mock = False
            return self.status()

    def status(self) -> dict:
        if self._panda is None:
            return {"connected": False}
        h = self._panda.health()
        safety = h.get("safety_mode", h.get("safety_model"))
        return {
            "connected": True,
            "mock": self._is_mock,
            "version": self._panda.get_version(),
            "safety_mode": safety,
            "can_send_errs": h.get("can_send_errs"),
            "can_rx_errs": h.get("can_rx_errs"),
        }

    def set_safety_mode(self, mode: int, param: int = 0) -> None:
        self.raw.set_safety_mode(mode, param)

    def set_can_speed(self, bus: int, kbps: int) -> None:
        self.raw.set_can_speed_kbps(bus, kbps)

    def recv(self):
        return self.raw.can_recv()

    def send(self, arb_id: int, data: bytes, bus: int) -> None:
        self.raw.can_send(arb_id, data, bus)

    def send_many(self, msgs) -> None:
        self.raw.can_send_many([(a, None, d, b) for (a, d, b) in msgs])

    def flash(self, fw_path: str | None = None) -> None:
        if fw_path:
            self.raw.flash(fw_path)
        else:
            self.raw.flash()

    def recover(self) -> bool:
        return bool(self.raw.recover())

    def close(self) -> None:
        if self._panda is not None:
            try:
                self._panda.close()
            except Exception:
                pass
        self._panda = None
        self._is_mock = False


# module-level singleton used by all tools
device = PandaDevice()
