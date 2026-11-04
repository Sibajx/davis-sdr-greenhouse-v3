"""Núcleo: lanza y supervisa rtldavis, decodifica tramas, mantiene el
estado compartido y ejecuta las tareas periódicas (CSV, JSON, heartbeat,
alertas, watchdog). TUI y headless llaman al MISMO `tick()` — en v2.3 ese
bloque estaba copiado en los dos archivos.

Cambios de diseño principales frente a v2.3:
  * Lector bloqueante `for linea in stdout` en su propio hilo (sin
    select() sobre un TextIOWrapper con buffer, que podía retrasar líneas).
  * Watchdog por PAQUETES válidos y en dos fases (150 s sincronizando /
    90 s sincronizado) en vez de "30 s sin líneas": rtldavis está callado
    hasta 135 s mientras sincroniza y el watchdog viejo lo mataba antes de
    lograrlo (los 65 huecos >30 s de v2.3 contienen todos un reinicio).
  * El lock solo protege memoria; SQLite y webhooks van por colas.
"""
import os
import json
import math
import time
import signal
import socket
import logging
import threading
import subprocess
from collections import deque
from datetime import datetime
from typing import Optional

from . import config, protocolo
from .modelos import EstadoEstacion, EstadoSDR, AnalizadorTendencias, AgregadorVentana, historial
from .basedatos import EscritorSQLite, fila_evento, lluvia_acumulada_desde
from .almacenamiento import GestorCSV, _fmt
from .alertas import Notificador, calcular_backoff
from .rutas import Rutas

logger = logging.getLogger("DavisMonitor")
VERSION = "3.2.0"
MAX_VUELCOS_POR_TRAMA = 30  # salto mayor = contador re-sincronizado, no lluvia real


