#!/usr/bin/env python3
"""
Panda Firmware Flasher

Flasht die in pandacan gebündelte Firmware auf den Panda.
Die pandacan-Version bestimmt welche Firmware-Version geflasht wird.

WARNUNG für Nachbauten / Clones
─────────────────────────────────
Vor dem Flashen sicherstellen dass der Clone STM32F413 oder STM32F4xx verwendet
(gleich wie Original Red Panda). Bei abweichender Hardware → Brick-Risiko.

Ablauf
──────
1. Panda in Normalbetrieb verbinden
2. Script startet → zeigt aktuelle Firmware
3. Bestätigung abwarten
4. Panda geht in Bootstub-Modus (blinkt anders)
5. Neue Firmware wird geflasht
6. Panda startet neu

Debug-Firmware (ALLOW_DEBUG)
──────────────────────────────
Für VW-spezifische Safety-Modi (VOLKSWAGEN_MEB, VOLKSWAGEN_MQB_EVO) mit
check_relay-Support wird eine mit ALLOW_DEBUG kompilierte Firmware benötigt.
Die Standard-pandacan-Firmware hat das NICHT.

Optionen:
  a) Offizielle Comma-Firmware mit ALLOW_DEBUG:
     https://github.com/commaai/panda → selbst kompilieren mit ALLOW_DEBUG=1
  b) Sunnypilot-Fork (hat ggf. andere Features):
     Separate Releases auf GitHub
  c) Für einfache Tests: Standard-Firmware + ALLOUTPUT reicht!
"""

from __future__ import annotations

import sys
import time


def main() -> None:
    try:
        from panda import Panda  # type: ignore
    except ImportError:
        sys.exit("pandacan nicht installiert — bitte: pip install pandacan")

    print("Suche Panda …")
    serials = Panda.list()
    if not serials:
        sys.exit("Kein Panda gefunden! USB-Kabel prüfen.")

    serial = serials[0]
    print(f"Gefunden: {serial}\n")

    p = Panda(serial=serial)
    current_version = p.get_version()
    hw_type = p.get_type()
    hw_int  = hw_type[0] if isinstance(hw_type, (bytes, bytearray)) else hw_type
    p.close()

    print(f"Aktuelle Firmware : {current_version}")
    print(f"Hardware-Typ      : {hw_int} (7 = Red Panda, 8 = Red Panda v2)")
    print()

    if hw_int not in (7, 8):
        print(f"WARNUNG: Hardware-Typ {hw_int} ist kein bekannter Red Panda!")
        print("         Flashen auf unbekannter Hardware kann den Panda bricken.")
        print("         Mit [Enter] trotzdem fortfahren, mit Ctrl+C abbrechen.")
        try:
            input()
        except KeyboardInterrupt:
            print("Abgebrochen.")
            return

    print("Was soll geflasht werden?")
    print("  [1] Standard-Firmware (aus pandacan-Paket) — ALLOUTPUT ja, ALLOW_DEBUG NEIN")
    print("  [2] Firmware aus Datei (z.B. selbst kompiliert mit ALLOW_DEBUG)")
    print("  [3] Abbrechen")
    print()

    choice = input("Wahl [1/2/3]: ").strip()

    if choice == "3" or choice == "":
        print("Abgebrochen.")
        return

    fw_path = None
    if choice == "2":
        fw_path = input("Pfad zur .bin-Datei: ").strip()
        if not fw_path:
            print("Kein Pfad angegeben, abgebrochen.")
            return

    print()
    print("LETZTE WARNUNG:")
    print("  Das Flashen löscht die aktuelle Firmware.")
    print("  Panda wird kurz nicht erreichbar sein.")
    print("  Bei Clones: Nur fortfahren wenn Hardware-Kompatibilität bekannt ist.")
    print()
    confirm = input("Jetzt flashen? (ja/nein): ").strip().lower()
    if confirm not in ("ja", "j", "yes", "y"):
        print("Abgebrochen.")
        return

    print("\nVerbinde erneut und starte Flash …")
    p = Panda(serial=serial)

    try:
        if fw_path:
            print(f"Flashe aus Datei: {fw_path}")
            p.flash(fw_path)
        else:
            print("Flashe Standard-Firmware aus pandacan …")
            p.flash()
    except Exception as e:
        print(f"\nFehler beim Flashen: {e}")
        print("Falls der Panda nicht mehr reagiert: DFU-Modus versuchen.")
        print("  → USB trennen, BOOT0-Taster halten, USB wieder stecken")
        print("  → dann: python -m panda.flash_release")
        return

    print("\nFlash abgeschlossen! Panda startet neu …")
    time.sleep(3)

    # Verify
    new_serials = Panda.list()
    if new_serials:
        p2 = Panda(new_serials[0])
        new_version = p2.get_version()
        p2.close()
        print(f"Neue Firmware: {new_version}")
        print("\nFertig. Zum Testen: python scripts/panda_info.py")
    else:
        print("Panda nicht gefunden nach Neustart — kurz warten und USB neu stecken.")


if __name__ == "__main__":
    main()
