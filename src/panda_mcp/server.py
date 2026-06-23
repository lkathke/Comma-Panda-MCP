"""MCP server exposing CAN reverse-engineering tools for the comma.ai Panda.

Run with the bundled stdio transport:  ``python -m panda_mcp.server``
or via the console script ``panda-mcp``.

Tool groups:
  device_*   connect / status / safety mode / speed / flash / recover
  capture_*  start / stop / get / list / delete recordings
  analyze_*  per-id stats, diff (transition + new-bits), find value, find toggle
  snapshot_* take / diff instantaneous bus state
  send_*     single / bulk / fuzz / replay  (requires ALLOUTPUT safety mode)
  crc_*      compute / brute-force / detect field
"""
from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

from . import analysis, crc as crcmod
from .device import SAFETY_MODES, device
from .model import CanFrame
from .session import Capture, store

mcp = FastMCP("panda-mcp")


# --------------------------------------------------------------------------- #
# Device management
# --------------------------------------------------------------------------- #
@mcp.tool()
def device_connect(serial: str | None = None, mock: bool = False) -> dict:
    """Connect to a Panda. Set mock=true to use the built-in synthetic CAN
    stream (no hardware needed). serial selects a specific device when several
    are attached."""
    return device.connect(serial=serial, mock=mock)


@mcp.tool()
def device_status() -> dict:
    """Report connection state, firmware version, safety mode and bus errors."""
    return device.status()


@mcp.tool()
def device_set_safety_mode(mode: str) -> dict:
    """Set the safety mode. 'silent' = listen only (default, safe).
    'alloutput' = UNLOCKS transmitting arbitrary frames on any bus. Required
    before send_*/fuzz/replay. Only use alloutput on a bench or a car you own."""
    key = mode.lower()
    if key not in SAFETY_MODES:
        return {"error": f"unknown mode '{mode}'. options: {', '.join(SAFETY_MODES)}"}
    device.set_safety_mode(SAFETY_MODES[key])
    return device.status()


@mcp.tool()
def device_set_can_speed(bus: int, kbps: int) -> dict:
    """Set the bitrate (kbps) of a CAN bus. Typical: 500 (powertrain) or
    100/125 (body). Must match the vehicle bus or you'll see only errors."""
    device.set_can_speed(bus, kbps)
    return {"bus": bus, "kbps": kbps, "ok": True}


@mcp.tool()
def device_flash(firmware_path: str | None = None) -> dict:
    """Flash Panda firmware. With no path, builds & flashes the stock firmware
    (the device's own ``flash()``); pass a .bin for custom firmware. Sending
    custom frames does NOT need this — use device_set_safety_mode('alloutput')
    instead. Flashing is only for firmware updates, custom safety logic, or
    recovery. The device reboots afterwards; reconnect with device_connect."""
    if device.is_mock:
        return {"ok": True, "note": "mock device: flash is a no-op"}
    device.flash(firmware_path)
    return {"ok": True, "note": "flashed; device rebooting — reconnect"}


@mcp.tool()
def device_recover() -> dict:
    """Recover a bricked/unresponsive Panda via DFU (reflash the bootstub +
    firmware). Use when the device won't enumerate normally."""
    if device.is_mock:
        return {"ok": True, "note": "mock device: recover is a no-op"}
    ok = device.recover()
    return {"ok": ok}


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
def _parse_ids(arb_ids: list[str] | None) -> set[int] | None:
    if not arb_ids:
        return None
    return {int(x, 0) for x in arb_ids}


@mcp.tool()
def capture_start(bus: int | None = None, arb_ids: list[str] | None = None,
                  duration_s: float | None = None,
                  max_frames: int | None = None) -> dict:
    """Begin recording CAN frames in the background. Optionally restrict to one
    bus and/or a list of arbitration ids (hex like '0x1a0' or decimal). Stops
    automatically after duration_s or max_frames if given. Returns a session_id
    used by every analysis tool."""
    if not device.connected:
        return {"error": "no device connected (call device_connect)"}
    sid = store.new_id("cap")
    cap = Capture(sid, device, bus, _parse_ids(arb_ids), duration_s, max_frames)
    store.add_capture(cap)
    cap.start()
    return {"session_id": sid, "running": True,
            "bus_filter": bus, "arb_id_filter": arb_ids}