# ---------------------------------------------------------------- systemd
def sd_notify(mensaje: str) -> None:
    """Protocolo sd_notify sin dependencias (Type=notify + WatchdogSec)."""
    destino = os.environ.get("NOTIFY_SOCKET")
    if not destino:
        return
    if destino.startswith("@"):
        destino = "\0" + destino[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(destino)
            s.sendall(mensaje.encode())
    except OSError:
        pass


try:  # libc se carga en el padre: en el hijo (tras fork) no se debe importar nada
    import ctypes
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except (ImportError, OSError):  # pragma: no cover
    _LIBC = None


def _pdeathsig():  # pragma: no cover - corre en el hijo
    """Si Python muere (incluso kill -9), el kernel le manda SIGTERM a
    rtldavis: evita dejar el dongle ocupado por un proceso huérfano."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def _inicio_del_dia(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class NucleoMonitoreo:
    def __init__(self, directorio: str = None, notificador: Notificador = None,
                 escritor: EscritorSQLite = None, gestor_csv: GestorCSV = None):
        self.rutas = Rutas(directorio).crear() if directorio else config.RUTAS.crear()
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.hilo_sdr_detenido = threading.Event()
        self.hora_inicio = time.time()

        self.est = EstadoEstacion()
        self.sdr = EstadoSDR()
        self.ventana = AgregadorVentana()
        self.tend_temp, self.tend_viento = AnalizadorTendencias(), AnalizadorTendencias()
        self.flecha_temp = self.flecha_viento = "─"
        self.hist_temp, self.hist_viento = historial(), historial()

        self.notif = notificador or Notificador()
        ruta_db = self.rutas.db
        self.db = escritor if escritor is not None else (
            EscritorSQLite(ruta_db, config.INTERVALO_COMMIT_SEG) if config.GUARDAR_TRAMAS_SQLITE else None)
        self.csv = gestor_csv or GestorCSV(self.rutas)

        self._dia = _inicio_del_dia(self.hora_inicio)
        self.est.lluvia_hoy_mm = lluvia_acumulada_desde(ruta_db, self._dia)

        self.proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._sincronizado = False
        self._paquetes_desde_inicio_proceso = 0
        self.errores_usb = 0
        self._alerta_dongle = False

        ahora = time.time()
        self._proxima_csv = math.ceil(ahora / config.INTERVALO_CSV_SEG) * config.INTERVALO_CSV_SEG
        self._ultimo_heartbeat = ahora
        self._ultimo_json = 0.0
        self._ultima_compresion = 0.0
        self._ultima_calidad: Optional[float] = None
        self._t_valida: Optional[float] = None   # hora de la última trama válida (ancla de ranuras)
        self._crc_pend: list = []                 # horas de CRC fallidos aún sin clasificar
        self.fases_crc: deque = deque(maxlen=4000)  # diagnóstico: fase de cada CRC respecto a la ranura (s)
        self._enlace_prev = self._contadores_enlace()
        self._enlace_inicio = ahora
        self._freq_err = [0.0, 0]          # suma, n de los hops de la ventana
        self.enlace_ultima: dict = {}      # última ventana cerrada (para estado.json)

        self._alerta = {"helada": False, "tormenta": False, "sin_datos": False}
        self._ultima_alerta_bateria = 0.0
        self._reinicios_ts: deque = deque(maxlen=20)
        self._ultima_alerta_enlace = 0.0

    # ------------------------------------------------------------ decodificación
    def procesar_linea(self, linea: str, ahora: float = None) -> Optional[protocolo.Lectura]:
        ahora = time.time() if ahora is None else ahora
        linea = linea.strip()
        if not linea:
            return None
        clase, dato = protocolo.clasificar_linea(linea)
        with self.lock:
            self.sdr.ultima_linea = ahora
        if clase == "paquete":
            return self.procesar_trama(dato, ahora)
        if clase == "usb_error":
            # rtldavis queda colgado tras perder el USB: matarlo ya, sin esperar al watchdog
            with self._proc_lock:
                p = self.proc
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        with self.lock:
            if clase == "hop":
                self.sdr.canal, freq, self.sdr.error_freq_hz = dato[0], dato[1], dato[2]
                self._freq_err[0] += dato[2]
                self._freq_err[1] += 1
                self.sdr.freq_mhz = round(freq / 1e6, 3)
            elif clase == "init":
                self.sdr.estado = "Sincronizando"
            elif clase == "sync":
                self.sdr.estado = "Recibiendo"
            elif clase == "crc":
                self.sdr.descartes_crc += 1
                if dato:                       # mismo bloque que una trama válida
                    self.sdr.crc_con_copia += 1
                elif self._t_valida is None:   # sincronizando: rtldavis escucha a ciegas, es ruido
                    self.sdr.crc_ruido += 1
                else:
                    self._crc_pend.append(ahora)
                    if len(self._crc_pend) > 5000:   # sin válidas por mucho tiempo
                        self.sdr.crc_ruido += len(self._crc_pend)
                        self._crc_pend.clear()
            elif clase == "perdida":
                self.sdr.perdidas += 1
            elif clase == "version":
                self.sdr.version_rtldavis = dato
                self.sdr.crc_disponible = "+crc" in dato
                if not self.sdr.crc_disponible:
                    logger.info(f"rtldavis {dato} sin el parche de CRC: el monitor de CRC solo verá la "
                                "validación propia (ver deploy/rtldavis/)")
            elif clase == "usb_error":
                self.sdr.estado = "ERROR USB"
                self.errores_usb += 1
                logger.error(f"rtldavis: {linea} — dongle desconectado, ocupado o con falso contacto")
            else:
                self.sdr.lineas_otras += 1
                logger.debug(f"rtldavis: {linea[:150]}")
        return None

    def procesar_trama(self, datos: bytes, ahora: float = None) -> Optional[protocolo.Lectura]:
        ahora = time.time() if ahora is None else ahora
        if not protocolo.validar_crc(datos):
            with self.lock:
                self.sdr.descartes_crc += 1
                self.sdr.crc_ruido += 1
            return None
        lec = protocolo.decodificar(datos, config.TAMANO_CUBETA_MM)
        if lec is None:   # CRC correcto pero contenido imposible (ceros, tipo inexistente)
            with self.lock:
                self.sdr.invalidas += 1
            return None
        if lec.tx_id != config.TX_ID_OBJETIVO:
            with self.lock:
                self.sdr.descartes_id += 1
            return None
        if not config.PLUVIOMETRO_ACTIVO:
            lec.valores.pop("lluvia_tasa", None)
            lec.valores.pop("lluvia_contador", None)

        alerta_bat = None
        with self.lock:
            lluvia_mm = self._lluvia_desde_contador(lec)
            v = lec.valores
            self.est.actualizar(v, ahora)
            self.est.tx_id = lec.tx_id
            self.est.ultimo_paquete = ahora
            self.ventana.registrar(v, lluvia_mm)
            self.sdr.paquetes += 1
            self._resolver_ranuras(ahora)
            self.sdr.ultima_trama = lec.trama_hex
            self.sdr.estado = "Recibiendo"
            self._paquetes_desde_inicio_proceso += 1
            if not self._sincronizado:
                self._sincronizado = True
                self.sdr.ultima_sync_seg = round(ahora - self.sdr.inicio_proceso, 1)
                logger.info(f"rtldavis sincronizado: primer paquete a los {self.sdr.ultima_sync_seg} s")
            if v.get("viento_vel") is not None:
                self.hist_viento.append(v["viento_vel"])
                self.flecha_viento = self.tend_viento.registrar(v["viento_vel"])
            if v.get("temp") is not None:
                self.hist_temp.append(v["temp"])
                self.flecha_temp = self.tend_temp.registrar(v["temp"])
            anterior = self.est.bateria_baja
            self.est.bateria_baja = lec.bateria_baja
            if anterior is not None and anterior != lec.bateria_baja:
                alerta_bat = lec.bateria_baja

        if self.db is not None:
            self.db.evento(fila_evento(ahora, lec, lluvia_mm))
        if alerta_bat is not None:
            self._alerta_bateria(alerta_bat, ahora)
        return lec

    def _lluvia_desde_contador(self, lec) -> float:
        """Convierte el contador rodante de vuelcos (0-127) en mm caídos."""
        c = lec.valores.get("lluvia_contador")
        if c is None:
            return 0.0
        c = int(c)
        prev, self.est.lluvia_contador = self.est.lluvia_contador, c
        if prev is None:
            return 0.0
        delta = (c - prev) % 128
        if delta > MAX_VUELCOS_POR_TRAMA:
            logger.warning(f"Salto del contador de lluvia {prev}->{c} ignorado (re-sincronización)")
            return 0.0
        mm = delta * config.TAMANO_CUBETA_MM
        self.est.lluvia_hoy_mm += mm
        return mm

    def _alerta_bateria(self, baja: bool, ahora: float) -> None:
        (logger.warning if baja else logger.info)(f"Batería del transmisor: {'BAJA' if baja else 'OK'}")
        if ahora - self._ultima_alerta_bateria > config.COOLDOWN_ALERTA_BATERIA_SEG:
            self._ultima_alerta_bateria = ahora
            self.notif.enviar("Batería baja en transmisor Davis" if baja else "Batería del transmisor OK",
                              "default" if baja else "low", "battery")

    # ------------------------------------------------------------ rtldavis
    def lector_rtldavis_worker(self) -> None:
        cmd = [config.RTLDAVIS_CMD] + config.RTLDAVIS_ARGS.split()
        fallos = 0
        logger.info(f"Hilo SDR: {' '.join(cmd)}")
        while not self.shutdown_event.is_set():
            inicio = time.time()
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                        errors="replace",
                                        preexec_fn=_pdeathsig if _LIBC is not None else None)
            except FileNotFoundError:
                logger.critical(f"No se encontró '{cmd[0]}' en el PATH. Hilo SDR detenido.")
                with self.lock:
                    self.sdr.estado = "ERROR: rtldavis no encontrado"
                self.hilo_sdr_detenido.set()
                return
            except OSError as e:
                logger.error(f"No se pudo lanzar rtldavis: {e}")
                proc = None

            if proc is not None:
                with self._proc_lock:
                    self.proc = proc
                with self.lock:
                    self.sdr.inicio_proceso = inicio
                    self.sdr.estado = "Sincronizando"
                    self._sincronizado = False
                    self._t_valida = None
                    self._paquetes_desde_inicio_proceso = 0
                logger.info(f"rtldavis iniciado (pid {proc.pid})")
                try:
                    for linea in proc.stdout:
                        self.procesar_linea(linea)
                        if self.shutdown_event.is_set():
                            break
                except Exception as e:  # noqa: BLE001 - el hilo no debe morir
                    logger.exception(f"Error leyendo rtldavis: {e}")
                finally:
                    self._detener_proceso(proc)
                    with self._proc_lock:
                        self.proc = None
                rc = proc.returncode
                if not self.shutdown_event.is_set():
                    logger.warning(f"rtldavis terminó (código {rc}) tras {time.time() - inicio:.0f} s")

            if self.shutdown_event.is_set():
                break
            with self.lock:
                util = self._paquetes_desde_inicio_proceso > 0
            fallos = 0 if (util or time.time() - inicio >= config.FALLO_RAPIDO_SEG) else fallos + 1
            if util:
                self._alerta_dongle = False
            espera = calcular_backoff(fallos)
            if fallos == 5 and not self._alerta_dongle:
                self._alerta_dongle = True
                self.notif.enviar("Davis: rtldavis no puede abrir el dongle RTL-SDR (¿desconectado?)",
                                  "high", "electric_plug,warning")
            if fallos >= 3:
                logger.warning(f"{fallos} caídas rápidas seguidas de rtldavis; espero {espera:.0f} s "
                               "(¿dongle desconectado u ocupado?)")
            self.shutdown_event.wait(espera)

    @staticmethod
    def _detener_proceso(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            except OSError:
                pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def forzar_reinicio_sdr(self, ahora: float, motivo: str) -> None:
        if self.hilo_sdr_detenido.is_set():
            return
        with self.lock:
            self.sdr.reinicios += 1
            self.sdr.ultimo_reinicio = datetime.fromtimestamp(ahora).strftime('%H:%M:%S')
            self.sdr.estado = "Reiniciando"
            self.sdr.inicio_proceso = ahora  # evita re-disparo mientras arranca el nuevo
            self._sincronizado = False
            self._t_valida = None
        logger.warning(f"Watchdog: reiniciando rtldavis ({motivo})")
        self._reinicios_ts.append(ahora)
        recientes = [t for t in self._reinicios_ts if ahora - t < 1800]
        if len(recientes) >= 3 and ahora - self._ultima_alerta_enlace > 3600:
            self._ultima_alerta_enlace = ahora
            self.notif.enviar(f"Davis: {len(recientes)} reinicios del enlace SDR en 30 min. "
                              "Revisar antena/dongle/estación.", "high", "satellite,warning")
        with self._proc_lock:
            p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------ tareas periódicas
    def tick(self, ahora: float = None) -> Optional[str]:
        """Llamar ~1 vez por segundo desde la interfaz. Devuelve la alerta
        climática activa ('HELADA' / 'TORMENTA' / None)."""
        ahora = time.time() if ahora is None else ahora
        with self.lock:
            sincronizado = self._sincronizado
            ref = self.est.ultimo_paquete if sincronizado else self.sdr.inicio_proceso
            proc_vivo = self.proc is not None
        limite = config.WATCHDOG_SIN_PAQUETES_SEG if sincronizado else config.WATCHDOG_SYNC_SEG
        if proc_vivo and ahora - ref > limite:
            self.forzar_reinicio_sdr(ahora, f"{ahora - ref:.0f} s sin paquetes "
                                     f"({'sincronía perdida' if sincronizado else 'no sincronizó'})")

        if _inicio_del_dia(ahora) != self._dia:
            with self.lock:
                logger.info(f"Cambio de día: lluvia de ayer {self.est.lluvia_hoy_mm:.1f} mm")
                self._dia = _inicio_del_dia(ahora)
                self.est.lluvia_hoy_mm = 0.0

        if ahora >= self._proxima_csv:
            self._guardar_ventana(ahora)
            self._proxima_csv = (math.floor(ahora / config.INTERVALO_CSV_SEG) + 1) * config.INTERVALO_CSV_SEG

        if ahora - self._ultimo_heartbeat >= config.INTERVALO_HEARTBEAT_SEG:
            self.generar_heartbeat(ahora)
        if ahora - self._ultimo_json >= config.INTERVALO_ESTADO_JSON_SEG:
            self.escribir_estado_json(ahora)
        if ahora - self._ultima_compresion >= config.INTERVALO_COMPRESION_SEG:
            self._ultima_compresion = ahora
            try:
                self.csv.comprimir_antiguos(config.DIAS_RETENER_SIN_COMPRIMIR,
                                            config.COMPRIMIR_HISTORIAL_RESUMEN)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"Error en compresión: {e}")

        alerta = self.evaluar_alertas(ahora)
        sd_notify(f"WATCHDOG=1\nSTATUS={self.sdr.estado} | {self.sdr.paquetes} paquetes")
        return alerta

    def datos_completos(self, ahora: float) -> bool:
        if ahora - self.hora_inicio > 300:
            return self.est.ultimo_paquete > 0
        req = [c.strip() for c in config.CAMPOS_REQUERIDOS.split(",") if c.strip()]
        return all(self.est.valores.get(c) is not None for c in req)

    def _resolver_ranuras(self, t: float) -> None:
        """Llamar con self.lock tomado, al llegar una trama válida en t.
        Entre la válida anterior y esta, las ranuras intermedias (cada periodo) no
        trajeron trama válida: si hubo un CRC fallido en ±tolerancia la transmisión
        llegó DAÑADA; si no, SIN RASTRO. Los CRC restantes son ruido o copias."""
        s, P, tol = self.sdr, config.periodo_tx_seg(), config.TOLERANCIA_RANURA_SEG
        prev, pend = self._t_valida, self._crc_pend
        self._t_valida, self._crc_pend = t, []
        if prev is None:
            s.crc_ruido += len(pend)
            return
        n = round((t - prev) / P)
        usados = set()
        for k in range(1, max(1, n)):
            ranura = prev + k * P
            hit = [i for i, tc in enumerate(pend) if abs(tc - ranura) <= tol]
            if hit:
                s.danadas += 1
                usados.update(hit)
            else:
                s.sin_rastro += 1
        for i, tc in enumerate(pend):
            # diagnóstico: fase respecto a la ranura más cercana (ancla = válida anterior)
            f = (tc - prev) % P
            self.fases_crc.append(round(f - P if f > P / 2 else f, 4))
            if i in usados:
                continue
            if abs(tc - t) <= tol:
                s.crc_con_copia += 1
            else:
                s.crc_ruido += 1

    def _histograma_fases(self) -> dict:
        """Diagnóstico de la tolerancia: CRC fallidos por fase respecto a la ranura
        (bins de 1 ms en ±20 ms). Las tramas dañadas se agolparían en 0; el ruido es plano."""
        h = {}
        for f in list(self.fases_crc):
            k = "fuera" if abs(f) > 0.02 else str(int(round(f * 1000)))
            h[k] = h.get(k, 0) + 1
        return h

    def _contadores_enlace(self) -> dict:
        s = self.sdr
        return {"validas": s.paquetes, "crc_fallos": s.descartes_crc, "crc_ruido": s.crc_ruido,
                "crc_con_copia": s.crc_con_copia, "danadas": s.danadas, "sin_rastro": s.sin_rastro,
                "perdidas": s.perdidas, "invalidas": s.invalidas, "otro_tx": s.descartes_id,
                "errores_usb": self.errores_usb, "reinicios": s.reinicios}

    def _cerrar_enlace(self, ahora: float) -> None:
        """Contadores del enlace de esta ventana (diferencias, no acumulados).
        Se registra aunque no haya datos meteorológicos: así se ven las caídas."""
        with self.lock:
            act = self._contadores_enlace()
            d = {k: act[k] - self._enlace_prev.get(k, 0) for k in act}
            self._enlace_prev = act
            fe = round(self._freq_err[0] / self._freq_err[1]) if self._freq_err[1] else None
            self._freq_err = [0.0, 0]
            crc_ok = self.sdr.crc_disponible
        seg = max(1.0, ahora - self._enlace_inicio)
        self._enlace_inicio = ahora
        # esperadas por RANURAS resueltas: válidas + dañadas + sin rastro (suma exacta, 100 %)
        esperadas = d["validas"] + d["danadas"] + d["sin_rastro"]
        self.enlace_ultima = dict(d, segundos=round(seg, 1), esperadas=esperadas, error_freq_hz=fe,
                                  crc_disponible=crc_ok)
        if self.db is not None:
            self.db.enlace((ahora, round(seg, 1), d["validas"], esperadas, d["danadas"], d["sin_rastro"],
                            d["crc_fallos"], d["crc_ruido"], d["crc_con_copia"], d["perdidas"], d["invalidas"],
                            d["otro_tx"], d["errores_usb"], d["reinicios"], fe, int(crc_ok)))

    def _guardar_ventana(self, ahora: float) -> None:
        self._cerrar_enlace(ahora)
        with self.lock:
            if not self.datos_completos(ahora):
                self.sdr.estado_csv = "Sincronizando..."
                self.ventana.reiniciar(ahora)
                return
            r = self.ventana.resumen()
            seg = ahora - self.ventana.inicio
            self.ventana.reiniciar(ahora)
            e = self.est
            edad = config.MAX_EDAD_DATO_SEG
            temp = r["temp_prom"] if r["temp_prom"] is not None else e.valor("temp", ahora, edad)
            hum = r["humedad_prom"] if r["humedad_prom"] is not None else e.valor("humedad", ahora, edad)
            lluvia_hoy, bateria = e.lluvia_hoy_mm, e.bateria_baja
        if not config.PLUVIOMETRO_ACTIVO:
            r["lluvia_mm"] = r["lluvia_tasa_max"] = lluvia_hoy = None
        esperados = max(1, round(seg / config.periodo_tx_seg()))
        calidad = round(min(100.0, 100.0 * r["paquetes"] / esperados), 1)
        self._ultima_calidad = calidad
        rocio, vpd = protocolo.punto_rocio(temp, hum), protocolo.vpd_kpa(temp, hum)
        dir_prom = r["viento_dir_prom"]
        fila = (temp, r["temp_min"], r["temp_max"], hum, rocio, vpd,
                r["viento_vel_prom"], r["viento_vel_max"], dir_prom, r["rafaga_max"],
                r["lluvia_mm"], lluvia_hoy, r["lluvia_tasa_max"], r["uv_prom"], r["uv_max"],
                r["solar_prom"], r["solar_max"])
        cuando = datetime.fromtimestamp(ahora)
        bat_txt = "" if bateria is None else ("Baja" if bateria else "OK")
        decs = (1, 1, 1, 1, 1, 3, 1, 1, 0, 1, 2, 2, 1, 2, 2, 1, 1)
        self.csv.escribir(cuando, [_fmt(x, d) for x, d in zip(fila, decs)][:9]
                          + [protocolo.grados_a_cardinal(dir_prom) if dir_prom is not None else ""]
                          + [_fmt(x, d) for x, d in zip(fila[9:], decs[9:])]
                          + [bat_txt, r["paquetes"], esperados, _fmt(calidad, 1)])
        if self.db is not None:
            self.db.resumen((cuando.strftime('%Y-%m-%d %H:%M:%S'), ahora, round(seg, 1)) + fila
                            + (None if bateria is None else int(bateria), r["paquetes"], esperados, calidad))
        with self.lock:
            self.sdr.estado_csv = f"CSV OK {cuando:%H:%M} ({calidad:.0f}%)"

    def generar_heartbeat(self, ahora: float) -> None:
        self._ultimo_heartbeat = ahora
        with self.lock:
            s = self.sdr
            logger.info(
                f"HEARTBEAT │ estado={s.estado} │ válidas={s.paquetes} │ crc_fail={s.descartes_crc} (ruido {s.crc_ruido}, dañadas {s.danadas}) │ "
                f"sin_rastro={s.sin_rastro} │ "
                f"perdidas={s.perdidas} │ invalidas={s.invalidas} │ "
                f"id_fail={s.descartes_id} │ reinicios={s.reinicios} │ errores_usb={self.errores_usb} │ última_sync={s.ultima_sync_seg}s │ "
                f"calidad_última_ventana={self._ultima_calidad}% │ lluvia_hoy={self.est.lluvia_hoy_mm:.1f}mm │ "
                f"sqlite={'%d filas' % self.db.filas_escritas if self.db else 'off'}")

    def escribir_estado_json(self, ahora: float) -> None:
        self._ultimo_json = ahora
        e, s, *_ = self.obtener_instantanea()
        temp, hum = e.valores["temp"], e.valores["humedad"]
        payload = {
            "version": VERSION,
            "actualizado": datetime.fromtimestamp(ahora).isoformat(timespec="seconds"),
            "estacion": {
                "temp_c": temp, "humedad_pct": hum,
                "punto_rocio_c": protocolo.punto_rocio(temp, hum), "vpd_kpa": protocolo.vpd_kpa(temp, hum),
                "viento_kmh": e.valores["viento_vel"], "viento_dir_grados": e.valores["viento_dir"],
                "viento_cardinal": protocolo.grados_a_cardinal(e.valores["viento_dir"]),
                "rafaga_kmh": e.valores["rafaga"], "lluvia_tasa_mm_h": e.valores["lluvia_tasa"],
                "lluvia_hoy_mm": round(e.lluvia_hoy_mm, 2) if config.PLUVIOMETRO_ACTIVO else None,
                "pluviometro_activo": config.PLUVIOMETRO_ACTIVO, "uv": e.valores["uv"],
                "solar_w_m2": e.valores["solar"],
                "bateria_tx": None if e.bateria_baja is None else ("Baja" if e.bateria_baja else "OK"),
                "edad_seg": {k: e.edad(k, ahora) for k in e.valores if k in e.ts},
            },
            "sdr": {
                "estado": s.estado, "freq_mhz": s.freq_mhz, "canal": s.canal,
                "error_freq_hz": s.error_freq_hz, "paquetes_validos": s.paquetes,
                "descartes_crc": s.descartes_crc, "descartes_id": s.descartes_id,
                "crc_ruido": s.crc_ruido, "crc_con_copia": s.crc_con_copia, "danadas": s.danadas,
                "sin_rastro": s.sin_rastro, "perdidas": s.perdidas, "invalidas": s.invalidas,
                "crc_disponible": s.crc_disponible, "version_rtldavis": s.version_rtldavis,
                "enlace_ultima_ventana": self.enlace_ultima,
                "fase_crc_ms": self._histograma_fases(),
                "reinicios": s.reinicios, "ultimo_reinicio": s.ultimo_reinicio,
                "errores_usb": self.errores_usb,
                "ultima_sync_seg": s.ultima_sync_seg,
                "segundos_sin_paquetes": round(ahora - (e.ultimo_paquete or s.inicio_proceso), 1),
                "calidad_ultima_ventana_pct": self._ultima_calidad,
            },
        }
        tmp = self.rutas.estado + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.rutas.estado)
        except OSError as ex:
            logger.error(f"Error escribiendo estado.json: {ex}")

    def evaluar_alertas(self, ahora: float) -> Optional[str]:
        edad = config.MAX_EDAD_DATO_SEG
        with self.lock:
            temp = self.est.valor("temp", ahora, edad)
            tasa = self.est.valor("lluvia_tasa", ahora, edad)
            ultimo = self.est.ultimo_paquete or self.hora_inicio
        a = self._alerta
        # Histéresis: activa en <= umbral, se apaga solo al subir umbral+histéresis
        if temp is not None:
            if not a["helada"] and temp <= config.UMBRAL_HELADA_C:
                a["helada"] = True
                self.notif.enviar(f"Advertencia de helada: {temp:.1f}°C", "urgent", "snowflake,warning")
            elif a["helada"] and temp > config.UMBRAL_HELADA_C + config.HISTERESIS_HELADA_C:
                a["helada"] = False
                self.notif.enviar(f"Fin de helada: {temp:.1f}°C", "low", "snowflake")
        if tasa is not None:
            if not a["tormenta"] and tasa >= config.UMBRAL_TORMENTA_MM_H:
                a["tormenta"] = True
                self.notif.enviar(f"Tormenta severa: {tasa:.1f} mm/h", "urgent", "cloud_with_lightning,warning")
            elif a["tormenta"] and tasa < config.UMBRAL_TORMENTA_MM_H / 2:
                a["tormenta"] = False
        sin = ahora - ultimo
        if not a["sin_datos"] and sin > config.ALERTA_SIN_DATOS_SEG:
            a["sin_datos"] = True
            self.notif.enviar(f"Davis: sin datos de la estación hace {sin / 60:.0f} min", "high", "warning")
        elif a["sin_datos"] and sin < 60:
            a["sin_datos"] = False
            self.notif.enviar("Davis: recepción restablecida", "low", "white_check_mark")
        if a["helada"]:
            return "HELADA"
        if a["tormenta"]:
            return "TORMENTA"
        return None

    # ------------------------------------------------------------ utilidades
    def obtener_instantanea(self):
        with self.lock:
            return (self.est.clonar(), self.sdr.clonar(), self.flecha_temp, self.flecha_viento,
                    list(self.hist_temp), list(self.hist_viento))

    def iniciar_lector(self) -> threading.Thread:
        t = threading.Thread(target=self.lector_rtldavis_worker, name="SDR", daemon=True)
        t.start()
        return t

    def finalizar(self) -> None:
        if getattr(self, "_finalizado", False):
            return
        self._finalizado = True
        self.shutdown_event.set()
        with self._proc_lock:
            p = self.proc
        if p:
            self._detener_proceso(p)
        try:
            self._guardar_ventana(time.time())  # no perder la ventana parcial
        except Exception:  # noqa: BLE001
            pass
        if self.db is not None:
            self.db.cerrar()
        self.notif.cerrar()
