#!/usr/bin/env python3
"""
Xbox-Controller -> HCA_03 (Lenkung) + ACC_18 (Gas/Bremse)
fuer VW MEB (Golf 8, Cupra Leon 2021, ID.3/ID.4, ...)

Controller-Belegung
-------------------
  Linker Stick X   -> Lenkung (Kruemmung ±MAX_CURV rad/m)
  Rechter Trigger  -> Gas     (0 bis +2.0 m/s²)
  Linker Trigger   -> Bremse  (0 bis -3.5 m/s²)
  A (Button 0)     -> Aktivieren
  B (Button 1)     -> Deaktivieren (Nothalt)
  Ctrl+C           -> Beenden

Modi (Flags)
------------
  Standard (kein Flag):
    VOLKSWAGEN_MEB (34), param=FLAG_VW_LONG_CONTROL (1).
    Erfordert ACC aktiv im Fahrzeug fuer Lenkung UND Gas/Bremse.
    check_relay aktiv: Kamera-HCA_03 und Kamera-ACC_18 werden geblockt
    sobald wir auf bus 0 senden.

  --bench:
    VOLKSWAGEN_MEB (34), param=FLAG_VW_LONG_CONTROL|FLAG_VW_BENCH_TEST (3).
    Lenkung: controls_allowed ueberbrueckt -> funktioniert im Stand.
    Gas/Bremse: weiterhin controls_allowed noetig (braucht echtes ACC).
    Erfordert ALLOW_DEBUG-Firmware (firmware/panda_h7.bin.signed).

Wie das Relay funktioniert
--------------------------
  Wir senden ACC_18 auf bus 0 nur wenn aktiv (A-Button).
  Wenn deaktiviert (B-Button): kein ACC_18 auf bus 0
  -> Kamera-ACC_18 von bus 2 wird wieder durchgeleitet.
  So gibt die Kamera automatisch die Laengsdynamik-Kontrolle zurueck.

Abhaengigkeiten
---------------
  pip install pandacan pygame
  (opendbc ist bereits als Teil des Projekts installiert)
"""

from __future__ import annotations

import argparse
import sys
import time

# ─── Konfiguration ─────────────────────────────────────────────────────────────

BUS        = 0         # ADAS-Bus (Harness-Seite J533/Gateway)
RATE_HZ    = 50        # 50 Hz - gleich wie openpilot
MAX_CURV   = 0.05      # rad/m Lenkgrenze (konservativ)
MAX_POWER  = 20        # % Lenkkraft-Limit
DEADZONE   = 0.05      # Joystick-Totzone
ACCEL_MAX  = 2.0       # m/s² Gaslimit (Panda erzwingt max. 2.0)
ACCEL_MIN  = -3.5      # m/s² Bremslimit (Panda erzwingt min. -3.5)
ACCEL_INACTIVE = 3.01  # Sentinel: "kein ACC-Kommando" (oberhalb physik. Range)

# ─── Safety-Konstanten ─────────────────────────────────────────────────────────

SAFETY_VOLKSWAGEN_MEB = 34
FLAG_VW_LONG_CONTROL  = 1   # schaltet ACC_18 in TX-Whitelist
FLAG_VW_BENCH_TEST    = 2   # ueberbrueckt controls_allowed fuer HCA_03

MSG_LH_EPS_03 = 0x09F   # EPS-Feedback: Lenkwinkel + HCA-Status
MSG_ESP_21    = 0x0FD   # Fahrzeuggeschwindigkeit (km/h)

_HCA_STATUS = {0: "disabled", 1: "init", 2: "FAULT", 3: "ready", 4: "rejected", 5: "active"}

# ─── HCA_03 Aufbau (Lenkung) ───────────────────────────────────────────────────

def build_hca03(curvature: float, power_pct: float, active: bool) -> bytes:
    """Baut eine 24-Byte HCA_03-Nachricht (kein CRC, kein Counter)."""
    data = bytearray(24)
    data[1] = (4 if active else 2) << 4
    data[2] = min(255, int(power_pct / 0.4))
    curv_raw = min(32767, int(abs(curvature) / 6.7e-6))
    data[3] = curv_raw & 0xFF
    data[4] = (curv_raw >> 8) & 0x7F
    if curvature > 0.0:
        data[4] |= 0x80
    if active:
        data[8] |= 0x04
    return bytes(data)


# ─── ACC_18 Aufbau (Gas/Bremse) ────────────────────────────────────────────────