@mcp.tool()
def capture_stop(session_id: str) -> dict:
    """Stop a running capture and return its summary (frame count, unique ids)."""
    try:
        cap = store.get_capture(session_id)
    except KeyError as e:
        return {"error": str(e)}
    cap.stop()
    return cap.meta()


@mcp.tool()
def capture_list() -> dict:
    """List all captures with their summaries."""
    return {"captures": [c.meta() for c in store.captures.values()]}


@mcp.tool()
def capture_get(session_id: str, limit: int = 100, offset: int = 0,
                bus: int | None = None, arb_id: str | None = None) -> dict:
    """Fetch raw frames from a capture, newest-first, with paging and optional
    bus/id filtering. Use limit/offset to page through large captures."""
    try:
        cap = store.get_capture(session_id)
    except KeyError as e:
        return {"error": str(e)}
    frames = cap.snapshot_frames()
    if bus is not None:
        frames = [f for f in frames if f.bus == bus]
    if arb_id is not None:
        target = int(arb_id, 0)
        frames = [f for f in frames if f.arb_id == target]
    total = len(frames)
    page = frames[offset:offset + limit]
    return {"session_id": session_id, "total": total, "returned": len(page),
            "offset": offset, "frames": [f.to_dict() for f in page]}


@mcp.tool()
def capture_delete(session_id: str) -> dict:
    """Stop (if running) and discard a capture and its frames."""
    store.delete_capture(session_id)
    return {"deleted": session_id}


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _frames(session_id: str) -> list[CanFrame]:
    return store.get_capture(session_id).snapshot_frames()


@mcp.tool()
def analyze_stats(session_id: str) -> dict:
    """Per-id overview of a capture: message count, period (Hz), payload length,
    and which byte positions ever change. Good first pass to see what's on the
    bus and which ids carry dynamic data."""
    try:
        return {"session_id": session_id, "ids": analysis.per_id_stats(_frames(session_id))}
    except KeyError as e:
        return {"error": str(e)}


@mcp.tool()
def analyze_diff_transition(session_id: str, split_ts: float) -> dict:
    """Find bits that cleanly flip 0->1 or 1->0 between the part of the capture
    before split_ts and the part after (capture-relative seconds). Implements
    comma's can_bit_transition heuristic — use when you toggled something
    (lights, door, gear) partway through one recording."""
    try:
        return {"session_id": session_id,
                "transitions": analysis.find_toggle(_frames(session_id), split_ts)}
    except KeyError as e:
        return {"error": str(e)}


@mcp.tool()
def analyze_diff_new_bits(interesting_session_id: str,
                          background_session_ids: list[str]) -> dict:
    """Find bits present in the 'interesting' capture that NEVER appeared in any
    of the background captures. Implements comma's can_unique heuristic — record
    the bus idle as background, then record again while performing an action."""
    try:
        interesting = analysis.build_bit_states(_frames(interesting_session_id))
        merged: dict[int, analysis.BitState] = {}
        for sid in background_session_ids:
            for arb_id, st in analysis.build_bit_states(_frames(sid)).items():
                m = merged.get(arb_id)
                if m is None:
                    m = merged[arb_id] = analysis.BitState()
                for i in range(len(m.seen_one)):
                    m.seen_one[i] |= st.seen_one[i]
                    m.seen_zero[i] |= st.seen_zero[i]
        return {"new_bits": analysis.diff_new_bits(interesting, merged)}
    except KeyError as e:
        return {"error": str(e)}


@mcp.tool()
def analyze_find_value(session_id: str, value: int, bit_length: int = 16,
                       little_endian: bool | None = None) -> dict:
    """Locate where an integer value sits in the payloads — pin a known physical
    quantity (speed, rpm, steering angle) to an id + byte offset. Set
    little_endian to true/false to fix endianness, or leave null to try both."""
    try:
        hits = analysis.find_value(_frames(session_id), value, bit_length,
                                   little_endian if little_endian is not None else None)
        return {"value": value, "bit_length": bit_length, "matches": hits}
    except KeyError as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
