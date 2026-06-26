#!/usr/bin/env python3
"""
Xbox-Controller → HCA_03 Lenktest für VW MQB Evo / MEB
(Golf 8, Cupra Leon 2021, ID.3, ID.4, ...)

Modi
────
Ohne Flag (Standard):
  SAFETY_VOLKSWAGEN_MEB (34), kein bench-test param.
  check_relay aktiv → Kamera-HCA_03 wird geblockt.
  TX nur wenn Fahrzeug controls_allowed setzt (ACC aktiv).

--bench (Standtest mit Kamera verbunden):
  SAFETY_VOLKSWAGEN_MEB (34) + FLAG_VW_BENCH_TEST (param=2).
  check_relay aktiv → Kamera-HCA_03 geblockt.
  controls_allowed-Prüfung überbrückt → funktioniert im Stand.
  Erfordert ALLOW_DEBUG-Firmware (firmware/panda_h7.bin.signed).
  Flash: python scripts/flash_panda.py → Option 2

--alloutput (Fallback ohne Debug-Firmware):
  SAFETY_ALLOUTPUT (17), kein check_relay.
  Kamera MUSS vorher getrennt werden.

WARNUNG
───────
Nur auf Privatgelände / stehendem Fahrzeug testen.
Fahrer muss jederzeit eingreifen können.

Abhängigkeiten
──────────────
  pip install pandacan pygame
"""

from __future__ import annotations

import argparse
import sys
import time

# ─── Konfiguration ────────────────────────────────────────────────────────────

BUS        = 0       # ADAS-Bus (Harness-Seite J533/Gateway)
RATE_HZ    = 50      # 50 Hz — gleich wie openpilot
MAX_CURV   = 0.05    # rad/m Limit (konservativ; 0.05 ≈ weite Kurve, 0.10 ≈ enge)
MAX_POWER  = 20      # % Lenkkraft-Limit (openpilot nutzt bis 50 %)
DEADZONE   = 0.05    # Joystick-Totzone

# ─── HCA_03 Nachricht (0x303, 24 Byte, kein CRC, kein Counter) ────────────────
#
# Signal-Layout (DBC vw_mqbevo.dbc, BO_ 771):
#   Byte 1  Bits 4-7  : RequestStatus  4=aktiv, 2=bereit
#   Byte 2            : Power raw      factor 0.4 → pct/0.4
#   Bytes 3+4[0:6]    : Curvature      factor 6.7e-6 rad/m → val/6.7e-6
#   Byte 4  Bit 7     : Curvature_VZ   1=links (positiv), 0=rechts
#   Byte 8  Bit 2     : HighSendRate   1=50 Hz aktiv


def build_hca03(curvature: float, power_pct: float, active: bool) -> bytes:
    """Baut eine 24-Byte HCA_03-Nachricht.

    curvature  : Krümmung in rad/m — positiv = links, negativ = rechts
    power_pct  : Lenkkraft 0-100 %
    active     : True = Lenkeingriff anfordern
    """
    data = bytearray(24)

    # RequestStatus: Byte 1, oberes Nibble (Bits 12-15 im DBC)
    data[1] = (4 if active else 2) << 4

    # Power: Byte 2, factor 0.4
    data[2] = min(255, int(power_pct / 0.4))

    # Curvature: 15 Bit ab Bit 24, factor 6.7e-6
    curv_raw = min(32767, int(abs(curvature) / 6.7e-6))
    data[3] = curv_raw & 0xFF
    data[4] = (curv_raw >> 8) & 0x7F

    # Curvature_VZ: Bit 39 = Byte 4, Bit 7
    if curvature > 0.0:
        data[4] |= 0x80

    # HighSendRate: Bit 66 = Byte 8, Bit 2
    if active:
        data[8] |= 0x04

    return bytes(data)


# ─── Relay-Status diagnostizieren ─────────────────────────────────────────────

