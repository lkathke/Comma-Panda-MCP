#!/usr/bin/env python3
"""Diagnose-Script: Zeigt alle relevanten Infos über den angeschlossenen Panda."""

from __future__ import annotations

SAFETY_NAMES = {
    0:  "SILENT (nur zuhören)",
    1:  "HONDA_NIDEC",
    2:  "TOYOTA",
    3:  "ELM327",
    4:  "GM",
    5:  "HONDA_BOSCH_GIRAFFE",
    8:  "HYUNDAI",
    9:  "CHRYSLER",
    10: "TESLA",
    11: "SUBARU",
    13: "MAZDA",
    14: "NISSAN",
    15: "VOLKSWAGEN_MQB (Golf 7, Passat B8)",
    17: "ALLOUTPUT [OK] (kann senden, kein Safety-Check)",
    19: "NO_OUTPUT",
    20: "HONDA_BOSCH",
    21: "VOLKSWAGEN_PQ",
    25: "VOLKSWAGEN_MLB",
    27: "BODY",
    28: "HYUNDAI_CANFD",
    33: "RIVIAN",
    34: "VOLKSWAGEN_MEB [OK] (Golf 8 / Cupra Leon / ID.3 — mit ALLOW_DEBUG)",
}

def main() -> None:
    try:
        from panda import Panda  # type: ignore
    except ImportError:
        print("pandacan nicht installiert — bitte: pip install pandacan")
        return

    print("Suche Panda ...")
    serials = Panda.list()
    if not serials:
        print("Kein Panda gefunden! USB-Kabel prüfen.")
        return

    print(f"Gefundene Pandas: {serials}\n")

    for serial in serials:
        try:
            p = Panda(serial=serial)
        except RuntimeError as e:
            if "health packet" in str(e).lower() or "version" in str(e).lower():
                print(f"  Firmware-Version inkompatibel mit pandacan 0.0.10: {e}")
                print("  -> Debug-Firmware flashen: python scripts/flash_panda.py")
                print("    Option 2 -> firmware/panda_h7.bin.signed")
            else:
                print(f"  Verbindung fehlgeschlagen: {e}")
            continue
        h = p.health()

        fw_version  = p.get_version()
        hw_type_raw = p.get_type()
        hw_type_int = hw_type_raw[0] if isinstance(hw_type_raw, (bytes, bytearray)) else hw_type_raw

        hw_names = {
            0: "UNKNOWN",
            1: "White Panda",
            2: "Grey Panda",
            3: "Black Panda",
            4: "Pedal",
            5: "UNO",
            6: "DOS",
            7: "Red Panda",
            8: "Red Panda v2",
        }
        hw_name = hw_names.get(hw_type_int, f"Unbekannt ({hw_type_int})")

        safety_mode = h.get("safety_mode", h.get("safety_model", "?"))
        safety_name = SAFETY_NAMES.get(safety_mode, f"Unbekannt ({safety_mode})")

        print(f"Serial:        {serial}")
        print(f"Hardware:      {hw_name} (type={hw_type_int})")
        print(f"Firmware:      {fw_version}")
        print(f"Safety-Mode:   {safety_mode} — {safety_name}")
        print(f"Spannung:      {h.get('voltage', '?')} mV")
        print(f"Faults:        {h.get('faults', '?')}")
        print(f"CAN TX-Fehler: {h.get('can_send_errs', '?')}")
        print(f"CAN RX-Fehler: {h.get('can_rx_errs', '?')}")
        print()

        # ALLOUTPUT-Test
        print("Teste ALLOUTPUT-Modus ...")
        try:
            p.set_safety_mode(17)  # SAFETY_ALLOUTPUT
            h2 = p.health()
            mode_after = h2.get("safety_mode", h2.get("safety_model", "?"))
            if mode_after == 17:
                print("  [OK] ALLOUTPUT funktioniert — Standard-Firmware unterstützt Senden")
            else:
                print(f"  ? Safety-Mode nach set: {mode_after}")
        except Exception as e:
            print(f"  [FAIL] ALLOUTPUT fehlgeschlagen: {e}")

        # Zurück auf SILENT
        p.set_safety_mode(0)

        # Prüfe ob Debug-Firmware (ALLOW_DEBUG) -> VOLKSWAGEN_MEB (Mode 34)
        print("\nPrüfe ALLOW_DEBUG (VOLKSWAGEN_MEB = Mode 34, deckt auch Golf 8 / Cupra Leon ab) ...")
        try:
            p.set_safety_mode(34)
            h3 = p.health()
            mode_after = h3.get("safety_mode", h3.get("safety_model", "?"))
            if mode_after == 34:
                print("  [OK] Debug-Firmware aktiv — VOLKSWAGEN_MEB Safety-Modus verfügbar!")
                print("    -> check_relay aktiv (HCA_03 Relay-Blocking wenn ACC engaged)")
                print("    -> HINWEIS: TX braucht controls_allowed (Motor_51 vom Fahrzeug)")
            else:
                print(f"  ? Modus nach set: {mode_after} (erwartet 34)")
        except Exception as e:
            print(f"  [FAIL] VOLKSWAGEN_MEB Modus nicht verfügbar: {e}")
            print("    -> Standard-Firmware oder Health-Mismatch — neu flashen mit firmware/panda_h7.bin.signed")

        p.set_safety_mode(0)
        p.close()

    print("\nFazit:")
    print("  Für Lenktest:         ALLOUTPUT reicht (Standard-Firmware)")
    print("  Für check_relay:      Debug-Firmware mit ALLOW_DEBUG nötig")
    print("  Zum Flashen:          python scripts/flash_panda.py")


if __name__ == "__main__":
    main()


