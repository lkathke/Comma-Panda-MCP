<div align="center">

# 🐼 panda-mcp

**Reverse-engineer your car's CAN bus by talking to Claude.**

An [MCP](https://modelcontextprotocol.io) server that turns a [comma.ai Panda](https://github.com/commaai/panda) into a conversational CAN reverse-engineering rig — *record → diff → find the signal → confirm → send it back* — without writing a line of glue code.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)](https://modelcontextprotocol.io)
[![Tools](https://img.shields.io/badge/tools-26-success.svg)](#-tool-reference)
[![Tests](https://img.shields.io/badge/tests-7%20passing-brightgreen.svg)](#-development)
[![No hardware required](https://img.shields.io/badge/mock%20mode-no%20hardware%20needed-orange.svg)](#-mock-mode)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

</div>

---

## Why this exists

Finding the one CAN frame that controls your blinker — or your steering angle, or your door locks — is a slow, fiddly loop: log traffic, do the thing, log again, diff bytes by hand, guess the CRC, replay it, repeat. Every step lives in a different script.

**panda-mcp collapses that loop into a conversation.** You tell Claude *"record the bus, I'll flip the blinker, now find what changed"* and it drives the Panda, runs comma.ai's own diff heuristics, cracks the CRC, and replays the candidate frame — all through 26 typed MCP tools.

And because it ships with a **full mock CAN bus**, you can learn the entire workflow on your laptop on the train home, with no Panda and no car.

```
You:    Connect in mock mode, record a baseline, then record again — I'll toggle the blinker.
Claude: [device_connect → capture_start → capture_stop ×2]
You:    Which bits only showed up while blinking?
Claude: [analyze_diff_new_bits] → 0xE1, byte 0 bit 0 flipped 0→1. That's your blinker.
You:    Prove it. Send it back.
Claude: [device_set_safety_mode('alloutput') → send_frame 0xE1 ...] → sent 20×. 🚗💡
```

## ✨ Features

- 🎙️ **Background capture** — non-blocking recording with bus / arbitration-ID / duration / frame-count filters
- 🔍 **Two diff engines, straight from comma.ai** — `can_bit_transition` (clean 0→1/1→0 flips across a split) and `can_unique` (bits that appear only against a baseline)
- 🎯 **Signal hunting** — pin a known number (speed, RPM, steering angle) to an exact ID + byte offset + endianness
- 📸 **Snapshots** — freeze the bus state before/after an action and XOR-diff it
- 📤 **Transmit & fuzz** — single frame, bulk, single-byte sweep, or full timed replay of a recording
- 🧮 **CRC toolkit** — a catalogue of real automotive CRCs (J1850, AUTOSAR, CCITT…), brute-force identification with per-message magic-init sweep, and automatic CRC-field detection across a whole capture
- 🧪 **Mock mode** — a deterministic synthetic bus that exercises *every* tool, so dev & tests need zero hardware
- 🔒 **Safe by default** — listen-only until you explicitly unlock transmission
- 💾 **Interop** — export to candump `.log` for Cabana / SavvyCAN / can-utils

## 📑 Table of contents

- [Quickstart](#-quickstart)
- [Mock mode](#-mock-mode)
- [How it works](#-how-it-works)
- [Tool reference](#-tool-reference)
- [Workflows](#-workflows)
- [Sending frames vs. flashing firmware](#-sending-frames-vs-flashing-firmware)
- [Safety](#-safety)
- [Development](#-development)
- [Roadmap](#-roadmap)
- [Credits & license](#-credits--license)

## 🚀 Quickstart

```bash
git clone https://github.com/<you>/panda-mcp.git
cd panda-mcp

py -m venv .venv
.venv/Scripts/python -m pip install -e .            # mock mode only
.venv/Scripts/python -m pip install -e ".[panda]"   # + real hardware (pandacan)
```

Register the server with Claude Code by pointing it at the bundled [`claude.json`](claude.json):

```jsonc
{
  "mcpServers": {
    "panda": {
      "command": ".venv/Scripts/python.exe",
      "args": ["-m", "panda_mcp.server"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

Then just ask Claude to `device_connect(mock=true)` and start exploring.

## 🧪 Mock mode

No Panda? No car? No problem. `device_connect(mock=true)` spins up a deterministic synthetic CAN stream designed to make every analysis tool light up:

| Arb ID | Rate | Payload | Demonstrates |
|:------:|:----:|---------|--------------|
| `0x1A0` | 20 Hz | rolling counter nibble **+ J1850 CRC8** over bytes 0–6 | `crc_detect_field`, `crc_brute_force` |
| `0x2B0` | 10 Hz | static `DEADBEEF…` | baseline noise for diffs |
| `0x3C0` | 50 Hz | int16 "steering angle" sweeping via `sin(t)` | `analyze_find_value` |
| `0x0E1` |  1 Hz | one toggle bit driven by a virtual "blinker" | `analyze_diff_transition` |

Everything you learn in mock mode maps 1:1 onto real hardware — only the `mock=true` flag changes.

## 🛠 How it works

```
┌──────────────┐   MCP tools    ┌─────────────────────────────────────────┐
│    Claude    │ ─────────────► │             panda_mcp.server            │
│  (you, chat) │ ◄───────────── │            FastMCP · 26 tools           │
└──────────────┘    results     └───────────────────┬─────────────────────┘
                                                     │
        ┌────────────────────────┬───────────────────┼────────────────────┐
        ▼                        ▼                    ▼                    ▼
 ┌────────────┐          ┌──────────────┐     ┌──────────────┐     ┌────────────┐
 │  device.py │          │  session.py  │     │ analysis.py  │     │   crc.py   │
 │ Panda  +   │          │ bg-threaded  │     │ bit-state    │     │ automotive │
 │ MockPanda  │          │ capture store│     │ diff engines │     │ CRC engine │
 └─────┬──────┘          └──────────────┘     └──────────────┘     └────────────┘
       │ USB
       ▼
 ┌────────────┐
 │ Panda Red  │  STM32H725 · CAN/CAN-FD
 │  🚗 car    │
 └────────────┘
```

| Module | Responsibility |
|--------|----------------|
| `server.py`   | FastMCP entry point — all 26 tools, argument parsing, safety gating |
| `device.py`   | Panda abstraction + `MockPanda` synthetic stream + safety-mode constants |
| `session.py`  | Background-threaded capture store and snapshots (all in-memory) |
| `analysis.py` | Bit-state tracking, the two diff engines, `find_value`, per-ID stats |
| `crc.py`      | CRC catalogue, brute-force identification, CRC-field detection |
| `model.py`    | The `CanFrame` dataclass |

## 📖 Tool reference

<table>
<tr><th>Group</th><th>Tool</th><th>What it does</th></tr>

<tr><td rowspan="6"><b>Device</b></td>
<td><code>device_connect</code></td><td>Connect to a Panda (or <code>mock=true</code>)</td></tr>
<tr><td><code>device_status</code></td><td>Connection, firmware, safety mode, bus errors</td></tr>
<tr><td><code>device_set_safety_mode</code></td><td><code>silent</code> (listen-only) / <code>alloutput</code> (unlock TX)</td></tr>
<tr><td><code>device_set_can_speed</code></td><td>Set a bus bitrate in kbps</td></tr>
<tr><td><code>device_flash</code></td><td>Flash stock or custom firmware</td></tr>
<tr><td><code>device_recover</code></td><td>DFU recovery for a bricked device</td></tr>

<tr><td rowspan="5"><b>Capture</b></td>
<td><code>capture_start</code></td><td>Begin recording (bus/ID/duration/count filters)</td></tr>
<tr><td><code>capture_stop</code></td><td>Stop and summarize</td></tr>
<tr><td><code>capture_list</code></td><td>List all captures</td></tr>
<tr><td><code>capture_get</code></td><td>Page through raw frames</td></tr>
<tr><td><code>capture_delete</code></td><td>Discard a capture</td></tr>

<tr><td rowspan="4"><b>Analyze</b></td>
<td><code>analyze_stats</code></td><td>Per-ID count, period, length, dynamic bytes</td></tr>
<tr><td><code>analyze_diff_transition</code></td><td>Clean bit flips before/after a split time</td></tr>
<tr><td><code>analyze_diff_new_bits</code></td><td>Bits unique to a capture vs. baselines</td></tr>
<tr><td><code>analyze_find_value</code></td><td>Locate a number → ID + offset + endianness</td></tr>

<tr><td rowspan="2"><b>Snapshot</b></td>
<td><code>snapshot_take</code></td><td>Freeze latest payload per (bus, ID)</td></tr>
<tr><td><code>snapshot_diff</code></td><td>XOR-diff two snapshots</td></tr>

<tr><td rowspan="4"><b>Send</b></td>
<td><code>send_frame</code></td><td>Transmit one frame (optionally repeated)</td></tr>
<tr><td><code>send_bulk</code></td><td>Transmit many frames at once</td></tr>
<tr><td><code>send_fuzz</code></td><td>Sweep one byte across a range</td></tr>
<tr><td><code>send_replay</code></td><td>Replay a capture with original timing</td></tr>

<tr><td rowspan="4"><b>CRC</b></td>
<td><code>crc_compute</code></td><td>Compute a catalogued CRC over hex data</td></tr>
<tr><td><code>crc_list</code></td><td>List CRC algorithms + parameters</td></tr>
<tr><td><code>crc_brute_force</code></td><td>Identify the algorithm (with init sweep)</td></tr>
<tr><td><code>crc_detect_field</code></td><td>Find the CRC byte + algo across a capture</td></tr>

<tr><td><b>Export</b></td>
<td><code>export_candump</code></td><td>Write a candump <code>.log</code> (Cabana/SavvyCAN)</td></tr>
</table>

## 🧭 Workflows

### Hunt down a control frame (the blinker)

```text
device_connect(mock=true)

capture_start()                  → cap-aaaa          # baseline: do nothing
capture_stop(cap-aaaa)

capture_start()                  → cap-bbbb          # now toggle the blinker
capture_stop(cap-bbbb)

analyze_diff_new_bits(cap-bbbb, [cap-aaaa])          # bits unique to "blinking"
# …or, if you toggled mid-capture:
analyze_diff_transition(cap-bbbb, split_ts=5.0)

device_set_safety_mode('alloutput')
send_frame(arb_id='0xE1', data='01000000000000', bus=0, count=20, interval_ms=50)
```

### Pin a numeric signal (speed / RPM / steering)

```text
analyze_find_value(cap-xxxx, value=2000, bit_length=16)
# → 0x3C0, byte_offset 0, little-endian, 14 matches
```

### Crack a CRC

```text
crc_detect_field(cap-xxxx, arb_id='0x1A0')
# → crc_index 7, confirmed: crc8_sae_j1850 (init 0xFF) across 30 frames ✅

crc_brute_force(frame='10123456780000CC')            # single-frame identification
crc_compute(data='10123456780000', algorithm='crc8_sae_j1850')
```

## ⚡ Sending frames vs. flashing firmware

> **You do _not_ need custom firmware to send frames.** Transmission is unlocked in software via the safety mode:

```text
device_set_safety_mode('alloutput')
```

`device_flash` / `device_recover` exist only for firmware *updates*, custom on-device safety logic, or un-bricking:

- `device_flash()` — build & flash the stock firmware (the Panda's own `flash()`)
- `device_flash('/path/fw.bin')` — flash a custom binary
- `device_recover()` — DFU recovery for an unresponsive device

The Panda Red is an **STM32H725**; flashing runs over USB DFU (VID `0x0483`, PID `0xdf11`). The device reboots afterward — call `device_connect` again.

## 🔒 Safety

> [!WARNING]
> Injecting frames onto a **moving vehicle's** powertrain bus can disable brakes, steering, or throttle. People can get hurt.

- The server boots **listen-only** (`silent`). Transmission requires an explicit `device_set_safety_mode('alloutput')` — there is no way to send by accident.
- Only unlock output on a **bench harness** or **a vehicle you own and have immobilized**.
- This project is for education, research, and tinkering on hardware you control. You are responsible for how you use it.

## 🧰 Development

```bash
.venv/Scripts/python -m pytest -q        # 7 hardware-free end-to-end tests
```

The whole test suite runs against `MockPanda` — capture, both diff engines, `find_value`, CRC detection, the safety gate, and replay — so CI never needs a device.

```
src/panda_mcp/
  server.py     FastMCP entry point · all 26 tools
  device.py     Panda abstraction + MockPanda synthetic stream
  session.py    background-threaded capture store + snapshots
  analysis.py   bit-state diffing + find_value + per-ID stats
  crc.py        automotive CRC catalogue + brute force + field detection
  model.py      CanFrame
tests/
  test_smoke.py hardware-free end-to-end tests
```

## 🗺 Roadmap

- [ ] **DBC decoding** via `cantools` — decode/encode against a `.dbc`
- [ ] **Persistence** — auto-save captures to disk, reload `.candump`/CSV as sessions
- [ ] **CAN-FD** — widen bit arrays to 64-byte payloads
- [ ] **Multi-message CRC counters** — auto-derive counter + checksum pairs
- [ ] **SavvyCAN/Cabana import** alongside the existing export

Contributions welcome — open an issue or PR. If you add a tool, add a mock signal that exercises it so the test suite stays hardware-free.

## 🙏 Credits & license

The two diff engines are faithful ports of comma.ai's own examples — [`can_bit_transition.py`](https://github.com/commaai/panda/blob/master/examples/can_bit_transition.py) and [`can_unique.py`](https://github.com/commaai/panda/blob/master/examples/can_unique.py). Hardware access is via the [`pandacan`](https://github.com/commaai/panda) library. Huge thanks to [comma.ai](https://comma.ai) for open-sourcing the Panda.

Released under the [MIT License](LICENSE) — same as panda.

<div align="center">
<sub>Built for CAN tinkerers. Not affiliated with comma.ai.</sub>
</div>
