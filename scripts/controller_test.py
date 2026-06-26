#!/usr/bin/env python3
"""Controller-Test ohne Panda/Fahrzeug.

Zeigt alle Achsen und Buttons des ersten gefundenen Controllers an.
Ctrl+C zum Beenden.

Verwendung:
    python scripts/controller_test.py
"""
import sys
import time


def normalize_trigger(val: float) -> float:
    return max(0.0, min(1.0, (val + 1.0) / 2.0))


def main() -> None:
    try:
        import pygame
    except ImportError:
        sys.exit("pygame nicht gefunden -- bitte: pip install pygame")

    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        sys.exit("Kein Controller gefunden!")

    joy = pygame.joystick.Joystick(0)
    joy.init()
    print(f"Controller: {joy.get_name()}")
    print(f"Achsen: {joy.get_numaxes()}   Buttons: {joy.get_numbuttons()}")
    print()
    print("Xbox-Erwartung:")
    print("  Axis 0  = Linker Stick X  (-1=links, +1=rechts)")
    print("  Axis 2  = Linker Trigger  (-1=los,   +1=voll)")
    print("  Axis 5  = Rechter Trigger (-1=los,   +1=voll)")
    print("  Button 0 = A,  Button 1 = B")
    print()
    print("Ctrl+C zum Beenden.")
    print()

    try:
        while True:
            pygame.event.pump()

            axes = [joy.get_axis(i) for i in range(joy.get_numaxes())]
            btns = [joy.get_button(i) for i in range(joy.get_numbuttons())]

            steer  = axes[0] if len(axes) > 0 else 0.0
            lt     = normalize_trigger(axes[2]) if len(axes) > 2 else 0.0
            rt     = normalize_trigger(axes[5]) if len(axes) > 5 else 0.0
            btn_a  = btns[0] if len(btns) > 0 else 0
            btn_b  = btns[1] if len(btns) > 1 else 0

            pressed_btns = [str(i) for i, v in enumerate(btns) if v]

            print(
                f"\r  Lenkung(Ax0):{steer:+.2f}  "
                f"Bremse(LT/Ax2):{lt:.2f}  "
                f"Gas(RT/Ax5):{rt:.2f}  "
                f"A:{btn_a} B:{btn_b}  "
                f"Alle Axes:[{', '.join(f'{a:+.2f}' for a in axes)}]  "
                f"Buttons:[{','.join(pressed_btns) or '-'}]   ",
                end="", flush=True,
            )

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nFertig.")
        pygame.quit()


if __name__ == "__main__":
    main()