def check_hca03_from_camera(panda, timeout_s: float = 1.0) -> bool:
    """Gibt True zurück wenn die Kamera HCA_03 auf bus 2 sendet (Relay-Konflikt!)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for arb_id, data, bus, _ in (panda.can_recv() or []):
            if arb_id == 0x303 and bus == 2:
                return True
        time.sleep(0.01)
    return False


# ─── Hauptschleife ────────────────────────────────────────────────────────────

SAFETY_ALLOUTPUT        = 17
SAFETY_VOLKSWAGEN_MEB   = 34
FLAG_VW_BENCH_TEST      = 2   # param für set_safety_mode — überbrückt controls_allowed

MSG_LH_EPS_03 = 0x09F  # EPS feedback: steering angle + HCA status

_HCA_STATUS = {0: "disabled", 1: "init", 2: "FAULT", 3: "ready", 4: "rejected", 5: "active"}


def parse_lh_eps_03(data: bytes) -> tuple[float, str]:
    """Parse LH_EPS_03 frame into (steering_angle_deg, hca_status_str).

    EPS_Berechneter_LW: bits 16-27 LE, scale 0.15 deg/LSB
    EPS_VZ_BLW:         bit 31 (1 = left/negative)
    EPS_HCA_Status:     bits 32-35 LE (lower nibble of byte 4)
    """
    if len(data) < 8:
        return 0.0, "?"
    raw = data[2] | ((data[3] & 0x0F) << 8)
    angle = raw * 0.15
    if (data[3] >> 7) & 0x01:
        angle = -angle
    hca = data[4] & 0x0F
    return angle, _HCA_STATUS.get(hca, f"?{hca}")


def _setup_safety(p, bench: bool, alloutput: bool) -> tuple[int, int, str]:
    """Setzt den Safety-Modus entsprechend dem gewählten Modus.

    Returns (safety_mode, param, label).
    """
    if alloutput:
        p.set_safety_mode(SAFETY_ALLOUTPUT)
        return SAFETY_ALLOUTPUT, 0, "ALLOUTPUT (kein check_relay — Kamera MUSS getrennt sein!)"

    param = FLAG_VW_BENCH_TEST if bench else 0
    try:
        p.set_safety_mode(SAFETY_VOLKSWAGEN_MEB, param)
        h = p.health()
        mode = h.get("safety_mode", h.get("safety_model", 0))
        if mode == SAFETY_VOLKSWAGEN_MEB:
            if bench:
                label = "VOLKSWAGEN_MEB + BENCH_TEST (check_relay aktiv, controls_allowed überbrückt)"
            else:
                label = "VOLKSWAGEN_MEB (check_relay aktiv, braucht ACC aktiv)"
            return SAFETY_VOLKSWAGEN_MEB, param, label
    except Exception as e:
        print(f"  Warnung: VW MEB Modus fehlgeschlagen ({e}), falle auf ALLOUTPUT zurück")

    p.set_safety_mode(SAFETY_ALLOUTPUT)
    return SAFETY_ALLOUTPUT, 0, "ALLOUTPUT (Fallback — kein check_relay, Kamera trennen!)"


def main() -> None:
    parser = argparse.ArgumentParser(description="Xbox-Controller HCA_03 Lenktest")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument(
        "--bench", action="store_true",
        help="Bench/Stand-Test: VOLKSWAGEN_MEB + FLAG_VW_BENCH_TEST (param=2). "
             "check_relay aktiv, controls_allowed überbrückt. "
             "Braucht ALLOW_DEBUG-Firmware (firmware/panda_h7.bin.signed).",
    )
    mode_grp.add_argument(
        "--alloutput", action="store_true",
        help="Fallback-Modus ALLOUTPUT (kein check_relay). Kamera MUSS getrennt sein.",
    )
    args = parser.parse_args()

    try:
        import pygame
    except ImportError:
        sys.exit("pygame nicht gefunden — bitte: pip install pygame")

    try:
        from panda import Panda  # type: ignore
    except ImportError:
        sys.exit("pandacan nicht gefunden — bitte: pip install pandacan")

    # Panda verbinden
    print("Verbinde Panda …")
    p = Panda()
    p.set_can_speed_kbps(BUS, 500)

    safety_mode, safety_param, safety_label = _setup_safety(p, bench=args.bench, alloutput=args.alloutput)
    print(f"  Panda {p.get_version()} — Safety: {safety_label}")

    if safety_mode == SAFETY_ALLOUTPUT:
        # Relay-Konflikt prüfen — nur nötig wenn kein check_relay aktiv
        print("Prüfe ob Kamera HCA_03 auf bus 2 sendet …")
        if check_hca03_from_camera(p):
            print(
                "\n  WARNUNG: Kamera sendet HCA_03 auf bus 2!\n"
                "  Im ALLOUTPUT-Modus gibt es kein automatisches Relay-Blocking.\n"
                "  → Kamera vom Harness trennen ODER mit --bench neu starten:\n"
                "    python scripts/steer_test.py --bench\n"
                "    (braucht: python scripts/flash_panda.py → firmware/panda_h7.bin.signed)\n"
                "  Mit [Enter] trotzdem fortfahren, mit Ctrl+C abbrechen."
            )
            try:
                input()
            except KeyboardInterrupt:
                p.close()
                return
        else:
            print("  OK — kein HCA_03 von Kamera erkannt.")

    # Xbox Controller init
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        p.close()
        sys.exit("Kein Controller gefunden!")

    joy = pygame.joystick.Joystick(0)
    joy.init()
    print(f"\nController: {joy.get_name()}")
    print("  Linker Stick X  → Lenkung")
    print("  A (Button 0)    → Lenkeingriff aktivieren")
    print("  B (Button 1)    → Lenkeingriff deaktivieren (Nothalt)")
    print("  Ctrl+C          → Beenden\n")

    active      = False
    period      = 1.0 / RATE_HZ
    last_sent   = time.monotonic()
    eps_angle   = 0.0
    eps_hca_str = "?"

    try:
        while True:
            t0 = time.monotonic()
            pygame.event.pump()

            # Drain CAN buffer — pick up EPS feedback (LH_EPS_03)
            for arb_id, data, bus, _ in (p.can_recv() or []):
                if arb_id == MSG_LH_EPS_03 and bus == BUS and len(data) >= 8:
                    eps_angle, eps_hca_str = parse_lh_eps_03(data)

            # Buttons
            if joy.get_button(0):   # A = aktivieren
                active = True
            if joy.get_button(1):   # B = deaktivieren
                active = False

            # Joystick X-Achse (Axis 0): -1.0 = ganz links, +1.0 = ganz rechts
            raw_x = joy.get_axis(0)
            if abs(raw_x) < DEADZONE:
                raw_x = 0.0

            # Vorzeichen: Joystick links (-) → positive Krümmung = links lenken
            curvature = -raw_x * MAX_CURV
            power     = MAX_POWER * abs(raw_x) if active else 0.0

            msg = build_hca03(curvature, power, active)
            p.can_send(0x303, msg, BUS)

            # Statuszeile
            arrow = "←" if curvature > 0.001 else ("→" if curvature < -0.001 else "·")
            state = "AKTIV  " if active else "STANDBY"
            print(
                f"\r[{state}] {arrow}  "
                f"Krümmung: {curvature:+.4f} rad/m  "
                f"Kraft: {power:4.1f}%  "
                f"| EPS: {eps_angle:+6.1f}°  HCA: {eps_hca_str}   ",
                end="", flush=True,
            )

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    except KeyboardInterrupt:
        print("\n\nStoppe …")
    finally:
        # Deaktivieren und sauber trennen
        p.can_send(0x303, build_hca03(0.0, 0.0, False), BUS)
        time.sleep(0.05)
        p.close()
        pygame.quit()
        print("Getrennt.")


if __name__ == "__main__":
    main()
