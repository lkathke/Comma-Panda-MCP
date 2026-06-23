"""Core data model shared across all tools."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CanFrame:
    """A single CAN frame as seen on a bus.

    ``ts`` is seconds relative to the start of the capture it belongs to, so
    timestamps from different sessions are not directly comparable.
    """
    ts: float
    arb_id: int
    data: bytes
    bus: int

    def to_dict(self) -> dict:
        return {
            "ts": round(self.ts, 6),
            "arb_id": self.arb_id,
            "arb_id_hex": f"0x{self.arb_id:X}",
            "bus": self.bus,
            "data": self.data.hex(),
            "len": len(self.data),
        }
