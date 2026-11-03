"""Alertas por webhook (json genérico o ntfy) con un único hilo de envío,
cola, reintentos, y vaciado al apagar (en v2.3 el aviso de "apagado" se
lanzaba en un hilo daemon que moría con el proceso y casi nunca llegaba)."""
import json
import queue
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime

from . import config

logger = logging.getLogger("DavisMonitor")


def calcular_backoff(fallos: int) -> float:
    if fallos <= 0:
        return config.BACKOFF_INICIAL_SEG
    return min(config.BACKOFF_INICIAL_SEG * (2 ** fallos), config.BACKOFF_MAX_SEG)


def _post(mensaje: str, prioridad: str = "default", tags: str = "") -> None:
    if config.WEBHOOK_FORMATO == "ntfy":
        headers = {"Title": "Estacion Davis", "Content-Type": "text/plain; charset=utf-8"}
        if prioridad:
            headers["Priority"] = prioridad
        if tags:
            headers["Tags"] = tags
        data = mensaje.encode("utf-8")
    else:
        headers = {"Content-Type": "application/json"}
        data = json.dumps({"texto": mensaje, "origen": "davis_monitor",
                           "hora": datetime.now().isoformat(timespec="seconds"),
                           "prioridad": prioridad, "tags": tags}).encode("utf-8")
    req = urllib.request.Request(config.WEBHOOK_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.WEBHOOK_TIMEOUT_SEG):
        pass


class Notificador:
    def __init__(self, reintentos: int = 3):
        self.reintentos = reintentos
        self._cola: "queue.Queue" = queue.Queue(maxsize=200)
        self._hilo = None
        self.enviados = 0
        self.fallidos = 0
        if config.WEBHOOK_URL:
            self._hilo = threading.Thread(target=self._bucle, name="Webhook", daemon=True)
            self._hilo.start()

    def enviar(self, mensaje: str, prioridad: str = "default", tags: str = "") -> None:
        logger.info(f"ALERTA: {mensaje}")
        if not self._hilo:
            return
        try:
            self._cola.put_nowait((mensaje, prioridad, tags))
        except queue.Full:
            logger.warning("Cola de webhook llena; alerta descartada")

    def _bucle(self) -> None:
        import time
        while True:
            item = self._cola.get()
            if item is None:
                return
            for intento in range(self.reintentos):
                try:
                    _post(*item)
                    self.enviados += 1
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    if intento == self.reintentos - 1:
                        self.fallidos += 1
                        logger.warning(f"Webhook falló tras {self.reintentos} intentos: {e}")
                    else:
                        time.sleep(2 ** intento)

    def cerrar(self, timeout: float = 8.0) -> None:
        if self._hilo:
            self._cola.put(None)
            self._hilo.join(timeout)


def probar_webhook(mensaje: str = "Mensaje de prueba desde davis_monitor") -> bool:
    if not config.WEBHOOK_URL:
        print("DAVIS_WEBHOOK_URL no está configurada — nada que probar.")
        return False
    print(f"Enviando prueba a: {config.WEBHOOK_URL}")
    try:
        _post(mensaje, "default", "test_tube")
        print("OK: el webhook respondió sin errores.")
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"ERROR enviando el webhook: {e}")
        return False