@mcp.tool()
def snapshot_take(session_id: str) -> dict:
    """Capture the latest payload per (bus,id) from a running/finished capture as
    an instantaneous bus state. Pair two snapshots with snapshot_diff to see what
    changed between two moments (e.g. before/after pressing a button)."""
    try:
        frames = _frames(session_id)
    except KeyError as e:
        return {"error": str(e)}
    state: dict[tuple[int, int], bytes] = {}
    for f in sorted(frames, key=lambda x: x.ts):
        state[(f.bus, f.arb_id)] = f.data
    sid = store.new_id("snap")
    store.snapshots[sid] = state
    return {"snapshot_id": sid, "entries": len(state)}


@mcp.tool()
def snapshot_diff(snapshot_id_a: str, snapshot_id_b: str) -> dict:
    """Diff two snapshots: report ids whose payload changed, with a per-byte XOR
    so you can see exactly which bytes/bits differ."""
    a = store.snapshots.get(snapshot_id_a)
    b = store.snapshots.get(snapshot_id_b)
    if a is None or b is None:
        return {"error": "unknown snapshot id"}
    changes = []
    for key in sorted(set(a) | set(b)):
        da, db = a.get(key), b.get(key)
        bus, arb_id = key
        if da == db:
            continue
        entry = {"arb_id": f"0x{arb_id:X}", "bus": bus,
                 "a": da.hex() if da else None, "b": db.hex() if db else None}
        if da and db:
            n = min(len(da), len(db))
            entry["xor"] = bytes(da[i] ^ db[i] for i in range(n)).hex()
        changes.append(entry)
    return {"changed": changes}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _require_output() -> str | None:
    st = device.status()
    if not st.get("connected"):
        return "no device connected (call device_connect)"
    if st.get("safety_mode") != SAFETY_MODES["alloutput"]:
        return "safety mode is not 'alloutput' — call device_set_safety_mode('alloutput')"
    return None


@mcp.tool()
def send_frame(arb_id: str, data: str, bus: int = 0,
               count: int = 1, interval_ms: int = 0) -> dict:
    """Transmit a CAN frame. arb_id hex/decimal, data as hex string (e.g.
    '0102030405060708'). Optionally repeat `count` times every interval_ms.
    Requires alloutput safety mode."""
    err = _require_output()
    if err:
        return {"error": err}
    aid = int(arb_id, 0)
    payload = bytes.fromhex(data)
    sent = 0
    for _ in range(max(1, count)):
        device.send(aid, payload, bus)
        sent += 1
        if interval_ms and sent < count:
            time.sleep(interval_ms / 1000.0)
    return {"sent": sent, "arb_id": f"0x{aid:X}", "bus": bus, "data": payload.hex()}


@mcp.tool()
def send_bulk(frames: list[dict]) -> dict:
    """Transmit many frames at once. Each item: {arb_id, data, bus}. Requires
    alloutput safety mode."""
    err = _require_output()
    if err:
        return {"error": err}
    msgs = [(int(f["arb_id"], 0), bytes.fromhex(f["data"]), int(f.get("bus", 0)))
            for f in frames]
    device.send_many(msgs)
    return {"sent": len(msgs)}


@mcp.tool()
def send_fuzz(arb_id: str, bus: int, base_data: str, byte_index: int,
              start: int = 0, end: int = 255, step: int = 1,
              interval_ms: int = 20) -> dict:
    """Sweep one byte of a frame across a range while holding the rest fixed —
    the classic way to find which value drives an actuator. base_data is the hex
    template; byte_index is the byte to vary from start..end (inclusive) in step
    increments. Requires alloutput safety mode."""
    err = _require_output()
    if err:
        return {"error": err}
    aid = int(arb_id, 0)
    template = bytearray(bytes.fromhex(base_data))
    if byte_index >= len(template):
        return {"error": f"byte_index {byte_index} out of range for {len(template)}-byte frame"}
    sent = []
    v = start
    while v <= end:
        template[byte_index] = v & 0xFF
        device.send(aid, bytes(template), bus)
        sent.append(bytes(template).hex())
        if interval_ms:
            time.sleep(interval_ms / 1000.0)
        v += step
    return {"arb_id": f"0x{aid:X}", "bus": bus, "byte_index": byte_index,
            "frames_sent": len(sent), "first": sent[0], "last": sent[-1]}