def build_acc18(packer, accel: float, active: bool, speed_kmh: float = 0.0) -> tuple[int, bytes, int]:
    """Baut eine 32-Byte ACC_18-Nachricht via opendbc CANPacker.

    CRC (VW CRC-8H2F/AUTOSAR) und Counter (0-15 rolling) werden vom
    Packer automatisch berechnet und gesetzt.

    active=True:  Beschleunigung wie angegeben, ACC_Status=ACTIVE.
    active=False: ACCEL_INACTIVE (3.01) senden -> Fahrzeug weiss
                  "kein Kommando von openpilot".
    """
    if active:
        acc_status = 3    # ACC_CTRL_ACTIVE
        acc_aktiv  = 1
        jerk       = 4.0  # m/s³ - moderater Jerk-Limit
    else:
        accel      = ACCEL_INACTIVE
        acc_status = 2    # ACC_CTRL_ENABLED (Standby)
        acc_aktiv  = 0
        jerk       = 0.0

    values = {
        "ACC_Typ":                    2,        # ACC_mit_StopAndGo
        "ACC_Status_ACC":             acc_status,
        "ACC_StartStopp_Info":        1 if active else 0,
        "ACC_Sollbeschleunigung_02":  accel,
        "ACC_zul_Regelabw_unten":     0.0,
        "ACC_zul_Regelabw_oben":      0.0,
        "ACC_neg_Sollbeschl_Grad_02": jerk,
        "ACC_pos_Sollbeschl_Grad_02": jerk,
        "ACC_Anfahren":               0,
        "ACC_Anhalten":               0,
        "ACC_Anhalteweg":             20.46,    # neutraler Rollout-Wert
        "ACC_Anforderung_HMS":        0,        # kein Halte-Request
        "ACC_AKTIV_regelt":           acc_aktiv,
        "Speed":                      speed_kmh,
        "SET_ME_0XFE":                0xFE,
        "SET_ME_0X1":                 0x1,
        "SET_ME_0X9":                 0x9,
    }
    return packer.make_can_msg("ACC_18", BUS, values)


# ─── EPS-Feedback parsen ───────────────────────────────────────────────────────

def parse_lh_eps_03(data: bytes) -> tuple[float, str]:
    """Lenkwinkel (Grad) und HCA-Status aus LH_EPS_03 (0x09F)."""
    if len(data) < 8:
        return 0.0, "?"
    raw   = data[2] | ((data[3] & 0x0F) << 8)
    angle = raw * 0.15
    if (data[3] >> 7) & 0x01:
        angle = -angle
    hca = data[4] & 0x0F
    return angle, _HCA_STATUS.get(hca, f"?{hca}")


def parse_esp_21_speed(data: bytes) -> float:
    """Fahrzeuggeschwindigkeit in km/h aus ESP_21 (0x0FD)."""
    if len(data) < 6:
        return 0.0
    return ((data[4] | (data[5] << 8)) * 0.01)


# ─── Trigger normalisieren ─────────────────────────────────────────────────────

def trigger_value(axis_raw: float) -> float:
    """Normalisiert Xbox-Trigger-Achse auf 0.0..1.0.

    pygame liefert je nach Treiber -1.0 (Ruhe) bis +1.0 (voll)
    oder direkt 0.0 bis 1.0. Beide Faelle werden abgefangen.
    """
    return max(0.0, min(1.0, (axis_raw + 1.0) / 2.0))


# ─── Safety-Setup ──────────────────────────────────────────────────────────────

def setup_safety(p, bench: bool) -> tuple[int, str]:
    """Setzt Safety-Modus. Gibt (param, label) zurueck."""
    param = FLAG_VW_LONG_CONTROL | (FLAG_VW_BENCH_TEST if bench else 0)
    try:
        p.set_safety_mode(SAFETY_VOLKSWAGEN_MEB, param)
        h = p.health()
        mode = h.get("safety_mode", h.get("safety_model", 0))
        if mode != SAFETY_VOLKSWAGEN_MEB:
            raise RuntimeError(f"Safety-Modus {mode} statt 34 gesetzt")
    except Exception as e:
        sys.exit(
            f"FEHLER: VOLKSWAGEN_MEB Modus nicht verfuegbar: {e}\n"
            "-> Debug-Firmware flashen: python scripts/flash_panda.py"
        )

    bench_note = " + BENCH_TEST (Lenkung im Stand)" if bench else ""
    label = f"VOLKSWAGEN_MEB + LONG_CONTROL{bench_note}"
    return param, label


