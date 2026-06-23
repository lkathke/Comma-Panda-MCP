"""Hardware-free smoke tests for the analysis + CRC + mock device pipeline."""
import time

from panda_mcp import analysis, crc
from panda_mcp.device import PandaDevice, SAFETY_MODES, crc8_j1850
from panda_mcp.session import Capture, Store


def _record(mock_seconds=1.2, signal=None):
    dev = PandaDevice()
    dev.connect(mock=True)
    if signal:
        dev.raw.mock_set_signal(*signal)
    store = Store()
    cap = Capture(store.new_id("cap"), dev, None, None, None, None)
    store.add_capture(cap)
    cap.start()
    time.sleep(mock_seconds)
    cap.stop()
    return dev, cap


def test_mock_capture_produces_frames():
    dev, cap = _record()
    frames = cap.snapshot_frames()
    assert frames, "mock should produce frames"
    ids = {f.arb_id for f in frames}
    assert {0x1A0, 0x2B0, 0x3C0}.issubset(ids)


def test_per_id_stats_detects_dynamic_bytes():
    dev, cap = _record()
    stats = {s["arb_id"]: s for s in analysis.per_id_stats(cap.snapshot_frames())}
    # 0x3C0 carries a sweeping int16 -> bytes 0/1 must be dynamic
    assert 0 in stats["0x3C0"]["dynamic_bytes"]


def test_find_value_locates_steering_angle():
    dev, cap = _record()
    # 0x3C0 holds angle=int(2000*sin(t)); 0 occurs near t=0/pi
    hits = analysis.find_value(cap.snapshot_frames(), 0, 16, little_endian=True)
    assert any(h["arb_id"] == "0x3C0" for h in hits)


def test_crc_brute_force_identifies_j1850():
    payload = bytes([0x10, 0x12, 0x34, 0x56, 0x78, 0x00, 0x00])
    frame = payload + bytes([crc8_j1850(payload)])
    matches = crc.brute_force(frame)
    names = {m["algorithm"] for m in matches}
    assert "crc8_sae_j1850" in names


def test_crc_detect_field_on_mock_1a0():
    dev, cap = _record()
    frames = [f for f in cap.snapshot_frames() if f.arb_id == 0x1A0]
    result = crc.detect_crc_field(frames)
    assert result["crc_index"] == 7  # last byte is the CRC in the mock


def test_diff_transition_finds_blinker_bit():
    # record idle, then flip the blinker mid-capture
    dev = PandaDevice()
    dev.connect(mock=True)
    store = Store()
    cap = Capture(store.new_id("cap"), dev, None, {0x0E1}, None, None)
    cap.start()
    time.sleep(1.5)
    split = 1.4
    dev.raw.mock_set_signal("blinker", 1)
    time.sleep(1.5)
    cap.stop()
    trans = analysis.find_toggle(cap.snapshot_frames(), split)
    assert any(t.get("arb_id") == "0xE1" for t in trans)


def test_send_requires_alloutput():
    dev = PandaDevice()
    dev.connect(mock=True)
    import pytest
    with pytest.raises(Exception):
        dev.send(0x1AA, b"\x01", 0)
    dev.set_safety_mode(SAFETY_MODES["alloutput"])
    dev.send(0x1AA, b"\x01\x02", 0)
    assert dev.raw.sent_frames[-1] == (0x1AA, b"\x01\x02", 0)
