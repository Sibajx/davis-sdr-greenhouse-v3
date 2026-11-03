"""Fuente de datos para la TUI cuando el receptor YA corre como servicio.

El servicio es dueño del dongle; esta clase NO abre rtldavis. Lee lo que el
servicio publica (estado/estado.json cada 5 s y la tabla `resumen` de SQLite
para las tendencias) y expone la misma interfaz que NucleoMonitoreo usa la TUI
(tick, obtener_instantanea, shutdown_event…). Salir de la TUI no detiene nada.
"""
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

from . import config
from .modelos import AnalizadorTendencias, EstadoEstacion, EstadoSDR

_MAPA_ESTACION = {   # clave en estado.json → campo de EstadoEstacion
    "temp_c": "temp", "humedad_pct": "humedad", "viento_kmh": "viento_vel", "viento_dir_grados": "viento_dir",
    "rafaga_kmh": "rafaga", "lluvia_tasa_mm_h": "lluvia_tasa", "uv": "uv", "solar_w_m2": "solar",
}


def _inicio_servicio(nombre: str = "davis-monitor") -> Optional[float]:
    """Hora de arranque del servicio (para el UPTIME), sin sudo."""
    try:
        r = subprocess.run(["systemctl", "show", nombre, "-p", "ActiveEnterTimestampMonotonic", "-p", "ActiveState"],
                           capture_output=True, text=True, timeout=3)
        d = dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)
        if d.get("ActiveState") != "active":
            return None
        mono = int(d.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
        return time.time() - (time.monotonic() - mono) if mono else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class NucleoRemoto:
    etiqueta_modo = "SERVICIO"

    def __init__(self):
        self.shutdown_event = threading.Event()
        self.hora_inicio = _inicio_servicio() or time.time()
        self.est, self.sdr = EstadoEstacion(), EstadoSDR(estado="Leyendo servicio…")
        self.flecha_temp = self.flecha_viento = "─"
        self.hist_temp, self.hist_viento = [], []
        self._tt, self._tv = AnalizadorTendencias(), AnalizadorTendencias()
        self._ultimo_json = self._ultimo_hist = 0.0
        self._ultima_actualizacion = None
        self.lock = threading.Lock()

    # la TUI llama a esto al arrancar; aquí no hay lector propio
    def iniciar_lector(self):
        return None

    def _leer_estado(self, ahora: float) -> None:
        try:
            edad = ahora - os.path.getmtime(config.ARCHIVO_ESTADO_JSON)
            with open(config.ARCHIVO_ESTADO_JSON, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            self.sdr.estado = "SIN DATOS: ¿servicio detenido?"
            self.sdr.estado_csv = f"No se puede leer {config.ARCHIVO_ESTADO_JSON}"
            return
        e, s = d.get("estacion", {}), d.get("sdr", {})
        est = EstadoEstacion()
        for k_json, campo in _MAPA_ESTACION.items():
            if e.get(k_json) is not None:
                est.valores[campo] = e[k_json]
                est.ts[campo] = ahora - ((e.get("edad_seg") or {}).get(campo) or 0)
        est.bateria_baja = None if e.get("bateria_tx") is None else e["bateria_tx"] == "Baja"
        est.lluvia_hoy_mm = e.get("lluvia_hoy_mm") or 0.0
        est.ultimo_paquete = ahora - (s.get("segundos_sin_paquetes") or 0)
        if est.valores["temp"] != self.est.valores.get("temp"):
            self.flecha_temp = self._tt.registrar(est.valores["temp"])
        if est.valores["viento_vel"] != self.est.valores.get("viento_vel"):
            self.flecha_viento = self._tv.registrar(est.valores["viento_vel"])
        sdr = EstadoSDR(
            estado=("FUERA DE LÍNEA · " if edad > 60 else "") + str(s.get("estado", "--")),
            freq_mhz=s.get("freq_mhz"), canal=s.get("canal"), error_freq_hz=s.get("error_freq_hz"),
            paquetes=s.get("paquetes_validos", 0), descartes_crc=s.get("descartes_crc", 0),
            descartes_id=s.get("descartes_id", 0), reinicios=s.get("reinicios", 0),
            ultimo_reinicio=s.get("ultimo_reinicio", "Nunca"), ultima_sync_seg=s.get("ultima_sync_seg"),
            ultima_trama=f"(la trama cruda la tiene el servicio) · errores USB: {s.get('errores_usb', 0)}",
            estado_csv=(f"Conectado al servicio · estado de hace {edad:.0f} s · calidad "
                        f"{s.get('calidad_ultima_ventana_pct', '--')}% · q = salir (el servicio sigue)"))
        with self.lock:
            self.est, self.sdr = est, sdr

    def _leer_historial(self, ahora: float) -> None:
        if not os.path.isfile(config.ARCHIVO_DB):
            return
        try:
            c = sqlite3.connect(f"file:{config.ARCHIVO_DB}?mode=ro", uri=True, timeout=3)
            filas = c.execute("SELECT temp_c, viento_prom_kmh FROM resumen WHERE ts_unix > ? ORDER BY ts_unix",
                              (ahora - 4 * 3600,)).fetchall()
            c.close()
        except sqlite3.Error:
            return
        with self.lock:
            self.hist_temp = [f[0] for f in filas if f[0] is not None]
            self.hist_viento = [f[1] for f in filas if f[1] is not None]

    def tick(self, ahora: float = None) -> Optional[str]:
        ahora = time.time() if ahora is None else ahora
        if ahora - self._ultimo_json >= 2:
            self._ultimo_json = ahora
            self._leer_estado(ahora)
        if ahora - self._ultimo_hist >= 60:
            self._ultimo_hist = ahora
            self._leer_historial(ahora)
        t = self.est.valores.get("temp")
        if t is not None and t <= config.UMBRAL_HELADA_C:
            return "HELADA"
        tasa = self.est.valores.get("lluvia_tasa")
        if config.PLUVIOMETRO_ACTIVO and tasa is not None and tasa >= config.UMBRAL_TORMENTA_MM_H:
            return "TORMENTA"
        return None

    def obtener_instantanea(self):
        with self.lock:
            return (self.est.clonar(), self.sdr.clonar(), self.flecha_temp, self.flecha_viento,
                    list(self.hist_temp), list(self.hist_viento))

    def finalizar(self) -> None:
        self.shutdown_event.set()