@mcp.tool()
def send_replay(session_id: str, speed_factor: float = 1.0,
                bus: int | None = None) -> dict:
    """Replay a recorded capture, preserving inter-frame timing (scaled by
    speed_factor; 2.0 = twice as fast). Optionally force all frames onto one bus.
    Requires alloutput safety mode."""
    err = _require_output()
    if err:
        return {"error": err}
    try:
        frames = sorted(_frames(session_id), key=lambda f: f.ts)
    except KeyError as e:
        return {"error": str(e)}
    if not frames:
        return {"error": "capture is empty"}
    prev = frames[0].ts
    sent = 0
    for f in frames:
        gap = (f.ts - prev) / max(speed_factor, 0.0001)
        if gap > 0:
            time.sleep(gap)
        device.send(f.arb_id, f.data, bus if bus is not None else f.bus)
        prev = f.ts
        sent += 1
    return {"replayed": sent, "session_id": session_id, "speed_factor": speed_factor}


# --------------------------------------------------------------------------- #
# CRC
# --------------------------------------------------------------------------- #
@mcp.tool()
def crc_compute(data: str, algorithm: str = "crc8_sae_j1850") -> dict:
    """Compute a CRC over a hex string. algorithm is one of the catalogued
    automotive variants (crc8, crc8_sae_j1850, crc8_autosar, crc16_ccitt, ...).
    Use crc_list to see all options."""
    try:
        value = crcmod.compute(bytes.fromhex(data), algorithm)
        return {"algorithm": algorithm, "crc": f"0x{value:X}", "crc_dec": value}
    except KeyError as e:
        return {"error": str(e)}


@mcp.tool()
def crc_list() -> dict:
    """List the catalogued CRC algorithms with their parameters."""
    return {"algorithms": [
        {"name": s.name, "width": s.width, "poly": f"0x{s.poly:X}",
         "init": f"0x{s.init:X}", "refin": s.refin, "refout": s.refout,
         "xorout": f"0x{s.xorout:X}"}
        for s in crcmod.CATALOG.values()]}


@mcp.tool()
def crc_brute_force(frame: str, crc_index: int | None = None) -> dict:
    """Identify which catalogued CRC reproduces a frame's check byte. frame is
    the full hex payload; crc_index is the candidate CRC byte (defaults to the
    last byte). Also sweeps a per-message init/magic constant. Run this on
    several frames of the same id and keep the algorithm that matches all."""
    matches = crcmod.brute_force(bytes.fromhex(frame), crc_index)
    return {"crc_index": crc_index if crc_index is not None else len(bytes.fromhex(frame)) - 1,
            "matches": matches,
            "hint": "confirm against multiple frames of the same id"}


@mcp.tool()
def crc_detect_field(session_id: str, arb_id: str) -> dict:
    """Guess which byte of an id's payload is the CRC (by per-byte value variety)
    and brute-force the algorithm on it. Record some varied traffic for the id
    first so the CRC byte actually changes."""
    try:
        frames = [f for f in _frames(session_id) if f.arb_id == int(arb_id, 0)]
    except KeyError as e:
        return {"error": str(e)}
    if not frames:
        return {"error": f"no frames for {arb_id} in capture"}
    return {"arb_id": arb_id, **crcmod.detect_crc_field(frames)}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@mcp.tool()
def export_candump(session_id: str, path: str) -> dict:
    """Write a capture to a candump-style log file (``(ts) busN ID#DATA``) for
    use with can-utils, Cabana or SavvyCAN."""
    try:
        frames = sorted(_frames(session_id), key=lambda f: f.ts)
    except KeyError as e:
        return {"error": str(e)}
    base = time.time()
    lines = [f"({base + f.ts:.6f}) can{f.bus} {f.arb_id:X}#{f.data.hex().upper()}"
             for f in frames]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"path": path, "frames": len(lines)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
