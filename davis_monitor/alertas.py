"""Alertas por Telegram (y, opcional, un webhook JSON genérico) con un único hilo de
envío, cola, reintentos y vaciado al apagar (en v2.3 el aviso de "apagado" se
lanzaba en un hilo daemon que moría con el proceso y casi nunca llegaba).

El token del bot nunca va en el repo ni en el log: se lee de config/entorno o de un
archivo KEY=valor (`telegram_env`, p. ej. ~/.config/sibajx-bot/telegram.env, modo 600)."""
import json
import os
import queue
import logging
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

from . import config

logger = logging.getLogger("DavisMonitor")

PREFIJO = "[davis-monitor] "
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_SILENCIOSAS = ("low", "min")   # llegan sin sonido


def calcular_backoff(fallos: int) -> float:
    if fallos <= 0:
        return config.BACKOFF_INICIAL_SEG
    return min(config.BACKOFF_INICIAL_SEG * (2 ** fallos), config.BACKOFF_MAX_SEG)


def _leer_env(ruta: str) -> dict:
    out = {}
    try:
        with open(os.path.expanduser(ruta), encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                k, v = linea.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def credenciales_telegram() -> tuple:
    token, chat = config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID
    if (not token or not chat) and config.TELEGRAM_ENV:
        env = _leer_env(config.TELEGRAM_ENV)
        token = token or env.get("TOKEN") or env.get("TELEGRAM_TOKEN", "")
        chat = chat or env.get("CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
    return (token, chat) if token and chat else ("", "")


def hay_canal() -> bool:
    return bool(credenciales_telegram()[0] or config.WEBHOOK_URL)


def _ocultar(texto: str) -> str:
    token = credenciales_telegram()[0]
    return texto.replace(token, "***") if token else texto


def _post(mensaje: str, prioridad: str = "default", tags: str = "") -> None:
    token, chat = credenciales_telegram()
    if token:
        campos = {"chat_id": chat, "text": PREFIJO + mensaje}
        if prioridad in _SILENCIOSAS:
            campos["disable_notification"] = "true"
        req = urllib.request.Request(_TELEGRAM_API.format(token=token),
                                     data=urllib.parse.urlencode(campos).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=config.WEBHOOK_TIMEOUT_SEG):
            pass
    if config.WEBHOOK_URL:
        data = json.dumps({"texto": mensaje, "origen": "davis_monitor",
                           "hora": datetime.now().isoformat(timespec="seconds"),
                           "prioridad": prioridad, "tags": tags}).encode("utf-8")
        req = urllib.request.Request(config.WEBHOOK_URL, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=config.WEBHOOK_TIMEOUT_SEG):
            pass


class Notificador:
    def __init__(self, reintentos: int = 3):
        self.reintentos = reintentos
        self._cola: "queue.Queue" = queue.Queue(maxsize=200)
        self._hilo = None
        self.enviados = 0
        self.fallidos = 0
        if hay_canal():
            self._hilo = threading.Thread(target=self._bucle, name="Alertas", daemon=True)
            self._hilo.start()

    def enviar(self, mensaje: str, prioridad: str = "default", tags: str = "") -> None:
        logger.info(f"ALERTA: {mensaje}")
        if not self._hilo:
            return
        try:
            self._cola.put_nowait((mensaje, prioridad, tags))
        except queue.Full:
            logger.warning("Cola de alertas llena; alerta descartada")

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
                        logger.warning(f"Alerta no enviada tras {self.reintentos} intentos: {_ocultar(str(e))}")
                    else:
                        time.sleep(2 ** intento)

    def cerrar(self, timeout: float = 8.0) -> None:
        if self._hilo:
            self._cola.put(None)
            self._hilo.join(timeout)


def probar_webhook(mensaje: str = "Mensaje de prueba desde davis_monitor") -> bool:
    """Manda una alerta de prueba por los canales configurados (`--test-webhook`)."""
    if not hay_canal():
        print("No hay canal de alertas: configura telegram_env (o telegram_token + telegram_chat_id).")
        return False
    canales = (["Telegram"] if credenciales_telegram()[0] else []) + (["webhook JSON"] if config.WEBHOOK_URL else [])
    print(f"Enviando prueba por: {', '.join(canales)}")
    try:
        _post(mensaje, "default")
        print("OK: la alerta salió sin errores.")
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"ERROR enviando la alerta: {_ocultar(str(e))}")
        return False
