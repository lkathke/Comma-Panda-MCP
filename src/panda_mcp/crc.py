"""CRC utilities for automotive CAN payloads.

Covers the algorithms that actually show up on vehicle buses (VW/Audi, Toyota,
Honda, generic AUTOSAR E2E). ``brute_force`` tries every catalogued variant
against a frame so you can identify which one a manufacturer used, including the
common trick of feeding a per-message "magic" init byte.
"""
from __future__ import annotations

from dataclasses import dataclass


def _crc(data: bytes, width: int, poly: int, init: int,
         refin: bool, refout: bool, xorout: int) -> int:
    topbit = 1 << (width - 1)
    mask = (1 << width) - 1

    def reflect(v, w):
        r = 0
        for i in range(w):
            if v & (1 << i):
                r |= 1 << (w - 1 - i)
        return r

    crc = init
    for byte in data:
        b = reflect(byte, 8) if refin else byte
        crc ^= (b << (width - 8)) & mask
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & topbit else (crc << 1) & mask
    if refout:
        crc = reflect(crc, width)
    return crc ^ xorout


@dataclass(frozen=True)
class CrcSpec:
    name: str
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int

    def compute(self, data: bytes) -> int:
        return _crc(data, self.width, self.poly, self.init,
                    self.refin, self.refout, self.xorout)


# Catalogue of variants seen on real vehicle buses.
CATALOG: dict[str, CrcSpec] = {
    "crc8":          CrcSpec("crc8", 8, 0x07, 0x00, False, False, 0x00),
    "crc8_sae_j1850":CrcSpec("crc8_sae_j1850", 8, 0x1D, 0xFF, False, False, 0xFF),
    "crc8_j1850_zero":CrcSpec("crc8_j1850_zero", 8, 0x1D, 0x00, False, False, 0x00),
    "crc8_autosar":  CrcSpec("crc8_autosar", 8, 0x2F, 0xFF, False, False, 0xFF),
    "crc8_maxim":    CrcSpec("crc8_maxim", 8, 0x31, 0x00, True, True, 0x00),
    "crc8_8h2f":     CrcSpec("crc8_8h2f", 8, 0x2F, 0xFF, False, False, 0xFF),
    "crc16_ccitt":   CrcSpec("crc16_ccitt", 16, 0x1021, 0xFFFF, False, False, 0x0000),
    "crc16_xmodem":  CrcSpec("crc16_xmodem", 16, 0x1021, 0x0000, False, False, 0x0000),
    "crc16_arc":     CrcSpec("crc16_arc", 16, 0x8005, 0x0000, True, True, 0x0000),
    "crc16_autosar": CrcSpec("crc16_autosar", 16, 0x1021, 0xFFFF, False, False, 0x0000),
}


def compute(data: bytes, algorithm: str) -> int:
    if algorithm not in CATALOG:
        raise KeyError(f"unknown algorithm '{algorithm}'. known: {', '.join(CATALOG)}")
    return CATALOG[algorithm].compute(data)


def brute_force(frame: bytes, crc_index: int | None = None) -> list[dict]:
    """Find catalogued CRCs that reproduce a byte of `frame`.

    If ``crc_index`` is given, that byte is the candidate CRC and the rest
    (everything before it) is the data. Otherwise the last byte is assumed to be
    the CRC. Also tries an optional 1-byte init sweep (0x00..0xFF) to catch the
    per-message magic-constant pattern VW/Audi use.
    """
    if len(frame) < 2:
        return []
    idx = crc_index if crc_index is not None else len(frame) - 1
    target = frame[idx]
    data = frame[:idx]
    matches = []
    for name, spec in CATALOG.items():
        if spec.width != 8:
            continue
        if spec.compute(data) == target:
            matches.append({"algorithm": name, "init": spec.init, "magic": False})
        # init sweep for 8-bit specs (magic constant per message id)
        for init in range(256):
            swept = CrcSpec(name, spec.width, spec.poly, init,
                            spec.refin, spec.refout, spec.xorout)
            if init != spec.init and swept.compute(data) == target:
                matches.append({"algorithm": name, "init": init, "magic": True})
                break
    return matches


def detect_crc_field(per_id_frames) -> dict:
    """Heuristically locate the CRC byte for one id's frames.

    A CRC byte changes whenever the payload changes but is not itself a smooth
    counter. We score each byte position by how often it flips together with the
    rest of the payload; the CRC typically has high entropy and flips on nearly
    every distinct payload. Returns the best-guess index plus brute-force hits.
    """
    frames = [bytes(f.data) for f in per_id_frames]
    if not frames:
        return {"crc_index": None, "reason": "no frames"}
    n = len(frames[0])
    if any(len(f) != n for f in frames):
        # variable length: fall back to last byte
        n = min(len(f) for f in frames)
    distinct = {f[:n] for f in frames}
    # entropy proxy: number of distinct values per byte position
    variety = [len({f[i] for f in frames}) for i in range(n)]
    # CRC tends to have the most distinct values and sit at the end
    best = max(range(n), key=lambda i: (variety[i], i))

    # Keep only algorithms (incl. a per-id magic init) that reproduce the CRC
    # byte on EVERY frame — a single frame is ambiguous, the whole set is not.
    confirmed = []
    for name, spec in CATALOG.items():
        if spec.width != 8:
            continue
        if all(spec.compute(f[:best]) == f[best] for f in frames):
            confirmed.append({"algorithm": name, "init": spec.init, "magic": False})
            continue
        for init in range(256):
            swept = CrcSpec(name, spec.width, spec.poly, init,
                            spec.refin, spec.refout, spec.xorout)
            if all(swept.compute(f[:best]) == f[best] for f in frames):
                confirmed.append({"algorithm": name, "init": init, "magic": True})
                break

    return {
        "crc_index": best,
        "distinct_values_per_byte": variety,
        "distinct_payloads": len(distinct),
        "frames_checked": len(frames),
        "confirmed": confirmed,
        "brute_force_first_frame": brute_force(frames[0], crc_index=best),
    }
