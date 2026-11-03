"""Modo sin interfaz (systemd). Toda la lógica está en NucleoMonitoreo.tick()."""
import logging
import time

from .nucleo import NucleoMonitoreo, sd_notify

logger = logging.getLogger("DavisMonitor")


def ejecutar_headless(core: NucleoMonitoreo) -> None:
    logger.info("Modo headless")
    core.iniciar_lector()
    sd_notify("READY=1")
    estado_prev = None
    while not core.shutdown_event.is_set():
        alerta = core.tick()
        if alerta != estado_prev:
            if alerta:
                logger.warning(f"ALERTA CLIMA ACTIVA: {alerta}")
            elif estado_prev:
                logger.info(f"Alerta de clima {estado_prev} finalizada")
            estado_prev = alerta
        # wait() en vez de sleep(): SIGTERM corta la espera al instante
        core.shutdown_event.wait(max(0.2, 1.0 - (time.time() % 1.0)))
