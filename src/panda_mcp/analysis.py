"""Frame analysis primitives: bit-state tracking, diffing, value/toggle search.

The bit-state approach is lifted from comma's own examples
(``examples/can_bit_transition.py`` and ``examples/can_unique.py``): for each
arbitration id we track, per bit, whether it was *ever* seen as 1 and *ever*
seen as 0. A bit that is only-ever-0 in window A but only-ever-1 in window B is a
clean transition worth reporting.
"""
from __future__ import annotations

from .model import CanFrame

MAX_LEN = 8  # classic CAN; CAN-FD up to 64 would widen the bit arrays


class BitState:
    """Per-id record of which bits were seen as 1 and as 0."""

    def __init__(self):
        self.seen_one = bytearray(MAX_LEN)   # byte i, bit mask of 1s observed
        self.seen_zero = bytearray(MAX_LEN)  # byte i, bit mask of 0s observed
        self.count = 0
        self.maxlen = 0

    def update(self, data: bytes) -> None:
        self.count += 1
        self.maxlen = max(self.maxlen, len(data))
        for i in range(min(len(data), MAX_LEN)):
            b = data[i]
            self.seen_one[i] |= b
            self.seen_zero[i] |= (~b) & 0xFF


def build_bit_states(frames, arb_ids=None) -> dict[int, BitState]:
    states: dict[int, BitState] = {}
    for f in frames:
        if arb_ids is not None and f.arb_id not in arb_ids:
            continue
        st = states.get(f.arb_id)
        if st is None:
            st = states[f.arb_id] = BitState()
        st.update(f.data)
    return states


def _bit_positions(mask: int):
    return [bit for bit in range(8) if mask & (1 << bit)]


def diff_bit_states(states_a: dict[int, BitState],
                    states_b: dict[int, BitState]) -> list[dict]:
    """Bits that are constant-0 in A but constant-1 in B (or vice versa).

    This is the can_bit_transition.py heuristic: a bit only counts as a
    transition if it never wavered within each window.
    """
    results = []
    for arb_id in sorted(set(states_a) | set(states_b)):
        a = states_a.get(arb_id)
        b = states_b.get(arb_id)
        if a is None or b is None:
            results.append({
                "arb_id": f"0x{arb_id:X}",
                "note": "only present in " + ("A" if b is None else "B"),
            })
            continue
        changes = []
        for i in range(MAX_LEN):
            # constant-0 in A: seen_zero set, seen_one clear
            const0_a = (~a.seen_one[i]) & a.seen_zero[i]
            const1_a = a.seen_one[i] & (~a.seen_zero[i]) & 0xFF
            const0_b = (~b.seen_one[i]) & b.seen_zero[i]
            const1_b = b.seen_one[i] & (~b.seen_zero[i]) & 0xFF
            rose = const0_a & const1_b   # 0 -> 1
            fell = const1_a & const0_b   # 1 -> 0
            for bit in _bit_positions(rose):
                changes.append({"byte": i, "bit": bit, "transition": "0->1"})
            for bit in _bit_positions(fell):
                changes.append({"byte": i, "bit": bit, "transition": "1->0"})
        if changes:
            results.append({"arb_id": f"0x{arb_id:X}", "changes": changes})
    return results


def diff_new_bits(interesting: dict[int, BitState],
                  background: dict[int, BitState]) -> list[dict]:
    """Bits seen in `interesting` that were NEVER seen in `background`.

    This is the can_unique.py heuristic for spotting frames triggered by an
    action against a recorded baseline.
    """
    results = []
    for arb_id in sorted(interesting):
        a = interesting[arb_id]
        bg = background.get(arb_id)
        new = []
        for i in range(MAX_LEN):
            bg_one = bg.seen_one[i] if bg else 0
            bg_zero = bg.seen_zero[i] if bg else 0
            new_ones = a.seen_one[i] & (~bg_one) & 0xFF
            new_zeros = a.seen_zero[i] & (~bg_zero) & 0xFF
            for bit in _bit_positions(new_ones):
                new.append({"byte": i, "bit": bit, "value": 1})
            for bit in _bit_positions(new_zeros):
                new.append({"byte": i, "bit": bit, "value": 0})
        if new:
            results.append({
                "arb_id": f"0x{arb_id:X}",
                "new_in_background": bg is None,
                "new_bits": new,
            })
    return results


def find_value(frames, value: int, bit_length: int,
               little_endian: bool = True) -> list[dict]:
    """Locate an int of `bit_length` bits equal to `value` inside any frame.

    Slides a window over every byte-aligned bit offset of each id's payloads and
    reports id/offset/endianness hits. Useful for pinning a known physical value
    (speed, rpm, steering angle) to a signal position.
    """
    hits: dict[tuple, dict] = {}
    nbytes = (bit_length + 7) // 8
    for f in frames:
        data = f.data
        for off in range(0, len(data) - nbytes + 1):
            chunk = data[off:off + nbytes]
            for le in (True, False) if little_endian is None else (little_endian,):
                val = int.from_bytes(chunk, "little" if le else "big")
                if bit_length % 8:
                    val &= (1 << bit_length) - 1
                if val == value:
                    key = (f.arb_id, off, le)
                    rec = hits.get(key)
                    if rec is None:
                        hits[key] = {
                            "arb_id": f"0x{f.arb_id:X}",
                            "byte_offset": off,
                            "endian": "little" if le else "big",
                            "match_count": 1,
                        }
                    else:
                        rec["match_count"] += 1
    return sorted(hits.values(), key=lambda r: -r["match_count"])


def find_toggle(frames, t_split: float) -> list[dict]:
    """Compare bit states before vs. after `t_split` (capture-relative seconds).

    Convenience wrapper around diff_bit_states for the 'do action at time X,
    what changed?' workflow.
    """
    before = [f for f in frames if f.ts < t_split]
    after = [f for f in frames if f.ts >= t_split]
    return diff_bit_states(build_bit_states(before), build_bit_states(after))


def per_id_stats(frames) -> list[dict]:
    """Summary per id: count, period, payload length, which bytes ever change."""
    by_id: dict[int, list[CanFrame]] = {}
    for f in frames:
        by_id.setdefault(f.arb_id, []).append(f)
    out = []
    for arb_id, fl in sorted(by_id.items()):
        fl.sort(key=lambda f: f.ts)
        span = fl[-1].ts - fl[0].ts if len(fl) > 1 else 0.0
        period = (span / (len(fl) - 1)) if len(fl) > 1 else None
        st = BitState()
        for f in fl:
            st.update(f.data)
        changing = [i for i in range(MAX_LEN)
                    if st.seen_one[i] & st.seen_zero[i]]  # bit both 0 and 1
        out.append({
            "arb_id": f"0x{arb_id:X}",
            "count": len(fl),
            "period_ms": round(period * 1000, 2) if period else None,
            "payload_len": st.maxlen,
            "dynamic_bytes": changing,
        })
    return out