# ─── Hauptschleife ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Xbox-Controller Lenk- und Beschleunigungstest (VW MEB)"
    )
    parser.add_argument(
        "--bench", action="store_true",
        help=(
            "Stand-Test-Modus: BENCH_TEST-Flag setzt controls_allowed fuer HCA_03 "
            "auf True -> Lenkung ohne echtes ACC. Gas/Bremse braucht weiterhin "
            "echtes ACC. Erfordert ALLOW_DEBUG-Firmware."
        ),
    )
    args = parser.parse_args()

    try:
        import pygame
    except ImportError:
        sys.exit("pygame nicht gefunden -- bitte: pip install pygame")

    try:
        from panda import Panda
    except ImportError:
        sys.exit("pandacan nicht gefunden -- bitte: pip install pandacan")

    try:
        from opendbc.can import CANPacker
    except ImportError:
        sys.exit("opendbc nicht gefunden -- bitte: pip install -e '.[panda]'")

    packer = CANPacker("vw_meb")

    print("Verbinde Panda ...")
    p = Panda()
    p.set_can_speed_kbps(BUS, 500)

    param, safety_label = setup_safety(p, bench=args.bench)
    print(f"  Panda {p.get_version()} -- Safety: {safety_label}")
    print()
    print("HINWEIS: Gas/Bremse (ACC_18) erfordert immer echtes ACC im Fahrzeug.")
    print("         Lenkung im --bench-Modus funktioniert auch ohne ACC.")
    print()

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        p.close()
        sys.exit("Kein Controller gefunden!")

    joy = pygame.joystick.Joystick(0)
    joy.init()
    print(f"Controller: {joy.get_name()}")
    print("  Linker Stick X   -> Lenkung")
    print("  Rechter Trigger  -> Gas    (Axis 5)")
    print("  Linker Trigger   -> Bremse (Axis 4)")
    print("  A (Button 0)     -> Aktivieren")
    print("  B (Button 1)     -> Deaktivieren")
    print("  Ctrl+C           -> Beenden")
    print()

    active     = False
    period     = 1.0 / RATE_HZ
    eps_angle  = 0.0
    eps_hca    = "?"
    speed_kmh  = 0.0

    try:
        while True:
            t0 = time.monotonic()
            pygame.event.pump()

            # CAN-Puffer leeren: EPS-Feedback + Geschwindigkeit lesen
            for arb_id, data, bus, _ in (p.can_recv() or []):
                if arb_id == MSG_LH_EPS_03 and bus == BUS:
                    eps_angle, eps_hca = parse_lh_eps_03(data)
                elif arb_id == MSG_ESP_21 and bus == BUS:
                    speed_kmh = parse_esp_21_speed(data)

            # Buttons
            if joy.get_button(0):   # A = aktivieren
                active = True
            if joy.get_button(1):   # B = deaktivieren
                active = False

            # Lenkung: linker Stick X
            raw_x = joy.get_axis(0)
            if abs(raw_x) < DEADZONE:
                raw_x = 0.0
            curvature = -raw_x * MAX_CURV
            power     = MAX_POWER * abs(raw_x) if active else 0.0

            # Beschleunigung: Trigger
            rt = trigger_value(joy.get_axis(5))  # Rechter Trigger -> Gas
            lt = trigger_value(joy.get_axis(4))  # Linker Trigger  -> Bremse
            if active:
                accel = rt * ACCEL_MAX - lt * abs(ACCEL_MIN)
                accel = max(ACCEL_MIN, min(ACCEL_MAX, accel))
            else:
                accel = 0.0

            # HCA_03 senden (Lenkung) - immer bei 50 Hz
            hca_msg = build_hca03(curvature, power, active)
            p.can_send(0x303, hca_msg, BUS)

            # ACC_18 senden (Gas/Bremse) - nur wenn aktiv
            # Wenn nicht aktiv: kein Senden -> Kamera-Relay wieder frei
            if active:
                acc_addr, acc_data, acc_bus = build_acc18(
                    packer, accel, active=True, speed_kmh=speed_kmh
                )
                p.can_send(acc_addr, acc_data, acc_bus)

            # Statuszeile
            steer_arrow = "<<" if curvature > 0.001 else (">>" if curvature < -0.001 else " |")
            state = "AKTIV  " if active else "STANDBY"
            accel_str = f"{accel:+5.2f} m/s2" if active else "  (aus) "
            print(
                f"\r[{state}] {steer_arrow} "
                f"Kurve:{curvature:+.4f}  Kraft:{power:4.1f}%  "
                f"| Accel:{accel_str}  "
                f"| EPS:{eps_angle:+6.1f} deg  HCA:{eps_hca}  "
                f"| {speed_kmh:.1f} km/h   ",
                end="", flush=True,
            )

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    except KeyboardInterrupt:
        print("\n\nStoppe ...")
    finally:
        # Sauber deaktivieren
        p.can_send(0x303, build_hca03(0.0, 0.0, False), BUS)
        time.sleep(0.05)
        p.set_safety_mode(0)
        p.close()
        pygame.quit()
        print("Getrennt.")


if __name__ == "__main__":
    main()
