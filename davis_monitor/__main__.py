"""Uso:
    python3 -m davis_monitor              # interfaz de texto (curses)
    python3 -m davis_monitor --headless   # servicio systemd
    python3 -m davis_monitor --visor      # visor de solo lectura de estado.json
    python3 -m davis_monitor --tui-servicio   # la TUI completa conectada al servicio (no usa el dongle)
    python3 -m davis_monitor --test-webhook
    python3 -m davis_monitor --replay archivo.txt   # procesa una captura de rtldavis sin dongle
"""
import argparse
import logging
import signal
import sys
import time

from . import config, __version__

logger = logging.getLogger("DavisMonitor")


def main() -> None:
    p = argparse.ArgumentParser(description="Monitor Davis Vantage Pro 2 vía SDR")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--headless", action="store_true", help="sin interfaz (systemd)")
    g.add_argument("--visor", action="store_true", help="visor de solo lectura de estado.json")
    g.add_argument("--tui-servicio", action="store_true",
                   help="interfaz de texto conectada al servicio en marcha (no abre el dongle)")
    g.add_argument("--test-webhook", action="store_true")
    g.add_argument("--replay", metavar="ARCHIVO", help="procesa una salida guardada de rtldavis")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args()

    if a.test_webhook:
        from .alertas import probar_webhook
        sys.exit(0 if probar_webhook() else 1)
    if a.visor:
        from .visor import ejecutar_visor
        ejecutar_visor()
        return
    if a.tui_servicio:
        import curses
        from .remoto import NucleoRemoto
        from .tui import MotorInterfazTUI
        try:
            curses.wrapper(MotorInterfazTUI(NucleoRemoto()).ejecutar)
        except KeyboardInterrupt:
            pass
        print("TUI cerrada. El servicio sigue capturando (systemctl status davis-monitor).")
        return

    config.preparar_entorno(log_a_stderr=None if a.headless or a.replay else False)
    from .nucleo import NucleoMonitoreo, sd_notify
    monitor = NucleoMonitoreo()

    def _senal(signum, _frame):
        logger.info(f"Señal {signal.Signals(signum).name} recibida: apagando")
        monitor.shutdown_event.set()

    signal.signal(signal.SIGTERM, _senal)
    signal.signal(signal.SIGINT, _senal)
    signal.signal(signal.SIGHUP, _senal)

    logger.info(f"===== INICIO davis_monitor {__version__} =====")
    logger.info(f"Configuración: {config.resumen_config()}")
    try:
        if a.replay:
            with open(a.replay, encoding="utf-8", errors="replace") as f:
                n = sum(1 for linea in f if monitor.procesar_linea(linea))
            monitor.tick(time.time())
            print(f"Replay: {n} tramas válidas procesadas")
            return
        monitor.notif.enviar("Sistema Davis iniciado", "low", "rocket")
        if a.headless:
            from .headless import ejecutar_headless
            ejecutar_headless(monitor)
        else:
            import curses
            from .tui import MotorInterfazTUI
            curses.wrapper(MotorInterfazTUI(monitor).ejecutar)
    finally:
        sd_notify("STOPPING=1")
        if not a.replay:
            monitor.notif.enviar("Sistema Davis apagado", "default", "octagonal_sign")
        monitor.finalizar()
        logger.info("===== APAGADO =====")


if __name__ == "__main__":
    main()
